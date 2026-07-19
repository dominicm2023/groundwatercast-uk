"""Build the explorer's river-polyline layer from OS Open Rivers (OGL v3).

RiverCast expansion (2026-07-19): the explorer's "rivers view" draws the
gauged rivers themselves, not just the gauge diamonds. This script extracts,
for every PUBLISHED flow gauge (``data/processed/flow_pilot.csv``), the OS
Open Rivers watercourse links that carry the gauge's river name within
``SEARCH_RADIUS_M`` of the gauge, simplifies them hard (Douglas–Peucker in
OSGB metres — this is a context layer on a static site, not survey data), and
writes one MultiLineString feature per river name to
``data/processed/river_polylines.geojson``. The pack build copies it verbatim
into the pack as ``rivers.geojson`` (same optional-input pattern as
``geology.geojson``); the explorer lazy-loads it the first time the rivers
view is switched on.

Reuses the valley build's machinery (``scripts/build_valley_rivers``): the
no-GDAL GPKG/WKB LineString parser and the viz-grade OSGB→WGS84 conversion.
Unlike the valley build there is NO main-stem chaining — the display layer
wants the real braided network, and per-link Douglas–Peucker already collapses
each link to a handful of points.

Name matching is deliberately dumb-but-transparent: the catalogue's
``river_name`` (pipe-separated candidates) tried verbatim plus with/without a
"River " prefix, against ``watercourse_link.watercourse_name``, spatially
limited to the gauge's neighbourhood so a "River Colne" in Essex never drags
in the Hertfordshire Colne. Gauges with no name match are reported — they
simply get no polyline (the diamond still renders).

Static artifact: re-run only when the published gauge list changes (the
pilot-selection step), not daily.

Usage:
    python -m scripts.build_river_polylines
    python -m scripts.build_river_polylines --tolerance 60 --radius 30000
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from scripts.build_terrain_tile import wgs84_to_osgb
from scripts.build_valley_rivers import GPKG, osgb_to_wgs84, parse_gpkg_line
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = ROOT / "data/processed/flow_pilot.csv"
CATALOGUE_PATH = ROOT / "data/processed/flow_catalogue.csv"
OUT_PATH = ROOT / "data/processed/river_polylines.geojson"

SEARCH_RADIUS_M = 25_000.0     # bbox half-width around each gauge
SIMPLIFY_TOL_M = 40.0          # Douglas–Peucker tolerance (OSGB metres)
MIN_SEG_PTS = 2
COORD_DP = 5                   # ~1 m — matches the simplification class


def name_variants(raw: str) -> list[str]:
    """Candidate ``watercourse_name`` values for one catalogue river name.
    The catalogue uses pipe-separated alternatives ("River Isis|River
    Thames"); OS sometimes drops or adds the "River " prefix."""
    out: list[str] = []
    for part in str(raw or "").split("|"):
        n = part.strip()
        if not n:
            continue
        for v in (n,
                  n[6:] if n.lower().startswith("river ") else "River " + n):
            if v and v not in out:
                out.append(v)
    return out


def simplify_dp(pts: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Iterative Douglas–Peucker (stack-based — link geometries can be long
    enough that recursion depth would be a real risk)."""
    if len(pts) <= 2:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = pts[i0]
        x1, y1 = pts[i1]
        dx, dy = x1 - x0, y1 - y0
        seg2 = dx * dx + dy * dy
        best_d2, best_i = -1.0, -1
        for i in range(i0 + 1, i1):
            px, py = pts[i]
            if seg2 <= 1e-12:
                d2 = (px - x0) ** 2 + (py - y0) ** 2
            else:
                t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg2))
                d2 = (px - (x0 + t * dx)) ** 2 + (py - (y0 + t * dy)) ** 2
            if d2 > best_d2:
                best_d2, best_i = d2, i
        if best_d2 > tol * tol:
            keep[best_i] = True
            stack.append((i0, best_i))
            stack.append((best_i, i1))
    return [p for p, k in zip(pts, keep) if k]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pilot", default=str(PILOT_PATH))
    ap.add_argument("--catalogue", default=str(CATALOGUE_PATH))
    ap.add_argument("--gpkg", default=str(GPKG))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--radius", type=float, default=SEARCH_RADIUS_M,
                    help="search half-width around each gauge, metres "
                         "(default: %(default)s)")
    ap.add_argument("--tolerance", type=float, default=SIMPLIFY_TOL_M,
                    help="Douglas-Peucker tolerance, metres (default: %(default)s)")
    return ap.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    pilot_path, cat_path = Path(args.pilot), Path(args.catalogue)
    gpkg_path = Path(args.gpkg)
    for p, hint in ((pilot_path, "run scripts.select_flow_pilot first"),
                    (cat_path, "run scripts.build_flow_catalogue first")):
        if not p.exists():
            print(f"{p} not found ({hint}) — river polylines skipped.")
            return 0
    if not gpkg_path.exists():
        print(f"ERROR: {gpkg_path} missing — download OS Open Rivers "
              "(GeoPackage, GB); see scripts/build_valley_rivers.py for the "
              "curl command.", file=sys.stderr)
        return 2

    pilot = pd.read_csv(pilot_path, dtype={"gauge_id": str})
    cat = (pd.read_csv(cat_path, dtype={"station_id": str})
           .drop_duplicates("station_id").set_index("station_id"))

    con = sqlite3.connect(gpkg_path)
    seen_fids: set[int] = set()
    by_name: dict[str, list[list[tuple[float, float]]]] = {}
    unmatched: list[str] = []
    n_links = 0

    for gid in pilot["gauge_id"]:
        if gid not in cat.index:
            continue
        row = cat.loc[gid]
        raw_name = row.get("river_name")
        variants = name_variants(raw_name)
        if not variants:
            unmatched.append(f"{row.get('station_name')} (no river name)")
            continue
        E, N = wgs84_to_osgb(float(row["lon"]), float(row["lat"]))
        minx, maxx = E - args.radius, E + args.radius
        miny, maxy = N - args.radius, N + args.radius
        ph = ",".join("?" * len(variants))
        rows = con.execute(
            # NULL-safe canal exclusion: `form != 'canal'` alone silently
            # drops rows whose form is NULL (SQL three-valued logic) — the
            # intent is to exclude only canals, never unclassified links.
            f"""select w.fid, w.watercourse_name, w.geometry
                from watercourse_link w
                join rtree_watercourse_link_geometry r on w.fid = r.id
                where w.watercourse_name in ({ph})
                  and (w.form is null or w.form != 'canal')
                  and r.minx <= ? and r.maxx >= ? and r.miny <= ? and r.maxy >= ?""",
            (*variants, maxx, minx, maxy, miny)).fetchall()
        if not rows:
            unmatched.append(f"{row.get('station_name')} ({raw_name})")
            continue
        for fid, wname, geom in rows:
            if fid in seen_fids:
                continue
            seen_fids.add(fid)
            try:
                pts = parse_gpkg_line(geom)
            except ValueError:
                continue
            simp = simplify_dp(pts, args.tolerance)
            if len(simp) < MIN_SEG_PTS:
                continue
            coords = [[round(c, COORD_DP) for c in osgb_to_wgs84(e, n)]
                      for e, n in simp]
            by_name.setdefault(wname, []).append(coords)
            n_links += 1

    if not by_name:
        print("no watercourse links matched — refusing to write", file=sys.stderr)
        return 2

    features = [{
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {"type": "MultiLineString", "coordinates": segs},
    } for name, segs in sorted(by_name.items())]
    fc = {"type": "FeatureCollection",
          "source": "OS Open Rivers (Ordnance Survey, OGL v3) — simplified for display",
          "features": features}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(fc, separators=(",", ":"))
    out_path.write_text(body, encoding="utf-8")

    n_pts = sum(len(s) for segs in by_name.values() for s in segs)
    gz = len(gzip.compress(body.encode("utf-8")))
    print(f"river polylines: {len(features)} rivers, {n_links} links, "
          f"{n_pts} points -> {out_path}")
    print(f"  size: {len(body) / 1024:.0f} KiB raw, {gz / 1024:.0f} KiB gzipped "
          f"(budget: well under 1024 KiB gzipped)")
    if unmatched:
        print(f"  no polyline for {len(unmatched)} gauge(s): "
              + "; ".join(sorted(set(unmatched))))
    if gz > 900 * 1024:
        print("WARNING: gzipped size is close to the 1 MiB payload budget — "
              "raise --tolerance or trim --radius.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        sys.exit(run(parse_args()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

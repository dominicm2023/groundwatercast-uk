"""Build real watercourse geometry for the valley-3D prototype (OS Open Rivers).

The rivers half of the valley-3D "real geometry" step: replaces the
hand-sketched waypoints with the real network from **OS Open Rivers** (OGL v3,
GB GeoPackage under ``data/raw/os_openrivers/``). No GDAL — a GeoPackage is
SQLite (stdlib) and the geometries are WKB LineStrings behind a small GPKG
header.

Per selected watercourse: gather its links inside the bbox, orient each by
``flow_direction``, chain them source→mouth through start/end nodes taking the
LONGEST path (the Test braids heavily through its water meadows — parallel
side-carriers share the name; the main stem is the longest chain), resample to
~200 m, convert OSGB→WGS84 (inverse Helmert+TM, viz-grade), and sample bed
elevations from the OS Terrain 50 tile (carved 0.6 m; the prototype re-derives
and monotonic-clamps them again at load, so the two stay consistent).

Head-walking fractions (where the flowing river starts at drought/normal/
winter groundwater) are hydrological behaviour, not geometry — carried over
per name from the hand-authored data.js values.

Usage:
  python -m scripts.build_valley_rivers --js web/valley/test/rivers.js
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import struct
import sys
from pathlib import Path

from scripts.build_terrain_tile import Terrain50, T50_ZIP, wgs84_to_osgb

ROOT = Path(__file__).resolve().parents[1]
GPKG = ROOT / "data/raw/os_openrivers/Data/oprvrs_gb.gpkg"
DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)

# The Test system, with head-walking behaviour carried from data.js. OS calls
# the Bourne Rill "Bourne Rivulet". Itchen-system courses in the block's SE
# corner are deliberately excluded — this is the Test's block.
COURSES = {
    "River Test":      dict(main=True, headPerennial=0.10, headDrought=0.30, headWinter=0.01),
    "Bourne Rivulet":  dict(winterbourne=True, label="Bourne Rill",
                            headPerennial=0.45, headDrought=0.98, headWinter=0.02),
    "River Dever":     dict(winterbourne=True,
                            headPerennial=0.30, headDrought=0.70, headWinter=0.03),
    "River Anton":     dict(headPerennial=0.18, headDrought=0.45, headWinter=0.02),
    "Pillhill Brook":  dict(headPerennial=0.25, headDrought=0.60, headWinter=0.03),
    "Wallop Brook":    dict(winterbourne=True,
                            headPerennial=0.35, headDrought=0.85, headWinter=0.03),
    "River Dun":       dict(headPerennial=0.12, headDrought=0.35, headWinter=0.02),
}
RESAMPLE_M = 200.0


# --------------------------------------------------------------------------
# OSGB36 E/N → WGS84 lon/lat (inverse of build_terrain_tile.wgs84_to_osgb;
# same viz-grade ~5 m class — do not reuse for survey maths)
# --------------------------------------------------------------------------

def osgb_to_wgs84(E: float, N: float) -> tuple[float, float]:
    a, b = 6377563.396, 6356256.909                  # Airy 1830
    e2 = 1 - (b * b) / (a * a)
    F0, lat0, lon0, E0, N0 = 0.9996012717, math.radians(49), math.radians(-2), 400000.0, -100000.0
    n = (a - b) / (a + b)
    lat = lat0
    M = 0.0
    for _ in range(20):                              # iterate the meridian arc
        lat = (N - N0 - M) / (a * F0) + lat
        dlat, slat = lat - lat0, lat + lat0
        M = b * F0 * (
            (1 + n + 1.25 * n * n + 1.25 * n ** 3) * dlat
            - (3 * n + 3 * n * n + 2.625 * n ** 3) * math.sin(dlat) * math.cos(slat)
            + (1.875 * n * n + 1.875 * n ** 3) * math.sin(2 * dlat) * math.cos(2 * slat)
            - (35 / 24) * n ** 3 * math.sin(3 * dlat) * math.cos(3 * slat))
        if abs(N - N0 - M) < 1e-5:
            break
    sin_l, cos_l, tan_l = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * F0 / math.sqrt(1 - e2 * sin_l ** 2)
    rho = a * F0 * (1 - e2) / (1 - e2 * sin_l ** 2) ** 1.5
    eta2 = nu / rho - 1
    VII = tan_l / (2 * rho * nu)
    VIII = tan_l / (24 * rho * nu ** 3) * (5 + 3 * tan_l ** 2 + eta2 - 9 * tan_l ** 2 * eta2)
    IX = tan_l / (720 * rho * nu ** 5) * (61 + 90 * tan_l ** 2 + 45 * tan_l ** 4)
    X = 1 / (cos_l * nu)
    XI = 1 / (cos_l * 6 * nu ** 3) * (nu / rho + 2 * tan_l ** 2)
    XII = 1 / (cos_l * 120 * nu ** 5) * (5 + 28 * tan_l ** 2 + 24 * tan_l ** 4)
    XIIA = 1 / (cos_l * 5040 * nu ** 7) * (61 + 662 * tan_l ** 2 + 1320 * tan_l ** 4 + 720 * tan_l ** 6)
    dE = E - E0
    lat2 = lat - VII * dE ** 2 + VIII * dE ** 4 - IX * dE ** 6
    lon2 = math.radians(-2) + X * dE - XI * dE ** 3 + XII * dE ** 5 - XIIA * dE ** 7
    # geodetic (Airy) → cartesian → inverse Helmert → geodetic (WGS84)
    nu2 = a / math.sqrt(1 - e2 * math.sin(lat2) ** 2)
    x = nu2 * math.cos(lat2) * math.cos(lon2)
    y = nu2 * math.cos(lat2) * math.sin(lon2)
    z = nu2 * (1 - e2) * math.sin(lat2)
    tx, ty, tz = 446.448, -125.157, 542.060          # negated OSGB36→WGS84
    rx, ry, rz = (0.1502 / 3600 * math.pi / 180,
                  0.2470 / 3600 * math.pi / 180,
                  0.8421 / 3600 * math.pi / 180)
    s = -20.4894e-6
    x2 = tx + (1 + s) * x - rz * y + ry * z
    y2 = ty + rz * x + (1 + s) * y - rx * z
    z2 = tz - ry * x + rx * y + (1 + s) * z
    a1, f1 = 6378137.0, 1 / 298.257223563
    e2w = f1 * (2 - f1)
    p = math.hypot(x2, y2)
    latw = math.atan2(z2, p * (1 - e2w))
    for _ in range(6):
        nuw = a1 / math.sqrt(1 - e2w * math.sin(latw) ** 2)
        latw = math.atan2(z2 + e2w * nuw * math.sin(latw), p)
    return math.degrees(math.atan2(y2, x2)), math.degrees(latw)


# --------------------------------------------------------------------------
# GPKG geometry: header (magic, version, flags, srs, optional envelope) + WKB
# --------------------------------------------------------------------------

def parse_gpkg_line(blob: bytes) -> list[tuple[float, float]]:
    if blob[:2] != b"GP":
        raise ValueError("not a GPKG geometry")
    flags = blob[3]
    env_code = (flags >> 1) & 0x07
    env_len = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[env_code]
    off = 8 + env_len
    little = blob[off] == 1
    bo = "<" if little else ">"
    gtype = struct.unpack_from(bo + "I", blob, off + 1)[0] & 0xFF
    if gtype != 2:                                   # LineString
        raise ValueError(f"unexpected geometry type {gtype}")
    npts = struct.unpack_from(bo + "I", blob, off + 5)[0]
    vals = struct.unpack_from(bo + f"{npts * 2}d", blob, off + 9)
    return [(vals[2 * i], vals[2 * i + 1]) for i in range(npts)]


# --------------------------------------------------------------------------
# Chain links source→mouth, longest path through the braids
# --------------------------------------------------------------------------

def longest_chain(links: list[dict]) -> list[dict]:
    """links: oriented dicts with startN/endN/length. Returns the ordered link
    list of the longest flow-directed path (by summed length)."""
    out_edges: dict[str, list[dict]] = {}
    for lk in links:
        out_edges.setdefault(lk["startN"], []).append(lk)
    memo: dict[str, tuple[float, list]] = {}
    stack: set[str] = set()

    def best_from(node: str) -> tuple[float, list]:
        if node in memo:
            return memo[node]
        if node in stack:                            # cycle guard (data quirks)
            return (0.0, [])
        stack.add(node)
        best = (0.0, [])
        for lk in out_edges.get(node, []):
            d, path = best_from(lk["endN"])
            if lk["length"] + d > best[0]:
                best = (lk["length"] + d, [lk] + path)
        stack.discard(node)
        memo[node] = best
        return best

    starts = {lk["startN"] for lk in links} - {lk["endN"] for lk in links}
    overall = (0.0, [])
    for s in (starts or {lk["startN"] for lk in links}):
        cand = best_from(s)
        if cand[0] > overall[0]:
            overall = cand
    return overall[1]


GAP_M = 400.0


def bridge_chains(links: list[dict]) -> list[dict]:
    """longest_chain over the links PLUS zero-length virtual edges bridging
    small spatial gaps. OS Open Rivers breaks node connectivity where a river
    passes a lake, a weir complex, a braid fork or the inland→tidal
    transition (the geometry continues a few metres away under fresh node
    ids), so the pure node-graph path stops short — the Test lost its source
    reach above the Chilbolton lakes and everything below Redbridge. A
    virtual edge from any link END to any link START within GAP_M lets the
    longest-path search cross those gaps (zero length: bridges connect, they
    never add distance, so parallel braids still lose to the main stem)."""
    starts = {}
    for lk in links:
        starts.setdefault(lk["startN"], lk["pts"][0])
    virtual = []
    for lk in links:
        ep = lk["pts"][-1]
        for sn, sp in starts.items():
            if sn == lk["endN"]:
                continue
            if math.hypot(ep[0] - sp[0], ep[1] - sp[1]) <= GAP_M:
                virtual.append({"pts": [ep, sp], "length": 0.0,
                                "startN": lk["endN"], "endN": sn,
                                "form": "bridge"})
    return longest_chain(links + virtual)


def build(bbox):
    lon_min, lat_min, lon_max, lat_max = bbox
    corners = [wgs84_to_osgb(lon, lat) for lon in (lon_min, lon_max)
               for lat in (lat_min, lat_max)]
    minx, maxx = min(c[0] for c in corners), max(c[0] for c in corners)
    miny, maxy = min(c[1] for c in corners), max(c[1] for c in corners)
    con = sqlite3.connect(GPKG)
    dem = Terrain50(T50_ZIP)
    out = []
    for name, meta in COURSES.items():
        rows = con.execute(
            """select w.geometry, w.flow_direction, w.length, w.start_node, w.end_node,
                      w.form
               from watercourse_link w
               join rtree_watercourse_link_geometry r on w.fid = r.id
               where w.watercourse_name = ? and w.form != 'canal'
                 and r.minx <= ? and r.maxx >= ? and r.miny <= ? and r.maxy >= ?""",
            (name, maxx, minx, maxy, miny)).fetchall()
        links = []
        for geom, fdir, length, sn, en, form in rows:
            pts = parse_gpkg_line(geom)
            if fdir == "in opposite direction":      # orient every link with the flow
                pts = pts[::-1]
                sn, en = en, sn
            links.append({"pts": pts, "length": length or 0.0,
                          "startN": sn, "endN": en, "form": form})
        chain = bridge_chains(links)
        if not chain:
            print(f"  {name}: no links — skipped")
            continue
        # concatenate + resample to ~RESAMPLE_M along-course; remember where
        # the tidal reach starts (the estuary is drawn but never GW-coupled)
        raw, tidal_raw = [], None
        for lk in chain:
            if lk["form"] == "tidalRiver" and tidal_raw is None:
                tidal_raw = max(0, len(raw) - 1)
            raw.extend(lk["pts"] if not raw else lk["pts"][1:])
        pts, acc = [raw[0]], 0.0
        tidal_idx = 0 if tidal_raw == 0 else None
        for i in range(1, len(raw)):
            acc += math.hypot(raw[i][0] - raw[i - 1][0], raw[i][1] - raw[i - 1][1])
            if acc >= RESAMPLE_M or i == len(raw) - 1:
                pts.append(raw[i]); acc = 0.0
                if tidal_idx is None and tidal_raw is not None and i >= tidal_raw:
                    tidal_idx = len(pts) - 1
        wps = []
        for e, n in pts:
            lon, lat = osgb_to_wgs84(e, n)
            wps.append([round(lon, 5), round(lat, 5), round(dem.elev(e, n) - 0.6, 1)])
        # rtree returns links that INTERSECT the bbox, so a course can poke past
        # the block edge (the Dun's source, the Test below Romsey) — trim the
        # out-of-bounds head/tail so tubes never float beyond the terrain.
        inb = lambda p: lon_min <= p[0] <= lon_max and lat_min <= p[1] <= lat_max
        head_trim = 0
        while wps and not inb(wps[0]):
            wps.pop(0); head_trim += 1
        while wps and not inb(wps[-1]):
            wps.pop()
        if len(wps) < 4:
            print(f"  {name}: too short after bbox trim — skipped")
            continue
        km = sum(l["length"] for l in chain) / 1000
        tidal_t = None
        if tidal_idx is not None:
            k = min(len(wps) - 1, max(0, tidal_idx - head_trim))
            tidal_t = round(k / (len(wps) - 1), 4)
        print(f"  {meta.get('label', name):16s} {len(rows):3d} links → main stem "
              f"{km:5.1f} km, {len(wps)} waypoints"
              + (f", tidal from t={tidal_t}" if tidal_t is not None else ""))
        wc = {"name": meta.get("label", name), "waypoints": wps}
        if tidal_t is not None:
            wc["tidalT"] = tidal_t
        for k in ("main", "winterbourne", "headPerennial", "headDrought", "headWinter"):
            if k in meta:
                wc[k] = meta[k]
        out.append(wc)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("LONMIN", "LATMIN", "LONMAX", "LATMAX"),
                    default=list(DEFAULT_BBOX))
    ap.add_argument("--js", type=Path, required=True)
    args = ap.parse_args()
    if not GPKG.exists():
        print(f"ERROR: {GPKG} missing — download OS Open Rivers (GeoPackage, GB)\n"
              "  curl -L -o data/raw/os_openrivers/oprvrs_gpkg_gb.zip \\\n"
              "    'https://api.os.uk/downloads/v1/products/OpenRivers/downloads?area=GB&format=GeoPackage&redirect'\n"
              "  then extract Data/oprvrs_gb.gpkg", file=sys.stderr)
        return 2
    wcs = build(tuple(args.bbox))
    if not wcs:
        print("no watercourses — refusing to write", file=sys.stderr)
        return 2
    payload = {"source": "OS Open Rivers (Ordnance Survey, OGL v3)",
               "watercourses": wcs}
    args.js.write_text(
        "// Generated by scripts/build_valley_rivers.py - real river geometry.\n"
        "// Contains OS data (c) Crown copyright and database right 2026 (OGL v3).\n"
        f"window.TEST3D_RIVERS = {json.dumps(payload)};\n", encoding="ascii")
    print(f"wrote {args.js} ({len(wcs)} watercourses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

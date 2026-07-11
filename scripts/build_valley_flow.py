"""Weekly observed river flow for the valley-3D scene (the Rivers pilot).

Fetches ~3 years of DAILY MEAN gauged flow (m3/s) from the EA Hydrology
archive for the flow gauges on the block's drawn watercourses, resamples to
weekly means on the SAME Monday axis stations.js carries, and writes a small
classic-script global. First brick of the low-flow Rivers layer: the same
ingest the per-gauge product needs, piloted where the 3-D scene can show it.

Honesty invariants baked in:
- a gauge is a MEASUREMENT at a point; the drawn ribbon between gauges stays
  an indication (the scene's card says so) — gauges near no drawn course are
  dropped rather than decorating empty terrain
- per-gauge context is the gauge's OWN weekly record (p10/p50/p90 of these
  3 years), not a long-term Q95 — labelled accordingly

Usage:
  python -m scripts.build_valley_flow --js web/valley/test/flow.js
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIONS_JS = ROOT / "web/valley/test/stations.js"
RIVERS_JS = ROOT / "web/valley/test/rivers.js"
HYDRO = "https://environment.data.gov.uk/hydrology/id"
DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)
CENTRE = (-1.43, 51.085)
MAX_COURSE_KM = 1.2               # gauge must sit on a drawn watercourse
SITE_DEDUPE_KM = 0.35             # Main/Side/Total share a site — keep one
# a gauge must MEASURE one of the drawn rivers, not merely sit near one — the
# M27 Blackwater gauge is within a kilometre of the tidal Test but gauges a
# different river; snapping it to the Test would blend the wrong flow in
RIVER_NAMES = {"River Test", "Bourne Rivulet", "River Dever", "River Anton",
               "Pillhill Brook", "Wallop Brook", "River Dun"}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "GWC-valley3d-flow"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def history_weeks():
    txt = STATIONS_JS.read_text(encoding="ascii")
    data = json.loads(re.search(r"window\.TEST3D_STATIONS = (.*);", txt).group(1))
    weeks = data.get("historyWeeks")
    if not weeks:
        raise SystemExit("stations.js has no historyWeeks — rebuild it first")
    return weeks


def course_points():
    txt = RIVERS_JS.read_text(encoding="ascii")
    data = json.loads(re.search(r"window\.TEST3D_RIVERS = (.*);", txt).group(1))
    kx = 111.32 * math.cos(math.radians(51.12))
    pts = []
    for wc in data["watercourses"]:
        for lon, lat, _ in wc["waypoints"]:
            pts.append((lon * kx, lat * 111.2))
    return pts, kx


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float, default=list(DEFAULT_BBOX))
    ap.add_argument("--js", type=Path, required=True)
    args = ap.parse_args()
    lon_min, lat_min, lon_max, lat_max = args.bbox

    weeks = history_weeks()
    d0 = weeks[0]
    d1 = (dt.date.fromisoformat(weeks[-1]) + dt.timedelta(days=6)).isoformat()
    cpts, kx = course_points()

    lon_c, lat_c = CENTRE
    st = _get(f"{HYDRO}/stations.json?observedProperty=waterFlow"
              f"&lat={lat_c}&long={lon_c}&dist=30&_limit=100").get("items", [])
    cands = []
    for s in st:
        lon, lat = s.get("long"), s.get("lat")
        if lon is None or not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
            continue
        river = s.get("riverName")
        rivers = river if isinstance(river, list) else [river]
        if not any(r in RIVER_NAMES for r in rivers):
            continue
        dmin = min(math.hypot(lon * kx - px, lat * 111.2 - pz) for px, pz in cpts)
        if dmin <= MAX_COURSE_KM:
            cands.append((s, dmin))
    # same-site dedupe: Chilbolton Main/Side/Total etc. — prefer "Total"
    cands.sort(key=lambda c: (0 if "Total" in c[0]["label"] else 1, c[0]["label"]))
    kept = []
    for s, dmin in cands:
        x, z = s["long"] * kx, s["lat"] * 111.2
        if any(math.hypot(x - k["long"] * kx, z - k["lat"] * 111.2) < SITE_DEDUPE_KM
               for k in kept):
            continue
        kept.append(s)

    gauges = []
    for s in kept:
        try:
            measures = _get(s["@id"] + "/measures.json").get("items", [])
        except Exception:
            continue
        meas = next((m["notation"] for m in measures
                     if "-flow-m-86400-" in m["notation"]), None)
        if not meas:
            continue
        try:
            rows = _get(f"{HYDRO}/measures/{urllib.parse.quote(meas)}/readings.json"
                        f"?mineq-date={d0}&maxeq-date={d1}&_limit=2000").get("items", [])
        except Exception:
            continue
        vals = {r["date"]: r["value"] for r in rows if r.get("value") is not None}
        if len(vals) < 200:
            continue
        weekly = []
        for wk in weeks:
            monday = dt.date.fromisoformat(wk)
            days = [(monday + dt.timedelta(days=i)).isoformat() for i in range(7)]
            got = [vals[d] for d in days if d in vals]
            weekly.append(round(sum(got) / len(got), 3) if len(got) >= 5 else None)
        ok = sorted(v for v in weekly if v is not None)
        if len(ok) < 26:
            continue
        q = lambda p: ok[min(len(ok) - 1, int(p * len(ok)))]
        river = s.get("riverName")
        if isinstance(river, list):
            river = river[0]
        gauges.append({
            "name": s["label"], "river": river,
            "lon": round(s["long"], 5), "lat": round(s["lat"], 5),
            "p10": round(q(0.10), 3), "p50": round(q(0.50), 3),
            "p90": round(q(0.90), 3),
            "t33": round(q(1 / 3), 3), "t67": round(q(2 / 3), 3),
            "weekly": weekly,
        })
        n_ok = sum(v is not None for v in weekly)
        print(f"  {s['label']} ({river}): {n_ok}/{len(weeks)} weeks, "
              f"median {q(0.50):.2f} m3/s")
    if not gauges:
        print("no usable flow gauges — nothing written", file=sys.stderr)
        return 2

    payload = {
        "source": "EA Hydrology API daily mean flow (OGL v3), gauges on the "
                  "block's drawn watercourses",
        "note": "weekly mean m3/s on the stations.js Monday axis; p10/p50/p90 "
                "are the gauge's own record over these 3 years, not long-term "
                "flow statistics; a gauge is a point measurement — the drawn "
                "river between gauges remains indicative",
        "gauges": gauges,
    }
    args.js.write_text(
        "// Generated by scripts/build_valley_flow.py - gauged river flow.\n"
        "// Contains EA data (c) Environment Agency, OGL v3.\n"
        f"window.TEST3D_FLOW = {json.dumps(payload)};\n", encoding="ascii")
    print(f"{len(gauges)} gauges -> {args.js} ({args.js.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

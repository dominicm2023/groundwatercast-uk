"""Build a catchment slice of real pack stations for the valley-3D prototype.

The "real stations" step of the valley-3D plan: pulls every
GroundwaterCast station inside a WGS84 bbox from the published artifact pack
(live site by default, or a local pack dir), keeps the fields the 3-D scene
needs — position, latest observed level, pack status, and the station's own
monthly-normals envelope (annual min P10 / max P90 + mean tercile bounds,
which give each tube its REAL seasonal swing) — and writes a classic-script
global the prototype loads like data.js.

Usage (Test-valley slice from the live pack):
  python -m scripts.build_valley_stations --js web/valley/test/stations.js
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)   # the prototype block (data.js)
DEFAULT_PACK_URL = "https://groundwatercast.com/pack"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "GWC-valley3d-slice"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _load(pack_url: str | None, pack_dir: Path | None, rel: str):
    if pack_dir is not None:
        return json.loads((pack_dir / rel).read_text(encoding="utf-8"))
    return _get(f"{pack_url}/{rel}")


def week_axis(years=3):
    """Monday-aligned weekly date axis ending this week, ~`years` back."""
    import datetime as dt
    today = dt.date.today()
    end = today - dt.timedelta(days=today.weekday())        # this Monday
    n = years * 52
    return [(end - dt.timedelta(weeks=n - 1 - k)).isoformat() for k in range(n)]


def weekly_levels(series, weeks):
    """Last observation at or before each week's Monday (within 30 days),
    None where the record hasn't started or the gap is too long — the scene
    holds the previous value; we don't invent movement."""
    import datetime as dt
    out, j = [], 0
    dates = [r[0] for r in series]
    for wk in weeks:
        while j < len(series) and dates[j] <= wk:
            j += 1
        if j == 0:
            out.append(None)
            continue
        d0 = dt.date.fromisoformat(dates[j - 1])
        w0 = dt.date.fromisoformat(wk)
        v = series[j - 1][1]
        out.append(round(v, 2) if v is not None and (w0 - d0).days <= 30 else None)
    return out


def build(bbox, pack_url, pack_dir):
    lon_min, lat_min, lon_max, lat_max = bbox
    index = _load(pack_url, pack_dir, "stations/index.json")
    hits = [s for s in index
            if lon_min <= s["lon"] <= lon_max and lat_min <= s["lat"] <= lat_max]
    print(f"{len(hits)} stations in bbox; fetching details…")
    weeks = week_axis()
    out, skipped = [], 0
    for s in hits:
        try:
            d = _load(pack_url, pack_dir, f"stations/{s['station_id']}.json")
        except Exception:
            skipped += 1
            continue
        obs = (d.get("observed") or {}).get("series") or []
        norms = d.get("normals") or []
        if not obs or not norms:
            skipped += 1                     # a tube needs a level + an envelope
            continue
        level, date = obs[-1][1], obs[-1][0]
        p10s = [r["p10"] for r in norms if r.get("p10") is not None]
        p90s = [r["p90"] for r in norms if r.get("p90") is not None]
        t1s = [r["t1"] for r in norms if r.get("t1") is not None]
        t2s = [r["t2"] for r in norms if r.get("t2") is not None]
        if not p10s or not p90s or level is None:
            skipped += 1
            continue
        st = (d.get("status") or {}).get("status")
        rec = {
            "id": s["station_id"], "slug": s["slug"], "name": s["name"],
            "lon": round(s["lon"], 5), "lat": round(s["lat"], 5),
            "level": round(level, 2), "obsDate": date,
            "status0": st,
            "p10min": round(min(p10s), 2), "p90max": round(max(p90s), 2),
            "t1m": round(sum(t1s) / len(t1s), 2) if t1s else None,
            "t2m": round(sum(t2s) / len(t2s), 2) if t2s else None,
            "hasForecast": bool(s.get("has_forecast")),
        }
        # Forecast stations carry their published 14-day fan (P10/P50/P90 per
        # date, forecast segment) plus the nowcast's modelled-today level —
        # the timeline the 3-D forecast scrubber walks (valley-3D step 3).
        fan = (d.get("forecast") or {}).get("fan") or []
        fseg = [r for r in fan
                if r.get("segment") != "nowcast" and r.get("p50") is not None]
        nseg = [r for r in fan
                if r.get("segment") == "nowcast" and r.get("p50") is not None]
        if fseg:
            rec["fan"] = [[r["date"], round(r["p10"], 2), round(r["p50"], 2),
                           round(r["p90"], 2)] for r in fseg]
            if nseg:
                rec["now50"] = round(nseg[-1]["p50"], 2)
        # Seasonal outlook (up to 6 monthly weighted-mean quantiles) extends
        # the 3-D timeline beyond day 14 — a fortnight barely moves a chalk
        # system; the seasonal frames are where the winterbournes really walk.
        #
        # FRESHNESS GUARD: the pack's seasonal artifact currently mixes runs —
        # 163/671 stations carry outlooks with origins as old as Aug 2025
        # recycled under the latest run stamp, and the fresh cohort's
        # origin_date is misdated ~2 weeks into the future (both filed as a
        # production bug, 2026-07-09). Only plausibly-fresh outlooks (origin
        # within ±45 days of today) belong in the scene; stale ones are worse
        # than none. Past months are dropped too — the timeline walks forward.
        import datetime as _dt
        se = d.get("seasonal") or {}
        months = se.get("months") or []
        today = _dt.date.today()
        fresh = False
        try:
            odate = _dt.date.fromisoformat(se.get("origin_date") or "")
            fresh = abs((today - odate).days) <= 45
        except ValueError:
            pass
        month0 = today.replace(day=1).isoformat()
        srows = [[m["month_start"], round(m["gw_p10"], 2),
                  round(m["gw_p50"], 2), round(m["gw_p90"], 2)]
                 for m in months
                 if m.get("month_start") and m["month_start"] >= month0
                 and m.get("gw_p50") is not None
                 and m.get("gw_p10") is not None and m.get("gw_p90") is not None]
        if fresh and srows:
            rec["seasonal"] = srows
        # Observed weekly history (valley-3D hindcast mode): the pack's
        # observed tail resampled onto the shared week axis. Solid data first —
        # the scene's timeline earns its trust replaying measurements before
        # it asks anyone to believe a forecast.
        hist = weekly_levels(obs, weeks)
        if sum(v is not None for v in hist) >= 26:          # ≥ half a year real
            rec["hist"] = hist
        out.append(rec)
    n_hist = sum(1 for r in out if "hist" in r)
    print(f"kept {len(out)} (skipped {skipped}: no level/normals); "
          f"{n_hist} with weekly history x {len(weeks)} weeks")
    return out, weeks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("LONMIN", "LATMIN", "LONMAX", "LATMAX"),
                    default=list(DEFAULT_BBOX))
    ap.add_argument("--pack-url", default=DEFAULT_PACK_URL)
    ap.add_argument("--pack-dir", type=Path,
                    help="read a local pack instead of --pack-url")
    ap.add_argument("--js", type=Path, required=True)
    args = ap.parse_args()
    stations, weeks = build(tuple(args.bbox),
                            None if args.pack_dir else args.pack_url, args.pack_dir)
    if not stations:
        print("no stations — refusing to write an empty slice", file=sys.stderr)
        return 2
    payload = {
        "source": "GroundwaterCast artifact pack (EA Hydrology data, OGL v3)",
        "note": "levels as measured at obsDate; seasonal swing = the station's "
                "own monthly-normals envelope; indicative between measurements",
        "historyWeeks": weeks,
        "stations": stations,
    }
    args.js.write_text(
        "// Generated by scripts/build_valley_stations.py - real pack stations.\n"
        "// Contains EA data (c) Environment Agency, OGL v3.\n"
        f"window.TEST3D_STATIONS = {json.dumps(payload)};\n", encoding="ascii")
    print(f"wrote {args.js} ({len(stations)} stations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

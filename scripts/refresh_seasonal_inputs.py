"""Fetch & cache everything the seasonal outlook needs (main-env step).

The pastas-env builder (scripts/build_seasonal_outlook.py) is pure compute
from caches — this step (the seasonal analogue of refresh_pet) does all the
network work:

  1. ERA5 daily precip per in-scope borehole, back to trace_start_year, and
  2. PET (ET0) per borehole — self-computed FAO-56 from ERA5 met fields,
     both via ONE Copernicus CDS bounding-box fetch for the whole fleet
     (src/data/cds_era5.py + et0.py — the free-data migration,
     docs/free_data_migration.md). Daily statistics are aggregated
     server-side, so a fleet-wide 35-year pull is a few MB and a handful of
     (queued) requests, NOT 655 throttled per-point calls. The legacy
     per-point Open-Meteo archive path stays as a fallback (--source
     open_meteo, or automatically when the CDS path can't run / no key).
  3. The current SEAS5 monthly members — ONE Copernicus CDS box
     (``seasonal-monthly-single-levels``) cached per borehole; falls back to
     the per-point Open-Meteo seasonal API when CDS is unavailable.

Run monthly (after SEAS5's update on the 5th), before build_seasonal_outlook:
  python -m scripts.refresh_seasonal_inputs            # incremental CDS top-up
  python -m scripts.refresh_seasonal_inputs --full     # full CDS backfill (cutover)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.data import era5_precip, pet
from src.forecast.seasonal import seas5
from src.forecast.ensemble.scope import select_scope

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "data" / "processed" / "catalogue.csv"


def _config_scope() -> str:
    """Default seasonal scope = the configured forecast scope (pastas.scope), so
    ERA5/PET/SEAS5 are cached for exactly the boreholes that have calibrated
    models (build_seasonal_outlook runs over all of them). Falls back to 'live'."""
    try:
        cfg = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("forecast", {}).get("ensemble", {})
                .get("pastas", {}).get("scope", "live"))
    except Exception:
        return "live"


def _incremental_start(points: dict, cache_root: Path, full_start: date) -> date:
    """Earliest date to fetch for an incremental top-up: a week before the
    oldest per-station cache tail. Returns ``full_start`` (= full backfill) if any
    in-scope station has no cache yet — a partial fleet must be completed first."""
    maxes = []
    for sid in points:
        p = cache_root / f"{sid}.csv"
        if not p.exists():
            return full_start
        try:
            d = pd.read_csv(p, usecols=["date"])
            if d.empty:
                return full_start
            maxes.append(pd.Timestamp(d["date"].iloc[-1]).date())
        except Exception:
            return full_start
    return (min(maxes) - timedelta(days=7)) if maxes else full_start


def _refresh_via_cds(points: dict, start: date, end: date, *, full: bool) -> int:
    """One UK-box CDS fetch → every station's era5_precip + pet cache (W4/W5).

    Precip = 1 box; ET0 = 6 met fields self-computed via FAO-56. The cache
    writers emit the exact schemas era5_precip/pet maintain, so downstream code
    is untouched. Elevations default to 0 m (the FAO-56 pressure term is a small
    effect over the UK and the Pastas models recalibrate on this ET0 anyway)."""
    from src.data import cds_era5
    box_start = start if full else _incremental_start(
        points, era5_precip.PRECIP_CACHE_ROOT, start)
    print(f"CDS box fetch {box_start} → {end}  "
          f"(precip + 6 met fields for FAO-56 ET0)")
    n = cds_era5.update_precip_caches(points, box_start, end)
    cds_era5.update_pet_caches(points, box_start, end)
    print(f"CDS: {n} stations' precip + PET caches updated")
    return n


def _refresh_via_cds_ts(points: dict, start: date, end: date, *, full: bool) -> int:
    """FAST path — CDS ARCO point time-series (cds_timeseries): one direct
    per-point CSV pull with NO request queue → era5_precip + pet caches. Validated
    ~15-25 s/point vs the box path's ~80-min queued requests (scripts/
    validate_cds_timeseries; docs/free_data_migration.md W4). Returns stations
    updated; 0 → caller falls back to the box path."""
    from src.data import cds_timeseries as ts
    box_start = start if full else _incremental_start(
        points, era5_precip.PRECIP_CACHE_ROOT, start)
    print(f"CDS timeseries (ARCO point) fetch {box_start} → {end} for "
          f"{len(points)} boreholes …")
    ok, failed = ts.update_caches_timeseries(points, box_start, end)
    if failed:
        print(f"  {len(failed)} point(s) failed (tolerated; re-run fills them); "
              f"e.g. {failed[0]}")
    print(f"CDS-ts: {ok}/{len(points)} stations' precip + PET caches updated")
    return ok


def _refresh_via_open_meteo(points: dict, start: date, end: date) -> int:
    """Legacy per-point Open-Meteo archive fetch (dev / no-CDS fallback).

    NOTE: Open-Meteo's free tier is non-commercial only and rate-limits the
    multi-decade archive pulls — see docs/free_data_migration.md. CDS is the
    commercial path; this remains for local dev convenience."""
    ok = failed = 0
    for sid, (lat, lon) in points.items():
        try:
            p = era5_precip.fetch_station_precip(sid, lat, lon, start, end)
            e = pet.fetch_station_pet(sid, lat, lon, start, end)
            ok += 1
            if ok % 10 == 0:
                print(f"  …{ok} done (latest {sid[:8]}: "
                      f"{len(p)} precip d, {len(e)} pet d)")
        except Exception as exc:
            failed += 1
            print(f"  ! {sid[:8]} failed: {exc}")
    if failed:
        print(f"  ({failed} per-point fetch failures)")
    return ok


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = json.loads((ROOT / "config/config.json").read_text())
    scfg = cfg.get("forecast", {}).get("seasonal", {})
    if not scfg.get("enabled", True):
        print("forecast.seasonal.enabled = false — nothing to do")
        return 0

    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["user", "live", "fleet"],
                    default=_config_scope())
    ap.add_argument("--stations", type=int, default=0,
                    help="cap on borehole count (0 = no cap; for probing)")
    ap.add_argument("--full", action="store_true",
                    help="force a full CDS backfill from trace_start_year "
                         "(overwrites any Open-Meteo-fetched cache with raw "
                         "CDS-ERA5); default tops up only the missing tail")
    ap.add_argument("--source", choices=["cds_ts", "cds", "open_meteo"],
                    default="cds_ts",
                    help="reanalysis source: cds_ts (ARCO point time-series — "
                         "fast, no queue; default) | cds (daily-stats box — "
                         "slower, queued) | open_meteo (legacy per-point archive)")
    args = ap.parse_args()

    start = date(int(scfg.get("trace_start_year", 1991)), 1, 1)
    end = date.today()
    months = int(scfg.get("months", 6))

    ids = sorted(select_scope(args.scope))
    if args.stations:
        ids = ids[:args.stations]
    cat = (pd.read_csv(CATALOGUE).query("measure_type == 'groundwater'")
           .dropna(subset=["lat", "lon"]).drop_duplicates("station_id")
           .set_index("station_id"))

    points = {sid: (float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"]))
              for sid in ids if sid in cat.index}
    skipped = len(ids) - len(points)
    print(f"Seasonal inputs — scope={args.scope}, {len(points)} boreholes "
          f"({skipped} skipped: no coords), ERA5/PET back to {start.year}, "
          f"source={args.source}{' [FULL backfill]' if args.full else ''}")

    ok = 0
    if args.source in ("cds_ts", "cds"):
        try:
            if args.source == "cds_ts":
                ok = _refresh_via_cds_ts(points, start, end, full=args.full)
            if ok == 0:                    # box path (chosen, or ts-empty fallback)
                if args.source == "cds_ts":
                    print("timeseries returned nothing — falling back to CDS box")
                ok = _refresh_via_cds(points, start, end, full=args.full)
        except Exception as exc:
            print(f"CDS path failed ({type(exc).__name__}: {exc}); "
                  f"falling back to Open-Meteo per-point")
            ok = _refresh_via_open_meteo(points, start, end)
    else:
        ok = _refresh_via_open_meteo(points, start, end)

    # SEAS5 monthly members: one CDS box for all points (free + commercial),
    # else per-point Open-Meteo. Outlook month m_ahead = forecastMonth m+1,
    # so request leadtime 1..months+1 to cover all `months` outlook months.
    _refresh_seas5(points, _seas5_ref(end), months)

    print(f"Done: {ok} cached, {skipped} skipped (no coords)")
    return 0


def _seas5_ref(today: date) -> date:
    """Latest available SEAS5 init month — this month once its run is out
    (released ~5th), else the previous month."""
    if today.day >= 6:
        return today.replace(day=1)
    year, month = (today.year, today.month - 1) if today.month > 1 \
        else (today.year - 1, 12)
    return date(year, month, 1)


def _refresh_seas5(points: dict[str, tuple[float, float]], ref: date,
                   months: int) -> None:
    if not points:
        return
    try:
        n = seas5.fetch_seas5_cds(ref, points, months=months + 1)
        print(f"SEAS5: {n} points via CDS (init {ref:%Y-%m})")
        return
    except Exception as exc:
        print(f"SEAS5 CDS unavailable ({type(exc).__name__}: {exc}); "
              f"falling back to Open-Meteo per-point")
    for sid, (lat, lon) in points.items():
        try:
            seas5.fetch_seas5_daily(lat, lon, forecast_days=31 * months)
        except Exception as exc:
            print(f"  ! SEAS5 {sid[:8]} failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())

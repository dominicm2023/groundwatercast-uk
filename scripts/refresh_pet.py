"""Fetch & cache PET (ET0) for every borehole in a forecast scope.

Prerequisite for the Pastas pipeline (build_pastas_models needs PET cached per
borehole). PET is self-computed FAO-56 from ERA5 met fields via ONE Copernicus
CDS bounding-box fetch for the whole scope (src/data/cds_era5.py + src/data/
et0.py — the free-data migration, docs/free_data_migration.md). Idempotent —
tops up only the missing tail unless --full. The legacy per-point Open-Meteo
path stays as --source open_meteo (dev / no key), and is used automatically when
the CDS path can't run.

  python -m scripts.refresh_pet            # incremental CDS top-up
  python -m scripts.refresh_pet --full     # full CDS backfill (cutover)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from src.data import pet
from src.forecast.ensemble.scope import select_scope

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "data" / "processed" / "catalogue.csv"


def _config_scope() -> str:
    """Default PET scope = the configured forecast scope, so PET is cached for
    exactly the boreholes calibration (8f) will fit. Falls back to 'live'."""
    try:
        cfg = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))
        return (cfg.get("forecast", {}).get("ensemble", {})
                .get("pastas", {}).get("scope", "live"))
    except Exception:
        return "live"


def _incremental_start(points: dict, cache_root: Path, full_start: date) -> date:
    """Earliest date to fetch for an incremental top-up: a week before the oldest
    per-station cache tail. Returns ``full_start`` if any station has no cache."""
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


def _refresh_via_open_meteo(points: dict, joined: pd.DataFrame, end: date) -> int:
    """Legacy per-point Open-Meteo archive fetch (dev / no-CDS fallback)."""
    ok = failed = 0
    for sid, (lat, lon) in points.items():
        g = joined[joined["station_id"] == sid].dropna(subset=["GW_Level"])
        if g.empty:
            continue
        start = pd.Timestamp(g["dateTime"].min()).date()
        try:
            s = pet.fetch_station_pet(sid, lat, lon, start, end)
            ok += 1
            if ok % 10 == 0:
                print(f"  …{ok} cached (latest {sid[:8]}: {len(s)} days)")
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
    cfg = json.loads((ROOT / "config" / "config.json").read_text(encoding="utf-8"))

    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["user", "live", "fleet"],
                    default=_config_scope())
    ap.add_argument("--full", action="store_true",
                    help="force a full CDS backfill from download.min_date "
                         "(overwrites any Open-Meteo-fetched cache); default tops "
                         "up only the missing tail")
    ap.add_argument("--source", choices=["cds_ts", "cds", "open_meteo"],
                    default="cds_ts",
                    help="ET0 source: cds_ts (ARCO point time-series — fast, no "
                         "queue; default) | cds (daily-stats box — queued) | "
                         "open_meteo (legacy per-point archive)")
    args = ap.parse_args()

    ids = sorted(select_scope(args.scope))
    cat = (pd.read_csv(CATALOGUE).query("measure_type == 'groundwater'")
           .dropna(subset=["lat", "lon"]).drop_duplicates("station_id")
           .set_index("station_id"))
    joined = pd.read_csv(ROOT / "data/features/joined_timeseries.csv",
                         usecols=["dateTime", "GW_Level", "station_id"])
    has_gw = set(joined.dropna(subset=["GW_Level"])["station_id"].unique())

    points = {sid: (float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"]))
              for sid in ids if sid in cat.index and sid in has_gw}
    skipped = len(ids) - len(points)

    floor = date.fromisoformat(str(cfg.get("download", {})
                                   .get("min_date", "2018-01-01")))
    end = date.today()
    print(f"PET refresh — scope={args.scope}, {len(points)} boreholes, "
          f"source={args.source}{' [FULL backfill]' if args.full else ''}")

    box_start = floor if args.full else _incremental_start(
        points, pet.PET_CACHE_ROOT, floor)
    if args.source in ("cds_ts", "cds"):
        try:
            n = 0
            if args.source == "cds_ts":
                from src.data import cds_timeseries as ts
                print(f"CDS timeseries (ARCO point) fetch {box_start} → {end} "
                      f"(precip + FAO-56 ET0) for {len(points)} boreholes …")
                n, failed = ts.update_caches_timeseries(points, box_start, end)
                if failed:
                    print(f"  {len(failed)} point(s) failed; e.g. {failed[0]}")
                print(f"CDS-ts: {n}/{len(points)} stations' PET caches updated")
            if n == 0:                     # box path (chosen, or ts-empty fallback)
                if args.source == "cds_ts":
                    print("timeseries returned nothing — falling back to CDS box")
                from src.data import cds_era5
                print(f"CDS box fetch {box_start} → {end} (6 met fields → FAO-56 ET0)")
                n = cds_era5.update_pet_caches(points, box_start, end)
                print(f"CDS: {n} stations' PET caches updated")
        except Exception as exc:
            print(f"CDS path failed ({type(exc).__name__}: {exc}); "
                  f"falling back to Open-Meteo per-point")
            _refresh_via_open_meteo(points, joined, end)
    else:
        _refresh_via_open_meteo(points, joined, end)

    print(f"Done → {pet.PET_CACHE_ROOT.relative_to(ROOT)} "
          f"({skipped} skipped: no coords/GW)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

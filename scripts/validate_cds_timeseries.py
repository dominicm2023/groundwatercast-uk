"""Validate the CDS timeseries ARCO path (src/data/cds_timeseries.py) BEFORE cutover.

Checks, against the already-cached `era5_precip` series:
  1. CSV schema (prints raw columns so the canonical mapping can't drift silently).
  2. Day-alignment — the zero-shift correlation must beat its +-1-day neighbours by
     a wide margin (a de-accumulation/labelling bug craters this). Sweeps the
     accumulation hour-shift {0, -1} and reports the better.
  3. Magnitude — mean bias + annual-total within source-difference tolerance
     (cached is Open-Meteo's downscaled blend; timeseries is raw ERA5 0.25° — same
     real source gap as W4, so this is NOT an equality test).
  4. ET0 sanity (self-computed FAO-56 stays in a physical range).
  5. Speed — times each point pull, to confirm it beats the queued box path.

    python -m scripts.validate_cds_timeseries [--stations 3] [--start 2020-01-01] [--end 2022-12-31]

Exit 0 = PASS (all points aligned + sane + ET0 ok), 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data import cds_timeseries as ts                      # noqa: E402
from src.data.et0 import et0_fao56_daily                       # noqa: E402
from src.data.era5_precip import PRECIP_CACHE_ROOT             # noqa: E402

ALIGN_MARGIN = 0.30
ALIGN_R_MIN = 0.70
BIAS_MAX = 0.6          # mm/day (source-difference tolerance, raw vs downscaled)
TOTAL_REL_MAX = 0.25


def _iso(s: str) -> date:
    return pd.Timestamp(s).date()


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", type=int, default=3)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2022-12-31")
    args = ap.parse_args(argv)
    start, end = _iso(args.start), _iso(args.end)

    cat = (pd.read_csv(ROOT / "data/processed/catalogue.csv")
           .query("measure_type == 'groundwater'").dropna(subset=["lat", "lon"])
           .drop_duplicates("station_id").set_index("station_id"))
    cached = sorted(PRECIP_CACHE_ROOT.glob("*.csv"))
    picks = [p for p in cached if p.stem in cat.index][:args.stations]
    if not picks:
        print("no era5_precip-cached boreholes to compare against")
        return 1

    # --- 1. schema probe (short window, first pick) ---
    sid0 = picks[0].stem
    lat0, lon0 = float(cat.loc[sid0, "lat"]), float(cat.loc[sid0, "lon"])
    probe_end = min(end, _iso("2020-03-31"))
    print(f"== schema probe: {sid0[:8]}  {start}..{probe_end} ==")
    t0 = time.time()
    raw = ts.fetch_point_raw(lat0, lon0, start, probe_end)
    print(f"  fetched in {time.time() - t0:.1f}s; raw columns: {list(raw.columns)}")
    print(raw.head(3).to_string(), "\n")

    rows = []
    for p in picks:
        sid = p.stem
        lat, lon = float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"])
        name = str(cat.loc[sid, "station_name"])
        t0 = time.time()
        hourly = ts.fetch_point_hourly(lat, lon, start, end)
        dt = time.time() - t0

        ref = pd.read_csv(p, parse_dates=["date"]).set_index("date")["precip_mm"]
        ref.index = pd.DatetimeIndex(ref.index).tz_localize(None)

        def corr(precip: pd.Series, shift: int) -> float:
            m = pd.concat([precip.shift(shift).rename("c"), ref.rename("o")],
                          axis=1).dropna()
            return float(np.corrcoef(m["c"], m["o"])[0, 1]) if len(m) > 30 else float("nan")

        best = None
        for sh in (0, -1):                       # accumulation day-label sweep
            precip, met = ts.daily_aggregate(hourly, accum_shift_h=sh)
            r0, rm1, rp1 = corr(precip, 0), corr(precip, -1), corr(precip, 1)
            if best is None or (r0 == r0 and r0 > best["r0"]):
                best = {"sh": sh, "r0": r0, "rn": max(rm1, rp1),
                        "precip": precip, "met": met}

        precip, met = best["precip"], best["met"]
        m = pd.concat([precip.rename("c"), ref.rename("o")], axis=1).dropna()
        bias = float((m["c"] - m["o"]).mean())
        tot = float(abs(m["c"].sum() - m["o"].sum()) / m["o"].sum())
        et0 = et0_fao56_daily(met, lat_deg=lat)
        et0_ok = (et0.dropna().between(0, 15).mean() > 0.99
                  and 0.4 < float(et0.mean()) < 4.5)
        aligned = best["r0"] >= ALIGN_R_MIN and best["r0"] - best["rn"] >= ALIGN_MARGIN
        ok = aligned and abs(bias) <= BIAS_MAX and tot <= TOTAL_REL_MAX and et0_ok
        rows.append(ok)
        print(f"  {name[:24]:<24} {dt:5.1f}s shift={best['sh']:>2}h "
              f"r0={best['r0']:.3f} (±1d {best['rn']:.3f}) "
              f"bias={bias:+.2f} tot={tot:.0%} ET0μ={float(et0.mean()):.2f} "
              f"{'PASS' if ok else 'FAIL'}")

    verdict = "PASS" if rows and all(rows) else "FAIL"
    print(f"\nVERDICT: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""W5 validation — self-computed FAO-56 ET0 (src/data/et0.py) vs the cached
Open-Meteo ``et0_fao_evapotranspiration`` series (same FAO-56 upstream).

Fetches daily ERA5 met fields (Open-Meteo archive — dev/validation use)
for a few PET-cached boreholes, computes ET0 with pyet, and scores against
the cached series. Expectation per docs/free_data_migration.md W5:
r > 0.99 with a small bias. Report: outputs/et0_validation.md.

    python -m scripts.validate_et0 [--stations 3] [--years 3]

Exit 0 = every station meets the gates (r >= 0.99, |bias| <= 0.15 mm/day,
MAE <= 0.25 mm/day); 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.data.et0 import et0_fao56_daily, et0_hargreaves_daily  # noqa: E402
from src.data.pet import PET_CACHE_ROOT  # noqa: E402

_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_MET_DAILY = ["temperature_2m_mean", "temperature_2m_max",
              "temperature_2m_min", "dew_point_2m_mean",
              "wind_speed_10m_mean", "shortwave_radiation_sum"]
_RENAME = {"temperature_2m_mean": "tmean_c", "temperature_2m_max": "tmax_c",
           "temperature_2m_min": "tmin_c", "dew_point_2m_mean": "dewpoint_c",
           "wind_speed_10m_mean": "wind10_ms",
           "shortwave_radiation_sum": "srad_mj"}

R_MIN, BIAS_MAX, MAE_MAX = 0.99, 0.15, 0.25


def _fetch_met(lat: float, lon: float, start: str, end: str) -> tuple[pd.DataFrame, float]:
    params = {"latitude": round(lat, 4), "longitude": round(lon, 4),
              "start_date": start, "end_date": end,
              "daily": ",".join(_MET_DAILY),
              "wind_speed_unit": "ms", "timezone": "GMT"}
    r = requests.get(_ARCHIVE, params=params, timeout=120)
    if r.status_code == 429:
        time.sleep(60)
        r = requests.get(_ARCHIVE, params=params, timeout=120)
    r.raise_for_status()
    payload = r.json()
    d = payload["daily"]
    met = pd.DataFrame({_RENAME[k]: d[k] for k in _MET_DAILY},
                       index=pd.to_datetime(d["time"]))
    return met, float(payload.get("elevation", 0.0))


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stations", type=int, default=3)
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args(argv)

    cat = pd.read_csv(ROOT / "data/processed/catalogue.csv")
    cat = (cat[cat["measure_type"] == "groundwater"]
           .drop_duplicates("station_id").set_index("station_id"))
    cached = sorted(PET_CACHE_ROOT.glob("*.csv"))
    picks = [p for p in cached if p.stem in cat.index][:args.stations]
    if not picks:
        print("no PET-cached boreholes found - run scripts/refresh_pet first")
        return 1

    end = pd.Timestamp.today().normalize() - pd.Timedelta(days=10)
    start = end - pd.DateOffset(years=args.years)

    rows = []
    for p in picks:
        sid = p.stem
        lat, lon = float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"])
        name = str(cat.loc[sid, "station_name"])
        ref = pd.read_csv(p, parse_dates=["date"]).set_index("date")["et0_mm"]
        ref.index = pd.DatetimeIndex(ref.index).tz_localize(None)

        met, elev = _fetch_met(lat, lon, start.date().isoformat(),
                               end.date().isoformat())
        ours = et0_fao56_daily(met, lat_deg=lat, elevation_m=elev)
        harg = et0_hargreaves_daily(met, lat_deg=lat)
        time.sleep(1.0)                      # free-tier courtesy spacing

        m = pd.concat([ours.rename("ours"), harg.rename("hargreaves"),
                       ref.rename("ref")], axis=1).dropna()
        if len(m) < 300:
            print(f"  {name}: only {len(m)} overlapping days - skipping")
            continue
        r = float(np.corrcoef(m["ours"], m["ref"])[0, 1])
        bias = float((m["ours"] - m["ref"]).mean())
        mae = float((m["ours"] - m["ref"]).abs().mean())
        r_h = float(np.corrcoef(m["hargreaves"], m["ref"])[0, 1])
        ok = r >= R_MIN and abs(bias) <= BIAS_MAX and mae <= MAE_MAX
        rows.append({"station": name, "n_days": len(m), "r": r, "bias": bias,
                     "mae": mae, "r_hargreaves": r_h, "pass": ok})
        print(f"  {name[:30]:<30} n={len(m)} r={r:.4f} bias={bias:+.3f} "
              f"mae={mae:.3f} (hargreaves r={r_h:.3f}) "
              f"{'PASS' if ok else 'FAIL'}")

    if not rows:
        return 1
    res = pd.DataFrame(rows)
    verdict = "PASS" if bool(res["pass"].all()) else "FAIL"

    out = ROOT / "outputs" / "et0_validation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ET0 validation — pyet FAO-56 PM vs Open-Meteo `et0_fao_evapotranspiration` (W5)",
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- Generated: {pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Window: {start.date()} → {end.date()} (daily)",
        "- Inputs: ERA5 daily met (tmean/tmax/tmin, dewpoint→ea, 10 m wind→2 m, "
        "shortwave radiation) via the Open-Meteo archive (validation transport "
        "only — production met comes from CDS at cutover).",
        "",
        "| Borehole | days | r | bias (mm/d) | MAE (mm/d) | Hargreaves r | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r_ in res.iterrows():
        lines.append(f"| {r_['station']} | {int(r_['n_days'])} | {r_['r']:.4f} "
                     f"| {r_['bias']:+.3f} | {r_['mae']:.3f} | "
                     f"{r_['r_hargreaves']:.3f} | "
                     f"{'PASS' if r_['pass'] else 'FAIL'} |")
    lines += [
        "",
        f"Gates: r >= {R_MIN}, |bias| <= {BIAS_MAX} mm/day, MAE <= {MAE_MAX} "
        "mm/day. Hargreaves shown for the fallback's expected (cruder) skill.",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"verdict: {verdict} -> {out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Build the per-borehole monthly GW normals (main-env, core group).

The quantile ladder (p10 / t1 / median / t2 / p90 per calendar month) is
the yardstick the whole product's vocabulary hangs off: the dashboard's
current status vs normal, the seasonal tercile outlook, and the forecast
tier's p_above_p90 signal. Pure pandas from the joined timeseries —
rebuild whenever it changes (run_chain --core runs this after freshness).

  python -m scripts.build_gw_normals
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from src.forecast.seasonal.normals import gw_monthly_normals

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "data/features/joined_timeseries.csv"
DEFAULT_OUT = "data/model/gw_monthly_normals.csv"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not JOINED.exists():
        print(f"No joined timeseries at {JOINED} — run src.pipeline.run first.")
        return 1
    cfg = json.loads((ROOT / "config/config.json").read_text())
    out_path = ROOT / cfg.get("forecast", {}).get("seasonal", {}).get(
        "normals_cache", DEFAULT_OUT)

    joined = pd.read_csv(JOINED, index_col=0, parse_dates=True)
    df = gw_monthly_normals(
        joined.reset_index().rename(
            columns={joined.index.name or "index": "dateTime"}))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"Monthly normals: {df['station_id'].nunique()} boreholes × "
          f"{df['month'].nunique()} months → {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

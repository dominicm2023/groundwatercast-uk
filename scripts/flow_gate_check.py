"""Low-flow Rivers layer — Stage 4 admission-gate CLI
(``docs/product/lowflow/build_plan.md``).

Runs ``src.forecast.pastas.flow_gate.admit_gauge`` for a list of flow gauges
and writes one row per gauge to ``outputs/flow_gate_check.csv``. Per-gauge data
assembly (flow shard, rainfall raw archives, PET cache — ingest-if-missing)
lives in ``src.download.flow.load_gauge_series``, shared with the Stage-5
fleet scan (``scripts/flow_fleet_scan.py``); a gauge with data that can't be
assembled is recorded as ``status_only``/``no_data``, never aborts the batch.

Usage:
    python -m scripts.flow_gate_check                       # every flow_links.csv gauge
    python -m scripts.flow_gate_check --gauges ID1,ID2,...  # explicit list
    python -m scripts.flow_gate_check --limit 10            # first N (sorted by id)
    python -m scripts.flow_gate_check --out outputs/flow_gate_check.csv

Pastas lives in the dedicated ``.venv-pastas`` environment:
    .venv-pastas\\Scripts\\python.exe -m scripts.flow_gate_check --gauges ...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.download.build import load_config
from src.download.flow import (
    FLOW_CATALOGUE_PATH,
    FLOW_LINKS_PATH,
    load_flow_measure_map,
    load_gauge_series,
)
from src.forecast.pastas import flow_gate as G
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
FLOW_CATALOGUE = ROOT / FLOW_CATALOGUE_PATH
OUT_PATH = ROOT / "outputs" / "flow_gate_check.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gauges", default=None,
                    help="comma-separated GaugeIDs (default: every gauge in "
                         "flow_links.csv)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N gauges, sorted by id")
    ap.add_argument("--links", default=str(FLOW_LINKS_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    config = load_config()
    links_path = Path(args.links)
    if not links_path.exists():
        print(f"ERROR: {links_path} not found.", file=sys.stderr)
        return 2
    links_df = pd.read_csv(links_path, dtype=str).set_index("GaugeID")
    measure_map = load_flow_measure_map(links_path)
    cat_df = pd.read_csv(FLOW_CATALOGUE, dtype={"station_id": str}).set_index("station_id")

    if args.gauges:
        gauge_ids = [g.strip() for g in args.gauges.split(",") if g.strip()]
    else:
        gauge_ids = sorted(measure_map)
    if args.limit is not None:
        gauge_ids = gauge_ids[: args.limit]

    print(f"Flow gate check: {len(gauge_ids)} gauge(s)")
    rows = []
    for i, gauge_id in enumerate(gauge_ids, 1):
        name = (cat_df.loc[gauge_id, "station_name"]
                if gauge_id in cat_df.index else gauge_id)
        try:
            loaded = load_gauge_series(gauge_id, links_df, cat_df, measure_map, config)
        except Exception as exc:
            print(f"  [{i}/{len(gauge_ids)}] {gauge_id[:8]} {name}: LOAD FAIL "
                  f"({type(exc).__name__}: {exc})")
            loaded = None

        if loaded is None:
            row = {"gauge_id": gauge_id, "station_name": name, "gate_pass": False,
                   "tier": "status_only", "rain_dependent": False,
                   "n_origins": 0, "n_years": 0, "range_logq": None,
                   "floor_skill": None, "floor_cov14": None, "floor_band_frac": None,
                   "ceiling_skill": None, "ceiling_cov14": None, "ceiling_band_frac": None,
                   "reason": "no_data"}
        else:
            q, prec, evap = loaded
            result = G.admit_gauge(gauge_id, q, prec, evap)
            floor = result.get("floor") or {}
            ceiling = result.get("ceiling") or {}
            row = {
                "gauge_id": gauge_id, "station_name": name,
                "gate_pass": result["gate_pass"], "tier": result["tier"],
                "rain_dependent": result["rain_dependent"],
                "n_origins": result["n_origins"], "n_years": result["n_years"],
                "range_logq": result["range_logq"],
                "floor_skill": floor.get("skill_ratio"),
                "floor_cov14": floor.get("cov14"),
                "floor_band_frac": floor.get("band_frac"),
                "ceiling_skill": ceiling.get("skill_ratio"),
                "ceiling_cov14": ceiling.get("cov14"),
                "ceiling_band_frac": ceiling.get("band_frac"),
                "reason": result["reason"],
            }
        rows.append(row)
        print(f"  [{i}/{len(gauge_ids)}] {gauge_id[:8]} {str(name)[:24]:24s} "
              f"{row['tier']:14s} origins={row['n_origins']}/{row['n_years']}y  "
              f"{row['reason']}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False)
    print(f"\nWrote {len(rows)} row(s) -> {out_path}")
    if not df.empty:
        print(f"Tiers: {df['tier'].value_counts().to_dict()}")
    return 0


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        sys.exit(run(parse_args()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

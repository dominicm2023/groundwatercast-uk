"""Low-flow Rivers layer — Stage 6 pilot selection
(``docs/product/lowflow/build_plan.md``).

Reads the Stage-5 fleet scan (``outputs/flow_fleet_scan.csv``) and selects the
~50-gauge southern chalk pilot DETERMINISTICALLY: every ``tier=="tier1"`` row,
sorted by ``floor_skill`` ascending (best floor-robust skill first — lower is
better, ``skill_ratio = mean|obs-P50| / mean|obs-recession|``), ties broken by
``gauge_id`` for full reproducibility, capped at ``PILOT_SIZE``. Writes
``data/processed/flow_pilot.csv`` (``gauge_id, station_name, floor_skill`` —
Q95 is deliberately NOT computed here; that is ``build_flow_models.py``'s job,
from each gauge's full flow shard once it's ingested).

The fleet scan is a long-running background job (Stage 5) that may still be
mid-run when this is invoked — a partial scan (whatever rows exist so far) is
scored the same way; re-running once the scan completes just refreshes the
list. Never raises on a missing/partial scan: a host that hasn't run the
fleet scan yet must not error, it just has nothing to select.

Usage:
    python -m scripts.select_flow_pilot
    python -m scripts.select_flow_pilot --pilot-size 10   # smoke test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
SCAN_PATH = ROOT / "outputs" / "flow_fleet_scan.csv"
OUT_PATH = ROOT / "data" / "processed" / "flow_pilot.csv"

PILOT_SIZE = 50
OUT_COLS = ["gauge_id", "station_name", "floor_skill"]
REQUIRED_SCAN_COLS = {"gauge_id", "station_name", "tier", "floor_skill"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan", default=str(SCAN_PATH),
                    help="path to the Stage-5 fleet scan CSV (default: %(default)s)")
    ap.add_argument("--out", default=str(OUT_PATH),
                    help="path to write the pilot CSV (default: %(default)s)")
    ap.add_argument("--pilot-size", type=int, default=PILOT_SIZE,
                    help="max gauges in the pilot (default: %(default)s)")
    return ap.parse_args(argv)


def _display_path(path: Path) -> str:
    """Path for log output: relative to the repo root when possible (the
    normal case), else the path as given — ``--out``/``--scan`` accept
    arbitrary overrides (tests use a tmp dir outside the repo), and
    ``Path.relative_to`` raises on those rather than falling back."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def select_pilot(scan: pd.DataFrame, *, pilot_size: int = PILOT_SIZE) -> pd.DataFrame:
    """Deterministic pilot selection: tier1 rows only, sorted by
    ``floor_skill`` ascending (ties broken by ``gauge_id``), top
    ``pilot_size``. Pure function — no I/O, easy to test in isolation."""
    tier1 = scan[scan["tier"] == "tier1"].copy()
    tier1 = tier1.sort_values(["floor_skill", "gauge_id"], ascending=[True, True],
                              kind="mergesort")  # stable — reproducible on ties
    return tier1[OUT_COLS].head(pilot_size).reset_index(drop=True)


def run(args: argparse.Namespace) -> int:
    scan_path = Path(args.scan)
    if not scan_path.exists():
        # Not a hard requirement of any chain stage (this script isn't wired
        # into run_chain — it's a manual/periodic step like flow_fleet_scan.py
        # itself), but the same "never abort on an absent optional-subsystem
        # input" discipline applies: exit 0, say why, don't raise.
        print(f"{scan_path} not found — pilot selection skipped (run "
              f"'python -m scripts.flow_fleet_scan' first).")
        return 0

    scan = pd.read_csv(scan_path, dtype={"gauge_id": str})
    missing_cols = REQUIRED_SCAN_COLS - set(scan.columns)
    if missing_cols:
        print(f"{scan_path} is missing expected column(s) {sorted(missing_cols)} "
              f"— pilot selection skipped.")
        return 0

    n_tier1_total = int((scan["tier"] == "tier1").sum())
    pilot = select_pilot(scan, pilot_size=args.pilot_size)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pilot.to_csv(out_path, index=False)

    print(f"Fleet scan: {len(scan)} gauge(s) scored so far, {n_tier1_total} tier1.")
    print(f"Pilot: {len(pilot)} gauge(s) selected (cap {args.pilot_size}) "
          f"-> {_display_path(out_path)}")
    if len(pilot):
        print(f"  floor_skill range: {pilot['floor_skill'].min():.3f}"
              f"..{pilot['floor_skill'].max():.3f}")
    return 0


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        sys.exit(run(parse_args()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

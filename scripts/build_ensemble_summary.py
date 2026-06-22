"""Phase 3 build — aggregate member trajectories into the probabilistic summary.

Reads data/model/forecast_ensemble_members.parquet, resolves each borehole's
breach threshold, and writes:
  - data/model/forecast_ensemble_summary.csv   (one row per BH: breach prob,
    first-crossing distribution, headline sentence)
  - data/model/forecast_ensemble_fan.csv       (per-day P10/P50/P90, latest run)
  - data/model/forecast_ensemble_archive.parquet  (append-only summary scalars,
    for Phase C calibration once actuals arrive — design §11)
  - data/model/forecast_ensemble_fan_archive.parquet  (append-only per-day fan —
    the raw material for spread-skill / PIT / CRPS verification; roadmap 0.1)

Usage: python -m scripts.build_ensemble_summary
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import json
import pandas as pd

from src.features.io import load_features
from src.forecast.ensemble.aggregate import aggregate
from src.utils.io_encoding import force_utf8_stdio

MEMBERS = _ROOT / "data" / "model" / "forecast_ensemble_members.parquet"
SUMMARY = _ROOT / "data" / "model" / "forecast_ensemble_summary.csv"
FAN = _ROOT / "data" / "model" / "forecast_ensemble_fan.csv"
ARCHIVE = _ROOT / "data" / "model" / "forecast_ensemble_archive.parquet"
FAN_ARCHIVE = _ROOT / "data" / "model" / "forecast_ensemble_fan_archive.parquet"
CATALOGUE = _ROOT / "data" / "processed" / "catalogue.csv"


def append_archive(prior: pd.DataFrame | None, summary: pd.DataFrame) -> pd.DataFrame:
    """Run-stamped append with dedup: `run` is floored to the hour, so a re-run
    within the same hour REPLACES its rows instead of duplicating them. Archive
    rows pre-dating the scope column get scope="unknown"."""
    combined = (pd.concat([prior, summary], ignore_index=True)
                if prior is not None else summary.copy())
    if "scope" in combined.columns:
        combined["scope"] = combined["scope"].fillna("unknown")
    return combined.drop_duplicates(subset=["station_id", "run"], keep="last")


def append_fan_archive(prior: pd.DataFrame | None, fan: pd.DataFrame) -> pd.DataFrame:
    """Append-only per-day fan archive (roadmap 0.1). Same run-stamped dedup as
    the summary archive but keyed on (station_id, run, lead) so a same-hour rerun
    replaces its rows. The summary archive keeps only scalars; without this the
    full P10/P50/P90 fan — the raw material for spread-skill / PIT / CRPS — is
    overwritten and lost every run."""
    if fan is None or fan.empty:
        return prior if prior is not None else fan
    combined = (pd.concat([prior, fan], ignore_index=True)
                if prior is not None else fan.copy())
    return combined.drop_duplicates(subset=["station_id", "run", "lead"], keep="last")


def main() -> int:
    force_utf8_stdio()
    if not MEMBERS.exists():
        print(f"ERROR: {MEMBERS} not found — run scripts.build_ensemble_members first.")
        return 2

    traj = pd.read_parquet(MEMBERS)
    traj["date"] = pd.to_datetime(traj["date"])
    run = pd.Timestamp.now(tz="UTC").floor("h")

    # Per-station historical GW P90 for the proxy-threshold fallback.
    cfg = json.loads((_ROOT / "config" / "config.json").read_text())
    hist, _ = load_features(cfg)
    gw_p90 = (hist.groupby("station_id")["GW_Level"].quantile(0.90)
              if "GW_Level" in hist.columns else pd.Series(dtype=float))
    gw_p90_by_station = gw_p90.to_dict()

    summary, fan = aggregate(traj, run=run, gw_p90_by_station=gw_p90_by_station)

    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(SUMMARY, index=False)
    fan.to_csv(FAN, index=False)

    # Append-only archives for Phase C (run-stamped, same-run reruns deduped):
    # summary scalars + the full per-day fan (roadmap 0.1 — the verification trail).
    combined = append_archive(pd.read_parquet(ARCHIVE) if ARCHIVE.exists() else None,
                              summary)
    combined.to_parquet(ARCHIVE, compression="snappy", index=False)
    fan_combined = append_fan_archive(
        pd.read_parquet(FAN_ARCHIVE) if FAN_ARCHIVE.exists() else None, fan)
    fan_combined.to_parquet(FAN_ARCHIVE, compression="snappy", index=False)

    names = dict(zip(pd.read_csv(CATALOGUE)["station_id"],
                     pd.read_csv(CATALOGUE)["station_name"]))
    print(f"Run {run}  ·  {len(summary)} boreholes\n")
    for _, r in summary.iterrows():
        nm = names.get(r["station_id"], r["station_id"][:8])
        src = r["threshold_source"]
        print(f"• {nm}  [{src}]")
        print(f"    {r['headline']}")
    print(f"\nWrote:\n  {SUMMARY.relative_to(_ROOT)}\n  {FAN.relative_to(_ROOT)}")
    print(f"  {ARCHIVE.relative_to(_ROOT)}  ({len(combined)} archived rows)")
    print(f"  {FAN_ARCHIVE.relative_to(_ROOT)}  ({len(fan_combined)} archived fan rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

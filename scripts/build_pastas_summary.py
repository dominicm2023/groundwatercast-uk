"""Aggregate Pastas member trajectories into the probabilistic summary (module 3).

Reads forecast_pastas_members.parquet (module 2) + pastas_models.json (for the
AR1 sigma/alpha) + forecast_ensemble_members.parquet (roll P50 for the model-
spread band), resolves each borehole's breach threshold, Monte-Carlos
trajectories (member spread + AR1 noise), and writes:
  - forecast_pastas_summary.csv   (breach prob, first-crossing, headline, model-spread)
  - forecast_pastas_fan.csv       (per-day P10/P50/P90 + roll_p50 + model_spread)
  - forecast_pastas_archive.parquet (append-only summary scalars — Phase C trail)
  - forecast_pastas_fan_archive.parquet (append-only per-day fan — verification
    raw material for spread-skill / PIT / CRPS; roadmap 0.1)

Runs in either environment (no pastas import), but is wired into the venv cron
chain after build_pastas_members. Usage:
  python -m scripts.build_pastas_summary
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.forecast.pastas import recharge as R
from src.forecast.pastas.summary import aggregate_pastas

JOINED = ROOT / "data/features/joined_timeseries.csv"
CATALOGUE = ROOT / "data/processed/catalogue.csv"
ROLL_MEMBERS = ROOT / "data/model/forecast_ensemble_members.parquet"
NORMALS = ROOT / "data/model/gw_monthly_normals.csv"


def _load_monthly_p90s(path: Path) -> dict[str, dict[int, float]]:
    """{station → {calendar month → p90 normal}} from gw_monthly_normals.csv
    (built by scripts/build_gw_normals.py). Empty dict when absent — the
    p_above_p90_14d signal degrades to NaN rather than blocking the cron."""
    if not path.exists():
        print("WARNING: gw_monthly_normals.csv missing — p_above_p90_14d will "
              "be NaN (run `python -m scripts.build_gw_normals`).")
        return {}
    df = pd.read_csv(path)
    if "p90" not in df.columns:
        print("WARNING: normals artefact pre-dates the quantile ladder — "
              "rebuild with `python -m scripts.build_gw_normals`.")
        return {}
    out: dict[str, dict[int, float]] = {}
    for sid, g in df.groupby("station_id"):
        out[str(sid)] = dict(zip(g["month"].astype(int), g["p90"].astype(float)))
    return out


def append_archive(prior: pd.DataFrame | None, summary: pd.DataFrame) -> pd.DataFrame:
    """Run-stamped append with dedup: `run` is floored to the hour, so a re-run
    within the same hour REPLACES its rows instead of duplicating them. Archive
    rows pre-dating the scope column get scope="unknown". (Mirrors the helper in
    build_ensemble_summary — kept duplicated to avoid a shared module.)"""
    combined = (pd.concat([prior, summary], ignore_index=True)
                if prior is not None else summary.copy())
    if "scope" in combined.columns:
        combined["scope"] = combined["scope"].fillna("unknown")
    return combined.drop_duplicates(subset=["station_id", "run"], keep="last")


def append_fan_archive(prior: pd.DataFrame | None, fan: pd.DataFrame) -> pd.DataFrame:
    """Append-only per-day fan archive (roadmap 0.1), keyed on
    (station_id, run, lead) so a same-hour rerun replaces its rows. The summary
    archive keeps only scalars; without this the full P10/P50/P90 fan — the raw
    material for spread-skill / PIT / CRPS verification — is overwritten each run.
    (Nowcast leads are negative, forecast leads positive, so lead is unique per
    station-run.)"""
    if fan is None or fan.empty:
        return prior if prior is not None else fan
    combined = (pd.concat([prior, fan], ignore_index=True)
                if prior is not None else fan.copy())
    return combined.drop_duplicates(subset=["station_id", "run", "lead"], keep="last")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = json.loads((ROOT / "config/config.json").read_text())
    pcfg = cfg["forecast"]["ensemble"]["pastas"]

    members_path = ROOT / pcfg["members_cache"]
    if not members_path.exists():
        print("No Pastas members parquet — run scripts.build_pastas_members first.")
        return 2
    members = pd.read_parquet(members_path)
    members["date"] = pd.to_datetime(members["date"])
    models = R.load_models(ROOT / pcfg["models_cache"])
    run = pd.Timestamp.now(tz="UTC").floor("h")

    # Per-station historical GW P90 (proxy-threshold fallback) — from the joined
    # CSV so we don't import the sklearn-bearing model layer in the venv.
    joined = pd.read_csv(JOINED, usecols=["GW_Level", "station_id"])
    gw_p90 = joined.groupby("station_id")["GW_Level"].quantile(0.90).to_dict()

    # Roll P50 per (station, date) for the model-spread band.
    roll_p50: dict[str, pd.Series] = {}
    if ROLL_MEMBERS.exists():
        ro = pd.read_parquet(ROLL_MEMBERS)
        ro["date"] = pd.to_datetime(ro["date"])
        # Staleness check: a roll run older than the pastas members covers an
        # earlier forecast window, so roll_p50 / model_spread would be missing
        # or misleading.
        lag_days = (members["date"].max() - ro["date"].max()).days
        if lag_days > 2:
            print(f"WARNING: roll members end {lag_days} d before the Pastas "
                  f"members ({ro['date'].max().date()} vs "
                  f"{members['date'].max().date()}) — roll_p50/model_spread will "
                  "be missing or misleading. Re-run build_ensemble_members and "
                  "build_pastas_members together.")
        for sid, g in ro.groupby("station_id"):
            roll_p50[sid] = g.groupby("date")["gw_pred"].median()

    # Per-station monthly P90 normals — drives the p_above_p90_14d tier
    # signal ("unusually high for the season").
    monthly_p90s = _load_monthly_p90s(NORMALS)

    summary, fan = aggregate_pastas(
        members, models, run=run, gw_p90_by_station=gw_p90,
        roll_p50_by_station=roll_p50, monthly_p90_by_station=monthly_p90s,
        n_samples=int(pcfg.get("mc_samples", 4000)))

    (ROOT / pcfg["summary_cache"]).parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(ROOT / pcfg["summary_cache"], index=False)
    fan.to_csv(ROOT / pcfg["fan_cache"], index=False)

    archive = ROOT / pcfg["archive_cache"]
    combined = append_archive(pd.read_parquet(archive) if archive.exists() else None,
                              summary)
    combined.to_parquet(archive, compression="snappy", index=False)

    # Append-only per-day fan archive (roadmap 0.1 — the verification trail).
    fan_archive = ROOT / pcfg["fan_archive_cache"]
    fan_combined = append_fan_archive(
        pd.read_parquet(fan_archive) if fan_archive.exists() else None, fan)
    fan_combined.to_parquet(fan_archive, compression="snappy", index=False)

    names = dict(zip(pd.read_csv(CATALOGUE)["station_id"],
                     pd.read_csv(CATALOGUE)["station_name"]))
    print(f"Run {run}  ·  {len(summary)} boreholes (Pastas)\n")
    for _, r in summary.iterrows():
        nm = names.get(r["station_id"], r["station_id"][:8])
        print(f"• {nm}  [{r['threshold_source']}]  model-spread {r['model_spread_mean']:.2f} m")
        print(f"    {r['headline']}")
    print(f"\nWrote {pcfg['summary_cache']}, {pcfg['fan_cache']}, "
          f"{pcfg['archive_cache']} ({len(combined)} archived rows), "
          f"{pcfg['fan_archive_cache']} ({len(fan_combined)} archived fan rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""First verification dry-run (2026-07-18) — score archived operational fans.

Scores every archived (station, run, lead) fan row whose valid date has an
observation, split by calibration era per docs/phase3_verification_scope.md
(era boundary 2026-07-04T12:00Z, cohorts NEVER blended). The archive holds
quantiles only (P10/P50/P90), so the honest metric set is: interval coverage
vs nominal 80%, tail asymmetry (below-P10 / above-P90), MAE(P50), mean
pinball loss over the three quantiles, and band sharpness. This is a harness
de-risk on low-signal summer windows — NOT a calibration claim; the winter
archive remains the real test.

Input: a scored-input parquet with columns
  station_id, run, lead, date, gw_p10, gw_p50, gw_p90, obs
built from the production fan archive + observation shards. The archive lives
on the VPS; build the extract there (one ssh) and scp it down — recipe:

  fan  = data/model/forecast_pastas_fan_archive.parquet
         -> keep lead >= 1, date <= yesterday, quantile cols
  obs  = data/features/gw_by_station/<id>.parquet tails (date >= archive
         start), DROPPING data_source == 'logged_live_stuck' rows (mirror
         pack._read_shard) and left-merge on (station_id, date)

Joins are calendar-date joins by construction — the positional-slicing hazard
noted for run_hindcast (BUGS.md) cannot arise here.

    python -m scripts.score_fan_archive_dryrun <scored_input.parquet>

Results of the first run are recorded in docs/phase3_verification_scope.md
(§First dry-run). Supersedes nothing; the Phase-3 engine replaces this.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

ERA_CUT = "2026-07-04T12"          # PR #76 gauge-recalibration boundary
NOMINAL = 0.80                     # P10-P90 band


def pinball(obs, q, tau):
    d = obs - q
    return np.where(d >= 0, tau * d, (tau - 1) * d)


def score(df: pd.DataFrame) -> pd.DataFrame:
    """Attach per-row scores; returns only rows with an observation."""
    df = df[df["obs"].notna()].copy()
    df["era"] = np.where(df["run"].astype(str) >= ERA_CUT,
                         "gauge-calibration", "joined-calibration")
    df["below"] = df["obs"] < df["gw_p10"]
    df["above"] = df["obs"] > df["gw_p90"]
    df["inband"] = ~(df["below"] | df["above"])
    df["ae50"] = (df["obs"] - df["gw_p50"]).abs()
    df["width"] = df["gw_p90"] - df["gw_p10"]
    df["pb"] = (pinball(df["obs"], df["gw_p10"], 0.1)
                + pinball(df["obs"], df["gw_p50"], 0.5)
                + pinball(df["obs"], df["gw_p90"], 0.9)) / 3
    return df


def _block(g: pd.DataFrame, label: str) -> None:
    print(f"\n== {label} ==  n={len(g)}  stations={g['station_id'].nunique()}")
    print(f"  coverage P10-P90: {g['inband'].mean():.3f}  (nominal {NOMINAL})   "
          f"below-P10: {g['below'].mean():.3f}  above-P90: {g['above'].mean():.3f}")
    print(f"  MAE(P50): {g['ae50'].mean():.4f} m (median {g['ae50'].median():.4f})   "
          f"band width: mean {g['width'].mean():.3f} m   "
          f"pinball: {g['pb'].mean():.4f}")


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    df = score(pd.read_parquet(args[0]))
    print(f"scored rows: {len(df)}  stations: {df['station_id'].nunique()}  "
          f"runs: {df['run'].nunique()}")

    _block(df, "ALL")
    for era in ["joined-calibration", "gauge-calibration"]:
        _block(df[df["era"] == era], era)

    print("\n== by lead ==")
    for era in ["joined-calibration", "gauge-calibration"]:
        g = df[df["era"] == era]
        t = g.groupby("lead").agg(
            n=("inband", "size"), cov=("inband", "mean"),
            below=("below", "mean"), above=("above", "mean"),
            mae=("ae50", "mean"), width=("width", "mean"))
        print(f"\n{era}:")
        print(t.round(3).to_string())

    g = df[df["era"] == "gauge-calibration"]
    st = g.groupby("station_id").agg(n=("inband", "size"),
                                     cov=("inband", "mean"))
    st = st[st["n"] >= 10]
    print(f"\n== per-station coverage, gauge era (n>=10; {len(st)}) ==")
    print(st["cov"].describe().round(3).to_string())
    print(f"stations with cov < 0.5: {(st['cov'] < 0.5).sum()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

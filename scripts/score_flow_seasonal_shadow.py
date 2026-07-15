"""Score CLOSED months in the flow seasonal shadow archive against observed
shard flows — Stage 6b of ``docs/product/lowflow/build_plan.md``.

The shadow archive (``scripts/build_flow_seasonal_shadow.py``) writes one row
per ``(gauge_id, run, month_ahead)`` the day it is forecast; a row's outlook
month is only verifiable once that CALENDAR MONTH has fully ended. This
script never touches the archive — it only reads it and the gauges' flow
shards, printing a scoring summary. Not wired into ``run_chain`` (same
"manual/periodic step" status as ``scripts/select_flow_pilot.py`` — nothing
downstream depends on it).

For each closed ``(gauge_id, run, month_ahead)`` row:
  - ``coverage_hit``  : was the observed monthly-mean flow inside the
    archived ``[q_p10_m3s, q_p90_m3s]`` band?
  - ``tercile_hit``   : flow has no separate monthly-normals climatology yet
    (unlike GW's ``gw_monthly_normals.csv``) to define an independent
    below/near/above split, so this scores against the archived band's OWN
    thirds (``[p10, t1), [t1, t2], (t2, p90]`` with
    ``t1/t2 = p10 + {1,2}/3*(p90-p10)``): did the observed monthly-mean and
    the archived p50 land in the same third? A crude but self-consistent
    "did the shape of the distribution point the right way" check.
  - ``brier``         : ``(p_sub_q95 - observed_indicator)^2`` — the classic
    Brier score for the P(>=1 day < Q95) probabilistic forecast, where
    ``observed_indicator`` is 1 iff the shard actually had a day with
    ``Flow_m3s < q95_m3s`` that month.

Prints "No closed months yet" (exit 0) when nothing in the archive has
closed — the expected state for the first several months after the shadow
archive starts.

Usage:
    python -m scripts.score_flow_seasonal_shadow
    python -m scripts.score_flow_seasonal_shadow --archive path/to/other.parquet
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.download.build import load_config
from src.download.flow import FLOW_SHARD_DIR

ROOT = Path(__file__).resolve().parents[1]

SCORE_COLS = [
    "gauge_id", "run", "month_ahead", "month_start",
    "observed_mean_m3s", "q_p10_m3s", "q_p50_m3s", "q_p90_m3s",
    "coverage_hit", "tercile_hit",
    "p_sub_q95", "observed_sub_q95", "brier",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--archive", default=None,
                    help="path to the shadow archive parquet (default: "
                         "config forecast.flow_seasonal.archive_cache)")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Pure scoring functions — unit tested directly with synthetic archives/shards.
# ---------------------------------------------------------------------------

def is_closed_month(month_start, today: pd.Timestamp) -> bool:
    """A calendar month is CLOSED once its own last day has passed."""
    month_end = pd.Timestamp(month_start) + pd.offsets.MonthEnd(0)
    return month_end < pd.Timestamp(today).normalize()


def observed_month_stats(shard: pd.DataFrame, month_start, q95_m3s: float
                         ) -> tuple[float, bool] | None:
    """(observed monthly-mean m3/s, had >=1 day < q95) for one calendar month
    from a gauge's flow shard. None if the shard has no rows that month."""
    ms = pd.Timestamp(month_start)
    me = ms + pd.offsets.MonthEnd(0)
    dates = pd.to_datetime(shard["date"])
    in_month = (dates >= ms) & (dates <= me)
    if not in_month.any():
        return None
    vals = pd.to_numeric(shard.loc[in_month, "Flow_m3s"], errors="coerce").dropna().to_numpy()
    if len(vals) == 0:
        return None
    return float(np.mean(vals)), bool(np.any(vals < q95_m3s))


def score_row(row: pd.Series, observed_mean: float, observed_sub_q95: bool) -> dict:
    """Pure per-row scoring: coverage / tercile-of-own-band / Brier."""
    p10, p50, p90 = float(row["q_p10_m3s"]), float(row["q_p50_m3s"]), float(row["q_p90_m3s"])
    coverage_hit = bool(p10 <= observed_mean <= p90)

    span = p90 - p10
    t1 = p10 + span / 3.0
    t2 = p10 + 2.0 * span / 3.0

    def _third(v: float) -> str:
        if v < t1:
            return "below"
        if v > t2:
            return "above"
        return "near"

    tercile_hit = _third(observed_mean) == _third(p50)
    p_sub = float(row["p_sub_q95"])
    brier = (p_sub - float(observed_sub_q95)) ** 2

    return {
        "gauge_id": row["gauge_id"], "run": row["run"],
        "month_ahead": int(row["month_ahead"]), "month_start": row["month_start"],
        "observed_mean_m3s": observed_mean,
        "q_p10_m3s": p10, "q_p50_m3s": p50, "q_p90_m3s": p90,
        "coverage_hit": coverage_hit, "tercile_hit": tercile_hit,
        "p_sub_q95": p_sub, "observed_sub_q95": observed_sub_q95, "brier": brier,
    }


def score_archive(archive: pd.DataFrame, shards: dict[str, pd.DataFrame], *,
                  today: pd.Timestamp | None = None) -> pd.DataFrame:
    """Pure scoring pass over an already-loaded archive + {gauge_id: shard}
    dict. Rows whose month isn't closed yet, or whose shard has no data for
    that month, are silently excluded (not an error)."""
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now().normalize()
    closed = archive[archive["month_start"].apply(lambda m: is_closed_month(m, today))]

    rows = []
    for _, row in closed.iterrows():
        shard = shards.get(row["gauge_id"])
        if shard is None or shard.empty:
            continue
        stats = observed_month_stats(shard, row["month_start"], float(row["q95_m3s"]))
        if stats is None:
            continue
        observed_mean, observed_sub_q95 = stats
        rows.append(score_row(row, observed_mean, observed_sub_q95))
    return pd.DataFrame(rows, columns=SCORE_COLS)


# ---------------------------------------------------------------------------
# Orchestration (I/O)
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace, cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    fscfg = cfg.get("forecast", {}).get("flow_seasonal", {})
    archive_path = (Path(args.archive) if args.archive else
                    ROOT / fscfg.get("archive_cache",
                                     "data/model/flow_seasonal_shadow_archive.parquet"))

    if not archive_path.exists():
        print(f"{archive_path} not found — no shadow archive yet (run "
             f"'.venv-pastas\\Scripts\\python -m scripts.build_flow_seasonal_shadow' "
             f"first).")
        return 0
    archive = pd.read_parquet(archive_path)
    if archive.empty:
        print(f"{archive_path} is empty — no shadow rows archived yet.")
        return 0

    today = pd.Timestamp.now().normalize()
    closed_mask = archive["month_start"].apply(lambda m: is_closed_month(m, today))
    if not bool(closed_mask.any()):
        print(f"No closed months yet — {len(archive)} shadow row(s) archived "
             f"across {archive['gauge_id'].nunique()} gauge(s), earliest "
             f"month_start {archive['month_start'].min()}, all still open. "
             f"Re-run this scorer after that month ends.")
        return 0

    shards: dict[str, pd.DataFrame] = {}
    for gid in archive.loc[closed_mask, "gauge_id"].unique():
        fp = FLOW_SHARD_DIR / f"{gid}.parquet"
        if fp.exists():
            shards[gid] = pd.read_parquet(fp)

    scored = score_archive(archive, shards, today=today)
    if scored.empty:
        print(f"{int(closed_mask.sum())} closed row(s) in the archive, but none "
             f"have matching shard observations yet (gauge shard not ingested / "
             f"no data that month).")
        return 0

    print(f"Scored {len(scored)} closed (gauge, run, month_ahead) row(s) across "
         f"{scored['gauge_id'].nunique()} gauge(s):")
    print(f"  coverage (obs in [p10,p90]): {scored['coverage_hit'].mean():.0%}")
    print(f"  tercile hit rate:            {scored['tercile_hit'].mean():.0%}")
    print(f"  mean Brier (P sub-Q95):      {scored['brier'].mean():.3f}")
    by_lead = scored.groupby("month_ahead")[["coverage_hit", "tercile_hit", "brier"]].mean()
    print("\nBy month_ahead:")
    print(by_lead.to_string(float_format=lambda v: f"{v:.3f}"))
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

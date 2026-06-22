"""Current groundwater status vs normal — the risk index's replacement.

"Where is this borehole right now?" answered in the product's one
vocabulary: **below / near / above normal** for the current calendar
month, plus an approximate percentile ("84th percentile for June"), a
7-day trend, and the observation's age. Derived from two things that
already exist: the freshest observed level (per-station shard, live tail
included — `seeding.freshest_gw`) and the monthly quantile ladder
(`gw_monthly_normals.csv`, built by scripts/build_gw_normals.py).

No composite weights, no thresholds to defend — just the station against
its own history.
"""
from __future__ import annotations

from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

_ROOT = Path(__file__).parents[2]
NORMALS_PATH = _ROOT / "data" / "model" / "gw_monthly_normals.csv"

# Quantile ladder stored in the normals (keep in sync with
# src/forecast/seasonal/normals.py QUANTILE_*).
_Q_LEVELS = np.array([0.10, 1 / 3, 0.50, 2 / 3, 0.90])
_Q_COLS = ("p10", "t1", "median", "t2", "p90")

STATUS_LABEL = {"below": "below normal", "near": "near normal",
                "above": "above normal"}
# Same palette as the seasonal tercile bars — one vocabulary, one colouring.
STATUS_COLOR = {"below": "#d4a017", "near": "#8a8a8a", "above": "#1f77b4",
                None: "#9e9e9e"}
STATUS_CHIP = {"below": "🟡 below normal", "near": "⚪ near normal",
               "above": "🔵 above normal"}
UNKNOWN_CHIP = "◻ no status"
TREND_ARROW = {"rising": "↑", "falling": "↓", "stable": "→"}

# |7-day level change| below this is "stable" (sensor-noise scale).
TREND_EPS_M = 0.02
# An observation older than this can't honestly carry a current status.
MAX_STATUS_AGE_DAYS = 45


def load_normals(path: Path | None = None) -> pd.DataFrame:
    p = path or NORMALS_PATH
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p)


def percentile_of(level: float, qrow: pd.Series) -> float:
    """Approximate percentile of `level` against the month's quantile
    ladder (linear interpolation; clamped to [2, 98] — the ladder can't
    resolve the extreme tails)."""
    qs = np.array([float(qrow[c]) for c in _Q_COLS])
    if not np.isfinite(qs).all() or np.any(np.diff(qs) < 0):
        return float("nan")
    p = float(np.interp(level, qs, _Q_LEVELS * 100.0,
                        left=2.0, right=98.0))
    return min(max(p, 2.0), 98.0)


def sgi_from_percentile(percentile: float | None) -> float | None:
    """Ladder-based SGI approximation (Bloomfield & Marchant 2013 normal-
    scores): Phi^-1(percentile/100). Because ``percentile`` is clamped to
    [2, 98] by ``percentile_of``, SGI saturates at ~+/-2.05 and CANNOT
    resolve the tails — this is an approximation, not the full normal-scores
    transform. None when percentile is None/non-finite."""
    if percentile is None or not np.isfinite(percentile):
        return None
    return NormalDist().inv_cdf(float(percentile) / 100.0)


def status_of(level: float, qrow: pd.Series) -> str:
    if level < float(qrow["t1"]):
        return "below"
    if level > float(qrow["t2"]):
        return "above"
    return "near"


def current_status(sid: str, normals: pd.DataFrame, *,
                   now: pd.Timestamp | None = None) -> dict:
    """Status dict for one borehole (all-None when not derivable).

    Keys: status (below|near|above|None), percentile, trend
    (rising|falling|stable|None), level, obs_date, age_days, month, sgi.
    """
    try:
        from src.forecast.ensemble.seeding import freshest_gw
        s = freshest_gw(sid)
    except Exception:
        s = None
    return status_from_series(s, sid, normals, now=now)


def status_from_series(s: pd.Series | None, sid: str, normals: pd.DataFrame,
                       *, now: pd.Timestamp | None = None) -> dict:
    """``current_status`` for an already-loaded observed series (the shard
    read factored out so callers holding the series — e.g. the artifact-pack
    builder, which also needs the observed tail — pay for one read, not two)."""
    out = {"status": None, "percentile": float("nan"), "trend": None,
           "level": float("nan"), "obs_date": None,
           "age_days": float("nan"), "month": None, "sgi": None}
    if s is None or s.dropna().empty:
        return out
    s = s.dropna().sort_index()
    obs_date = pd.Timestamp(s.index.max())
    level = float(s.iloc[-1])
    now = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
    now = now.tz_localize(None) if now.tzinfo else now
    od = obs_date.tz_localize(None) if obs_date.tzinfo else obs_date
    age = (now.normalize() - od.normalize()).days

    out.update({"level": level, "obs_date": od, "age_days": float(age),
                "month": int(od.month)})

    # trend: level now vs ~7 days before the latest observation
    week_ago = od - pd.Timedelta(days=7)
    prior = s[s.index <= week_ago]
    if not prior.empty:
        d = level - float(prior.iloc[-1])
        out["trend"] = ("stable" if abs(d) < TREND_EPS_M
                        else "rising" if d > 0 else "falling")

    if age > MAX_STATUS_AGE_DAYS or normals is None or normals.empty:
        return out                       # too stale / no yardstick → no status
    row = normals[(normals["station_id"] == sid)
                  & (normals["month"] == od.month)]
    if row.empty:
        return out
    qrow = row.iloc[0]
    out["status"] = status_of(level, qrow)
    out["percentile"] = percentile_of(level, qrow)
    out["sgi"] = sgi_from_percentile(out["percentile"])
    return out


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def status_chip(status: str | None, trend: str | None = None,
                percentile: float | None = None) -> str:
    """Compact display chip: '🔵 above normal ↑ (84th pct)'."""
    if status is None or (isinstance(status, float) and pd.isna(status)):
        return UNKNOWN_CHIP
    parts = [STATUS_CHIP.get(status, UNKNOWN_CHIP)]
    if trend in TREND_ARROW:
        parts.append(TREND_ARROW[trend])
    if percentile is not None and np.isfinite(percentile):
        parts.append(f"({_ordinal(round(percentile))} pct)")
    return " ".join(parts)


# Worst-first tie-break ordinal for the triage sort: high water first.
_STATUS_RANK = {"above": 0, "near": 1, "below": 2}


def attach_current_status(triage: pd.DataFrame,
                          normals: pd.DataFrame | None = None, *,
                          now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Left-join the current vs-normal status onto the triage frame
    (replaces the retired risk-band join — same tolerant contract).

    Adds: status_now, status_percentile, status_trend, status_age_days,
    status_rank. Missing normals/shards → None/NaN (grey chips), never an
    exception. Inputs are not mutated.
    """
    out = triage.copy()
    cols = ["status_now", "status_percentile", "status_trend",
            "status_age_days"]
    if out.empty or "station_id" not in out.columns:
        for c in cols + ["status_rank"]:
            out[c] = pd.NA
        return out
    normals = normals if normals is not None else load_normals()

    rows = []
    for sid in out["station_id"]:
        st = current_status(str(sid), normals, now=now)
        rows.append({"station_id": sid,
                     "status_now": st["status"],
                     "status_percentile": st["percentile"],
                     "status_trend": st["trend"],
                     "status_age_days": st["age_days"]})
    join = pd.DataFrame(rows).drop_duplicates("station_id")
    out = out.merge(join, on="station_id", how="left")
    out["status_rank"] = (out["status_now"].map(_STATUS_RANK)
                          .fillna(len(_STATUS_RANK)).astype(int))

    # Status is a TIE-BREAK only — tier definitions and the fresh/stale
    # strata stay untouched (mirrors the old band join's contract).
    sort_cols = ["tier_rank", "is_fresh", "adjusted_score"]
    if set(sort_cols).issubset(out.columns):
        out = (out.sort_values(sort_cols + ["status_rank"],
                               ascending=[True, False, False, True])
               .reset_index(drop=True))
    return out

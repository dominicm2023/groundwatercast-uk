"""Aggregate member GW trajectories into the probabilistic outputs (design §7).

From the per-member forecast (forecast_ensemble_members.parquet) compute, per
borehole:
  - breach probability  P(GW crosses T within the horizon),
  - first-crossing-date distribution (median, P25, P75) among crossing members,
    with the non-crossing fraction reported as `censored_frac` (never dropped),
  - the GW fan (per-day P10/P50/P90).

Breach direction: a breach is GW rising **above** the threshold T (high
groundwater → infiltration/spill risk), i.e. gw_pred ≥ T.

All outputs are labelled *indicative / uncalibrated* (design §9): the MVP
propagates rainfall-member spread only, so the ensemble is under-dispersed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .thresholds import resolve_threshold

# The triage tiers are calibrated on this window; the headline must agree with it.
OPERATIONAL_WINDOW_DAYS = 14


def _wide(traj_bh: pd.DataFrame) -> pd.DataFrame:
    """date (sorted) × member matrix of gw_pred."""
    return (traj_bh.pivot_table(index="date", columns="member", values="gw_pred")
            .sort_index())


def fan_quantiles(traj_bh: pd.DataFrame) -> pd.DataFrame:
    """Per-day P10/P50/P90 of member GW. Columns: lead, date, gw_p10/p50/p90."""
    w = _wide(traj_bh)
    return pd.DataFrame({
        "lead": range(1, len(w) + 1),
        "date": w.index,
        "gw_p10": w.quantile(0.10, axis=1).to_numpy(),
        "gw_p50": w.quantile(0.50, axis=1).to_numpy(),
        "gw_p90": w.quantile(0.90, axis=1).to_numpy(),
    })


def breach_stats(traj_bh: pd.DataFrame, threshold: float | None, *,
                 operational_window_days: int = OPERATIONAL_WINDOW_DAYS) -> dict:
    """Breach probability + censored first-crossing distribution for one BH.

    Two breach windows when the horizon exceeds the operational window
    (the 46-day extended forecast): ``p_breach`` over the FULL horizon and
    ``p_breach_14d`` over the first ``operational_window_days`` — breach
    probability grows with window length, and the triage tiers were
    calibrated against 14-day windows, so tiering keys on the latter.
    At horizon ≤ the window the two are identical.
    """
    w = _wide(traj_bh)
    dates = list(w.index)
    horizon = len(dates)
    out = {
        "horizon_days": horizon,
        "n_members": w.shape[1],
        "p_breach": np.nan,
        "p_breach_14d": np.nan,
        "censored_frac": np.nan,
        "first_cross_median": pd.NaT,
        "first_cross_p25": pd.NaT,
        "first_cross_p75": pd.NaT,
        "first_cross_median_lead": np.nan,
        # guarded: dict literals evaluate BEFORE the empty check below, so an
        # unguarded iloc[-1] made the w.empty half of that guard dead code
        # (IndexError for any direct caller with an empty frame).
        "gw_p50_end": float(w.iloc[-1].median()) if not w.empty else np.nan,
    }
    if threshold is None or w.empty:
        return out

    ge = w >= threshold                       # H × N booleans
    crossed = ge.any(axis=0)                   # per member
    p_breach = float(crossed.mean())
    out["p_breach"] = p_breach
    out["p_breach_14d"] = float(
        ge.iloc[:int(operational_window_days)].any(axis=0).mean())
    out["censored_frac"] = 1.0 - p_breach
    if crossed.any():
        # first True date per crossing member → lead day (1-based)
        lead_of = {d: i + 1 for i, d in enumerate(dates)}
        first_dates = ge.idxmax(axis=0)[crossed]
        leads = first_dates.map(lead_of).astype(float)
        med, p25, p75 = (float(leads.quantile(q)) for q in (0.5, 0.25, 0.75))
        # round half UP (not Python's banker's round): a fractional lead like
        # 2.5 must map to day 3, agreeing with the reported fractional lead.
        _half_up = lambda x: int(np.floor(x + 0.5))  # noqa: E731
        out["first_cross_median_lead"] = med
        out["first_cross_median"] = dates[_half_up(med) - 1]
        out["first_cross_p25"] = dates[_half_up(p25) - 1]
        out["first_cross_p75"] = dates[_half_up(p75) - 1]
    return out


def headline_sentence(row: dict | pd.Series) -> str:
    """Canonical probabilistic statement (design §1)."""
    src = row.get("threshold_source", "none")
    if src == "none" or pd.isna(row.get("threshold")):
        return ("No breach threshold available for this borehole — "
                "fan shown without a breach probability.")
    thr = float(row["threshold"])
    h = int(row["horizon_days"])
    p = float(row["p_breach"])
    note = " (proxy threshold)" if src == "gw_p90_proxy" else ""
    tag = "  Indicative — uncalibrated."
    if p <= 0:
        # p=0 means "no sampled trajectory crossed", not "impossible" — the
        # resolvable floor is 1/n where n is the sampling basis: `n_samples`
        # (Pastas Monte-Carlo, src/forecast/pastas/summary.py reuses this
        # function) or `n_members` (the 51-member roll). Display-floored at 0.1%.
        ns = row.get("n_samples")
        has_samples = ns is not None and pd.notna(ns)
        nm = row.get("n_members")
        n = float(ns) if has_samples else (
            float(nm) if nm is not None and pd.notna(nm) else 51.0)
        floor = max(1.0 / n, 0.001)
        basis = "sampled trajectories" if has_samples else "members"
        return (f"<{floor:.1%} chance of breaching {thr:.1f} mAOD within {h} days{note}; "
                f"no {basis} cross." + tag)
    # When the horizon exceeds the operational window, the dashboard tier keys
    # on the 14-day breach probability. Surface BOTH (with their windows) so the
    # headline cannot contradict the badge (design §7 dual-window breach stats).
    p14 = row.get("p_breach_14d")
    op_clause = ""
    if h > OPERATIONAL_WINDOW_DAYS and p14 is not None and pd.notna(p14):
        op_clause = (f" ({float(p14):.0%} within the "
                     f"{OPERATIONAL_WINDOW_DAYS}-day operational window)")
    md = pd.Timestamp(row["first_cross_median"]).date()
    a = pd.Timestamp(row["first_cross_p25"]).date()
    b = pd.Timestamp(row["first_cross_p75"]).date()
    cens = float(row["censored_frac"])
    return (f"{p:.0%} chance of breaching {thr:.1f} mAOD within {h} days{op_clause}{note}; "
            f"median first crossing {md} (P25–P75: {a}–{b}). "
            f"{cens:.0%} of members do not cross." + tag)


def aggregate(traj: pd.DataFrame, *, run: pd.Timestamp,
              gw_p90_by_station: dict[str, float] | None = None
              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate all boreholes in `traj` into (summary_df, fan_df)."""
    gw_p90_by_station = gw_p90_by_station or {}
    summaries, fans = [], []
    for sid, g in traj.groupby("station_id"):
        thr, src = resolve_threshold(sid, gw_p90=gw_p90_by_station.get(sid))
        stats = breach_stats(g, thr)
        # Provenance: members parquets pre-dating the scope column → "unknown".
        scope = str(g["scope"].iloc[0]) if "scope" in g.columns else "unknown"
        row = {"station_id": sid, "run": run, "scope": scope, "threshold": thr,
               "threshold_source": src, **stats}
        row["headline"] = headline_sentence(row)
        summaries.append(row)
        fan = fan_quantiles(g)
        fan.insert(0, "station_id", sid)
        fan["run"] = run
        fans.append(fan)
    cols = ["station_id", "run", "scope", "horizon_days", "threshold", "threshold_source",
            "p_breach", "p_breach_14d",
            "first_cross_median", "first_cross_p25", "first_cross_p75",
            "first_cross_median_lead", "censored_frac", "gw_p50_end",
            "n_members", "headline"]
    summary_df = pd.DataFrame(summaries)[cols]
    fan_df = pd.concat(fans, ignore_index=True) if fans else pd.DataFrame()
    return summary_df, fan_df

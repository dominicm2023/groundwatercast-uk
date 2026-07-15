"""Aggregate flow member trajectories into the probabilistic low-flow summary
— Stage 6 of ``docs/product/lowflow/build_plan.md``.

Mirrors ``src/forecast/pastas/summary.py``'s Monte-Carlo aggregation (member
spread + AR1 noise, sampled in the model's native fitted space) but for the
two-pathway flow model:

  - ``simulate_path``/``ensemble.drive_borehole`` (both reused UNCHANGED —
    architecture decision 2, "exponentiate only at publish") return LOGQ for
    a ``model_kind="flow_2s"`` rec, not raw m3/s. Every stage in this module
    — the Monte-Carlo sampling, the fan quantiles, the breach test — runs in
    logQ, exactly like the GW summary runs in head-space. The exponentiation
    ``Q = exp(logQ) - eps`` happens ONLY at the two output boundaries: the
    fan's P10/P50/P90 columns and ``q_p50_end_m3s`` — the "aggregation
    boundary" the build plan specifies.
  - Breach direction is LOW-flow: P(Q crosses BELOW the gauge's own Q95)
    within the window, the opposite sense from the GW summary's "rises
    above". This reuses ``summary._breach_from_samples``'s ``direction``
    parameter (added alongside this module) rather than forking the
    crossing/first-crossing logic — ``direction="below"`` is the ONLY
    difference from the GW call, so the two breach senses cannot silently
    diverge.

Pure numpy/pandas — no pastas import (runs in either environment, like
``summary.py``).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .summary import OPERATIONAL_WINDOW_DAYS, _breach_from_samples, mc_trajectories

SUMMARY_COLS = [
    "gauge_id", "run", "origin_date", "stale_days", "horizon_days",
    "q95_m3s", "threshold_source",
    "p_below_q95", "p_below_q95_14d",
    "first_cross_median", "first_cross_p25", "first_cross_p75",
    "first_cross_median_lead", "censored_frac",
    "q_p50_end_m3s", "n_members", "n_samples", "headline",
]

FAN_COLS = ["gauge_id", "run", "lead", "date",
           "q_p10_m3s", "q_p50_m3s", "q_p90_m3s", "segment"]


def exp_q(logq: np.ndarray, eps: float) -> np.ndarray:
    """LogQ -> m3/s at the aggregation boundary (``logq = log(Q + eps)`` at
    fit time, so ``Q = exp(logq) - eps``). Clipped at 0: a sampled AR1-noise
    excursion can push ``logq`` below ``log(eps)``, which would exponentiate
    to a small negative flow — flow physically floors at 0."""
    return np.clip(np.exp(np.asarray(logq, dtype=float)) - eps, 0.0, None)


def flow_headline_sentence(row: dict | pd.Series) -> str:
    """Canonical low-flow probabilistic statement: P(Q < Q95), never a
    "breach" sentence — build_plan.md's honesty invariants require Q95 be
    presented as a climatological proxy (labelled like ``gw_p90_proxy``, not
    a licence Hands-off-Flow value) and "indicative/experimental", never
    "warning"; the gauged-flow/abstraction caveat is carried in every line."""
    thr = row.get("q95_m3s")
    tag = ("  Indicative — uncalibrated; gauged flow, including abstraction "
           "effects.")
    if thr is None or pd.isna(thr):
        return "No Q95 threshold available for this gauge — fan shown without a below-Q95 probability." + tag
    h = int(row["horizon_days"])
    p = float(row["p_below_q95"])
    thr = float(thr)
    if p <= 0:
        ns = row.get("n_samples")
        n = float(ns) if ns is not None and pd.notna(ns) else 51.0
        floor = max(1.0 / n, 0.001)
        return (f"<{floor:.1%} chance of falling below the Q95 low-flow proxy "
                f"({thr:.3f} m3/s) within {h} days; no sampled trajectories "
                f"cross." + tag)
    p14 = row.get("p_below_q95_14d")
    op_clause = ""
    if h > OPERATIONAL_WINDOW_DAYS and p14 is not None and pd.notna(p14):
        op_clause = (f" ({float(p14):.0%} within the "
                     f"{OPERATIONAL_WINDOW_DAYS}-day operational window)")
    md = pd.Timestamp(row["first_cross_median"]).date()
    a = pd.Timestamp(row["first_cross_p25"]).date()
    b = pd.Timestamp(row["first_cross_p75"]).date()
    cens = float(row["censored_frac"])
    return (f"{p:.0%} chance of falling below the Q95 low-flow proxy "
            f"({thr:.3f} m3/s) within {h} days{op_clause}; median first drop "
            f"{md} (P25-P75: {a}-{b}). {cens:.0%} of members stay above." + tag)


def aggregate_flow(members_df: pd.DataFrame, models: dict, *,
                   run: pd.Timestamp,
                   q95_by_gauge: dict[str, float] | None = None,
                   n_samples: int = 4000, seed: int = 12345,
                   ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate flow member trajectories (as emitted by
    ``src.forecast.pastas.ensemble.drive_borehole`` reused unchanged, so the
    frame carries GW's column names — ``station_id``, ``gw_pred``,
    ``gw_sigma`` — holding gauge_id / logQ mean / logQ sigma respectively)
    into ``(summary_df, fan_df)`` with flow-native column names and units.

    ``q95_by_gauge`` overrides the per-gauge Q95 stored on the ModelRec
    (``rec["q95_m3s"]``, set by ``build_flow_models.py``); the rec value is
    the fallback so a caller only needs to pass overrides.
    """
    q95_by_gauge = q95_by_gauge or {}
    rng = np.random.default_rng(seed)
    summaries, fans = [], []

    id_col = "station_id" if "station_id" in members_df.columns else "gauge_id"
    has_segment = "segment" in members_df.columns
    for gid, g_all in members_df.groupby(id_col):
        rec = models.get(gid)
        if rec is None:
            continue
        eps = float(rec.get("eps", 0.001))
        g = g_all[g_all["segment"] == "forecast"] if has_segment else g_all
        if g.empty:
            continue

        W = (g.pivot_table(index="date", columns="member", values="gw_pred")
             .sort_index())
        dates = pd.DatetimeIndex(W.index)
        sigma_vec = (g.groupby("date")["gw_sigma"].first().reindex(dates)
                    .to_numpy(float))
        samples = mc_trajectories(W.to_numpy(float), sigma_vec,
                                  float(rec["alpha"]), n_samples, rng)  # logQ
        q10, q50, q90 = (exp_q(x, eps)
                         for x in np.quantile(samples, [0.10, 0.50, 0.90], axis=0))

        origin = (pd.Timestamp(g["origin_date"].iloc[0])
                 if "origin_date" in g.columns else pd.NaT)
        stale_days = (int((pd.Timestamp(run).tz_localize(None) - origin).days)
                     if pd.notna(origin) else np.nan)

        thr = q95_by_gauge.get(gid, rec.get("q95_m3s"))
        thr_ok = thr is not None and pd.notna(thr)
        thr_logq = float(np.log(max(float(thr), 0.0) + eps)) if thr_ok else None
        thr_source = "q95_proxy" if thr_ok else "none"

        stats = _breach_from_samples(samples, dates, thr_logq, direction="below")
        q_p50_end = float(exp_q(np.array([stats.pop("gw_p50_end")]), eps)[0])

        row = {
            "gauge_id": gid, "run": run,
            "origin_date": origin.date().isoformat() if pd.notna(origin) else None,
            "stale_days": stale_days,
            "q95_m3s": (float(thr) if thr_ok else None),
            "threshold_source": thr_source,
            "p_below_q95": stats.pop("p_breach"),
            "p_below_q95_14d": stats.pop("p_breach_14d"),
            "q_p50_end_m3s": q_p50_end,
            "n_members": int(W.shape[1]),
            **stats,   # horizon_days, n_samples, censored_frac, first_cross_*
        }
        row["headline"] = flow_headline_sentence(row)
        summaries.append(row)

        fans.append(pd.DataFrame({
            "gauge_id": gid, "run": run,
            "lead": range(1, len(dates) + 1), "date": dates,
            "q_p10_m3s": q10, "q_p50_m3s": q50, "q_p90_m3s": q90,
            "segment": "forecast",
        }))

        # Nowcast fan over the observed-rainfall gap (last obs -> today), same
        # parametric-band construction as the GW summary's nowcast segment —
        # members are identical here, so the band is the logQ predictive sd
        # alone, exponentiated at this same boundary.
        if has_segment:
            nc = g_all[g_all["segment"] == "nowcast"].sort_values("date")
            if not nc.empty:
                z = 1.2815515594
                mu = nc["gw_pred"].to_numpy(float)
                sd = nc["gw_sigma"].to_numpy(float)
                fans.append(pd.DataFrame({
                    "gauge_id": gid, "run": run,
                    "lead": range(-len(nc), 0), "date": pd.DatetimeIndex(nc["date"]),
                    "q_p10_m3s": exp_q(mu - z * sd, eps),
                    "q_p50_m3s": exp_q(mu, eps),
                    "q_p90_m3s": exp_q(mu + z * sd, eps),
                    "segment": "nowcast",
                }))

    summary_df = (pd.DataFrame(summaries)[SUMMARY_COLS] if summaries
                 else pd.DataFrame(columns=SUMMARY_COLS))
    fan_df = (pd.concat(fans, ignore_index=True)[FAN_COLS] if fans
             else pd.DataFrame(columns=FAN_COLS))
    return summary_df, fan_df

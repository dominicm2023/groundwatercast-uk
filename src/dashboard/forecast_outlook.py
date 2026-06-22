"""Data prep for the Forecast-outlook page — turn the per-borehole Pastas
summary into a triage-ranked table.

Pure pandas/numpy (no streamlit) so it's unit-testable. The ranking is a
*confidence-adjusted, proxy-aware, staleness-demoting* worst-first order:
rows sort by (tier, is_fresh, adjusted_score) — tier labels describe the raw
forecast, but within a tier every fresh-seed (≤ FRESH_SEED_MAX_DAYS) borehole
ranks above every stale-seed one, and the confidence/proxy-weighted score
orders the rest. So a stale-seed BREACH_LIKELY still outranks fresh lower
tiers, but never a fresh borehole in its own tier (the judge-panel's key
concern).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# A seed observation older than this is "stale" (no live feed) — single source
# of truth for the dashboard's freshness threshold (ensemble_view, gw_outlook).
FRESH_SEED_MAX_DAYS = 14

# Tier ranks (lower = more urgent) — drives the row badge and the primary sort.
TIERS = {"BREACH_LIKELY": 0, "BREACH_POSSIBLE": 1, "WATCH": 2, "STABLE": 3}
TIER_LABEL = {"BREACH_LIKELY": "🔴 Breach likely", "BREACH_POSSIBLE": "🟠 Breach possible",
              "WATCH": "🟡 Watch", "STABLE": "🟢 Stable"}
_PROXY = "gw_p90_proxy"

# The current-state join ("where is this borehole right now?") lives in
# src/dashboard/status.py — status vs the borehole's own monthly normals,
# the risk index's replacement.


def _tier(p_breach: float, p_above_p90: float) -> str:
    """Tier from the operational-window breach probability plus the
    seasonal-rarity secondary signal: P(any day in the window above the
    month's P90 normal) — same ~10% baseline rarity the retired
    risk-HIGH signal had, so the thresholds keep their calibration."""
    pb = 0.0 if pd.isna(p_breach) else float(p_breach)
    pr = 0.0 if pd.isna(p_above_p90) else float(p_above_p90)
    if pb >= 0.50:
        return "BREACH_LIKELY"
    if pb >= 0.10 or pr >= 0.50:
        return "BREACH_POSSIBLE"
    if pb > 0 or pr >= 0.10:
        return "WATCH"
    return "STABLE"


def _conf_factor(stale_days: float) -> float:
    # NaN seed-age is treated as stale (demoted), consistent with is_fresh.
    s = 1e9 if pd.isna(stale_days) else float(stale_days)
    if s <= FRESH_SEED_MAX_DAYS:
        return 1.0
    return 0.5 if s <= 60 else 0.25


def build_pastas_triage(summary: pd.DataFrame, catalogue: pd.DataFrame,
                        pinned_ids: set[str]) -> pd.DataFrame:
    """Join the Pastas summary to catalogue metadata and add triage tier/score,
    sorted worst-first. One row per borehole. Inputs are not mutated.

    ``pinned_ids`` — stations the user has declared operationally meaningful
    (those with a user-supplied breach threshold); badged in the UI."""
    if summary.empty:
        return summary.copy()
    cat = (catalogue[["station_id", "station_name", "lat", "lon", "aquifer_name"]]
           .drop_duplicates("station_id"))
    df = summary.merge(cat, on="station_id", how="left")
    df["station_name"] = df["station_name"].fillna(df["station_id"].str[:8])
    df["is_pinned"] = df["station_id"].isin(pinned_ids)
    df["is_proxy"] = df["threshold_source"].eq(_PROXY)
    df["is_fresh"] = df["stale_days"].fillna(1e9) <= FRESH_SEED_MAX_DAYS

    # Tiering keys on the OPERATIONAL 14-day breach window — the tier
    # definitions were calibrated on 14-day windows, and a 46-day p_breach
    # is structurally higher. Tolerant fallback to p_breach for artifacts
    # pre-dating the extended horizon (where the two were identical anyway).
    if "p_breach_14d" in df.columns:
        df["p_breach_op"] = df["p_breach_14d"].fillna(df["p_breach"])
    else:
        df["p_breach_op"] = df["p_breach"]

    # Secondary tier signal: P(above the month's P90 normal within the
    # operational window). Tolerant fallback to the retired p_risk_high
    # column for artifacts pre-dating the vocabulary unification.
    if "p_above_p90_14d" in df.columns:
        df["p_secondary"] = df["p_above_p90_14d"]
    elif "p_risk_high" in df.columns:
        df["p_secondary"] = df["p_risk_high"]
    else:
        df["p_secondary"] = np.nan

    df["tier"] = [_tier(b, r) for b, r in zip(df["p_breach_op"], df["p_secondary"])]
    pb = df["p_breach_op"].fillna(0.0).to_numpy(float)
    pr = df["p_secondary"].fillna(0.0).to_numpy(float)
    horizon = df["horizon_days"].fillna(14).to_numpy(float)
    lead = df["first_cross_median_lead"].to_numpy(float)
    urgency = np.where(np.isfinite(lead), 0.15 * (1.0 - np.clip(lead, 0, horizon) / horizon), 0.0)
    proxy_w = np.where(df["is_proxy"].to_numpy(bool), 0.85, 1.0)
    df["triage_score"] = (0.7 * pb + 0.3 * pr + urgency) * proxy_w
    df["conf_factor"] = df["stale_days"].map(_conf_factor)
    df["adjusted_score"] = df["triage_score"] * df["conf_factor"]
    df["tier_rank"] = df["tier"].map(TIERS)

    # Within a tier, fresh-seed rows always rank above stale ones (the score
    # only orders within the same tier+freshness stratum).
    return (df.sort_values(["tier_rank", "is_fresh", "adjusted_score"],
                           ascending=[True, False, False])
            .reset_index(drop=True))

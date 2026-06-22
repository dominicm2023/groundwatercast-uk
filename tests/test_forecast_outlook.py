"""Tests for the Forecast-outlook triage builder (pure data prep)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.dashboard.forecast_outlook import build_pastas_triage, _tier, _conf_factor


def _summary(rows):
    cols = {"station_id": [], "p_breach": [], "p_risk_high": [], "stale_days": [],
            "threshold_source": [], "horizon_days": [], "first_cross_median_lead": []}
    for r in rows:
        for k in cols:
            cols[k].append(r.get(k))
    return pd.DataFrame(cols)


_CAT = pd.DataFrame({"station_id": ["a", "b", "c", "d"],
                     "station_name": ["A", "B", "C", "D"],
                     "lat": [51]*4, "lon": [-1]*4, "aquifer_name": ["Chalk"]*4})


def test_tier_boundaries():
    assert _tier(0.6, 0.0) == "BREACH_LIKELY"
    assert _tier(0.2, 0.0) == "BREACH_POSSIBLE"
    assert _tier(0.0, 0.6) == "BREACH_POSSIBLE"      # risk-high alone can lift
    assert _tier(0.05, 0.2) == "WATCH"
    assert _tier(0.0, 0.0) == "STABLE"


def test_conf_factor_demotes_stale():
    assert _conf_factor(14) == 1.0
    assert _conf_factor(50) == 0.5
    assert _conf_factor(200) == 0.25


def test_worst_first_order_and_columns():
    rows = [
        {"station_id": "a", "p_breach": 0.8, "p_risk_high": 0.5, "stale_days": 14,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 5},
        {"station_id": "b", "p_breach": 0.0, "p_risk_high": 0.0, "stale_days": 14,
         "threshold_source": "gw_p90_proxy", "horizon_days": 14, "first_cross_median_lead": np.nan},
        {"station_id": "c", "p_breach": 0.2, "p_risk_high": 0.1, "stale_days": 14,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 10},
    ]
    out = build_pastas_triage(_summary(rows), _CAT, pinned_ids={"c"})
    assert list(out["station_id"]) == ["a", "c", "b"]          # likely > possible > stable
    assert list(out["tier"]) == ["BREACH_LIKELY", "BREACH_POSSIBLE", "STABLE"]
    assert out.loc[out.station_id == "c", "is_pinned"].iloc[0]
    assert out.loc[out.station_id == "b", "is_proxy"].iloc[0]
    assert {"station_name", "lat", "lon", "tier", "adjusted_score"}.issubset(out.columns)


def test_stale_high_breach_demoted_below_fresh_equal():
    # Two BHs, identical breach; the stale one must rank BELOW the fresh one.
    rows = [
        {"station_id": "a", "p_breach": 0.6, "p_risk_high": 0.0, "stale_days": 200,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 7},
        {"station_id": "c", "p_breach": 0.6, "p_risk_high": 0.0, "stale_days": 14,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 7},
    ]
    out = build_pastas_triage(_summary(rows), _CAT, pinned_ids=set())
    assert list(out["station_id"]) == ["c", "a"]               # fresh first
    assert not out.loc[out.station_id == "a", "is_fresh"].iloc[0]


def test_stale_breach_likely_below_fresh_likely_but_above_fresh_possible():
    # Staleness is a sort key BETWEEN tier and score: a stale BREACH_LIKELY
    # ranks below a fresh BREACH_LIKELY but still above a fresh BREACH_POSSIBLE.
    rows = [
        {"station_id": "a", "p_breach": 0.9, "p_risk_high": 0.0, "stale_days": 70,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 3},
        {"station_id": "b", "p_breach": 0.55, "p_risk_high": 0.0, "stale_days": 2,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 7},
        {"station_id": "c", "p_breach": 0.3, "p_risk_high": 0.0, "stale_days": 2,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 7},
    ]
    out = build_pastas_triage(_summary(rows), _CAT, pinned_ids=set())
    assert list(out["station_id"]) == ["b", "a", "c"]
    assert list(out["tier"]) == ["BREACH_LIKELY", "BREACH_LIKELY", "BREACH_POSSIBLE"]


def test_proxy_downweighted_below_real_threshold_when_tied():
    rows = [
        {"station_id": "a", "p_breach": 0.3, "p_risk_high": 0.0, "stale_days": 14,
         "threshold_source": "gw_p90_proxy", "horizon_days": 14, "first_cross_median_lead": 7},
        {"station_id": "c", "p_breach": 0.3, "p_risk_high": 0.0, "stale_days": 14,
         "threshold_source": "user", "horizon_days": 14, "first_cross_median_lead": 7},
    ]
    out = build_pastas_triage(_summary(rows), _CAT, pinned_ids=set())
    assert list(out["station_id"]) == ["c", "a"]               # real-threshold first

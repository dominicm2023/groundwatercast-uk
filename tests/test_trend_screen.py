"""Tests for the trend screen (src/diagnostics/trend_screen.py).

Calibration is LOCKED here: the synthetic Moor Hall ramp must come out
HIGH / artifact_like / review_exclude, and the Liverpool North control must
not be flagged. These regressions guard against threshold drift.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.diagnostics import trend_screen as ts

# Mirrors config.json diagnostics.trend_screen defaults (kept inline so the
# unit tests don't depend on the config file).
CFG = dict(
    min_years=3.0, min_obs=730, slope_min_m_per_yr=0.10, r2_min=0.50,
    drift_ratio_min=1.0, change_min_m=0.15, change_med_m=0.5, change_high_m=2.0,
    step_threshold_m=0.30, step_rel_k=4.0, step_min_count=1, rain_corr_min=0.35,
    neighbour=dict(radius_km=15.0, min_overlap_years=3.0, min_neighbours=2,
                   isolation_slope_ratio=3.0),
)

YEARS = 8


def _idx(years=YEARS, start="2018-01-02"):
    return pd.date_range(start, periods=int(years * 365.25), freq="D")


def _gw(idx, slope_m_yr=0.0, seasonal_amp=0.5, base=30.0, noise=0.02, seed=0):
    rng = np.random.default_rng(seed)
    t = (idx - idx[0]).days.to_numpy(float) / 365.25
    seasonal = (seasonal_amp / 2) * np.sin(2 * np.pi * idx.dayofyear / 365.25)
    return pd.Series(base + slope_m_yr * t + seasonal + rng.normal(0, noise, len(idx)), index=idx)


def _rain(idx, year_mult=None, seed=1):
    """Daily rainfall with a per-year multiplier (default non-monotonic: wet, then
    drought yrs 4-5, then wet yrs 6-7) so the cumulative anomaly is non-monotonic."""
    rng = np.random.default_rng(seed)
    if year_mult is None:
        year_mult = [1.0, 1.0, 1.0, 0.5, 0.5, 1.5, 1.5, 1.0]
    yr = idx.year - idx.year.min()
    mult = np.array([year_mult[min(y, len(year_mult) - 1)] for y in yr])
    seasonal = 2.5 + 2.0 * np.clip(np.sin(2 * np.pi * (idx.dayofyear / 365.25 + 0.5)), 0, None)
    return pd.Series(np.maximum(0, seasonal * mult * rng.gamma(2.0, 0.5, len(idx))), index=idx)


# ---------------------------------------------------------------------------
# 1. Slope estimators + metrics
# ---------------------------------------------------------------------------

def test_ramp_slope_and_r2():
    idx = _idx()
    m = ts.screen_series(_gw(idx, slope_m_yr=0.70), None, CFG)
    assert m["slope_sen"] == pytest.approx(0.70, abs=0.05)
    assert m["r2"] > 0.90
    assert m["trend_change_m"] == pytest.approx(0.70 * m["record_years"], rel=0.1)
    assert m["n_steps"] == 0  # smooth ramp, no datum jumps


def test_flat_control_not_a_trend():
    idx = _idx()
    m = ts.screen_series(_gw(idx, slope_m_yr=0.013), None, CFG)
    assert abs(m["slope_sen"]) < 0.05
    assert ts.classify(m, CFG)["is_trend"] is False


def test_theil_sen_resists_step_that_inflates_ols():
    idx = _idx()
    g = _gw(idx, slope_m_yr=0.05, noise=0.01)
    g.iloc[len(g) // 2:] += 3.0           # inject a +3 m datum step mid-record
    monthly = g.resample("MS").mean()
    ft = ts.fit_trend(monthly)
    assert abs(ft["slope_sen"]) < abs(ft["slope_ols"])  # Sen far less inflated


def test_big_seasonal_zero_trend_not_flagged():
    idx = _idx()
    m = ts.screen_series(_gw(idx, slope_m_yr=0.0, seasonal_amp=3.0), None, CFG)
    assert m["seasonal_amp_m"] > 1.0
    assert ts.classify(m, CFG)["is_trend"] is False  # responsive-aquifer guard


def test_gap_spanning_move_is_not_a_step():
    # Gap-aware step detection: a dipped (~monthly) series climbing steadily must
    # NOT read as a same-day datum jump — the move spans weeks, not a day.
    idx = pd.date_range("2020-01-01", periods=24, freq="MS")
    s = pd.Series(np.linspace(30.0, 42.0, len(idx)), index=idx)  # ~0.5 m / month
    assert ts.step_metrics(s, step_threshold=0.30)["n_steps"] == 0


def test_same_day_jump_in_quiet_record_is_a_step():
    # A genuine one-day datum jump in an otherwise-quiet record IS still caught
    # (tiny 95th-pct scale → the jump is an obvious outlier).
    idx = pd.date_range("2020-01-01", periods=400, freq="D")
    s = pd.Series(30.0, index=idx)
    s.iloc[200:] += 0.5  # permanent +0.5 m offset from day 200
    m = ts.step_metrics(s, step_threshold=0.30, rel_k=4.0)
    assert m["n_steps"] == 1
    assert m["max_daily_step"] == pytest.approx(0.5, abs=1e-6)


def test_responsive_aquifer_recharge_not_a_step():
    # A flashy borehole (random walk with ~0.15 m daily moves) genuinely has
    # >30 cm days, but none are outliers FOR IT — so none count as datum jumps.
    idx = pd.date_range("2019-01-01", periods=800, freq="D")
    rng = np.random.default_rng(3)
    s = pd.Series(30.0 + np.cumsum(rng.normal(0, 0.15, len(idx))), index=idx)
    m = ts.step_metrics(s, step_threshold=0.30, rel_k=4.0)
    assert m["max_daily_step"] > 0.30   # it does move >30 cm in a day...
    assert m["n_steps"] == 0            # ...but that's normal for this borehole


# ---------------------------------------------------------------------------
# 2. Rainfall coherence (artefact vs real discriminator)
# ---------------------------------------------------------------------------

def test_monotonic_ramp_is_rainfall_incoherent():
    idx = _idx()
    rain = _rain(idx)                                   # non-monotonic cum anomaly
    m = ts.screen_series(_gw(idx, slope_m_yr=0.70), rain, CFG)
    assert m["rain_corr"] < CFG["rain_corr_min"]        # a clean ramp does NOT track rain


def test_recharge_tracking_rise_is_coherent():
    idx = _idx()
    rain = _rain(idx)
    monthly_rain = rain.resample("MS").sum()
    anom = monthly_rain - monthly_rain.groupby(monthly_rain.index.month).transform("mean")
    cum = anom.cumsum()
    # GW that genuinely tracks cumulative rainfall anomaly (resampled back to daily)
    gw_monthly = 30.0 + 0.02 * cum
    gw_daily = gw_monthly.reindex(idx, method="ffill").bfill()
    gw_daily = gw_daily + 0.25 * np.sin(2 * np.pi * idx.dayofyear / 365.25)
    m = ts.screen_series(gw_daily, rain, CFG)
    assert m["rain_corr"] > 0.7


# ---------------------------------------------------------------------------
# 3. classify() — provenance/action matrix (the artefact-vs-real resolver)
# ---------------------------------------------------------------------------

def test_moorhall_isolated_incoherent_is_artifact_high():
    idx = _idx()
    m = ts.screen_series(_gw(idx, slope_m_yr=0.70), _rain(idx), CFG)
    m.update(ts.neighbour_isolation(m["slope_sen"], [0.013, 0.0], CFG))  # two flat neighbours
    assert m["isolation_class"] == "isolated"
    c = ts.classify(m, CFG)
    assert (c["severity"], c["provenance_class"], c["recommended_action"]) == \
        ("high", "artifact_like", "review_exclude")


def test_isolated_but_coherent_is_local_real_not_excluded():
    m = dict(r2=0.95, slope_sen=0.4, drift_ratio=3.0, trend_change_m=3.0,
             isolation_class="isolated", rain_corr=0.8)
    c = ts.classify(m, CFG)
    assert c["provenance_class"] == "local_real_candidate"
    assert c["recommended_action"] == "metadata_check"      # never auto-excluded


def test_step_shift_flagged_for_review():
    # A datum step: ~0 Theil-Sen slope (slips the linear is_trend test) but a
    # 3 m jump → must be surfaced for a metadata check, not called stationary.
    m = dict(r2=0.20, slope_sen=0.02, drift_ratio=0.1, trend_change_m=0.1,
             isolation_class="no_neighbours", rain_corr=float("nan"),
             n_steps=1, max_daily_step=3.0)
    c = ts.classify(m, CFG)
    assert c["is_trend"] is False
    assert c["provenance_class"] == "step_shift"
    assert c["recommended_action"] == "metadata_check"
    assert c["severity"] == "high"             # 3.0 m >= change_high_m (2.0)


def test_no_step_stays_stationary():
    # No step + no trend → unchanged stationary verdict (no false positives).
    m = dict(r2=0.20, slope_sen=0.02, drift_ratio=0.1, trend_change_m=0.1,
             isolation_class="no_neighbours", rain_corr=float("nan"),
             n_steps=0, max_daily_step=0.05)
    c = ts.classify(m, CFG)
    assert c["provenance_class"] == "stationary"
    assert c["severity"] == "none"


def test_rainfall_coherent_jump_is_not_a_datum_artefact():
    # Big same-day jumps that track rainfall = flashy recharge, not a datum
    # event — must not be flagged as step_shift.
    m = dict(r2=0.20, slope_sen=0.02, drift_ratio=0.1, trend_change_m=0.1,
             isolation_class="no_neighbours", rain_corr=0.7,
             n_steps=2, max_daily_step=0.6)
    c = ts.classify(m, CFG)
    assert c["is_trend"] is False
    assert c["provenance_class"] == "stationary"


def test_few_cm_drift_ratio_below_floor_not_flagged():
    # Low-amplitude borehole: a few-cm drift clears drift_ratio>=1 against a tiny
    # seasonal swing, but the absolute change is below change_min_m -> not a trend.
    m = dict(r2=0.95, slope_sen=0.02, drift_ratio=2.0, trend_change_m=0.08,
             isolation_class="no_neighbours", rain_corr=float("nan"),
             n_steps=0, max_daily_step=0.05)
    assert ts.classify(m, CFG)["is_trend"] is False


def test_regional_trend_is_real_detrend_candidate():
    m = dict(r2=0.95, slope_sen=0.4, drift_ratio=3.0, trend_change_m=3.0,
             isolation_class="regional", rain_corr=0.1)
    c = ts.classify(m, CFG)
    assert c["provenance_class"] == "regional_real"
    assert c["recommended_action"] == "review_detrend_or_keep"


# ---------------------------------------------------------------------------
# 4. Neighbour isolation
# ---------------------------------------------------------------------------

def test_neighbour_isolation_classes():
    assert ts.neighbour_isolation(0.70, [0.01, 0.0, -0.02], CFG)["isolation_class"] == "isolated"
    assert ts.neighbour_isolation(0.70, [0.6, 0.5, 0.7], CFG)["isolation_class"] == "regional"
    assert ts.neighbour_isolation(0.70, [0.01], CFG)["isolation_class"] == "no_neighbours"


# ---------------------------------------------------------------------------
# 5. Determinism
# ---------------------------------------------------------------------------

def test_pure_and_deterministic():
    idx = _idx()
    g, r = _gw(idx, slope_m_yr=0.5), _rain(idx)
    a = ts.screen_series(g, r, CFG)
    b = ts.screen_series(g, r, CFG)
    for k in ("slope_sen", "r2", "rain_corr", "trend_change_m"):
        assert a[k] == b[k]

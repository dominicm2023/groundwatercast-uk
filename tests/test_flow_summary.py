"""Tests for src/forecast/pastas/flow_summary.py — the low-flow aggregator
(build_plan.md Stage 6).

Pure numpy/pandas — no pastas import — so this runs in the main env too,
exactly like test_pastas_summary.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.forecast.pastas import flow_summary as FS


def _members(gauge_id, dates, *, eps=0.01, n_members=51,
            logq_level=0.0, logq_slope=0.0, sig0=0.1, sig_slope=0.01, seed=0):
    """Member frame in the exact shape src.forecast.pastas.ensemble.drive_borehole
    emits for a flow_2s rec: "station_id" holds the gauge id, "gw_pred"/
    "gw_sigma" hold logQ mean/sigma (NOT head, NOT raw m3/s — architecture
    decision 2: exponentiate only at publish)."""
    rng = np.random.default_rng(seed)
    origin = pd.Timestamp(dates[0]) - pd.Timedelta(days=1)
    rows = []
    for m in range(n_members):
        for i, d in enumerate(dates):
            rows.append({"station_id": gauge_id, "member": m, "date": d,
                        "precip_mm": 0.0,
                        "gw_pred": logq_level + logq_slope * i + rng.normal(0, 0.01),
                        "gw_sigma": sig0 + sig_slope * i, "origin_date": origin})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# exp_q: the "exponentiate only at the aggregation boundary" transform
# ---------------------------------------------------------------------------

def test_exp_q_inverts_the_fit_time_log_transform():
    eps = 0.05
    q_true = np.array([0.0, 0.5, 2.0, 10.0])
    logq = np.log(q_true + eps)
    assert np.allclose(FS.exp_q(logq, eps), q_true, atol=1e-9)


def test_exp_q_floors_at_zero_on_a_noise_excursion_below_log_eps():
    eps = 0.1
    logq = np.array([np.log(eps) - 5.0])   # far below log(eps) -> would be negative
    assert FS.exp_q(logq, eps)[0] == 0.0


# ---------------------------------------------------------------------------
# aggregate_flow: breach direction (LOW-flow — crossing BELOW Q95, not above)
# ---------------------------------------------------------------------------

def test_fan_dipping_below_q95_yields_positive_probability():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.01
    # logQ trajectory that dips well below the Q95 threshold mid-window.
    q95 = 0.5
    thr_logq = float(np.log(q95 + eps))
    df = _members("G1", dates, eps=eps, logq_level=thr_logq - 2.0, sig0=0.05, sig_slope=0.0)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": eps, "q95_m3s": q95}}
    summ, fan = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                  n_samples=4000, seed=1)
    r = summ.iloc[0]
    assert r["p_below_q95"] > 0.9
    assert r["p_below_q95_14d"] > 0.9
    assert r["threshold_source"] == "q95_proxy"
    # the fan's own P50 must actually sit below Q95 too (units check: m3/s)
    assert (fan["q_p50_m3s"] < q95).all()


def test_fan_staying_above_q95_yields_near_zero_probability():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.01
    q95 = 0.1
    thr_logq = float(np.log(q95 + eps))
    # Comfortably above the threshold, tiny noise -> essentially never dips.
    df = _members("G1", dates, eps=eps, logq_level=thr_logq + 5.0, sig0=0.02, sig_slope=0.0)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": eps, "q95_m3s": q95}}
    summ, fan = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                  n_samples=4000, seed=2)
    r = summ.iloc[0]
    assert r["p_below_q95"] < 0.01
    assert r["censored_frac"] > 0.99
    assert (fan["q_p50_m3s"] > q95).all()


def test_breach_probabilities_are_within_unit_interval():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.01
    q95 = 0.3
    thr_logq = float(np.log(q95 + eps))
    df = _members("G1", dates, eps=eps, logq_level=thr_logq, sig0=0.3, sig_slope=0.02)
    models = {"G1": {"sigma": 1.0, "alpha": 40.0, "eps": eps, "q95_m3s": q95}}
    summ, _ = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                n_samples=2000, seed=3)
    r = summ.iloc[0]
    assert 0.0 <= r["p_below_q95"] <= 1.0
    assert 0.0 <= r["p_below_q95_14d"] <= 1.0
    assert 0.0 <= r["censored_frac"] <= 1.0


def test_fan_values_are_positive_m3s():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.02
    df = _members("G1", dates, eps=eps, logq_level=0.5, sig0=0.1, sig_slope=0.01)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": eps, "q95_m3s": 0.2}}
    _, fan = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                               n_samples=2000, seed=4)
    assert (fan["q_p10_m3s"] >= 0).all()
    assert (fan["q_p50_m3s"] >= 0).all()
    assert (fan["q_p90_m3s"] >= 0).all()
    assert (fan["q_p10_m3s"] <= fan["q_p50_m3s"]).all()
    assert (fan["q_p50_m3s"] <= fan["q_p90_m3s"]).all()


def test_missing_q95_reports_none_source_and_nan_probability():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    df = _members("G1", dates, eps=0.01, logq_level=0.0)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": 0.01}}   # no q95_m3s
    summ, _ = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                n_samples=500, seed=5)
    r = summ.iloc[0]
    assert r["threshold_source"] == "none"
    assert pd.isna(r["p_below_q95"])
    assert "No Q95 threshold" in r["headline"]


def test_q95_by_gauge_override_takes_precedence_over_rec():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.01
    df = _members("G1", dates, eps=eps, logq_level=0.0)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": eps, "q95_m3s": 999.0}}
    summ, _ = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                q95_by_gauge={"G1": 0.3}, n_samples=500, seed=6)
    assert summ.iloc[0]["q95_m3s"] == pytest.approx(0.3)


def test_gauge_without_a_model_is_skipped_not_crashed():
    dates = pd.date_range("2026-08-01", periods=5, freq="D")
    df = _members("UNKNOWN_GAUGE", dates, n_members=5)
    summ, fan = FS.aggregate_flow(df, {}, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                  n_samples=200, seed=7)
    assert summ.empty and fan.empty
    assert list(summ.columns) == FS.SUMMARY_COLS
    assert list(fan.columns) == FS.FAN_COLS


def test_headline_never_says_breach_or_warning():
    dates = pd.date_range("2026-08-01", periods=14, freq="D")
    eps = 0.01
    q95 = 0.5
    thr_logq = float(np.log(q95 + eps))
    df = _members("G1", dates, eps=eps, logq_level=thr_logq - 2.0, sig0=0.05)
    models = {"G1": {"sigma": 1.0, "alpha": 50.0, "eps": eps, "q95_m3s": q95}}
    summ, _ = FS.aggregate_flow(df, models, run=pd.Timestamp("2026-08-03", tz="UTC"),
                                n_samples=1000, seed=8)
    headline = summ.iloc[0]["headline"]
    assert "warning" not in headline.lower()
    assert "breach" not in headline.lower()
    assert "abstraction" in headline.lower()

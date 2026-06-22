"""Tests for the corrected-additive seasonal band (uncertainty inheritance).

Locks the invariants the multi-agent decomposition debate required:
  - continuity: at the fan->seasonal handoff the band >= the fan terminal band;
  - AR1 single-count: model noise appears exactly once (no double-loading);
  - long-lead convergence: the inherited term decays away to (AR1 + climatology);
  - tau_state is the SLOWER of the two model clocks (Gamma memory vs alpha).
"""
from __future__ import annotations

import numpy as np
import pytest

from src.forecast.seasonal import esp

Z = 1.2815515594


# ---------------------------------------------------------------------------
# weighted_var
# ---------------------------------------------------------------------------

def test_weighted_var_uniform_matches_numpy():
    v = np.array([1.0, 2.0, 3.0, 4.0])
    w = np.ones(4)
    assert esp.weighted_var(v, w) == pytest.approx(np.var(v))


def test_weighted_var_identical_is_zero():
    assert esp.weighted_var(np.array([5.0, 5.0, 5.0]), np.ones(3)) == 0.0


def test_weighted_var_too_few_finite_is_zero():
    assert esp.weighted_var(np.array([np.nan, 2.0]), np.ones(2)) == 0.0


# ---------------------------------------------------------------------------
# state_memory_timescale — tau_state (max of the Gamma memory and alpha)
# ---------------------------------------------------------------------------

def _rec(names, params, alpha):
    return {"param_names": names, "params": params, "alpha": alpha}


def test_tau_from_gamma_params_when_gamma_is_slower():
    # Gamma(n=1, a=100): step response 1 - e^(-t/a) reaches 0.95 at
    # t = -a*ln(0.05) = 100 * 2.9957 ~ 299.6 days; alpha=8 is faster, so the
    # Gamma memory wins.
    rec = _rec(["rch_A", "rch_n", "rch_a", "noise_alpha"], [120.0, 1.0, 100.0, 8.0], 8.0)
    tau = esp.state_memory_timescale(rec)
    assert tau == pytest.approx(100.0 * 2.9957, rel=0.01)
    assert tau > rec["alpha"]


def test_tau_uses_alpha_when_alpha_is_slower():
    # Heavy-tailed Gamma whose 95%-time understates memory (n<1, a=50 -> ~30 d)
    # but alpha=68 d is the slower clock: tau_state = alpha (the Liverpool case).
    rec = _rec(["rch_A", "rch_n", "rch_a", "noise_alpha"],
               [0.29, 0.103, 50.0, 68.3], 68.3)
    assert esp.state_memory_timescale(rec) == pytest.approx(68.3, rel=1e-3)


def test_tau_fallback_when_no_gamma_params_uses_alpha():
    rec = _rec(["constant_d", "noise_alpha"], [1.0, 90.0], 90.0)
    assert esp.state_memory_timescale(rec) == pytest.approx(90.0)


def test_tau_fallback_on_degenerate_shape_uses_alpha():
    rec = _rec(["rch_n", "rch_a"], [-1.0, 100.0], 80.0)   # n<=0 -> Gamma dropped
    assert esp.state_memory_timescale(rec) == pytest.approx(80.0)


def test_tau_clamped_to_min():
    rec = _rec(["foo"], [1.0], 5.0)                       # max(0, 5) = 5 < 30
    assert esp.state_memory_timescale(rec, min_days=30.0) == 30.0


def test_tau_clamped_to_max():
    rec = _rec(["rch_n", "rch_a"], [1.0, 5000.0], 10.0)   # tau_gamma huge
    assert esp.state_memory_timescale(rec, max_days=2000.0) == 2000.0


def test_tau_capital_A_not_mistaken_for_scale_a():
    # "rch_A" (gain) must not be picked as the scale "rch_a"; with only A+n and
    # no lower-case a, the Gamma term drops and tau falls back to alpha.
    rec = _rec(["rch_A", "rch_n"], [120.0, 1.5], 55.0)
    assert esp.state_memory_timescale(rec) == pytest.approx(55.0)


# ---------------------------------------------------------------------------
# additive_band — the invariants
# ---------------------------------------------------------------------------

CFG = dict(sigma=0.05, alpha=10.0, tau_state=300.0)


def _hw(q10, q50, q90):
    """half-width (one-sided), should be z*sd."""
    return q90 - q50


def test_band_centered_at_weighted_median():
    mu = np.array([10.0, 10.1, 9.9, 10.2])
    q10, q50, q90 = esp.additive_band(mu, np.ones(4), **CFG, sd46=0.06,
                                      dt46=70, dt_month=70, lead_gap=0)
    assert q50 == pytest.approx(esp.weighted_quantiles(mu, np.ones(4), qs=(0.5,))[0])
    assert (q90 - q50) == pytest.approx(q50 - q10)        # symmetric


def test_continuity_at_handoff_band_ge_fan_terminal():
    # lead_gap=0, dt_month=dt46, identical analog means (V_esp=0): the band must
    # equal the fan terminal band exactly (V_ar1 + s_state^2 reconstruct sd46^2).
    sd46 = 0.06
    q10, q50, q90 = esp.additive_band(np.full(5, 10.0), np.ones(5), **CFG,
                                      sd46=sd46, dt46=70, dt_month=70, lead_gap=0)
    assert _hw(q10, q50, q90) == pytest.approx(Z * sd46, rel=1e-6)


def test_continuity_with_climatology_only_widens():
    # Same handoff but with analog spread: band must be >= the fan terminal band.
    sd46 = 0.06
    mu = np.array([9.8, 9.9, 10.0, 10.1, 10.2])          # V_esp > 0
    q10, q50, q90 = esp.additive_band(mu, np.ones(5), **CFG, sd46=sd46,
                                      dt46=70, dt_month=70, lead_gap=0)
    assert _hw(q10, q50, q90) >= Z * sd46


def test_ar1_counted_once_when_s_state_zero():
    # If the fan terminal is pure AR1 (sd46^2 == V_ar1(dt46)), s_state^2 == 0, so
    # the band is just the (single) AR1 width — model noise is NOT double-loaded.
    sigma, alpha, dt46 = 0.05, 10.0, 70.0
    sd46 = sigma * np.sqrt(1 - np.exp(-2 * dt46 / alpha))
    q10, q50, q90 = esp.additive_band(np.full(4, 10.0), np.ones(4), sigma=sigma,
                                      alpha=alpha, tau_state=300.0, sd46=sd46,
                                      dt46=dt46, dt_month=dt46, lead_gap=0)
    assert _hw(q10, q50, q90) == pytest.approx(Z * sd46, rel=1e-6)


def test_inherited_term_decays_with_lead_gap():
    sd46 = 0.08
    near = _hw(*esp.additive_band(np.full(4, 10.0), np.ones(4), **CFG, sd46=sd46,
                                  dt46=70, dt_month=80, lead_gap=10))
    far = _hw(*esp.additive_band(np.full(4, 10.0), np.ones(4), **CFG, sd46=sd46,
                                 dt46=70, dt_month=250, lead_gap=200))
    assert far < near                                     # inheritance fades out


def test_long_lead_converges_to_ar1_plus_climatology():
    # At long lead the inherited term -> 0, leaving sqrt(sigma^2 + V_esp).
    sigma = 0.05
    mu = np.array([9.7, 9.9, 10.1, 10.3])
    v_esp = esp.weighted_var(mu, np.ones(4))
    q10, q50, q90 = esp.additive_band(mu, np.ones(4), sigma=sigma, alpha=10.0,
                                      tau_state=120.0, sd46=0.09, dt46=70,
                                      dt_month=2000, lead_gap=2000)
    assert _hw(q10, q50, q90) == pytest.approx(Z * np.sqrt(sigma ** 2 + v_esp), rel=0.02)

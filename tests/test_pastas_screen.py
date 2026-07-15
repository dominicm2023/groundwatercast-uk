"""Tests for the short-record admission gate (src/forecast/pastas/screen.py).

The gate_pass rule and the leakage_safe_hindcast short-circuit branches are pure
(no pastas) — importing screen does NOT import pastas (recharge imports it lazily
inside _build_model), so these run in any env. The full calibrated-hindcast path
is exercised live against real boreholes (Clanville et al.), not unit-tested here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.forecast.pastas import screen


def test_gate_pass_rule():
    # gate_pass(cov, skill_ratio, band_frac): beats persistence + calibrated + sharp
    assert screen.gate_pass(80.0, 0.6, 0.3) is True
    assert screen.gate_pass(100.0, 1.0, 0.5) is True           # ties persistence, ok
    # loses to persistence (P50 wanders) → reject
    assert screen.gate_pass(80.0, 1.5, 0.3) is False
    # over-confident: coverage below floor → reject
    assert screen.gate_pass(40.0, 0.6, 0.3) is False
    # uselessly wide band → reject
    assert screen.gate_pass(100.0, 0.6, 0.9) is False
    # not evaluable → reject
    assert screen.gate_pass(None, 0.6, 0.3) is False
    assert screen.gate_pass(80.0, None, 0.3) is False
    assert screen.gate_pass(80.0, 0.6, None) is False


def test_gate_pass_threshold_overrides():
    # stricter coverage floor flips a borderline pass to fail
    assert screen.gate_pass(60.0, 0.6, 0.3, min_coverage_pct=80.0) is False
    # looser skill tolerance admits a slightly-worse-than-persistence forecast
    assert screen.gate_pass(80.0, 1.4, 0.3, max_skill_ratio=1.5) is True
    # tighter sharpness rejects a wide band
    assert screen.gate_pass(80.0, 0.6, 0.5, max_band_frac=0.4) is False


def test_inflation_factor_widens_to_target():
    # |z| whose 80th percentile is 2·Z90 → k = 2 (band must double to cover 80%)
    z = np.full(100, 2.0 * screen._Z90)
    assert abs(screen._inflation_factor(z, 0.80) - 2.0) < 1e-9
    # an over-confident spread: after inflation, coverage of the widened band = target
    zz = np.linspace(0.0, 4.0, 1000)
    k = screen._inflation_factor(zz, 0.80)
    covered = float((zz <= k * screen._Z90).mean())
    assert abs(covered - 0.80) < 0.02


def test_inflation_factor_floored_at_one():
    # already well-covered (small residuals) → never NARROW the band
    assert screen._inflation_factor(np.full(100, 0.4), 0.80) == 1.0
    assert screen._inflation_factor(np.array([]), 0.80) == 1.0


def test_hindcast_empty_head_short_circuits():
    empty = pd.Series(dtype=float)
    out = screen.leakage_safe_hindcast("X", empty, empty, empty, rfunc="Gamma",
                                       recharge="FlexModel", precip_source="gauge")
    assert out["gate_pass"] is False and out["reason"] == "no_obs"


def test_hindcast_insufficient_train_never_calls_pastas():
    # 100 obs total; every origin's train window is < MIN_TRAIN_ROWS, so each
    # origin returns None (before any pastas-importing calibration) and the gate
    # fails for lack of evaluable origins.
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    head = pd.Series(np.linspace(10.0, 11.0, 100), index=idx)
    dummy = pd.Series(dtype=float)
    out = screen.leakage_safe_hindcast("X", head, dummy, dummy, rfunc="Gamma",
                                       recharge="FlexModel", precip_source="gauge")
    assert out["gate_pass"] is False
    assert out["reason"].startswith("origins<")
    assert out["n_origins"] == 0 and out["origins"] == []
    assert out["range_m"] == 1.0                # max−min of the ramp
    assert out["sigma_inflation"] == 1.0        # default when not evaluable

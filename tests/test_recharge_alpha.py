"""Regression test for the AR1-decay clamp in the Pastas seeded forecast.

These exercise the pure ``_safe_alpha`` helper only (no pastas needed), so they
run in the main/grib env unlike the rest of test_pastas_recharge.py. A fitted
alpha beyond ~1 year (e.g. the ~999.5-day calibrate fallback) used to be read
straight into the seed-residual decay, carrying a glitchy/stale seed almost
undecayed across the whole fan.
"""
from __future__ import annotations

import numpy as np

from src.forecast.pastas.recharge import _ALPHA_MAX_DAYS, _safe_alpha


def test_normal_alpha_passes_through():
    assert _safe_alpha(45.0) == 45.0


def test_degenerate_fallback_is_capped():
    assert _safe_alpha(999.5) == _ALPHA_MAX_DAYS


def test_nan_inf_zero_negative_none_unparseable_go_to_cap():
    for bad in (float("nan"), float("inf"), 0.0, -3.0, None, "not-a-number"):
        assert _safe_alpha(bad) == _ALPHA_MAX_DAYS

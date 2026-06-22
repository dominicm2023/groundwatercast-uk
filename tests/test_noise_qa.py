"""Roadmap 0.3 — AR1 residual-fit diagnostic. Pure-function tests (no pastas),
so they run in the main/grib env. White innovations pass; autocorrelated,
seasonal, or heteroscedastic innovations are flagged for review.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.diagnostics import noise_qa as Q


def _idx(n):
    return pd.date_range("2018-01-01", periods=n, freq="D")


def test_white_noise_passes():
    rng = np.random.default_rng(0)
    s = pd.Series(rng.normal(0, 1, 800), index=_idx(800))
    out = Q.residual_diagnostics(s)
    assert out["passes"] is True
    assert out["flags"] == "ok"
    assert abs(out["lag1_autocorr"]) < 0.25


def test_autocorrelated_noise_flagged():
    rng = np.random.default_rng(1)
    n = 800
    e = rng.normal(0, 1, n)
    z = np.zeros(n)
    for k in range(1, n):
        z[k] = 0.8 * z[k - 1] + e[k]          # strong AR1
    out = Q.residual_diagnostics(pd.Series(z, index=_idx(n)))
    assert out["passes"] is False
    assert "autocorrelated_noise" in out["flags"]
    assert out["lag1_autocorr"] > 0.5


def test_seasonal_residual_flagged():
    n = 365 * 4
    idx = _idx(n)
    s = pd.Series(np.sin(2 * np.pi * idx.month / 12.0), index=idx)
    out = Q.residual_diagnostics(s)
    assert out["passes"] is False
    assert "seasonal_residual" in out["flags"]
    assert out["seasonal_frac"] > 0.15


def test_heteroscedastic_flagged():
    rng = np.random.default_rng(2)
    n = 800
    level = pd.Series(np.linspace(0, 100, n), index=_idx(n))
    noise = pd.Series(rng.normal(0, 1.0 + level.to_numpy() / 15.0), index=_idx(n))
    out = Q.residual_diagnostics(noise, level)
    assert out["passes"] is False
    assert "heteroscedastic" in out["flags"]


def test_short_series_does_not_cry_wolf():
    s = pd.Series(np.arange(20.0), index=_idx(20))
    out = Q.residual_diagnostics(s)
    assert out["passes"] is True
    assert out["flags"] == "insufficient_obs"

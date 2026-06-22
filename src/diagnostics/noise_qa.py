"""AR1/OU residual-fit diagnostic (roadmap 0.3) — report-only correctness check.

The Pastas seeded forecast band (``recharge.simulate_path``) decays the origin
residual as ``r0·exp(-Δt/α)`` and grows the predictive sd to the marginal
residual ``sigma``. That AR1 / Ornstein–Uhlenbeck formula is the textbook-correct
conditional-moment split — **but only if the calibrated noise actually satisfies
the AR1 assumption**: the innovations (``ml.noise()``) should be ~white (no
leftover autocorrelation), carry no seasonal structure the deterministic model
missed, and be homoscedastic (variance not growing with the head level). If any
fails, the predictive band is mis-scaled at the source, under every fan and
breach probability.

This module checks those three assumptions per station and emits a pass/flag
record. It changes nothing in the forecast — like the trend screen, it reports
and a human acts. Pure numpy/pandas (never imports pastas); importable anywhere.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Thresholds (overridable via cfg). Innovations of a well-specified AR1 noise
# model are ~white; these bound how far from that the calibrated noise may drift
# before the station is flagged for review.
DEFAULTS = dict(lag1_abs_max=0.25, seasonal_frac_max=0.15,
                hetero_abs_max=0.30, min_obs=60)


def lag1_autocorr(series) -> float:
    """Lag-1 autocorrelation of the (innovation) series. ~0 for white noise."""
    s = pd.Series(series).dropna()
    if len(s) < 3:
        return float("nan")
    c = s.autocorr(lag=1)
    return float(c) if pd.notna(c) else float("nan")


def seasonal_fraction(series) -> float:
    """Fraction of variance explained by the calendar-month means — leftover
    seasonality the deterministic recharge response did not capture (0 = none,
    →1 = strongly seasonal). Needs a DatetimeIndex; NaN otherwise."""
    s = pd.Series(series).dropna()
    if len(s) < 12 or not isinstance(s.index, pd.DatetimeIndex):
        return float("nan")
    tot = float(s.var(ddof=0))
    if not np.isfinite(tot) or tot <= 0:
        return float("nan")
    month_mean = s.groupby(s.index.month).transform("mean")
    return float(month_mean.var(ddof=0) / tot)


def hetero_corr(series, level) -> float:
    """Correlation of |innovation| with the head level — a proxy for
    heteroscedasticity (variance growing with level). ~0 when homoscedastic."""
    if level is None:
        return float("nan")
    j = pd.concat([pd.Series(series).rename("r"),
                   pd.Series(level).rename("l")], axis=1).dropna()
    if len(j) < 12:
        return float("nan")
    c = j["r"].abs().corr(j["l"])
    return float(c) if pd.notna(c) else float("nan")


def residual_diagnostics(noise, level=None, *, cfg: dict | None = None) -> dict:
    """Per-station AR1 fit check on the innovation series ``noise``.

    Returns lag-1 autocorrelation, seasonal variance fraction, |noise|-vs-level
    correlation, a ``passes`` bool and a ``flags`` string. Too few observations
    → passes=True with flag ``insufficient_obs`` (don't cry wolf on short series).
    """
    c = {**DEFAULTS, **(cfg or {})}
    s = pd.Series(noise).dropna()
    n = int(len(s))
    base = dict(n=n, lag1_autocorr=float("nan"), seasonal_frac=float("nan"),
                hetero_corr=float("nan"))
    if n < c["min_obs"]:
        return {**base, "passes": True, "flags": "insufficient_obs"}
    lag1 = lag1_autocorr(s)
    seas = seasonal_fraction(s)
    het = hetero_corr(s, level)
    flags = []
    if np.isfinite(lag1) and abs(lag1) > c["lag1_abs_max"]:
        flags.append("autocorrelated_noise")
    if np.isfinite(seas) and seas > c["seasonal_frac_max"]:
        flags.append("seasonal_residual")
    if np.isfinite(het) and abs(het) > c["hetero_abs_max"]:
        flags.append("heteroscedastic")
    return dict(n=n, lag1_autocorr=lag1, seasonal_frac=seas, hetero_corr=het,
                passes=(len(flags) == 0), flags=";".join(flags) if flags else "ok")

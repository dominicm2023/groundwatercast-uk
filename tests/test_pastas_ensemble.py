"""Tests for the Pastas ensemble driver (src/forecast/pastas/ensemble.py).

Skips in the main env (no pastas); runs under the pastas venv.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pastas")

from src.forecast.pastas import recharge as R
from src.forecast.pastas import ensemble as E


@pytest.fixture(scope="module")
def calibrated():
    idx = pd.date_range("2018-01-01", "2022-12-31", freq="D")
    doy = idx.day_of_year.to_numpy()
    rng = np.random.default_rng(1)
    prec = pd.Series(rng.gamma(0.5, 4.0, len(idx)), idx)
    evap = pd.Series(np.clip(2 + 1.5 * np.sin(2 * np.pi * doy / 365), 0, None), idx)
    net = np.clip(prec.values - 0.7 * evap.values, 0, None)
    k = np.exp(-np.arange(60) / 20.0); k /= k.sum()
    h = 30 + np.convolve(net, k)[:len(idx)] * 0.05 + rng.normal(0, 0.02, len(idx))
    head = pd.Series(h, idx)
    rec = R.calibrate("BH1", head, prec, evap)
    return rec, head, prec, evap


def _members(forecast_dates, n=5, seed=2):
    rng = np.random.default_rng(seed)
    rows = []
    for m in range(n):
        for d in forecast_dates:
            rows.append({"member": m, "date": d,
                         "precip_mm": float(rng.gamma(0.5, 4.0))})
    return pd.DataFrame(rows)


def test_drive_borehole_shape_and_spread(calibrated):
    rec, head, prec, evap = calibrated
    fdates = pd.date_range("2023-01-08", periods=14, freq="D")   # just after history end
    mdf = _members(fdates, n=6)
    out = E.drive_borehole("BH1", rec, head, prec, evap, mdf)

    assert list(out.columns) == E.MEMBER_COLS
    assert out["member"].nunique() == 6
    assert len(out) == 6 * 14
    assert np.isfinite(out["gw_pred"]).all()
    assert (out["gw_sigma"] > 0).all()
    # members disagree on at least one date (ensemble has spread)
    per_date_std = out.groupby("date")["gw_pred"].std()
    assert per_date_std.max() > 0
    # predictive sigma grows (or holds) with lead
    s = out.groupby("date")["gw_sigma"].first()
    assert s.iloc[-1] >= s.iloc[0]


def test_empty_members_returns_empty(calibrated):
    rec, head, prec, evap = calibrated
    out = E.drive_borehole("BH1", rec, head, prec, evap,
                           pd.DataFrame(columns=["member", "date", "precip_mm"]))
    assert out.empty and list(out.columns) == E.MEMBER_COLS

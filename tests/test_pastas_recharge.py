"""Tests for the production Pastas recharge layer (src/forecast/pastas/recharge.py).

Pastas lives in a dedicated venv, so these tests SKIP automatically in the main
environment (where pastas isn't installed) and RUN under the pastas venv:
  %LOCALAPPDATA%\\Temp\\pastas-venv314\\Scripts\\python.exe -m pytest tests/test_pastas_recharge.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pastas")            # skip in the main env

from src.forecast.pastas import recharge as R


@pytest.fixture(scope="module")
def synthetic():
    """A solvable synthetic borehole: head responds to net (rain−PET) recharge."""
    idx = pd.date_range("2018-01-01", "2022-12-31", freq="D")
    doy = idx.day_of_year.to_numpy()
    rng = np.random.default_rng(0)
    prec = pd.Series(rng.gamma(0.5, 4.0, len(idx)), idx)
    evap = pd.Series(np.clip(2 + 1.5 * np.sin(2 * np.pi * doy / 365), 0, None), idx)
    net = np.clip(prec.values - 0.7 * evap.values, 0, None)
    k = np.exp(-np.arange(60) / 20.0); k /= k.sum()
    h = 30 + np.convolve(net, k)[:len(idx)] * 0.05 + rng.normal(0, 0.02, len(idx))
    head = pd.Series(h, idx)
    return head, prec, evap


def test_calibrate_returns_serialisable_rec(synthetic):
    head, prec, evap = synthetic
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    assert rec["station_id"] == "BH1"
    assert rec["rfunc"] == "Gamma" and rec["recharge"] == "FlexModel"
    assert len(rec["params"]) == len(rec["param_names"]) > 0
    assert np.isfinite(rec["sigma"]) and rec["sigma"] >= 0
    assert np.isfinite(rec["alpha"]) and rec["alpha"] > 0
    assert rec["evp"] > 30                      # explains real variance on this signal
    assert rec["precip_source"] == "joined"     # default when the caller doesn't say
    # round-trips through JSON unchanged
    import json
    assert json.loads(json.dumps(rec)) == rec


def test_calibrate_records_precip_source(synthetic):
    # build_pastas_models passes this explicitly once a station has a gauge
    # link (src.forecast.ensemble.members.observed_daily_rainfall) — recorded
    # as provenance so a model still on the joined-CSV fallback is easy to spot.
    head, prec, evap = synthetic
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"),
                      precip_source="gauge")
    assert rec["precip_source"] == "gauge"


def test_seeded_forecast_anchors_and_shapes(synthetic):
    head, prec, evap = synthetic
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    origin = pd.Timestamp("2022-06-01")
    window, mean, sig = R.seeded_forecast(rec, head, prec, evap, origin, horizon=14)
    assert len(window) == len(mean) == len(sig) == 14
    assert window[0] == origin + pd.Timedelta(days=1)
    assert np.isfinite(mean).all() and (sig > 0).all()
    # seeded at the origin: lead-1 forecast is close to the observed origin level
    assert abs(mean[0] - float(head.loc[origin])) < 1.0
    # AR1 band widens with lead
    assert sig[-1] >= sig[0]


def test_save_load_roundtrip(synthetic, tmp_path):
    head, prec, evap = synthetic
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    p = R.save_models({"BH1": rec}, tmp_path / "models.json")
    loaded = R.load_models(p)
    assert set(loaded) == {"BH1"}
    assert loaded["BH1"]["params"] == rec["params"]
    assert R.load_models(tmp_path / "missing.json") == {}

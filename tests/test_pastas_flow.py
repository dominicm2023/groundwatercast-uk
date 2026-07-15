"""Tests for the two-pathway flow model glue (low-flow build_plan.md Stage 3):
``calibrate_flow`` + the ``model_kind``-dispatching ``simulate_path`` in
``src/forecast/pastas/recharge.py``.

Pastas lives in a dedicated venv, so these tests SKIP automatically in the main
environment (where pastas isn't installed) and RUN under the pastas venv:
  .venv-pastas\\Scripts\\python.exe -m pytest tests/test_pastas_flow.py

The Itchen @ Highbridge fixture (tests/fixtures/itchen_highbridge_2018_2026.csv)
is real EA/Open-Meteo data (OGL v3 — see the file header for provenance),
fetched live once via the production Stage-1/2 machinery
(src.forecast.ensemble.members.observed_daily_rainfall,
src.forecast.pastas.io.load_pet, src.download.flow) so the regression EVP is
representative of the real production path, not a synthetic stand-in.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pastas")            # skip in the main env

from src.forecast.pastas import recharge as R

FIXTURE = Path(__file__).parent / "fixtures" / "itchen_highbridge_2018_2026.csv"


@pytest.fixture(scope="module")
def itchen():
    """Real R. Itchen @ Highbridge flow + its 3-gauge rain + PET, 2018-07→2026-06
    (docs/product/lowflow/analysis.md §3/§5 — the model-spike gauge)."""
    df = pd.read_csv(FIXTURE, comment="#", parse_dates=["date"]).set_index("date")
    return df["Flow_m3s"], df["Rain_mm"], df["PET_mm"]


@pytest.fixture(scope="module")
def synthetic_winterbourne():
    """A synthetic gauge with a real dry spell (exact-zero flow days) — the
    zero-flow epsilon round-trip needs a series that actually dries out, which
    the perennial chalk-stream Itchen fixture never does."""
    idx = pd.date_range("2018-01-01", "2022-12-31", freq="D")
    doy = idx.day_of_year.to_numpy()
    rng = np.random.default_rng(1)
    prec = pd.Series(rng.gamma(0.5, 4.0, len(idx)), idx)
    evap = pd.Series(np.clip(2 + 1.5 * np.sin(2 * np.pi * doy / 365), 0, None), idx)
    net = np.clip(prec.values - 0.7 * evap.values, 0, None)
    k = np.exp(-np.arange(30) / 10.0); k /= k.sum()
    baseflow = 0.5 + np.convolve(net, k)[:len(idx)] * 0.02
    q = np.clip(baseflow + 0.01 * rng.normal(0, 1, len(idx)), 0, None)
    q[100:130] = 0.0                      # a 30-day dry spell (winterbourne)
    return pd.Series(q, idx), prec, evap


# ---------------------------------------------------------------------------
# calibrate_flow: basic shape + EVP regression (build_plan.md Stage 3 accept)
# ---------------------------------------------------------------------------

def test_calibrate_flow_returns_flow_2s_rec(itchen):
    q, prec, evap = itchen
    rec = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    assert rec["station_id"] == "itchen_highbridge"
    assert rec["model_kind"] == "flow_2s"
    assert rec["rfunc"] == "Gamma" and rec["recharge"] == "FlexModel"
    assert rec["precip_source"] == "gauge"       # flow's only source — no "joined" fallback
    assert len(rec["params"]) == len(rec["param_names"]) > 0
    # both stresses' params are present
    assert any(n.startswith("rch_") for n in rec["param_names"])
    assert any(n.startswith("quickflow_") for n in rec["param_names"])
    assert np.isfinite(rec["sigma"]) and rec["sigma"] >= 0
    assert np.isfinite(rec["alpha"]) and rec["alpha"] > 0
    assert np.isfinite(rec["eps"]) and rec["eps"] > 0
    # round-trips through JSON unchanged (the on-disk ModelRec format)
    assert json.loads(json.dumps(rec)) == rec


def test_calibrate_flow_evp_regression_itchen(itchen):
    # The model spike (docs/product/lowflow/analysis.md §3) measured EVP 89.9
    # on this gauge; the production glue must clear the go/no-go bar decisively.
    q, prec, evap = itchen
    rec = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    assert rec["evp"] >= 85.0


def test_calibrate_flow_eps_matches_spec_formula(itchen):
    # eps = max(0.001, Q[Q>0].min()/10) — build_plan.md Stage 3
    q, prec, evap = itchen
    rec = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    expected = max(0.001, float(q[q > 0].min()) / 10)
    assert rec["eps"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# calibrate -> serialize -> rebuild -> simulate equivalence (the critical seam)
# ---------------------------------------------------------------------------

def test_flow_calibrate_serialize_rebuild_simulate_equivalence(itchen):
    q, prec, evap = itchen
    rec = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    roundtripped = json.loads(json.dumps(rec))

    origin = pd.Timestamp("2025-01-01")
    target = pd.date_range(origin + pd.Timedelta(days=1), periods=14, freq="D")
    mean_fresh, sig_fresh = R.simulate_path(rec, q, prec, evap, origin, target)
    mean_rt, sig_rt = R.simulate_path(roundtripped, q, prec, evap, origin, target)

    assert np.allclose(mean_fresh, mean_rt, atol=1e-9)
    assert np.allclose(sig_fresh, sig_rt, atol=1e-9)
    # outputs stay in log space for flow — no exponentiation inside simulate_path
    assert np.all(np.abs(mean_fresh) < 10)          # a log-flow, not raw m3/s, scale
    assert (sig_fresh > 0).all()
    assert sig_fresh[-1] >= sig_fresh[0]             # AR1 band widens with lead


def test_flow_seeded_forecast_anchors_to_log_origin(itchen):
    q, prec, evap = itchen
    rec = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    origin = pd.Timestamp("2025-01-01")
    window, mean, sig = R.seeded_forecast(rec, q, prec, evap, origin, horizon=14)
    assert len(window) == len(mean) == len(sig) == 14
    expected_log_origin = float(np.log(q.loc[origin] + rec["eps"]))
    # lead-1 forecast is close to the observed (log) origin level
    assert abs(mean[0] - expected_log_origin) < 1.0


# ---------------------------------------------------------------------------
# Zero-flow epsilon round-trip (winterbournes)
# ---------------------------------------------------------------------------

def test_zero_flow_days_calibrate_and_roundtrip(synthetic_winterbourne):
    q, prec, evap = synthetic_winterbourne
    assert (q == 0).sum() > 0                       # sanity: the fixture actually dries out

    rec = R.calibrate_flow("winterbourne1", q, prec, evap)
    assert rec["model_kind"] == "flow_2s"
    expected_eps = max(0.001, float(q[q > 0].min()) / 10)
    assert rec["eps"] == pytest.approx(expected_eps)
    assert np.isfinite(rec["eps"]) and rec["eps"] > 0

    roundtripped = json.loads(json.dumps(rec))
    assert roundtripped["eps"] == rec["eps"]

    origin = pd.Timestamp("2022-06-01")
    window, mean1, sig1 = R.seeded_forecast(rec, q, prec, evap, origin, horizon=14)
    window2, mean2, sig2 = R.seeded_forecast(roundtripped, q, prec, evap, origin, horizon=14)
    assert np.allclose(mean1, mean2, atol=1e-9)
    assert np.allclose(sig1, sig2, atol=1e-9)
    assert np.isfinite(mean1).all() and np.isfinite(sig1).all()


def test_all_zero_flow_series_does_not_crash_eps_calc():
    # Degenerate edge case: no positive readings at all -> eps falls back to
    # the 0.001 floor (Q[Q>0] would be empty, so .min()/10 is undefined).
    idx = pd.date_range("2020-01-01", "2020-12-31", freq="D")
    q = pd.Series(0.0, idx)
    prec = pd.Series(0.0, idx)
    evap = pd.Series(1.0, idx)
    q_norm = q.copy()
    positive = q_norm[q_norm > 0]
    eps = max(0.001, float(positive.min()) / 10) if len(positive) else 0.001
    assert eps == 0.001


# ---------------------------------------------------------------------------
# Non-nanosecond datetime index (the parquet-shard reality)
# ---------------------------------------------------------------------------

def test_calibrate_flow_survives_microsecond_index(itchen):
    # A parquet round-trip (data/features/flow_by_station shards) yields a
    # datetime64[us] index, on which pastas 1.14 silently produced a DEGENERATE
    # fit — all-NaN residuals, sigma/EVP NaN — while the identical values on a
    # ns index fit fine (found live in the Stage-4 gate run, 2026-07-14).
    # recharge._norm now coerces to ns; this pins it.
    q, prec, evap = itchen
    q_us = q.copy()
    q_us.index = pd.DatetimeIndex(q_us.index).as_unit("us")
    cutoff = pd.Timestamp("2021-06-15")
    rec = R.calibrate_flow("itchen_us_index", q_us[q_us.index <= cutoff],
                           prec, evap, train_max=cutoff)
    assert np.isfinite(rec["sigma"]) and rec["sigma"] > 0
    assert np.isfinite(rec["evp"]) and rec["evp"] >= 85.0


# ---------------------------------------------------------------------------
# Backward compatibility: a ModelRec dict WITHOUT model_kind loads as "gw"
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def synthetic_gw():
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


def test_calibrate_sets_model_kind_gw(synthetic_gw):
    head, prec, evap = synthetic_gw
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    assert rec["model_kind"] == "gw"


def test_legacy_rec_without_model_kind_loads_and_simulates_as_gw(synthetic_gw):
    head, prec, evap = synthetic_gw
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    legacy = dict(rec)
    del legacy["model_kind"]                         # simulate a pre-Stage-3 ModelRec
    assert "model_kind" not in legacy

    origin = pd.Timestamp("2022-06-01")
    window, mean, sig = R.seeded_forecast(rec, head, prec, evap, origin, horizon=14)
    window_l, mean_l, sig_l = R.seeded_forecast(legacy, head, prec, evap, origin, horizon=14)
    assert np.allclose(mean, mean_l)
    assert np.allclose(sig, sig_l)


def test_legacy_rec_single_recharge_stress_only(synthetic_gw):
    # A "gw" simulate_path rebuild must NOT add the quickflow stress — the
    # param vector for a legacy GW rec has no quickflow_* entries to match.
    head, prec, evap = synthetic_gw
    rec = R.calibrate("BH1", head, prec, evap, train_max=pd.Timestamp("2021-12-31"))
    assert not any(n.startswith("quickflow_") for n in rec["param_names"])

"""Tests for the Stage-4 per-gauge admission gate
(``src/forecast/pastas/flow_gate.py`` — ``docs/product/lowflow/build_plan.md``).

The full calibrated-hindcast path needs real pastas (via ``calibrate_flow``/
``simulate_path``), so this whole file SKIPS in the main env and RUNS under
the dedicated pastas venv, mirroring ``tests/test_pastas_flow.py``:
  .venv-pastas\\Scripts\\python.exe -m pytest tests/test_pastas_flow_gate.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pastas")            # skip in the main env

from src.forecast.pastas import flow_gate as G
from src.forecast.pastas import screen

FIXTURE = Path(__file__).parent / "fixtures" / "itchen_highbridge_2018_2026.csv"


# ---------------------------------------------------------------------------
# Pure / short-circuit behaviour — never calls pastas
# ---------------------------------------------------------------------------

def test_admit_gauge_empty_series_short_circuits():
    empty = pd.Series(dtype=float)
    out = G.admit_gauge("X", empty, empty, empty)
    assert out["gate_pass"] is False
    assert out["tier"] == "status_only"
    assert out["reason"] == "no_obs"


def test_admit_gauge_no_rain_short_circuits():
    idx = pd.date_range("2018-01-01", periods=2000, freq="D")
    q = pd.Series(np.linspace(1.0, 2.0, len(idx)), idx)
    empty = pd.Series(dtype=float)
    out = G.admit_gauge("X", q, empty, empty)
    assert out["gate_pass"] is False
    assert out["reason"] == "no_rain"


def test_admit_gauge_insufficient_origins_never_calls_pastas():
    # A 100-day record can't find MIN_TRAIN_ROWS obs before ANY low-flow-season
    # origin -> zero candidate origins -> the gate fails for lack of evaluable
    # origins before any calibrate_flow() call.
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    q = pd.Series(np.linspace(1.0, 1.1, len(idx)), idx)
    prec = pd.Series(0.0, idx)
    evap = pd.Series(1.0, idx)
    out = G.admit_gauge("X", q, prec, evap)
    assert out["gate_pass"] is False
    assert out["reason"].startswith("origins<")
    assert out["n_origins"] == 0
    assert out["origins"] == []


def test_candidate_origins_spans_multiple_years_and_low_flow_season():
    idx = pd.date_range("2015-01-01", "2022-12-31", freq="D")
    cands = G._candidate_origins(idx, min_train_rows=1200, window=14)
    assert len(cands) >= G.MIN_ORIGINS
    assert len({c.year for c in cands}) >= 3
    assert all(6 <= c.month <= 10 for c in cands)
    assert len(cands) <= G.ORIGIN_CAP


def test_candidate_origins_respects_train_row_floor():
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    assert G._candidate_origins(idx, min_train_rows=1200, window=14) == []


def test_recession_baseline_is_constant_rate_continuation():
    # memory_skill_test.py / validation_fit.py's definition: kk = median
    # 14-day log decline over the trailing window, projected FLAT forward at
    # that same rate from the last training observation. On a perfectly
    # constant-rate synthetic decline the baseline must reproduce it exactly.
    idx = pd.date_range("2018-01-01", periods=1300, freq="D")
    rate = 0.01
    logq = pd.Series(-rate * np.arange(1300), idx)
    base = G._recession_baseline(logq, window=14)
    assert base is not None
    expected = float(logq.iloc[-1]) - rate * np.arange(1, 15)
    assert np.allclose(base, expected, atol=1e-9)


def test_recession_baseline_none_when_too_few_pairs():
    idx = pd.date_range("2018-01-01", periods=10, freq="D")
    logq = pd.Series(np.linspace(0.0, -0.1, 10), idx)
    assert G._recession_baseline(logq, window=14) is None


def test_sigma_inflation_reused_from_screen_not_reimplemented():
    # build_plan.md Stage 4: "reuse/mirror the sigma-inflation mechanism" —
    # flow_gate imports screen's functions directly rather than a divergent
    # copy, so both gates behave identically at the same confidence target.
    assert G._inflation_factor is screen._inflation_factor
    assert G.gate_pass is screen.gate_pass


def test_floor_robust_rule():
    # The resolved 2026-07-14 escalation: tier-1 = robustness on the memory-only
    # leg, NOT the ceiling's beat-recession-by-20% bar. Parity skill (<=1.05),
    # covered, and band <= a fifth of the gauge's logQ range.
    assert G._floor_robust(1.006, 80.0, 0.156) is True     # live Itchen numbers
    assert G._floor_robust(0.901, 80.0, 0.122) is True     # live Test numbers
    # parity skill but bands too wide to lean on (live Mole / Medway floors)
    assert G._floor_robust(0.994, 80.0, 0.451) is False
    assert G._floor_robust(0.976, 80.0, 0.293) is False
    # worse than recession on the floor (live Horton / Dearne)
    assert G._floor_robust(1.246, 80.0, 0.203) is False
    assert G._floor_robust(1.319, 80.0, 0.297) is False
    # boundary + under-coverage + not-evaluable
    assert G._floor_robust(1.05, 80.0, 0.20) is True
    assert G._floor_robust(1.0, 40.0, 0.15) is False
    assert G._floor_robust(None, 80.0, 0.15) is False
    assert G._floor_robust(1.0, None, 0.15) is False
    assert G._floor_robust(1.0, 80.0, None) is False


# ---------------------------------------------------------------------------
# Full live-fit fixtures (build_plan.md Stage 4 unit tests: a no-skill series
# fails, a strong-memory series passes, the sigma-inflation mechanism
# honestly widens, the Itchen fixture passes the floor gate)
# ---------------------------------------------------------------------------

def _no_skill_regime_shift():
    """Random discrete level jumps (abstraction on/off), uncorrelated with
    rain/PET or anything else predictable — the naive recession baseline (a
    smooth local extrapolation) is already close to the best available
    forecast for this kind of series, so the two-pathway model should NOT
    beat it meaningfully."""
    idx = pd.date_range("2016-01-01", "2022-12-31", freq="D")
    n = len(idx)
    rng = np.random.default_rng(23)
    levels = np.log(rng.uniform(0.3, 3.0, size=400))
    dur = rng.integers(8, 25, size=400)
    logq = np.zeros(n)
    i = li = 0
    while i < n:
        d = min(int(dur[li % len(dur)]), n - i)
        logq[i:i + d] = levels[li % len(levels)] + rng.normal(0, 0.04, d)
        i += d
        li += 1
    q = pd.Series(np.exp(logq), idx, name="Flow_m3s")
    prec = pd.Series(rng.gamma(0.4, 3.5, n), idx)
    doy = idx.day_of_year.to_numpy()
    evap = pd.Series(np.clip(2.2 + 1.8 * np.sin(2 * np.pi * (doy - 80) / 365), 0.1, None), idx)
    return q, prec, evap


def _parity_skill_wide_bands():
    """Mean-reverting white noise in logQ: the fitted model's flat prediction
    matches the recession baseline within noise on BOTH legs (parity skill),
    but the honest 80% band is enormous relative to the series' range — the
    exact profile the floor's sharpness leg exists to catch: recession-parity
    skill you still can't lean on without a rain forecast."""
    idx = pd.date_range("2014-01-01", "2022-12-31", freq="D")
    n = len(idx)
    rng = np.random.default_rng(11)
    logq = np.log(1.0) + rng.normal(0, 0.35, n)          # iid, zero memory
    q = pd.Series(np.exp(logq), idx, name="Flow_m3s")
    prec = pd.Series(rng.gamma(0.4, 3.5, n), idx)
    doy = idx.day_of_year.to_numpy()
    evap = pd.Series(np.clip(2.2 + 1.8 * np.sin(2 * np.pi * (doy - 80) / 365), 0.1, None), idx)
    return q, prec, evap


def _strong_memory():
    """A real recharge-driven baseflow (slow convolution of net rain, seasonal
    ET) plus a tiny quickflow pulse response and small noise — a textbook
    chalk-stream-like memory signal the two-pathway model should resolve well
    beyond the fixed-rate recession baseline, even on climatological rain."""
    idx = pd.date_range("2013-01-01", "2022-12-31", freq="D")
    n = len(idx)
    doy = idx.day_of_year.to_numpy()
    rng = np.random.default_rng(7)
    prec = pd.Series(rng.gamma(0.4, 3.5, n), idx)
    evap = pd.Series(np.clip(2.2 + 1.8 * np.sin(2 * np.pi * (doy - 80) / 365), 0.1, None), idx)
    net = np.clip(prec.to_numpy() - 0.6 * evap.to_numpy(), 0, None)
    kslow = np.exp(-np.arange(220) / 55.0)
    kslow /= kslow.sum()
    baseflow = 0.4 + np.convolve(net, kslow)[:n] * 0.05
    kfast = np.exp(-np.arange(10) / 2.0)
    kfast /= kfast.sum()
    quickflow = np.convolve(prec.to_numpy(), kfast)[:n] * 0.002
    q_vals = np.clip(baseflow + quickflow, 0.001, None) * np.exp(rng.normal(0, 0.01, n))
    return pd.Series(q_vals, idx, name="Flow_m3s"), prec, evap


@pytest.fixture(scope="module")
def no_skill_gate():
    q, prec, evap = _no_skill_regime_shift()
    return G.admit_gauge("synthetic_no_skill", q, prec, evap)


@pytest.fixture(scope="module")
def strong_memory_gate():
    q, prec, evap = _strong_memory()
    return G.admit_gauge("synthetic_strong_memory", q, prec, evap)


@pytest.fixture(scope="module")
def itchen_gate():
    df = pd.read_csv(FIXTURE, comment="#", parse_dates=["date"]).set_index("date")
    return G.admit_gauge("itchen_highbridge", df["Flow_m3s"], df["Rain_mm"], df["PET_mm"])


@pytest.fixture(scope="module")
def parity_wide_gate():
    q, prec, evap = _parity_skill_wide_bands()
    return G.admit_gauge("synthetic_parity_wide", q, prec, evap)


def test_no_skill_series_fails_the_gate(no_skill_gate):
    out = no_skill_gate
    assert out["n_origins"] >= G.MIN_ORIGINS        # the fit itself is fine; skill is the point
    assert out["gate_pass"] is False
    assert out["tier"] == "status_only"
    assert out["rain_dependent"] is False
    assert out["ceiling"]["gate_pass"] is False
    assert out["floor"]["robust"] is False          # band far too wide anyway


def test_strong_memory_series_passes_the_gate(strong_memory_gate):
    out = strong_memory_gate
    assert out["n_origins"] >= G.MIN_ORIGINS
    assert out["gate_pass"] is True
    assert out["tier"] == "tier1"
    assert out["rain_dependent"] is False
    assert out["ceiling"]["gate_pass"] is True
    assert out["floor"]["robust"] is True


def test_parity_skill_wide_bands_is_rain_dependent_not_tier1(parity_wide_gate):
    # The floor's sharpness leg in action: recession-parity floor skill but a
    # band spanning ~0.4 of the range must NOT reach tier-1 — it publishes
    # rain_dependent (wider, with the caveat) instead.
    out = parity_wide_gate
    assert out["n_origins"] >= G.MIN_ORIGINS
    assert out["ceiling"]["gate_pass"] is True
    assert out["floor"]["skill_ratio"] <= G.FLOOR_MAX_SKILL_RATIO   # parity skill...
    assert out["floor"]["band_frac"] > G.FLOOR_MAX_BAND_FRAC        # ...but too wide
    assert out["floor"]["robust"] is False
    assert out["tier"] == "rain_dependent"
    assert out["rain_dependent"] is True


def test_itchen_fixture_is_tier1(itchen_gate):
    # build_plan.md Stage 4 acceptance (as resolved by the 2026-07-14
    # escalation): the committed Itchen @ Highbridge fixture passes the
    # ceiling gate AND is floor-robust — tier1, not merely rain-dependent
    # (analysis.md §5: Itchen is the strongest chalk gauge of the validation
    # 10; at Jun-Oct origins its memory-only floor holds recession parity
    # with a sharp band).
    assert itchen_gate["n_origins"] >= G.MIN_ORIGINS
    assert itchen_gate["n_years"] >= 3
    assert itchen_gate["ceiling"]["gate_pass"] is True
    assert itchen_gate["floor"]["robust"] is True
    assert itchen_gate["gate_pass"] is True
    assert itchen_gate["tier"] == "tier1"
    assert itchen_gate["rain_dependent"] is False


def test_itchen_sigma_inflation_widens_the_band_honestly(itchen_gate):
    floor = itchen_gate["floor"]
    assert floor["sigma_inflation"] >= 1.0          # NEVER narrows a band
    assert floor["cov14"] >= G.MIN_COVERAGE_PCT
    # a meaningfully under-confident base fit gets meaningfully widened
    assert floor["cov14"] >= G.TARGET_COVERAGE_PCT - 1.0

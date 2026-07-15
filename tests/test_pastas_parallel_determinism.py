"""Cross-process pastas fit determinism — the core empirical claim behind
parallelizing ``scripts/build_pastas_models.py`` and
``scripts/build_flow_models.py`` via ``ProcessPoolExecutor`` (the
``scripts/flow_fleet_scan.py`` Stage-5 pattern): given IDENTICAL inputs,
``R.calibrate`` / ``R.calibrate_flow`` must return the SAME ``ModelRec``
whether run in-process or inside a spawned worker. If pastas ever solved
non-deterministically across processes (a different BLAS/numba thread count,
optimizer seed, etc.), the parallel scripts would silently publish different
models depending on ``--workers`` — this test exists to catch and report
that divergence rather than hide it.

Real subprocess spawning is used throughout (unlike
tests/test_build_flow_models.py / tests/test_build_pastas_models.py, which
use an in-process fake-executor to test the ORCHESTRATION logic only) — the
question under test is specifically "does crossing a real process boundary
change the answer", which a mocked executor can never exercise.

Pastas lives in a dedicated venv, so this module SKIPS automatically in the
main environment and RUNS under the pastas venv:
  .venv-pastas\\Scripts\\python.exe -m pytest tests/test_pastas_parallel_determinism.py
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pastas")            # skip in the main env

from src.forecast.pastas import recharge as R

FIXTURE = Path(__file__).parent / "fixtures" / "itchen_highbridge_2018_2026.csv"


def _strip_fitted_on(rec: dict) -> dict:
    # fitted_on is date.today() — same-day in both calls under normal test
    # run times, but stripped so a midnight rollover can never cause a false
    # failure of a check that is otherwise about numerical determinism.
    return {k: v for k, v in rec.items() if k != "fitted_on"}


@pytest.fixture(scope="module")
def itchen():
    """Real R. Itchen @ Highbridge flow + its 3-gauge rain + PET (the same
    fixture tests/test_pastas_flow.py uses)."""
    df = pd.read_csv(FIXTURE, comment="#", parse_dates=["date"]).set_index("date")
    return df["Flow_m3s"], df["Rain_mm"], df["PET_mm"]


@pytest.fixture(scope="module")
def synthetic_winterbourne():
    """Same construction as tests/test_pastas_flow.py's fixture of the same
    name — a second, independent flow station for the 2-worker check."""
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


@pytest.fixture(scope="module")
def synthetic_gw_stations():
    """3 synthetic GW head series — the GW equivalent of the real Itchen flow
    fixture (no small multi-borehole real fixture exists; this reuses the
    exact synthetic-borehole construction tests/test_pastas_recharge.py's
    ``synthetic`` fixture already established, one per station with a
    different seed/decay so the 3 fits are genuinely distinct problems)."""
    idx = pd.date_range("2018-01-01", "2022-12-31", freq="D")
    doy = idx.day_of_year.to_numpy()
    stations = {}
    for i, seed in enumerate([0, 1, 2]):
        rng = np.random.default_rng(seed)
        prec = pd.Series(rng.gamma(0.5, 4.0, len(idx)), idx)
        evap = pd.Series(np.clip(2 + 1.5 * np.sin(2 * np.pi * doy / 365), 0, None), idx)
        net = np.clip(prec.values - 0.7 * evap.values, 0, None)
        decay = 15.0 + 5.0 * i
        k = np.exp(-np.arange(60) / decay); k /= k.sum()
        h = ((30 + i * 5) + np.convolve(net, k)[:len(idx)] * 0.05
             + rng.normal(0, 0.02, len(idx)))
        stations[f"BH{i}"] = (pd.Series(h, idx), prec, evap)
    return stations


# ---------------------------------------------------------------------------
# calibrate_flow: real Itchen fixture, in-process vs a spawned worker
# ---------------------------------------------------------------------------

def test_calibrate_flow_identical_in_process_vs_subprocess(itchen):
    q, prec, evap = itchen
    in_process = R.calibrate_flow("itchen_highbridge", q, prec, evap)
    with ProcessPoolExecutor(max_workers=1) as ex:
        subprocess_rec = ex.submit(R.calibrate_flow, "itchen_highbridge",
                                   q, prec, evap).result()

    a, b = _strip_fitted_on(in_process), _strip_fitted_on(subprocess_rec)
    assert a == b, (
        "calibrate_flow diverged across a process boundary — pastas may be "
        f"non-deterministic here. in-process={a}  subprocess={b}"
    )


def test_calibrate_flow_two_stations_parallel_matches_serial(itchen, synthetic_winterbourne):
    # 2 distinct stations fit CONCURRENTLY in a 2-worker pool vs sequentially
    # in-process — mirrors build_flow_models.py's real dispatch shape.
    stations = {"itchen_highbridge": itchen, "synthetic_wb": synthetic_winterbourne}

    serial = {sid: R.calibrate_flow(sid, *args) for sid, args in stations.items()}

    with ProcessPoolExecutor(max_workers=2) as ex:
        futures = {sid: ex.submit(R.calibrate_flow, sid, *args)
                  for sid, args in stations.items()}
        parallel = {sid: fut.result() for sid, fut in futures.items()}

    for sid in stations:
        a, b = _strip_fitted_on(serial[sid]), _strip_fitted_on(parallel[sid])
        assert a == b, f"{sid}: calibrate_flow diverged under 2-worker concurrency"


# ---------------------------------------------------------------------------
# calibrate (GW): 3 synthetic boreholes, in-process serial vs a 2-worker pool
# ---------------------------------------------------------------------------

def test_calibrate_gw_three_stations_parallel_matches_serial(synthetic_gw_stations):
    serial = {sid: R.calibrate(sid, *args) for sid, args in synthetic_gw_stations.items()}

    with ProcessPoolExecutor(max_workers=2) as ex:
        futures = {sid: ex.submit(R.calibrate, sid, *args)
                  for sid, args in synthetic_gw_stations.items()}
        parallel = {sid: fut.result() for sid, fut in futures.items()}

    for sid in synthetic_gw_stations:
        a, b = _strip_fitted_on(serial[sid]), _strip_fitted_on(parallel[sid])
        assert a == b, f"{sid}: calibrate diverged under 2-worker concurrency"

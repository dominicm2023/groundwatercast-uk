"""Tests for scripts/build_flow_models.py — Q95 computation, graceful skip
(build_plan.md Stage 6), and the Stage-5-pattern ProcessPoolExecutor
parallelism added on top. No pastas import needed: calibrate_flow itself is
never exercised here (that's tests/test_pastas_flow.py, in .venv-pastas, and
tests/test_pastas_parallel_determinism.py for the cross-process fit-equality
check) — everything below mocks ``R.calibrate_flow``/``load_gauge_series``,
so it runs fine in the main env (src.forecast.pastas.recharge imports pastas
lazily, per its own module docstring).
"""
from __future__ import annotations

import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import scripts.build_flow_models as M
from scripts.build_flow_models import (calibrate_one_flow_gauge, compute_q95,
                                       parse_args, run)


# ---------------------------------------------------------------------------
# compute_q95: 5th percentile of the FULL daily flow record
# ---------------------------------------------------------------------------

def test_q95_matches_numpy_5th_percentile():
    rng = np.random.default_rng(0)
    q = pd.Series(rng.gamma(2.0, 0.3, size=2000))
    assert compute_q95(q) == pytest.approx(float(np.quantile(q, 0.05)))


def test_q95_on_all_zero_winterbourne_is_zero():
    q = pd.Series(0.0, index=pd.date_range("2020-01-01", periods=400))
    assert compute_q95(q) == 0.0


def test_q95_on_mostly_zero_winterbourne_is_zero():
    # A gauge that dries out most of the year: even the 5th percentile of
    # daily flow sits at zero (only the top ~5% of days carry any flow).
    idx = pd.date_range("2020-01-01", periods=1000)
    q = pd.Series(0.0, index=idx)
    q.iloc[:20] = [1.0, 2.0, 3.0] + [0.5] * 17    # ~2% of days flowing
    assert compute_q95(q) == 0.0


def test_q95_empty_series_is_zero_not_nan():
    assert compute_q95(pd.Series(dtype=float)) == 0.0


def test_q95_all_nan_series_is_zero_not_nan():
    q = pd.Series([np.nan, np.nan, np.nan])
    assert compute_q95(q) == 0.0


def test_q95_perennial_gauge_is_positive():
    # A perennial chalk stream never dries out — Q95 should sit well above 0.
    idx = pd.date_range("2018-01-01", periods=2000)
    q = pd.Series(1.0 + 0.3 * np.sin(np.arange(2000) / 50.0), index=idx)
    assert compute_q95(q) > 0.5


# ---------------------------------------------------------------------------
# run(): graceful skip when the pilot CSV (or links/catalogue) is absent
# ---------------------------------------------------------------------------

_MIN_CFG = {"forecast": {"ensemble": {"flow": {
    "enabled": True, "models_cache": "flow_models.json",
}}}, "download": {"raw_root": "data/raw"}}


def test_run_missing_pilot_csv_exits_zero(tmp_path, capsys):
    args = parse_args([
        "--pilot", str(tmp_path / "absent_pilot.csv"),
        "--links", str(tmp_path / "flow_links.csv"),
        "--catalogue", str(tmp_path / "flow_catalogue.csv"),
    ])
    assert run(args, cfg=_MIN_CFG) == 0
    assert "not found" in capsys.readouterr().out


def test_run_missing_links_or_catalogue_exits_zero(tmp_path, capsys):
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": ["g1"], "station_name": ["G1"],
                 "floor_skill": [0.5]}).to_csv(pilot_path, index=False)
    args = parse_args([
        "--pilot", str(pilot_path),
        "--links", str(tmp_path / "absent_links.csv"),
        "--catalogue", str(tmp_path / "absent_catalogue.csv"),
    ])
    assert run(args, cfg=_MIN_CFG) == 0
    assert "not found" in capsys.readouterr().out


def test_run_empty_pilot_csv_exits_zero(tmp_path, capsys):
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame(columns=["gauge_id", "station_name", "floor_skill"]).to_csv(
        pilot_path, index=False)
    links_path = tmp_path / "flow_links.csv"
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"GaugeID": [], "FlowMeasureID": []}).to_csv(links_path, index=False)
    pd.DataFrame({"station_id": [], "station_name": []}).to_csv(cat_path, index=False)
    args = parse_args(["--pilot", str(pilot_path), "--links", str(links_path),
                       "--catalogue", str(cat_path)])
    assert run(args, cfg=_MIN_CFG) == 0
    assert "empty" in capsys.readouterr().out


def test_run_disabled_via_config_exits_zero(tmp_path, capsys):
    args = parse_args(["--pilot", str(tmp_path / "flow_pilot.csv")])
    cfg = {"forecast": {"ensemble": {"flow": {"enabled": False}}}}
    assert run(args, cfg=cfg) == 0
    assert "enabled = false" in capsys.readouterr().out


def test_default_workers_capped_for_pet_rate_limit():
    # build_pastas_models has no network call in its hot loop and matches the
    # fleet scan's max(2, cpu-2); build_flow_models hits fetch_station_pet
    # (Open-Meteo, 429-prone — proven live in the Stage-5 fleet scan) so its
    # default must sit at or below that.
    assert M._DEFAULT_WORKERS <= 3


# ---------------------------------------------------------------------------
# calibrate_one_flow_gauge — worker-level failure isolation (mirrors
# tests/test_flow_fleet_scan.py's scan_one_gauge coverage)
# ---------------------------------------------------------------------------

def test_calibrate_one_flow_gauge_load_error_degrades_to_skip():
    with patch("scripts.build_flow_models.load_gauge_series",
              side_effect=RuntimeError("network exploded")):
        row = calibrate_one_flow_gauge("G1")
    assert row["rec"] is None
    assert "network exploded" in row["skip_reason"]
    assert row["gauge_id"] == "G1"


def test_calibrate_one_flow_gauge_no_data_when_load_returns_none():
    with patch("scripts.build_flow_models.load_gauge_series", return_value=None):
        row = calibrate_one_flow_gauge("G1")
    assert row["rec"] is None
    assert row["skip_reason"] == "no_data"


def test_calibrate_one_flow_gauge_calibration_error_degrades_to_skip(monkeypatch):
    # _W_FCFG is worker-global state normally populated by _worker_init; set
    # it directly since this test calls the worker function standalone.
    monkeypatch.setattr(M, "_W_FCFG", {})
    idx = pd.date_range("2018-01-01", periods=10, freq="D")
    q = pd.Series(1.0, idx); prec = pd.Series(0.0, idx); evap = pd.Series(1.0, idx)
    with patch("scripts.build_flow_models.load_gauge_series",
              return_value=(q, prec, evap)), \
         patch("scripts.build_flow_models.R.calibrate_flow",
              side_effect=RuntimeError("solver blew up")):
        row = calibrate_one_flow_gauge("G1")
    assert row["rec"] is None
    assert "calibration error" in row["skip_reason"]
    assert "solver blew up" in row["skip_reason"]


def test_calibrate_one_flow_gauge_success_records_q95(monkeypatch):
    monkeypatch.setattr(M, "_W_FCFG", {})
    idx = pd.date_range("2018-01-01", periods=10, freq="D")
    q = pd.Series(1.0, idx); prec = pd.Series(0.0, idx); evap = pd.Series(1.0, idx)
    fake_rec = {"station_id": "G1", "n_obs": 10, "evp": 90.0}
    with patch("scripts.build_flow_models.load_gauge_series",
              return_value=(q, prec, evap)), \
         patch("scripts.build_flow_models.R.calibrate_flow",
              return_value=dict(fake_rec)):
        row = calibrate_one_flow_gauge("G1")
    assert row["skip_reason"] is None
    assert row["rec"]["q95_m3s"] == compute_q95(q)


def test_calibrate_one_flow_gauge_outer_guard_survives_totally_broken_inner():
    # Belt-and-braces: even an exception raised BEFORE load_gauge_series is
    # reached (e.g. corrupt worker-global state) degrades to a skip entry,
    # matching flow_fleet_scan.scan_one_gauge's outer-guard discipline.
    with patch("scripts.build_flow_models._calibrate_one_gauge_inner",
              side_effect=ValueError("boom")):
        row = calibrate_one_flow_gauge("G1")
    assert row["rec"] is None
    assert "worker error" in row["skip_reason"]
    assert "boom" in row["skip_reason"]


# ---------------------------------------------------------------------------
# run(): the ProcessPoolExecutor orchestration — BrokenProcessPool re-raise,
# per-future exception isolation, and serial-vs-parallel equivalence.
#
# _ImmediateExecutor stands in for ProcessPoolExecutor: it runs each
# submission synchronously via a REAL concurrent.futures.Future (so
# concurrent.futures.as_completed behaves exactly as it would against a real
# pool), without spawning a subprocess — the worker function under test
# (calibrate_one_flow_gauge) is mocked anyway, so nothing here needs pastas
# or real multiprocessing to exercise run()'s own dispatch/fallback logic.
# ---------------------------------------------------------------------------

class _ImmediateExecutor:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def submit(self, fn, *args):
        fut = cf.Future()
        try:
            result = fn(*args)
        except Exception as exc:                                # noqa: BLE001
            fut.set_exception(exc)
        else:
            fut.set_result(result)
        return fut


def _pilot_and_links(tmp_path, gauge_ids):
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": gauge_ids, "station_name": gauge_ids}).to_csv(
        pilot_path, index=False)
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({"GaugeID": gauge_ids,
                 "FlowMeasureID": [f"{g}-flow" for g in gauge_ids]}).to_csv(
        links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": gauge_ids, "station_name": gauge_ids,
                 "lat": [51.0] * len(gauge_ids), "lon": [-1.0] * len(gauge_ids)}).to_csv(
        cat_path, index=False)
    return pilot_path, links_path, cat_path


def test_run_broken_process_pool_is_not_swallowed_as_a_skip_row(tmp_path, monkeypatch):
    gauge_ids = ["G1", "G2", "G3"]
    pilot_path, links_path, cat_path = _pilot_and_links(tmp_path, gauge_ids)
    monkeypatch.setattr(M, "ROOT", tmp_path)   # so out.relative_to(ROOT) resolves

    calls: list[str] = []

    def fake_calibrate(gauge_id: str) -> dict:
        calls.append(gauge_id)
        if gauge_id == "G2" and calls.count("G2") == 1:
            # First attempt (inside the "pool"): simulate the pool breaking.
            raise BrokenProcessPool("pool died")
        return {"gauge_id": gauge_id, "name": gauge_id,
               "rec": {"station_id": gauge_id, "n_obs": 1, "evp": 50.0, "q95_m3s": 0.0},
               "skip_reason": None}

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(M, "calibrate_one_flow_gauge", fake_calibrate)
    monkeypatch.setattr(M, "_worker_init", lambda *a, **kw: None)

    cfg = {"forecast": {"ensemble": {"flow": {
        "enabled": True, "models_cache": "flow_models.json",
    }}}, "download": {"raw_root": "data/raw"}}
    args = parse_args(["--pilot", str(pilot_path), "--links", str(links_path),
                       "--catalogue", str(cat_path), "--workers", "2"])
    rc = run(args, cfg=cfg)
    assert rc == 0

    # G2 must have been retried (serial fallback) after the break, not
    # recorded as a "future exception"/skip row straight from the pool.
    assert calls.count("G2") == 2
    store = M.R.load_models(tmp_path / "flow_models.json")
    assert set(store) == set(gauge_ids)          # nothing lost to the break


def test_run_non_broken_future_exception_isolated_to_one_gauge(tmp_path, monkeypatch):
    gauge_ids = ["G1", "G2", "G3"]
    pilot_path, links_path, cat_path = _pilot_and_links(tmp_path, gauge_ids)
    monkeypatch.setattr(M, "ROOT", tmp_path)   # so out.relative_to(ROOT) resolves

    def fake_calibrate(gauge_id: str) -> dict:
        if gauge_id == "G2":
            raise RuntimeError("ordinary worker crash")
        return {"gauge_id": gauge_id, "name": gauge_id,
               "rec": {"station_id": gauge_id, "n_obs": 1, "evp": 50.0, "q95_m3s": 0.0},
               "skip_reason": None}

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(M, "calibrate_one_flow_gauge", fake_calibrate)
    monkeypatch.setattr(M, "_worker_init", lambda *a, **kw: None)

    cfg = {"forecast": {"ensemble": {"flow": {
        "enabled": True, "models_cache": "flow_models.json",
    }}}, "download": {"raw_root": "data/raw"}}
    args = parse_args(["--pilot", str(pilot_path), "--links", str(links_path),
                       "--catalogue", str(cat_path), "--workers", "2"])
    rc = run(args, cfg=cfg)
    assert rc == 0

    store = M.R.load_models(tmp_path / "flow_models.json")
    assert set(store) == {"G1", "G3"}             # only G2 dropped
    assert "G2" not in store                      # the batch was not aborted


def test_run_serial_vs_parallel_produce_identical_model_store(tmp_path, monkeypatch):
    gauge_ids = ["G1", "G2", "G3"]
    pilot_path, links_path, cat_path = _pilot_and_links(tmp_path, gauge_ids)
    monkeypatch.setattr(M, "ROOT", tmp_path)   # so out.relative_to(ROOT) resolves
    idx = pd.date_range("2018-01-01", periods=20, freq="D")

    def fake_load(gauge_id, *a, **kw):
        return (pd.Series(1.0, idx), pd.Series(0.0, idx), pd.Series(1.0, idx))

    def fake_calibrate_flow(gauge_id, q, prec, evap, **kw):
        return {"station_id": gauge_id, "n_obs": len(q), "evp": 42.0,
               "params": [1.0, 2.0]}

    monkeypatch.setattr(M, "load_gauge_series", fake_load)
    monkeypatch.setattr(M.R, "calibrate_flow", fake_calibrate_flow)

    def _cfg(models_cache: str) -> dict:
        return {"forecast": {"ensemble": {"flow": {
            "enabled": True, "models_cache": models_cache,
        }}}, "download": {"raw_root": "data/raw"}}

    args_serial = parse_args(["--pilot", str(pilot_path), "--links", str(links_path),
                              "--catalogue", str(cat_path), "--workers", "1"])
    assert run(args_serial, cfg=_cfg("serial.json")) == 0

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    args_parallel = parse_args(["--pilot", str(pilot_path), "--links", str(links_path),
                                "--catalogue", str(cat_path), "--workers", "3"])
    assert run(args_parallel, cfg=_cfg("parallel.json")) == 0

    serial_store = M.R.load_models(tmp_path / "serial.json")
    parallel_store = M.R.load_models(tmp_path / "parallel.json")
    assert serial_store == parallel_store
    assert set(serial_store) == set(gauge_ids)

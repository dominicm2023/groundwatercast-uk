"""Tests for scripts/build_pastas_models.py — the Stage-5-pattern
ProcessPoolExecutor parallelism added on top of the borehole calibration
loop. No pastas import needed: ``R.calibrate``/``screen.leakage_safe_hindcast``
are always mocked here (real fits are covered by
tests/test_pastas_recharge.py and tests/test_pastas_parallel_determinism.py,
both in .venv-pastas) — every module this script imports lazy-imports pastas
per-function (see src/forecast/pastas/recharge.py's own docstring), so this
file runs fine in the main env.
"""
from __future__ import annotations

import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import patch

import pandas as pd
import pytest

import scripts.build_pastas_models as M
from scripts.build_pastas_models import calibrate_one_borehole, parse_args, run

MIN_ROWS = M.MIN_ROWS
MIN_ROWS_FAN = M.MIN_ROWS_FAN


def _joined_csv(tmp_path, sids_and_lengths: dict[str, int]) -> "pd.DataFrame":
    rows = []
    for sid, n in sids_and_lengths.items():
        idx = pd.date_range("2010-01-01", periods=n, freq="D")
        for d in idx:
            rows.append({"date": d, "station_id": sid, "GW_Level": 10.0,
                        "Rainfall": 1.0})
    df = pd.DataFrame(rows).set_index("date")
    path = tmp_path / "joined.csv"
    df.to_csv(path)
    return path


def _catalogue_csv(tmp_path, sids: list[str]):
    path = tmp_path / "catalogue.csv"
    pd.DataFrame({"station_id": sids, "measure_type": ["groundwater"] * len(sids)}).to_csv(
        path, index=False)
    return path


_PCFG = {"rfunc": "Gamma", "recharge": "FlexModel", "models_cache": "models.json",
         "scope": "fleet", "enabled": True,
         "short_record": {"enabled": True, "gate": {}}}


def _cfg() -> dict:
    return {"forecast": {"ensemble": {"pastas": dict(_PCFG)}},
           "download": {"raw_root": "data/raw"}}


# ---------------------------------------------------------------------------
# run(): config-driven skip / nothing-in-scope paths
# ---------------------------------------------------------------------------

def test_run_disabled_via_config_exits_zero(capsys):
    args = parse_args([])
    cfg = {"forecast": {"ensemble": {"pastas": {"enabled": False}}}}
    assert run(args, cfg=cfg) == 0
    assert "enabled = false" in capsys.readouterr().out


def test_run_nothing_in_scope_exits_zero(capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(), ids=[]) == 0
    assert "Nothing in scope" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# calibrate_one_borehole — worker-level failure isolation (mirrors
# tests/test_flow_fleet_scan.py's scan_one_gauge coverage)
# ---------------------------------------------------------------------------

def test_calibrate_one_borehole_insufficient_history(tmp_path, monkeypatch):
    joined_path = _joined_csv(tmp_path, {"BH1": 10})   # far below MIN_ROWS_FAN
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert "insufficient history" in row["skip_reason"]


def test_calibrate_one_borehole_not_in_catalogue(tmp_path):
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS_FAN + 10})
    cat_path = _catalogue_csv(tmp_path, ["OTHER"])     # BH1 missing from catalogue
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert "insufficient history" in row["skip_reason"]


def test_calibrate_one_borehole_no_pet_cache(tmp_path):
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS_FAN + 10})
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    with patch("scripts.build_pastas_models.load_pet", return_value=None):
        row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert row["skip_reason"] == "no PET cache"


def test_calibrate_one_borehole_short_record_no_gauge_rainfall_skips(tmp_path):
    # Below MIN_ROWS (short-record tier) but the joined-fallback Rainfall
    # column is all that's available (gauge_rainfall_for -> empty): the
    # short-record tier is gauge-rainfall-only, never joined-fallback.
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS_FAN + 10})
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    evap = pd.Series(1.0, pd.date_range("2010-01-01", periods=MIN_ROWS_FAN + 10))
    with patch("scripts.build_pastas_models.load_pet", return_value=evap), \
         patch("scripts.build_pastas_models.gauge_rainfall_for",
              return_value=pd.Series(dtype="float64")):
        row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert row["skip_reason"] == "short-record: no gauge rainfall"


def test_calibrate_one_borehole_short_record_gate_fail_skips(tmp_path):
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS_FAN + 10})
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    idx = pd.date_range("2010-01-01", periods=MIN_ROWS_FAN + 10)
    evap = pd.Series(1.0, idx)
    rain = pd.Series(1.0, idx)
    with patch("scripts.build_pastas_models.load_pet", return_value=evap), \
         patch("scripts.build_pastas_models.gauge_rainfall_for", return_value=rain), \
         patch("scripts.build_pastas_models.screen.leakage_safe_hindcast",
              return_value={"gate_pass": False, "reason": "coverage_too_low"}):
        row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert row["skip_reason"] == "short-record gate fail: coverage_too_low"


def test_calibrate_one_borehole_calibration_error_degrades_to_skip(tmp_path):
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS + 10})   # full record
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    idx = pd.date_range("2010-01-01", periods=MIN_ROWS + 10)
    evap = pd.Series(1.0, idx)
    rain = pd.Series(1.0, idx)
    with patch("scripts.build_pastas_models.load_pet", return_value=evap), \
         patch("scripts.build_pastas_models.gauge_rainfall_for", return_value=rain), \
         patch("scripts.build_pastas_models.R.calibrate",
              side_effect=RuntimeError("solver blew up")):
        row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert "calibration error" in row["skip_reason"]
    assert "solver blew up" in row["skip_reason"]


def test_calibrate_one_borehole_full_record_success(tmp_path):
    joined_path = _joined_csv(tmp_path, {"BH1": MIN_ROWS + 10})   # full record
    cat_path = _catalogue_csv(tmp_path, ["BH1"])
    M._worker_init(str(joined_path), str(cat_path), None, "data/raw", _PCFG, {})
    idx = pd.date_range("2010-01-01", periods=MIN_ROWS + 10)
    evap = pd.Series(1.0, idx)
    rain = pd.Series(1.0, idx)
    fake_rec = {"station_id": "BH1", "n_obs": MIN_ROWS + 10, "evp": 80.0,
               "sigma": 0.1, "alpha": 50.0, "precip_source": "gauge",
               "noise_qa": {"passes": True}}
    with patch("scripts.build_pastas_models.load_pet", return_value=evap), \
         patch("scripts.build_pastas_models.gauge_rainfall_for", return_value=rain), \
         patch("scripts.build_pastas_models.R.calibrate", return_value=dict(fake_rec)):
        row = calibrate_one_borehole("BH1")
    assert row["skip_reason"] is None
    assert row["rec"]["short_record"] is False
    assert "hindcast" not in row["rec"]


def test_calibrate_one_borehole_outer_guard_survives_totally_broken_inner():
    with patch("scripts.build_pastas_models._calibrate_one_inner",
              side_effect=ValueError("boom")):
        row = calibrate_one_borehole("BH1")
    assert row["rec"] is None
    assert "worker error" in row["skip_reason"]
    assert "boom" in row["skip_reason"]


# ---------------------------------------------------------------------------
# run(): the ProcessPoolExecutor orchestration — BrokenProcessPool re-raise,
# per-future exception isolation, and serial-vs-parallel equivalence.
#
# _ImmediateExecutor stands in for ProcessPoolExecutor (see
# tests/test_build_flow_models.py for the identical pattern and rationale).
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


def test_run_broken_process_pool_is_not_swallowed_as_a_skip_row(tmp_path, monkeypatch):
    sids = ["BH1", "BH2", "BH3"]
    monkeypatch.setattr(M, "ROOT", tmp_path)   # so out.relative_to(ROOT) resolves

    calls: list[str] = []

    def fake_calibrate(sid: str) -> dict:
        calls.append(sid)
        if sid == "BH2" and calls.count("BH2") == 1:
            raise BrokenProcessPool("pool died")
        return {"sid": sid, "rec": {"station_id": sid, "n_obs": 1, "evp": 50.0,
                                    "sigma": 0.1, "alpha": 10.0,
                                    "precip_source": "gauge"},
               "skip_reason": None}

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(M, "calibrate_one_borehole", fake_calibrate)
    monkeypatch.setattr(M, "_worker_init", lambda *a, **kw: None)

    args = parse_args(["--workers", "2"])
    cfg = _cfg()
    cfg["forecast"]["ensemble"]["pastas"]["models_cache"] = "models.json"
    rc = run(args, cfg=cfg, ids=sids)
    assert rc == 0

    assert calls.count("BH2") == 2
    store = M.R.load_models(tmp_path / "models.json")
    assert set(store) == set(sids)


def test_run_non_broken_future_exception_isolated_to_one_borehole(tmp_path, monkeypatch):
    sids = ["BH1", "BH2", "BH3"]
    monkeypatch.setattr(M, "ROOT", tmp_path)

    def fake_calibrate(sid: str) -> dict:
        if sid == "BH2":
            raise RuntimeError("ordinary worker crash")
        return {"sid": sid, "rec": {"station_id": sid, "n_obs": 1, "evp": 50.0,
                                    "sigma": 0.1, "alpha": 10.0,
                                    "precip_source": "gauge"},
               "skip_reason": None}

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    monkeypatch.setattr(M, "calibrate_one_borehole", fake_calibrate)
    monkeypatch.setattr(M, "_worker_init", lambda *a, **kw: None)

    args = parse_args(["--workers", "2"])
    cfg = _cfg()
    cfg["forecast"]["ensemble"]["pastas"]["models_cache"] = "models.json"
    rc = run(args, cfg=cfg, ids=sids)
    assert rc == 0

    store = M.R.load_models(tmp_path / "models.json")
    assert set(store) == {"BH1", "BH3"}
    assert "BH2" not in store


def test_run_serial_vs_parallel_produce_identical_model_store(tmp_path, monkeypatch):
    sids = ["BH1", "BH2", "BH3"]
    monkeypatch.setattr(M, "ROOT", tmp_path)
    joined_path = _joined_csv(tmp_path, {sid: MIN_ROWS + 10 for sid in sids})
    cat_path = _catalogue_csv(tmp_path, sids)
    idx = pd.date_range("2010-01-01", periods=MIN_ROWS + 10)

    def fake_calibrate(sid, head, prec, evap, **kw):
        return {"station_id": sid, "n_obs": len(head), "evp": 77.0,
               "sigma": 0.2, "alpha": 33.0, "precip_source": kw.get("precip_source"),
               "noise_qa": {"passes": True}}

    monkeypatch.setattr(M, "gauge_rainfall_for", lambda *a, **kw: pd.Series(1.0, idx))
    monkeypatch.setattr(M, "load_pet", lambda sid: pd.Series(1.0, idx))
    monkeypatch.setattr(M.R, "calibrate", fake_calibrate)

    cfg = _cfg()
    cfg["forecast"]["ensemble"]["pastas"]["models_cache"] = "serial.json"
    args_serial = parse_args(["--workers", "1"])
    assert run(args_serial, cfg=cfg, ids=sids,
              joined_path=joined_path, catalogue_path=cat_path) == 0

    monkeypatch.setattr(M, "ProcessPoolExecutor", _ImmediateExecutor)
    cfg2 = _cfg()
    cfg2["forecast"]["ensemble"]["pastas"]["models_cache"] = "parallel.json"
    args_parallel = parse_args(["--workers", "3"])
    assert run(args_parallel, cfg=cfg2, ids=sids,
              joined_path=joined_path, catalogue_path=cat_path) == 0

    serial_store = M.R.load_models(tmp_path / "serial.json")
    parallel_store = M.R.load_models(tmp_path / "parallel.json")
    assert serial_store == parallel_store
    assert set(serial_store) == set(sids)

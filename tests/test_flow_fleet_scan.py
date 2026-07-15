"""Tests for the Stage-5 fleet scan (``scripts/flow_fleet_scan.py`` —
``docs/product/lowflow/build_plan.md``).

Fixture-only: no live HTTP/EA calls, no pastas fit. Covers exactly the two
things the build plan calls out as mandatory: the resumability (skip-
already-done) logic and the row schema — both pure and importable in the
main env, since ``scan_one_gauge``'s only pastas dependency
(``src.forecast.pastas.flow_gate.admit_gauge``) is lazily imported inside
``recharge.py`` and is never reached by these tests.
"""
from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from scripts import flow_fleet_scan as S


# ---------------------------------------------------------------------------
# load_done_gauge_ids — the skip-already-done set
# ---------------------------------------------------------------------------

def test_load_done_gauge_ids_missing_file_is_empty(tmp_path):
    assert S.load_done_gauge_ids(tmp_path / "nope.csv") == set()


def test_load_done_gauge_ids_empty_file_is_empty(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("")
    assert S.load_done_gauge_ids(p) == set()


def test_load_done_gauge_ids_reads_existing_rows(tmp_path):
    p = tmp_path / "scan.csv"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "no_data"))
        w.writerow(S._empty_row("G2", "Beta", "no_data"))
    assert S.load_done_gauge_ids(p) == {"G1", "G2"}


def test_load_done_gauge_ids_corrupt_file_degrades_to_empty(tmp_path):
    # A killed run mid-write-of-a-line: pandas can't parse it, but a resume
    # must never crash — worst case is a bit of re-work, never a failure.
    p = tmp_path / "corrupt.csv"
    p.write_text("gauge_id,station_name\nG1,Alpha\n\"unterminated")
    assert S.load_done_gauge_ids(p) == set()


def test_load_done_gauge_ids_ignores_nan_gauge_id(tmp_path):
    p = tmp_path / "scan.csv"
    pd.DataFrame({"gauge_id": ["G1", None], "other": [1, 2]}).to_csv(p, index=False)
    assert S.load_done_gauge_ids(p) == {"G1"}


def test_run_skips_gauges_already_in_out_csv(tmp_path):
    # End-to-end resumability check on run(): given an existing CSV with one
    # gauge_id already done, only the remaining gauge is scanned this session.
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({
        "GaugeID": ["G1", "G2"],
        "FlowMeasureID": ["G1-flow-m-86400-m3s-qualified",
                          "G2-flow-m-86400-m3s-qualified"],
    }).to_csv(links_path, index=False)

    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": ["G1", "G2"], "station_name": ["Alpha", "Beta"],
                  "lat": [51.0, 52.0], "lon": [-1.0, -2.0]}).to_csv(cat_path, index=False)

    out_path = tmp_path / "scan.csv"
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "no_data"))

    log_path = tmp_path / "scan.log"
    lock_path = tmp_path / "scan.lock"
    seen: list[str] = []

    def fake_scan(gauge_id: str) -> dict:
        seen.append(gauge_id)
        return S._empty_row(gauge_id, gauge_id, "no_data")

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(out_path), "--log", str(log_path),
        "--lock", str(lock_path), "--workers", "1",
    ])
    with patch("scripts.flow_fleet_scan.scan_one_gauge", side_effect=fake_scan), \
         patch("scripts.flow_fleet_scan._worker_init", return_value=None):
        rc = S.run(args)
    assert rc == 0
    assert seen == ["G2"]                 # G1 skipped, already done
    assert not lock_path.exists()         # released on the way out

    result = pd.read_csv(out_path, dtype={"gauge_id": str})
    assert set(result["gauge_id"]) == {"G1", "G2"}
    assert len(result) == 2               # no duplicate row for G1


def test_run_respects_limit_flag(tmp_path):
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({
        "GaugeID": [f"G{i}" for i in range(5)],
        "FlowMeasureID": [f"G{i}-flow-m-86400-m3s-qualified" for i in range(5)],
    }).to_csv(links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": [f"G{i}" for i in range(5)],
                  "station_name": [f"S{i}" for i in range(5)],
                  "lat": [51.0] * 5, "lon": [-1.0] * 5}).to_csv(cat_path, index=False)
    out_path = tmp_path / "scan.csv"
    log_path = tmp_path / "scan.log"
    lock_path = tmp_path / "scan.lock"

    seen: list[str] = []

    def fake_scan(gauge_id: str) -> dict:
        seen.append(gauge_id)
        return S._empty_row(gauge_id, gauge_id, "no_data")

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(out_path), "--log", str(log_path),
        "--lock", str(lock_path), "--workers", "1",
        "--limit", "2",
    ])
    with patch("scripts.flow_fleet_scan.scan_one_gauge", side_effect=fake_scan), \
         patch("scripts.flow_fleet_scan._worker_init", return_value=None):
        S.run(args)
    assert seen == ["G0", "G1"]            # sorted by id, capped at --limit


# ---------------------------------------------------------------------------
# Row schema
# ---------------------------------------------------------------------------

def test_empty_row_has_every_row_field():
    row = S._empty_row("G1", "Alpha", "no_data")
    assert set(row.keys()) == set(S.ROW_FIELDS)
    assert row["gate_pass"] is False
    assert row["tier"] == "status_only"
    assert row["error"] == ""


def test_row_fields_matches_flow_gate_check_columns_plus_extras():
    # build_plan.md: "same columns as flow_gate_check.csv plus record_start
    # and any error/reason" — flow_gate_check.csv's columns must all be a
    # subset of ROW_FIELDS.
    gate_check_cols = {
        "gauge_id", "station_name", "gate_pass", "tier", "rain_dependent",
        "n_origins", "n_years", "range_logq", "floor_skill", "floor_cov14",
        "floor_band_frac", "ceiling_skill", "ceiling_cov14",
        "ceiling_band_frac", "reason",
    }
    assert gate_check_cols.issubset(set(S.ROW_FIELDS))
    assert "record_start" in S.ROW_FIELDS
    assert "error" in S.ROW_FIELDS


def test_scan_one_gauge_never_raises_on_load_exception():
    # Worker-level guarantee: an exception inside data assembly degrades to
    # an error row, it never propagates out of scan_one_gauge (which would
    # otherwise surface as a Future exception and, unhandled, could still be
    # caught by run()'s own guard — but this is the first line of defence).
    with patch("scripts.flow_fleet_scan.load_gauge_series",
              side_effect=RuntimeError("network exploded")):
        row = S.scan_one_gauge("G1")
    assert row["reason"] == "load_error"
    assert "network exploded" in row["error"]
    assert row["gate_pass"] is False
    assert row["tier"] == "status_only"
    assert set(row.keys()) == set(S.ROW_FIELDS)


def test_scan_one_gauge_no_data_when_load_returns_none():
    with patch("scripts.flow_fleet_scan.load_gauge_series", return_value=None):
        row = S.scan_one_gauge("G1")
    assert row["reason"] == "no_data"
    assert row["gate_pass"] is False


def test_scan_one_gauge_gate_error_degrades_to_row():
    idx = pd.date_range("2018-01-01", periods=10, freq="D")
    q = pd.Series(1.0, idx)
    prec = pd.Series(0.0, idx)
    evap = pd.Series(1.0, idx)
    with patch("scripts.flow_fleet_scan.load_gauge_series",
              return_value=(q, prec, evap)), \
         patch("scripts.flow_fleet_scan.G.admit_gauge",
              side_effect=RuntimeError("gate blew up")):
        row = S.scan_one_gauge("G1")
    assert row["reason"] == "gate_error"
    assert "gate blew up" in row["error"]
    assert row["record_start"] == "2018-01-01"


def test_scan_one_gauge_records_record_start_and_gate_result():
    idx = pd.date_range("2020-03-01", periods=10, freq="D")
    q = pd.Series(1.0, idx)
    prec = pd.Series(0.0, idx)
    evap = pd.Series(1.0, idx)
    fake_result = {
        "gate_pass": True, "tier": "tier1", "rain_dependent": False,
        "n_origins": 8, "n_years": 4, "range_logq": 1.23,
        "floor": {"skill_ratio": 0.9, "cov14": 81.0, "band_frac": 0.15},
        "ceiling": {"skill_ratio": 0.6, "cov14": 88.0, "band_frac": 0.10},
        "reason": "pass_floor_robust",
    }
    with patch("scripts.flow_fleet_scan.load_gauge_series",
              return_value=(q, prec, evap)), \
         patch("scripts.flow_fleet_scan.G.admit_gauge", return_value=fake_result):
        row = S.scan_one_gauge("G1")
    assert row["record_start"] == "2020-03-01"
    assert row["tier"] == "tier1"
    assert row["floor_skill"] == 0.9
    assert row["ceiling_band_frac"] == 0.10
    assert row["error"] == ""
    assert row["elapsed_s"] is not None
    assert set(row.keys()) == set(S.ROW_FIELDS)


def test_scan_one_gauge_outer_guard_survives_a_totally_broken_catalogue():
    # scan_one_gauge's outer try/except must swallow even an exception raised
    # before load_gauge_series is reached (e.g. a corrupt global state).
    with patch("scripts.flow_fleet_scan._scan_one_gauge_inner",
              side_effect=ValueError("boom")):
        row = S.scan_one_gauge("G1")
    assert row["reason"] == "worker_exception"
    assert "boom" in row["error"]


# ---------------------------------------------------------------------------
# ScanWriter — append + flush, header-once
# ---------------------------------------------------------------------------

def test_scan_writer_writes_header_once_and_appends(tmp_path):
    out_path = tmp_path / "scan.csv"
    log_path = tmp_path / "scan.log"

    w = S.ScanWriter(out_path, log_path)
    w.write_row(S._empty_row("G1", "Alpha", "no_data"), done=1, total=2)
    w.close()

    # Re-open (simulating a resumed process) and append a second row.
    w2 = S.ScanWriter(out_path, log_path)
    w2.write_row(S._empty_row("G2", "Beta", "no_data"), done=2, total=2)
    w2.close()

    lines = out_path.read_text().splitlines()
    assert lines[0].startswith("gauge_id,")
    assert len(lines) == 3                 # header + 2 data rows, no repeat header

    df = pd.read_csv(out_path, dtype={"gauge_id": str})
    assert list(df["gauge_id"]) == ["G1", "G2"]

    log_lines = log_path.read_text().splitlines()
    assert len(log_lines) == 2
    assert "[1/2]" in log_lines[0]
    assert "[2/2]" in log_lines[1]


# ---------------------------------------------------------------------------
# Single-instance lockfile (Finding 1: the per-worker Manager().Lock() only
# serialises downloads WITHIN one invocation's pool, not across two
# concurrent invocations of this script).
# ---------------------------------------------------------------------------

def test_acquire_lock_creates_file_and_release_removes_it(tmp_path):
    lock_path = tmp_path / "scan.lock"
    assert S._acquire_lock(lock_path) is True
    assert lock_path.exists()
    S._release_lock(lock_path)
    assert not lock_path.exists()


def test_acquire_lock_refuses_when_already_held(tmp_path):
    lock_path = tmp_path / "scan.lock"
    assert S._acquire_lock(lock_path) is True
    assert S._acquire_lock(lock_path) is False   # second acquire, same lock
    S._release_lock(lock_path)


def test_acquire_lock_steals_a_stale_lock(tmp_path, monkeypatch):
    lock_path = tmp_path / "scan.lock"
    lock_path.write_text("pid=99999 started=2020-01-01T00:00:00Z\n")
    old = time.time() - (S.LOCK_STALE_S + 3600)
    os.utime(lock_path, (old, old))
    assert S._acquire_lock(lock_path) is True    # stolen, not refused
    S._release_lock(lock_path)


def test_release_lock_missing_file_does_not_raise(tmp_path):
    S._release_lock(tmp_path / "never_existed.lock")   # no exception


def test_run_refuses_to_start_when_lock_already_held(tmp_path, capsys):
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({"GaugeID": ["G1"], "FlowMeasureID": ["G1-flow"]}).to_csv(
        links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": ["G1"], "station_name": ["Alpha"],
                  "lat": [51.0], "lon": [-1.0]}).to_csv(cat_path, index=False)
    lock_path = tmp_path / "scan.lock"
    assert S._acquire_lock(lock_path) is True    # simulate another scan running

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(tmp_path / "scan.csv"), "--log", str(tmp_path / "scan.log"),
        "--lock", str(lock_path), "--workers", "1",
    ])
    rc = S.run(args)
    assert rc == 3
    assert "already running" in capsys.readouterr().err
    assert lock_path.exists()    # the OTHER holder's lock — run() must not touch it
    S._release_lock(lock_path)


def test_run_releases_lock_even_when_worker_raises(tmp_path):
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({"GaugeID": ["G1"], "FlowMeasureID": ["G1-flow"]}).to_csv(
        links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": ["G1"], "station_name": ["Alpha"],
                  "lat": [51.0], "lon": [-1.0]}).to_csv(cat_path, index=False)
    lock_path = tmp_path / "scan.lock"

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(tmp_path / "scan.csv"), "--log", str(tmp_path / "scan.log"),
        "--lock", str(lock_path), "--workers", "1",
    ])
    with patch("scripts.flow_fleet_scan._worker_init",
              side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            S.run(args)
    assert not lock_path.exists()   # released despite the exception


# ---------------------------------------------------------------------------
# Default --workers cap (Finding 2a): mirrors build_flow_models.py's PET-
# fetch rate-limit cap instead of the older, higher max(2, cpu-2) default.
# ---------------------------------------------------------------------------

def test_default_workers_capped_for_pet_rate_limit():
    assert S._DEFAULT_WORKERS <= 3


def test_parse_args_workers_default_matches_module_constant():
    args = S.parse_args([])
    assert args.workers == S._DEFAULT_WORKERS


# ---------------------------------------------------------------------------
# --retry-errors (Finding 2b): error rows must not be skipped forever.
# ---------------------------------------------------------------------------

def test_rewrite_dropping_error_rows_missing_file_is_noop(tmp_path):
    assert S.rewrite_dropping_error_rows(tmp_path / "nope.csv") == 0


def test_rewrite_dropping_error_rows_empty_file_is_noop(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("")
    assert S.rewrite_dropping_error_rows(p) == 0


def test_rewrite_dropping_error_rows_drops_only_error_rows(tmp_path):
    p = tmp_path / "scan.csv"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "no_data"))                 # keep
        w.writerow(S._empty_row("G2", "Beta", "load_error", "boom"))       # drop
        w.writerow(S._empty_row("G3", "Gamma", "gate_error", "kaboom"))    # drop
        w.writerow(S._empty_row("G4", "Delta", "no_data"))                 # keep

    n_dropped = S.rewrite_dropping_error_rows(p)
    assert n_dropped == 2

    result = pd.read_csv(p, dtype={"gauge_id": str})
    assert set(result["gauge_id"]) == {"G1", "G4"}
    assert (result["error"].fillna("") == "").all()


def test_rewrite_dropping_error_rows_preserves_kept_rows_byte_for_byte(tmp_path):
    p = tmp_path / "scan.csv"
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "no_data"))
        w.writerow(S._empty_row("G2", "Beta", "load_error", "429 Too Many Requests"))

    with open(p, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        kept_row_before = next(r for r in reader if r["gauge_id"] == "G1")

    S.rewrite_dropping_error_rows(p)

    with open(p, "r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        kept_row_after = next(r for r in reader if r["gauge_id"] == "G1")

    assert kept_row_before == kept_row_after


def test_rewrite_dropping_error_rows_no_error_column_is_noop(tmp_path):
    p = tmp_path / "scan.csv"
    pd.DataFrame({"gauge_id": ["G1"], "other": [1]}).to_csv(p, index=False)
    assert S.rewrite_dropping_error_rows(p) == 0


def test_rewrite_dropping_error_rows_corrupt_file_degrades_to_zero(tmp_path):
    p = tmp_path / "corrupt.csv"
    p.write_text("gauge_id,error\nG1,\n\"unterminated")
    # csv.DictReader doesn't raise on this shape the way pandas does — but
    # the function must never raise regardless of malformed input.
    assert S.rewrite_dropping_error_rows(p) >= 0


def test_run_with_retry_errors_rescans_previously_failed_gauges(tmp_path):
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({
        "GaugeID": ["G1", "G2"],
        "FlowMeasureID": ["G1-flow-m-86400-m3s-qualified",
                          "G2-flow-m-86400-m3s-qualified"],
    }).to_csv(links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": ["G1", "G2"], "station_name": ["Alpha", "Beta"],
                  "lat": [51.0, 52.0], "lon": [-1.0, -2.0]}).to_csv(cat_path, index=False)

    out_path = tmp_path / "scan.csv"
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "load_error", "429 Too Many Requests"))

    log_path = tmp_path / "scan.log"
    lock_path = tmp_path / "scan.lock"
    seen: list[str] = []

    def fake_scan(gauge_id: str) -> dict:
        seen.append(gauge_id)
        return S._empty_row(gauge_id, gauge_id, "no_data")

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(out_path), "--log", str(log_path),
        "--lock", str(lock_path), "--workers", "1", "--retry-errors",
    ])
    with patch("scripts.flow_fleet_scan.scan_one_gauge", side_effect=fake_scan), \
         patch("scripts.flow_fleet_scan._worker_init", return_value=None):
        rc = S.run(args)
    assert rc == 0
    assert seen == ["G1", "G2"]      # G1's error row was dropped, so re-scanned

    result = pd.read_csv(out_path, dtype={"gauge_id": str})
    assert len(result) == 2          # no duplicate G1 row
    assert set(result["gauge_id"]) == {"G1", "G2"}
    assert (result["error"].fillna("") == "").all()   # G1 is fresh now, no error


def test_run_without_retry_errors_skips_previously_failed_gauges_forever(tmp_path):
    # Documents the default-off behaviour the fix must NOT change: a plain
    # resume still treats an error row as done.
    links_path = tmp_path / "flow_links.csv"
    pd.DataFrame({
        "GaugeID": ["G1", "G2"],
        "FlowMeasureID": ["G1-flow-m-86400-m3s-qualified",
                          "G2-flow-m-86400-m3s-qualified"],
    }).to_csv(links_path, index=False)
    cat_path = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": ["G1", "G2"], "station_name": ["Alpha", "Beta"],
                  "lat": [51.0, 52.0], "lon": [-1.0, -2.0]}).to_csv(cat_path, index=False)

    out_path = tmp_path / "scan.csv"
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.ROW_FIELDS)
        w.writeheader()
        w.writerow(S._empty_row("G1", "Alpha", "load_error", "429 Too Many Requests"))

    log_path = tmp_path / "scan.log"
    lock_path = tmp_path / "scan.lock"
    seen: list[str] = []

    def fake_scan(gauge_id: str) -> dict:
        seen.append(gauge_id)
        return S._empty_row(gauge_id, gauge_id, "no_data")

    args = S.parse_args([
        "--links", str(links_path), "--catalogue", str(cat_path),
        "--out", str(out_path), "--log", str(log_path),
        "--lock", str(lock_path), "--workers", "1",
    ])
    with patch("scripts.flow_fleet_scan.scan_one_gauge", side_effect=fake_scan), \
         patch("scripts.flow_fleet_scan._worker_init", return_value=None):
        rc = S.run(args)
    assert rc == 0
    assert seen == ["G2"]      # G1 skipped despite its error — old behaviour

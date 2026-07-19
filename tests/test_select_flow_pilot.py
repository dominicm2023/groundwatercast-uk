"""Tests for scripts/select_flow_pilot.py — deterministic pilot selection
from the Stage-5 fleet scan (build_plan.md Stage 6).
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.select_flow_pilot import (
    CURATED_OUT,
    parse_args,
    run as run_select,
    select_pilot,
)


def _scan_row(gauge_id, tier, floor_skill, name=None):
    return {"gauge_id": gauge_id, "station_name": name or gauge_id,
           "tier": tier, "floor_skill": floor_skill}


def _scan(rows):
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# select_pilot: pure function
# ---------------------------------------------------------------------------

def test_only_tier1_rows_are_selected():
    scan = _scan([
        _scan_row("a", "tier1", 0.5),
        _scan_row("b", "rain_dependent", 0.1),
        _scan_row("c", "status_only", 0.05),
        _scan_row("d", "tier1", 0.7),
    ])
    pilot = select_pilot(scan)
    assert set(pilot["gauge_id"]) == {"a", "d"}


def test_sorted_by_floor_skill_ascending():
    scan = _scan([
        _scan_row("a", "tier1", 0.9),
        _scan_row("b", "tier1", 0.2),
        _scan_row("c", "tier1", 0.5),
    ])
    pilot = select_pilot(scan)
    assert pilot["gauge_id"].tolist() == ["b", "c", "a"]
    assert pilot["floor_skill"].is_monotonic_increasing


def test_capped_at_pilot_size():
    scan = _scan([_scan_row(f"g{i}", "tier1", float(i)) for i in range(80)])
    pilot = select_pilot(scan, pilot_size=50)
    assert len(pilot) == 50
    # takes the LOWEST floor_skill rows, not an arbitrary 50
    assert pilot["floor_skill"].max() < 50.0


def test_default_is_uncapped_full_tier1():
    # The 2026-07-19 expansion: no cap by default — every tier-1 row (minus
    # the curation register) is selected, however many there are.
    scan = _scan([_scan_row(f"g{i}", "tier1", float(i)) for i in range(80)])
    pilot = select_pilot(scan)
    assert len(pilot) == 80


def test_curated_out_gauges_are_excluded():
    curated_id = next(iter(CURATED_OUT))
    scan = _scan([
        _scan_row(curated_id, "tier1", 0.1, name="Tollgate"),
        _scan_row("keep-me", "tier1", 0.5),
    ])
    pilot = select_pilot(scan)
    assert pilot["gauge_id"].tolist() == ["keep-me"]


def test_curated_out_reasons_are_recorded():
    # The register is the documentation-of-record for why a gate-passing
    # gauge is unpublished — every entry must actually say why.
    assert CURATED_OUT, "curation register unexpectedly empty"
    for gauge_id, reason in CURATED_OUT.items():
        assert reason and len(reason) > 20, f"no meaningful reason for {gauge_id}"


def test_deterministic_across_repeated_calls():
    scan = _scan([_scan_row(f"g{i}", "tier1", float(i % 10)) for i in range(30)])
    p1 = select_pilot(scan)
    p2 = select_pilot(scan)
    pd.testing.assert_frame_equal(p1, p2)


def test_ties_broken_by_gauge_id():
    # Several gauges share the same floor_skill — order must still be fixed,
    # not dependent on input row order (pandas sort is stable, but the
    # explicit gauge_id tiebreak makes it independent of upstream row order).
    scan = _scan([
        _scan_row("z", "tier1", 0.3),
        _scan_row("a", "tier1", 0.3),
        _scan_row("m", "tier1", 0.3),
    ])
    pilot = select_pilot(scan)
    assert pilot["gauge_id"].tolist() == ["a", "m", "z"]


def test_output_columns():
    scan = _scan([_scan_row("a", "tier1", 0.3, name="Test Gauge")])
    pilot = select_pilot(scan)
    assert list(pilot.columns) == ["gauge_id", "station_name", "floor_skill"]
    assert pilot.iloc[0]["station_name"] == "Test Gauge"


def test_no_tier1_rows_yields_empty_pilot():
    scan = _scan([_scan_row("a", "rain_dependent", 0.1),
                 _scan_row("b", "status_only", 0.9)])
    pilot = select_pilot(scan)
    assert pilot.empty
    assert list(pilot.columns) == ["gauge_id", "station_name", "floor_skill"]


# ---------------------------------------------------------------------------
# run(): I/O wrapper — graceful behaviour on a missing/malformed scan
# ---------------------------------------------------------------------------

def test_run_missing_scan_exits_zero_and_writes_nothing(tmp_path, capsys):
    scan_path = tmp_path / "absent.csv"
    out_path = tmp_path / "flow_pilot.csv"
    args = parse_args(["--scan", str(scan_path), "--out", str(out_path)])
    assert run_select(args) == 0
    assert not out_path.exists()
    assert "not found" in capsys.readouterr().out


def test_run_malformed_scan_missing_columns_exits_zero(tmp_path, capsys):
    scan_path = tmp_path / "scan.csv"
    pd.DataFrame({"gauge_id": ["a"], "station_name": ["A"]}).to_csv(scan_path, index=False)
    out_path = tmp_path / "flow_pilot.csv"
    args = parse_args(["--scan", str(scan_path), "--out", str(out_path)])
    assert run_select(args) == 0
    assert not out_path.exists()
    assert "missing expected column" in capsys.readouterr().out


def test_run_writes_pilot_csv(tmp_path):
    scan_path = tmp_path / "scan.csv"
    _scan([_scan_row("a", "tier1", 0.5), _scan_row("b", "status_only", 0.1)]).to_csv(
        scan_path, index=False)
    out_path = tmp_path / "flow_pilot.csv"
    args = parse_args(["--scan", str(scan_path), "--out", str(out_path)])
    assert run_select(args) == 0
    written = pd.read_csv(out_path)
    assert written["gauge_id"].tolist() == ["a"]

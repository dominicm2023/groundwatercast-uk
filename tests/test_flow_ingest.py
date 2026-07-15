"""Unit tests for src/download/flow.py + scripts/build_flow_shards.py
(low-flow build_plan.md Stage 2: flow ingest + daily top-up).

No live HTTP calls — src.download.build's download_measure/topup_measure are
monkeypatched. Covers the shard round-trip (including a zero-flow day, the
build plan's explicit winterbourne requirement) and the daily top-up path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from src.download import flow as F


# ---------------------------------------------------------------------------
# load_flow_measure_map
# ---------------------------------------------------------------------------

def test_load_flow_measure_map_reads_gauge_and_measure_ids(tmp_path):
    p = tmp_path / "flow_links.csv"
    pd.DataFrame({
        "GaugeID": ["G1", "G2", "G3"],
        "FlowMeasureID": ["G1-flow-m-86400-m3s-qualified",
                          "G2-flow-m-86400-m3s-qualified", None],
        "RainMeasureID_1": ["R1", "R2", "R3"],
    }).to_csv(p, index=False)

    result = F.load_flow_measure_map(p)
    assert result == {
        "G1": "G1-flow-m-86400-m3s-qualified",
        "G2": "G2-flow-m-86400-m3s-qualified",
    }
    assert "G3" not in result   # NaN FlowMeasureID dropped


def test_load_flow_measure_map_ids_kept_opaque(tmp_path):
    # Compound split-channel ids (guid_suffix) must round-trip unchanged.
    p = tmp_path / "flow_links.csv"
    compound = "eba748a3-ebd6-4141-a671-5ef94b896ffa_453202901"
    pd.DataFrame({
        "GaugeID": [compound],
        "FlowMeasureID": [f"{compound}-flow-m-86400-m3s-qualified"],
    }).to_csv(p, index=False)
    result = F.load_flow_measure_map(p)
    assert result[compound] == f"{compound}-flow-m-86400-m3s-qualified"


# ---------------------------------------------------------------------------
# _daily_mean_from_raw
# ---------------------------------------------------------------------------

def _write_raw_flow(path: Path, rows: list[tuple[str, float]]) -> None:
    pd.DataFrame(rows, columns=["dateTime", "value"]).to_csv(path, index=False)


def test_daily_mean_keeps_zero_flow_day(tmp_path):
    p = tmp_path / "M1.csv"
    _write_raw_flow(p, [
        ("2026-06-01T00:00:00Z", 0.0),      # winterbourne — dry
        ("2026-06-02T00:00:00Z", 0.012),
        ("2026-06-03T00:00:00Z", 1.5),
    ])
    daily = F._daily_mean_from_raw(p, after=None)
    assert list(daily.columns) == F.SHARD_COLS
    assert len(daily) == 3
    zero_row = daily[daily["date"] == pd.Timestamp("2026-06-01")]
    assert not zero_row.empty
    assert zero_row["Flow_m3s"].iloc[0] == 0.0


def test_daily_mean_averages_same_day_readings(tmp_path):
    p = tmp_path / "M1.csv"
    _write_raw_flow(p, [
        ("2026-06-01T00:00:00Z", 1.0),
        ("2026-06-01T12:00:00Z", 3.0),
    ])
    daily = F._daily_mean_from_raw(p, after=None)
    assert len(daily) == 1
    assert daily["Flow_m3s"].iloc[0] == 2.0


def test_daily_mean_filters_after(tmp_path):
    p = tmp_path / "M1.csv"
    _write_raw_flow(p, [
        ("2026-06-01T00:00:00Z", 1.0),
        ("2026-06-02T00:00:00Z", 2.0),
        ("2026-06-03T00:00:00Z", 3.0),
    ])
    daily = F._daily_mean_from_raw(p, after=pd.Timestamp("2026-06-01"))
    assert list(daily["date"]) == [pd.Timestamp("2026-06-02"), pd.Timestamp("2026-06-03")]


def test_daily_mean_empty_when_all_filtered(tmp_path):
    p = tmp_path / "M1.csv"
    _write_raw_flow(p, [("2026-06-01T00:00:00Z", 1.0)])
    daily = F._daily_mean_from_raw(p, after=pd.Timestamp("2026-06-05"))
    assert daily.empty
    assert list(daily.columns) == F.SHARD_COLS


def test_daily_mean_handles_empty_raw_file_gracefully(tmp_path):
    # Live-observed 2026-07-14 pilot ingest: the EA API can return 200 OK
    # with a 0-byte body for a catalogued-but-not-yet-populated qualified
    # measure. Must not raise — treated as "no data yet".
    p = tmp_path / "M1.csv"
    p.write_text("")
    daily = F._daily_mean_from_raw(p, after=None)
    assert daily.empty
    assert list(daily.columns) == F.SHARD_COLS


def test_build_shard_on_empty_raw_returns_no_raw(tmp_path):
    p = tmp_path / "M1.csv"
    p.write_text("")
    status, n = F.build_or_topup_flow_shard("G1", p, tmp_path / "shards")
    assert (status, n) == ("no_raw", 0)


def test_daily_mean_drops_unparseable_values(tmp_path):
    p = tmp_path / "M1.csv"
    df = pd.DataFrame({
        "dateTime": ["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"],
        "value": ["1.5", ""],
    })
    df.to_csv(p, index=False)
    daily = F._daily_mean_from_raw(p, after=None)
    assert len(daily) == 1
    assert daily["Flow_m3s"].iloc[0] == 1.5


# ---------------------------------------------------------------------------
# build_or_topup_flow_shard — the round-trip (build-from-scratch + top-up)
# ---------------------------------------------------------------------------

def test_builds_fresh_shard_with_zero_flow_day_roundtrip(tmp_path):
    raw = tmp_path / "M1.csv"
    _write_raw_flow(raw, [
        ("2026-06-01T00:00:00Z", 0.0),
        ("2026-06-02T00:00:00Z", 0.0),
        ("2026-06-03T00:00:00Z", 0.45),
    ])
    shard_dir = tmp_path / "shards"
    status, n = F.build_or_topup_flow_shard("G1", raw, shard_dir)
    assert (status, n) == ("built", 3)

    out = pd.read_parquet(shard_dir / "G1.parquet")
    assert list(out.columns) == F.SHARD_COLS
    assert list(out["Flow_m3s"]) == [0.0, 0.0, 0.45]      # zero flows survived the round-trip
    assert out["date"].is_monotonic_increasing
    assert set(out["data_source"]) == {"flow_logged"}


def test_no_raw_returns_no_raw_status(tmp_path):
    shard_dir = tmp_path / "shards"
    status, n = F.build_or_topup_flow_shard("G1", tmp_path / "missing.csv", shard_dir)
    assert (status, n) == ("no_raw", 0)


def test_topup_appends_new_tail(tmp_path):
    raw = tmp_path / "M1.csv"
    shard_dir = tmp_path / "shards"
    _write_raw_flow(raw, [
        ("2026-06-01T00:00:00Z", 1.0),
        ("2026-06-02T00:00:00Z", 2.0),
    ])
    status, n = F.build_or_topup_flow_shard("G1", raw, shard_dir)
    assert (status, n) == ("built", 2)

    # Raw archive advances (topup_measure semantics: same file, more rows).
    _write_raw_flow(raw, [
        ("2026-06-01T00:00:00Z", 1.0),
        ("2026-06-02T00:00:00Z", 2.0),
        ("2026-06-03T00:00:00Z", 0.0),      # dries up
    ])
    status, n = F.build_or_topup_flow_shard("G1", raw, shard_dir)
    assert (status, n) == ("advanced", 1)

    out = pd.read_parquet(shard_dir / "G1.parquet")
    assert len(out) == 3
    assert out["Flow_m3s"].iloc[-1] == 0.0


def test_topup_current_when_nothing_new(tmp_path):
    raw = tmp_path / "M1.csv"
    shard_dir = tmp_path / "shards"
    _write_raw_flow(raw, [("2026-06-01T00:00:00Z", 1.0)])
    F.build_or_topup_flow_shard("G1", raw, shard_dir)

    status, n = F.build_or_topup_flow_shard("G1", raw, shard_dir)
    assert (status, n) == ("current", 0)
    out = pd.read_parquet(shard_dir / "G1.parquet")
    assert len(out) == 1                                   # unchanged, not duplicated


def test_topup_never_duplicates_overlapping_dates(tmp_path):
    raw = tmp_path / "M1.csv"
    shard_dir = tmp_path / "shards"
    _write_raw_flow(raw, [("2026-06-01T00:00:00Z", 1.0), ("2026-06-02T00:00:00Z", 2.0)])
    F.build_or_topup_flow_shard("G1", raw, shard_dir)

    # A revised value lands on an already-shard-covered date (revision, not new date):
    # after= is the shard's max date so this row is filtered out — 'current'.
    _write_raw_flow(raw, [("2026-06-01T00:00:00Z", 1.0), ("2026-06-02T00:00:00Z", 2.5)])
    status, n = F.build_or_topup_flow_shard("G1", raw, shard_dir)
    assert status == "current"
    out = pd.read_parquet(shard_dir / "G1.parquet")
    assert len(out) == 2
    assert out["date"].is_unique


# ---------------------------------------------------------------------------
# ensure_flow_raw_current
# ---------------------------------------------------------------------------

def _cfg(raw_root):
    return {
        "download": {"raw_root": str(raw_root), "limit": 100, "max_retries": 1,
                     "backoff_base": 1, "chunk_years": 2, "min_date": "2018-01-01"},
        "api": {"readings_url_template": "http://example/{measure_id}/readings.csv"},
    }


def test_ensure_raw_current_downloads_when_absent(tmp_path):
    cfg = _cfg(tmp_path / "raw")
    with patch("src.download.flow.download_measure",
               return_value=("M1", "downloaded")) as mock_dl, \
         patch("src.download.flow.topup_measure") as mock_topup:
        _, status = F.ensure_flow_raw_current("M1", cfg)
    assert status == "downloaded"
    mock_dl.assert_called_once_with("M1", "flow", cfg)
    mock_topup.assert_not_called()


def test_ensure_raw_current_tops_up_when_present(tmp_path):
    raw_root = tmp_path / "raw"
    flow_dir = raw_root / "flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "M1.csv").write_text("dateTime,value\n2026-06-01T00:00:00Z,1.0\n")
    cfg = _cfg(raw_root)
    with patch("src.download.flow.topup_measure",
               return_value=("M1", "advanced")) as mock_topup, \
         patch("src.download.flow.download_measure") as mock_dl:
        _, status = F.ensure_flow_raw_current("M1", cfg)
    assert status == "advanced"
    mock_topup.assert_called_once_with("M1", "flow", cfg)
    mock_dl.assert_not_called()


def test_ensure_raw_current_heals_0byte_flow_raw_end_to_end(tmp_path):
    # Same EA 200-empty-body failure as the groundwater case (BUGS.md), on a
    # flow gauge. ensure_flow_raw_current routes exists->topup_measure, and
    # this exercises the real (unmocked) topup_measure/download_measure
    # chain end to end: the 0-byte file must be healed, not report "failed"
    # forever.
    from unittest.mock import MagicMock
    raw_root = tmp_path / "raw"
    flow_dir = raw_root / "flow"
    flow_dir.mkdir(parents=True)
    (flow_dir / "M1.csv").write_text("")   # 0-byte junk raw
    cfg = _cfg(raw_root)

    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b"dateTime,value\n2026-06-01T00:00:00Z,0.42\n"]
    mock_resp.raise_for_status.return_value = None
    with patch("src.download.build.requests.get", return_value=mock_resp):
        _, status = F.ensure_flow_raw_current("M1", cfg)

    assert status == "downloaded"
    out = pd.read_csv(flow_dir / "M1.csv")
    assert len(out) == 1
    assert out["value"].iloc[0] == 0.42

    # And the shard build now succeeds off the healed raw file — the real
    # downstream point of this fix.
    status2, n = F.build_or_topup_flow_shard("G1", flow_dir / "M1.csv", tmp_path / "shards")
    assert (status2, n) == ("built", 1)


# ---------------------------------------------------------------------------
# refresh_flow_gauge — full per-gauge orchestration
# ---------------------------------------------------------------------------

def test_refresh_flow_gauge_builds_shard_after_download(tmp_path):
    raw_root = tmp_path / "raw"
    shard_dir = tmp_path / "shards"
    cfg = _cfg(raw_root)

    def fake_download(measure_id, measure_type, config):
        out = Path(config["download"]["raw_root"]) / measure_type / f"{measure_id}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_raw_flow(out, [("2026-06-01T00:00:00Z", 0.0), ("2026-06-02T00:00:00Z", 0.3)])
        return measure_id, "downloaded"

    with patch("src.download.flow.download_measure", side_effect=fake_download):
        result = F.refresh_flow_gauge("G1", "M1", cfg, shard_dir=shard_dir)

    assert result == {"gauge_id": "G1", "download": "downloaded",
                      "shard": "built", "n_rows": 2}
    out = pd.read_parquet(shard_dir / "G1.parquet")
    assert len(out) == 2


def test_refresh_flow_gauge_reports_download_failure(tmp_path):
    cfg = _cfg(tmp_path / "raw")
    with patch("src.download.flow.download_measure", return_value=("M1", "failed")):
        result = F.refresh_flow_gauge("G1", "M1", cfg, shard_dir=tmp_path / "shards")
    assert result["download"] == "failed"
    assert result["shard"] == "no_raw"
    assert result["n_rows"] == 0


def test_refresh_flow_gauge_never_raises_on_download_exception(tmp_path):
    cfg = _cfg(tmp_path / "raw")
    with patch("src.download.flow.download_measure", side_effect=RuntimeError("boom")):
        result = F.refresh_flow_gauge("G1", "M1", cfg, shard_dir=tmp_path / "shards")
    assert result["download"] == "failed"
    assert result["shard"] == "error"
    assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# _TYPE_COLUMNS wiring (src/download/build.py)
# ---------------------------------------------------------------------------

def test_type_columns_gained_flow_entry():
    from src.download.build import _TYPE_COLUMNS
    assert _TYPE_COLUMNS["flow"] == ["FlowMeasureID"]


def test_extract_measure_ids_reads_flow_links_shape():
    from src.download.build import extract_measure_ids
    flow_links = pd.DataFrame({
        "GaugeID": ["G1", "G2"],
        "FlowMeasureID": ["G1-flow-m-86400-m3s-qualified",
                          "G2-flow-m-86400-m3s-qualified"],
        "RainMeasureID_1": ["R1", "R2"],
        "RainMeasureID_2": ["R3", "R4"],
        "RainMeasureID_3": ["R5", "R6"],
    })
    result = extract_measure_ids(flow_links)
    assert result["flow"] == ["G1-flow-m-86400-m3s-qualified",
                              "G2-flow-m-86400-m3s-qualified"]
    assert result["groundwater"] == []   # no GWMeasureID column in flow_links


def test_station_links_frame_yields_no_flow_ids():
    # Regression guard: adding "flow" to _TYPE_COLUMNS must stay inert for the
    # borehole pipeline's station_links.csv (no FlowMeasureID column there).
    from src.download.build import extract_measure_ids
    station_links = pd.DataFrame({
        "GWStationID": ["S1"],
        "GWMeasureID": ["gw-001"],
        "RainMeasureID_1": ["r-001"],
        "RainMeasureID_2": ["r-002"],
        "RainMeasureID_3": ["r-003"],
    })
    result = extract_measure_ids(station_links)
    assert result["flow"] == []


# ---------------------------------------------------------------------------
# resolve_flow_pilot_path — the single source of truth every flow-pilot
# consumer (build_ensemble_members, build_flow_models,
# build_flow_seasonal_shadow, refresh_seasonal_inputs) must resolve
# identically (sweep Finding 3).
# ---------------------------------------------------------------------------

def test_resolve_flow_pilot_path_falls_back_to_default_when_unset():
    cfg = {"forecast": {"ensemble": {"flow": {}}}}
    root = Path("/repo")
    assert F.resolve_flow_pilot_path(cfg, root) == root / F.DEFAULT_FLOW_PILOT_PATH


def test_resolve_flow_pilot_path_falls_back_when_no_flow_section_at_all():
    assert F.resolve_flow_pilot_path({}, Path("/repo")) == (
        Path("/repo") / F.DEFAULT_FLOW_PILOT_PATH)


def test_resolve_flow_pilot_path_honours_relative_config_override():
    cfg = {"forecast": {"ensemble": {"flow": {"pilot_path": "custom/pilot.csv"}}}}
    root = Path("/repo")
    assert F.resolve_flow_pilot_path(cfg, root) == root / "custom" / "pilot.csv"


def test_resolve_flow_pilot_path_honours_absolute_config_override(tmp_path):
    custom = tmp_path / "somewhere_else" / "pilot.csv"
    cfg = {"forecast": {"ensemble": {"flow": {"pilot_path": str(custom)}}}}
    # root is irrelevant once the config value is already absolute.
    assert F.resolve_flow_pilot_path(cfg, Path("/unrelated/root")) == custom


def test_resolve_flow_pilot_path_default_root_is_cwd_relative():
    cfg = {"forecast": {"ensemble": {"flow": {}}}}
    assert F.resolve_flow_pilot_path(cfg) == Path(".") / F.DEFAULT_FLOW_PILOT_PATH

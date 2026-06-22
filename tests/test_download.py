"""
Unit tests for src/download/build.py.
No real HTTP calls — requests is monkeypatched throughout.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.download.build import (
    _date_chunks,
    _download_chunked,
    _write_manifest,
    download_measure,
    extract_measure_ids,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def links_df():
    return pd.DataFrame({
        "GWStationID":         ["GW1", "GW2", "GW3"],
        "GWMeasureID":         ["gw-001", "gw-002", "gw-003"],
        "RainMeasureID_1":     ["r-001",  "r-002",  "r-001"],   # r-001 duplicated
        "RainMeasureID_2":     ["r-002",  "r-003",  "r-002"],   # r-002 duplicated
        "RainMeasureID_3":     ["r-003",  "r-004",  "r-003"],   # r-003 duplicated
    })


@pytest.fixture
def config(tmp_path):
    return {
        "api": {
            "readings_url_template": "https://example.com/measures/{measure_id}/readings.csv",
        },
        "linking": {"output_path": "data/processed/station_links.csv"},
        "download": {
            "min_date": "2022-01-01",
            "limit": 200000,
            "max_retries": 3,
            "backoff_base": 2,
            "raw_root": str(tmp_path / "raw"),
            "manifest_path": str(tmp_path / "raw" / "manifest.json"),
            "chunk_years": 2,
        },
    }


# ---------------------------------------------------------------------------
# extract_measure_ids
# ---------------------------------------------------------------------------

def test_extract_deduplicates_across_rows(links_df):
    result = extract_measure_ids(links_df)
    assert result["rainfall"].count("r-001") == 1
    assert result["rainfall"].count("r-002") == 1
    assert result["rainfall"].count("r-003") == 1


def test_extract_deduplicates_across_types(links_df):
    result = extract_measure_ids(links_df)
    all_ids = [mid for ids in result.values() for mid in ids]
    assert len(all_ids) == len(set(all_ids)), "Global duplicates found across types"


def test_extract_drops_none():
    df = pd.DataFrame({
        "GWMeasureID":         ["gw-001"],
        "RainMeasureID_1":     [None],
        "RainMeasureID_2":     [None],
        "RainMeasureID_3":     ["r-001"],
    })
    result = extract_measure_ids(df)
    for ids in result.values():
        assert None not in ids


def test_extract_all_types_present(links_df):
    result = extract_measure_ids(links_df)
    assert set(result.keys()) == {"groundwater", "rainfall"}


def test_extract_correct_counts(links_df):
    result = extract_measure_ids(links_df)
    assert len(result["groundwater"]) == 3
    assert len(result["rainfall"])    == 4   # r-001..r-004


# ---------------------------------------------------------------------------
# download_measure — skip existing
# ---------------------------------------------------------------------------

def test_download_skips_existing_file(config, tmp_path):
    out_path = tmp_path / "raw" / "groundwater" / "gw-001.csv"
    out_path.parent.mkdir(parents=True)
    out_path.write_text("date,value\n2022-01-01,10.0\n")

    config["download"]["raw_root"] = str(tmp_path / "raw")

    with patch("src.download.build.requests.get") as mock_get:
        _, status = download_measure("gw-001", "groundwater", config)

    assert status == "skipped"
    mock_get.assert_not_called()


def test_download_creates_subfolder(config, tmp_path):
    config["download"]["raw_root"] = str(tmp_path / "raw")

    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b"date,value\n2022-01-01,10.0\n"]
    mock_resp.raise_for_status.return_value = None

    with patch("src.download.build.requests.get", return_value=mock_resp):
        _, status = download_measure("gw-001", "groundwater", config)

    assert status == "downloaded"
    assert (tmp_path / "raw" / "groundwater" / "gw-001.csv").exists()


def test_download_returns_failed_on_error(config, tmp_path):
    config["download"]["raw_root"] = str(tmp_path / "raw")
    config["download"]["max_retries"] = 1

    with patch("src.download.build.requests.get", side_effect=OSError("network down")):
        _, status = download_measure("gw-001", "groundwater", config)

    assert status == "failed"


def test_download_removes_partial_file_on_failure(config, tmp_path):
    config["download"]["raw_root"] = str(tmp_path / "raw")
    config["download"]["max_retries"] = 1

    with patch("src.download.build.requests.get", side_effect=OSError("network down")):
        download_measure("gw-001", "groundwater", config)

    assert not (tmp_path / "raw" / "groundwater" / "gw-001.csv").exists()


# ---------------------------------------------------------------------------
# _download_chunked — dateTime deduplication
# ---------------------------------------------------------------------------

def _make_chunk_response(rows: list[dict]) -> MagicMock:
    import io
    df = pd.DataFrame(rows)
    csv_bytes = df.to_csv(index=False).encode()
    mock = MagicMock()
    mock.raise_for_status.return_value = None
    mock.content = csv_bytes
    mock.iter_content.return_value = [csv_bytes]
    return mock


def test_chunked_deduplicates_on_datetime(tmp_path):
    # min_date=2025-01-01 + chunk_years=1 produces exactly 2 chunks:
    #   chunk 1: 2025-01-01 → 2025-12-31
    #   chunk 2: 2026-01-01 → today
    out_path = tmp_path / "gw-001.csv"
    duplicate_ts = "2025-06-01T12:00:00Z"
    chunk1 = [{"dateTime": duplicate_ts, "value": 1.0},
              {"dateTime": "2025-06-02T12:00:00Z", "value": 2.0}]
    chunk2 = [{"dateTime": duplicate_ts, "value": 1.0},   # exact duplicate
              {"dateTime": "2026-01-01T12:00:00Z", "value": 3.0}]

    responses = [_make_chunk_response(chunk1), _make_chunk_response(chunk2)]
    with patch("src.download.build.requests.get", side_effect=responses):
        _download_chunked(
            url="https://example.com/measures/gw-001/readings.csv",
            path=out_path,
            min_date="2025-01-01",
            limit=200000,
            max_retries=1,
            backoff_base=2,
            chunk_years=1,
        )

    result = pd.read_csv(out_path)
    assert result["dateTime"].nunique() == len(result), "Duplicate timestamps remain"
    assert result["dateTime"].nunique() == 3   # 3 distinct timestamps


def test_chunked_sorts_by_datetime(tmp_path):
    out_path = tmp_path / "gw-002.csv"
    rows_unsorted = [
        {"dateTime": "2022-03-01T00:00:00Z", "value": 3.0},
        {"dateTime": "2022-01-01T00:00:00Z", "value": 1.0},
        {"dateTime": "2022-02-01T00:00:00Z", "value": 2.0},
    ]
    resp = _make_chunk_response(rows_unsorted)
    with patch("src.download.build.requests.get", return_value=resp):
        _download_chunked(
            url="https://example.com/measures/gw-002/readings.csv",
            path=out_path,
            min_date="2022-01-01",
            limit=200000,
            max_retries=1,
            backoff_base=2,
            chunk_years=6,
        )

    result = pd.read_csv(out_path)
    parsed = pd.to_datetime(result["dateTime"], utc=True)
    assert list(parsed) == sorted(parsed), "Rows not sorted by dateTime"


def test_chunked_utc_normalised(tmp_path):
    out_path = tmp_path / "gw-003.csv"
    rows = [
        {"dateTime": "2022-01-01T00:00:00+00:00", "value": 1.0},
        {"dateTime": "2022-01-02T00:00:00+01:00", "value": 2.0},  # +1h offset
    ]
    resp = _make_chunk_response(rows)
    with patch("src.download.build.requests.get", return_value=resp):
        _download_chunked(
            url="https://example.com/measures/gw-003/readings.csv",
            path=out_path,
            min_date="2022-01-01",
            limit=200000,
            max_retries=1,
            backoff_base=2,
            chunk_years=6,
        )

    result = pd.read_csv(out_path)
    # Both rows have distinct UTC times — no deduplication expected
    assert len(result) == 2


# ---------------------------------------------------------------------------
# _date_chunks
# ---------------------------------------------------------------------------

def test_date_chunks_covers_full_range():
    chunks = _date_chunks("2018-01-01", chunk_years=2)
    assert chunks[0][0] == "2018-01-01"
    assert chunks[-1][1] >= "2026-01-01"


def test_date_chunks_no_gaps():
    from datetime import date, timedelta
    chunks = _date_chunks("2020-01-01", chunk_years=1)
    for i in range(len(chunks) - 1):
        end   = date.fromisoformat(chunks[i][1])
        start = date.fromisoformat(chunks[i + 1][0])
        assert start == end + timedelta(days=1)


# ---------------------------------------------------------------------------
# _write_manifest
# ---------------------------------------------------------------------------

def test_write_manifest_structure(tmp_path):
    results = [
        ("gw-001", "downloaded"),
        ("gw-002", "skipped"),
        ("r-001",  "chunked"),
        ("r-002",  "failed"),
    ]
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(results, manifest_path)

    with open(manifest_path) as f:
        m = json.load(f)

    assert "gw-001" in m["downloaded"]
    assert "gw-002" in m["skipped"]
    assert "r-001"  in m["chunked"]
    assert "r-002"  in m["failed"]
    assert len(m["timestamp"]) == 1

"""Tests for the incremental raw-readings tail top-up (src/download/build.py).

The pure merge and the branch logic are unit-tested here (HTTP mocked); the live
fetch is exercised against the real EA archive during deploy.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from src.download import build as B


def _cfg(raw_root):
    return {
        "download": {"raw_root": str(raw_root), "limit": 100, "max_retries": 1,
                     "backoff_base": 1, "chunk_years": 2, "min_date": "2018-01-01"},
        "api": {"readings_url_template": "http://example/{measure_id}/readings.csv"},
    }


def test_merge_readings_dedups_sorts_new_wins():
    ex = pd.DataFrame({"dateTime": ["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"],
                       "value": [1.0, 2.0]})
    new = pd.DataFrame({"dateTime": ["2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z"],
                        "value": [2.5, 3.0]})   # 06-02 overlaps
    m = B._merge_readings(ex, new)
    assert list(m["dateTime"].dt.strftime("%Y-%m-%d")) == ["2026-06-01", "2026-06-02", "2026-06-03"]
    # revised overlap row: new wins (keep="last")
    assert float(m.loc[m["dateTime"].dt.day == 2, "value"].iloc[0]) == 2.5


def test_topup_absent_when_no_file(tmp_path):
    _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "absent"


def test_topup_heals_0byte_file_via_full_download(tmp_path):
    # EA API 200-OK-with-empty-body for a catalogued-but-not-yet-populated
    # measure, saved verbatim as the raw CSV (live 2026-07-14, BUGS.md).
    # Previously this returned "failed" forever; must now heal by deleting
    # the junk file and falling through to a full download.
    (tmp_path / "groundwater").mkdir()
    p = tmp_path / "groundwater" / "M1.csv"
    p.write_text("")
    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b"dateTime,value\n2026-06-01T00:00:00Z,1.0\n"]
    mock_resp.raise_for_status.return_value = None
    with patch("src.download.build.requests.get", return_value=mock_resp):
        _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "downloaded"
    out = pd.read_csv(p)
    assert len(out) == 1
    assert out["value"].iloc[0] == 1.0


def test_topup_heals_unparseable_junk_file(tmp_path):
    # Ragged rows (more fields than the header) trip the C parser -> raises
    # rather than returning an empty/valid frame. Same healing as 0-byte.
    (tmp_path / "groundwater").mkdir()
    p = tmp_path / "groundwater" / "M1.csv"
    p.write_text("a,b,c\n1,2\n3,4,5,6\n")
    mock_resp = MagicMock()
    mock_resp.iter_content.return_value = [b"dateTime,value\n2026-06-01T00:00:00Z,1.0\n"]
    mock_resp.raise_for_status.return_value = None
    with patch("src.download.build.requests.get", return_value=mock_resp):
        _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "downloaded"
    out = pd.read_csv(p)
    assert len(out) == 1


def test_topup_leaves_header_only_file_alone(tmp_path):
    # Valid CSV, zero data rows — a legitimate "not yet populated" measure
    # shape (download_measure/_stream_to_file write exactly this when the
    # archive is genuinely empty so far, see the truncation-check comment
    # in download_measure). Must NOT be deleted or re-fetched — still
    # reported "absent", same as before this fix.
    (tmp_path / "groundwater").mkdir()
    p = tmp_path / "groundwater" / "M1.csv"
    p.write_text("dateTime,value\n")
    with patch("src.download.build.requests.get") as mock_get:
        _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "absent"
    mock_get.assert_not_called()
    assert p.read_text() == "dateTime,value\n"


def test_topup_current_when_nothing_new(tmp_path, monkeypatch):
    (tmp_path / "groundwater").mkdir()
    p = tmp_path / "groundwater" / "M1.csv"
    pd.DataFrame({"dateTime": ["2026-06-01T00:00:00Z"], "value": [1.0]}).to_csv(p, index=False)
    monkeypatch.setattr(B, "_fetch_since", lambda *a, **k: pd.DataFrame())
    _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "current"


def test_topup_advances_and_merges(tmp_path, monkeypatch):
    (tmp_path / "groundwater").mkdir()
    p = tmp_path / "groundwater" / "M1.csv"
    pd.DataFrame({"dateTime": ["2026-06-01T00:00:00Z"], "value": [1.0]}).to_csv(p, index=False)
    monkeypatch.setattr(B, "_fetch_since",
                        lambda *a, **k: pd.DataFrame({"dateTime": ["2026-06-05T00:00:00Z"],
                                                      "value": [5.0]}))
    _, status = B.topup_measure("M1", "groundwater", _cfg(tmp_path))
    assert status == "advanced"
    out = pd.read_csv(p)
    assert len(out) == 2                       # appended, not overwritten
    assert out["dateTime"].iloc[-1].startswith("2026-06-05")

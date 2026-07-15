"""Tests for the surgical shard-tail updater (scripts/refresh_gw_shard_tail.py).

Covers the merge hierarchy (audited replaces live, never clobbers non-live),
the cheap-skip, the IQR screen, and source labelling — all against synthetic
shards/raw CSVs. The live EA path is exercised in deploy verification.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts import refresh_gw_shard_tail as S
from src.download.build import last_datetime_from_tail


def _write_raw(path, rows):
    pd.DataFrame(rows, columns=["measure", "dateTime", "date", "value",
                                "completeness", "quality", "qcode"]).to_csv(
        path, index=False)


def _shard(dates, sources, levels=None):
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "GW_Level": levels if levels is not None else [10.0] * len(dates),
        "is_interpolated": 0,
        "data_source": sources,
    })


@pytest.fixture
def env(tmp_path, monkeypatch):
    raw = tmp_path / "raw"; raw.mkdir()
    shards = tmp_path / "shards"; shards.mkdir()
    monkeypatch.setattr(S, "RAW_DIR", raw)
    monkeypatch.setattr(S, "PARQUET_DIR", shards)
    return raw, shards


def test_appends_archive_tail_as_logged(env):
    raw, shards = env
    _shard(["2026-04-18", "2026-04-19"], ["logged", "logged"]).to_parquet(
        shards / "S1.parquet", index=False)
    _write_raw(raw / "M1.csv", [
        ("m", "2026-04-19T09:00:00", "2026-04-19", 10.2, "", "Missing", ""),
        ("m", "2026-04-20T09:00:00", "2026-04-20", 10.4, "", "Missing", ""),
        ("m", "2026-04-21T09:00:00", "2026-04-21", 10.6, "", "Missing", ""),
    ])
    status, n = S.update_one_shard("S1", "M1")
    assert (status, n) == ("advanced", 2)          # 04-19 already audited
    out = pd.read_parquet(shards / "S1.parquet")
    assert list(out["date"].dt.strftime("%m-%d")) == ["04-18", "04-19", "04-20", "04-21"]
    assert set(out["data_source"]) == {"logged"}
    assert out["date"].is_monotonic_increasing


def test_audited_replaces_live_but_later_live_kept(env):
    raw, shards = env
    # audited to 04-19; live rows on 04-21 (archive will cover) and 04-25 (beyond)
    _shard(["2026-04-19", "2026-04-21", "2026-04-25"],
           ["logged", "logged_live", "logged_live"],
           [10.0, 99.0, 11.5]).to_parquet(shards / "S1.parquet", index=False)
    _write_raw(raw / "M1.csv", [
        ("m", "2026-04-21T09:00:00", "2026-04-21", 10.3, "", "Good", ""),
    ])
    status, n = S.update_one_shard("S1", "M1")
    assert (status, n) == ("advanced", 1)
    out = pd.read_parquet(shards / "S1.parquet").set_index(
        pd.read_parquet(shards / "S1.parquet")["date"].dt.strftime("%m-%d"))
    assert out.loc["04-21", "data_source"] == "logged"      # audited replaced live
    assert out.loc["04-21", "GW_Level"] == 10.3
    assert out.loc["04-25", "data_source"] == "logged_live" # later live untouched


def test_cheap_skip_without_full_read(env, monkeypatch):
    raw, shards = env
    _shard(["2026-04-19"], ["logged"]).to_parquet(shards / "S1.parquet", index=False)
    _write_raw(raw / "M1.csv", [
        ("m", "2026-04-19T09:00:00", "2026-04-19", 10.0, "", "Good", ""),
    ])
    monkeypatch.setattr(S, "_daily_tail",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("full read!")))
    status, n = S.update_one_shard("S1", "M1")
    assert (status, n) == ("current", 0)           # tail hint <= last audited


def test_iqr_screen_drops_wild_tail_values(env):
    raw, shards = env
    # 30 days of ~10.0 history, then a 9999 tail spike + one sane value
    hist = _shard([f"2026-03-{d:02d}" for d in range(1, 31)], ["logged"] * 30,
                  [10.0 + 0.01 * d for d in range(30)])
    hist.to_parquet(shards / "S1.parquet", index=False)
    _write_raw(raw / "M1.csv", [
        ("m", "2026-04-01T09:00:00", "2026-04-01", 9999.0, "", "Good", ""),
        ("m", "2026-04-02T09:00:00", "2026-04-02", 10.5, "", "Good", ""),
    ])
    status, n = S.update_one_shard("S1", "M1")
    assert (status, n) == ("advanced", 1)          # spike dropped, sane row kept
    out = pd.read_parquet(shards / "S1.parquet")
    assert out["GW_Level"].max() < 100


def test_dipped_measure_labelled_dipped(env):
    raw, shards = env
    _shard(["2026-04-19"], ["dipped"]).to_parquet(shards / "S1.parquet", index=False)
    _write_raw(raw / "S1-gw-dipped-i-mAOD-qualified.csv", [
        ("m", "2026-05-10T09:00:00", "2026-05-10", 10.1, "", "Good", ""),
    ])
    status, n = S.update_one_shard("S1", "S1-gw-dipped-i-mAOD-qualified")
    assert (status, n) == ("advanced", 1)
    out = pd.read_parquet(shards / "S1.parquet")
    assert list(out["data_source"]) == ["dipped", "dipped"]


def test_missing_shard_or_raw(env):
    raw, shards = env
    assert S.update_one_shard("nope", "M1") == ("no_shard", 0)
    _shard(["2026-04-19"], ["logged"]).to_parquet(shards / "S1.parquet", index=False)
    assert S.update_one_shard("S1", "M-missing") == ("no_raw", 0)


def test_last_datetime_from_tail(tmp_path):
    p = tmp_path / "m.csv"
    _write_raw(p, [
        ("m", "2026-04-19T09:00:00", "2026-04-19", 10.0, "", "Good", ""),
        ("m", "2026-06-19 08:00:00+00:00", "2026-06-19", 10.2, "", "Good", ""),
    ])
    ts = last_datetime_from_tail(p)
    assert ts is not None and ts.strftime("%Y-%m-%d %H:%M") == "2026-06-19 08:00"
    # header-only file → None (falls back to full read upstream)
    q = tmp_path / "empty.csv"
    q.write_text("measure,dateTime,date,value,completeness,quality,qcode\n")
    assert last_datetime_from_tail(q) is None

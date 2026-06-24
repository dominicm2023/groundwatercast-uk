"""Freshness builder must not present a frozen sensor as 'fresh'.

A live row flagged stuck (data_source='logged_live_stuck') is marked
is_interpolated=0, so it used to count as the last *real* reading → days_since=0,
label='fresh'. v15 now dates freshness from the last NON-stuck reading (so the
label demotes) while keeping the stuck data_source for the detail-panel warning.
"""
from __future__ import annotations

import pandas as pd

import scripts.v15_build_gw_freshness as v15


def _shard(dirpath, sid, dates, sources):
    df = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "GW_Level": [50.0] * len(dates),
        "is_interpolated": [0] * len(dates),
        "data_source": sources,
    })
    df.to_parquet(dirpath / f"{sid}.parquet", index=False)


def test_stuck_tail_demotes_freshness_but_keeps_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shard_dir = tmp_path / "data" / "features" / "gw_by_station"
    shard_dir.mkdir(parents=True)

    today = pd.Timestamp.now().normalize()
    # Last genuine reading ~10 days ago, then a frozen tail up to today.
    dates = [today - pd.Timedelta(days=d) for d in (12, 11, 10, 1, 0)]
    sources = ["logged", "logged", "logged_live",
               "logged_live_stuck", "logged_live_stuck"]
    _shard(shard_dir, "BH", dates, sources)

    v15.main()

    out = pd.read_csv(tmp_path / "data" / "processed" / "gw_freshness.csv")
    row = out[out["station_id"] == "BH"].iloc[0]
    # Dated from the last non-stuck reading (~10 days) → no longer 'fresh'.
    assert row["days_since"] >= 10
    assert row["freshness_label"] != "fresh"
    # ...but the stuck marker survives so the detail panel can still warn.
    assert row["data_source"] == "logged_live_stuck"


def test_non_stuck_live_stays_fresh(tmp_path, monkeypatch):
    # Regression guard: a normal live tail must still read as fresh, today.
    monkeypatch.chdir(tmp_path)
    shard_dir = tmp_path / "data" / "features" / "gw_by_station"
    shard_dir.mkdir(parents=True)
    today = pd.Timestamp.now().normalize()
    dates = [today - pd.Timedelta(days=d) for d in (2, 1, 0)]
    _shard(shard_dir, "BH", dates, ["logged", "logged_live", "logged_live"])

    v15.main()

    out = pd.read_csv(tmp_path / "data" / "processed" / "gw_freshness.csv")
    row = out[out["station_id"] == "BH"].iloc[0]
    assert row["days_since"] == 0
    assert row["freshness_label"] == "fresh"
    assert row["data_source"] == "logged_live"

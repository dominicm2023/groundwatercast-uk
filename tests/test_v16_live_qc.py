"""Regression test: the live-GW refresher must QC readings before they seed
the forecast. apply_qc was dead code (defined + unit-tested in
tests/test_live_levels.py but never invoked in production) until v16 was wired
to call it — so a single telemetry spike used to flow straight into the
per-station shard, freshest_gw(), the status chip and the forecast origin.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import scripts.v16_refresh_live_gw as v16


def _clean_shard(fp):
    """A clean ~400-day history around 10.0 m with a tight spread."""
    n = 400
    hist = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "GW_Level": 10.0 + 0.05 * np.sin(np.arange(n)),
        "is_interpolated": 0,
        "data_source": "logged",
    })
    hist.to_parquet(fp, index=False)


class TestLiveQcWired:
    def test_spike_is_dropped_before_seeding(self, tmp_path, monkeypatch):
        fp = tmp_path / "BH1.parquet"
        _clean_shard(fp)
        monkeypatch.setattr(v16, "PARQUET_DIR", tmp_path)

        # One good live reading + one gross spike (|z| >> 10 vs the ~0.035 std).
        live = pd.DataFrame({
            "dateTime": pd.to_datetime(
                ["2021-01-01T00:00:00Z", "2021-01-02T00:00:00Z"], utc=True),
            "value": [10.10, 1000.0],
        })
        monkeypatch.setattr(v16, "fetch_live_readings",
                            lambda *a, **k: (live, None))

        v16.update_one_shard("BH1", "fm-notation",
                             pd.Timestamp("2020-12-25", tz="UTC"))

        out = pd.read_parquet(fp)
        # The spike must never reach the shard; the good reading must.
        assert out["GW_Level"].max() < 20.0
        assert 1000.0 not in out["GW_Level"].to_numpy()
        assert (out["date"] == pd.Timestamp("2021-01-01")).any()

    def test_clean_live_passes_through(self, tmp_path, monkeypatch):
        fp = tmp_path / "BH2.parquet"
        _clean_shard(fp)
        monkeypatch.setattr(v16, "PARQUET_DIR", tmp_path)
        # A date well beyond the existing history (ends ~2021-02-03) → a true add.
        live = pd.DataFrame({
            "dateTime": pd.to_datetime(["2021-06-01T00:00:00Z"], utc=True),
            "value": [10.12],
        })
        monkeypatch.setattr(v16, "fetch_live_readings",
                            lambda *a, **k: (live, None))
        n_added, _ = v16.update_one_shard("BH2", "fm-notation",
                                          pd.Timestamp("2021-05-25", tz="UTC"))
        out = pd.read_parquet(fp)
        assert n_added == 1
        assert out.loc[out["date"] == pd.Timestamp("2021-06-01"),
                       "GW_Level"].iloc[0] == 10.12

    def test_stuck_sensor_marks_data_source(self, tmp_path, monkeypatch):
        # Two readings 48h apart (> STUCK_THRESHOLD_H=24) with IDENTICAL value
        # near the shard mean (~10.0, std ~0.035) so the z-filter keeps them →
        # apply_qc raises the "stuck_sensor" flag → new rows must be marked.
        fp = tmp_path / "BH3.parquet"
        _clean_shard(fp)
        monkeypatch.setattr(v16, "PARQUET_DIR", tmp_path)
        live = pd.DataFrame({
            "dateTime": pd.to_datetime(
                ["2021-06-01T00:00:00Z", "2021-06-03T00:00:00Z"], utc=True),
            "value": [10.05, 10.05],
        })
        monkeypatch.setattr(v16, "fetch_live_readings",
                            lambda *a, **k: (live, None))
        v16.update_one_shard("BH3", "fm-notation",
                             pd.Timestamp("2021-05-25", tz="UTC"))
        out = pd.read_parquet(fp)
        new_rows = out[out["date"] >= pd.Timestamp("2021-06-01")]
        assert len(new_rows) == 2
        assert (new_rows["data_source"] == "logged_live_stuck").all()

    def test_varying_live_marks_logged_live(self, tmp_path, monkeypatch):
        # Same two timestamps but DIFFERENT values → nunique==2 → no stuck flag
        # → normal "logged_live" marker (negative path / regression guard).
        fp = tmp_path / "BH4.parquet"
        _clean_shard(fp)
        monkeypatch.setattr(v16, "PARQUET_DIR", tmp_path)
        live = pd.DataFrame({
            "dateTime": pd.to_datetime(
                ["2021-06-01T00:00:00Z", "2021-06-03T00:00:00Z"], utc=True),
            "value": [10.05, 10.07],
        })
        monkeypatch.setattr(v16, "fetch_live_readings",
                            lambda *a, **k: (live, None))
        v16.update_one_shard("BH4", "fm-notation",
                             pd.Timestamp("2021-05-25", tz="UTC"))
        out = pd.read_parquet(fp)
        new_rows = out[out["date"] >= pd.Timestamp("2021-06-01")]
        assert len(new_rows) == 2
        assert (new_rows["data_source"] == "logged_live").all()

    def test_live_does_not_clobber_audited_overlap(self, tmp_path, monkeypatch):
        # H3: an audited 'logged' row exists on a date the live window also
        # covers. Live is sensor-grade; audited is canonical — so the audited
        # value must STAND (not be overwritten by the live mean).
        fp = tmp_path / "BH5.parquet"
        _clean_shard(fp)                       # 400 'logged' days from 2020-01-01
        monkeypatch.setattr(v16, "PARQUET_DIR", tmp_path)
        audited_date = pd.Timestamp("2021-01-15")   # within the shard's range
        existing = pd.read_parquet(fp)
        audited_val = existing.loc[existing["date"] == audited_date,
                                   "GW_Level"].iloc[0]
        # 10.1 passes the |z|>10 QC (mean ~10, std ~0.035) but differs from the
        # audited ~10.0 value, so a clobber would be visible.
        live = pd.DataFrame({
            "dateTime": pd.to_datetime(["2021-01-15T00:00:00Z"], utc=True),
            "value": [10.1],
        })
        monkeypatch.setattr(v16, "fetch_live_readings",
                            lambda *a, **k: (live, None))
        n_added, _ = v16.update_one_shard("BH5", "fm-notation",
                                          pd.Timestamp("2021-01-10", tz="UTC"))
        out = pd.read_parquet(fp)
        rows = out[out["date"] == audited_date]
        assert len(rows) == 1                              # no duplicate date
        assert rows["GW_Level"].iloc[0] == audited_val     # audited value stands
        assert rows["data_source"].iloc[0] == "logged"     # not overwritten by live
        assert n_added == 0

    def test_historical_stats_too_short_returns_nan(self):
        # <2 finite points → (nan, nan) so apply_qc skips the z-filter, no crash.
        m, s = v16._historical_stats(pd.DataFrame({"GW_Level": [5.0]}))
        assert np.isnan(m) and np.isnan(s)

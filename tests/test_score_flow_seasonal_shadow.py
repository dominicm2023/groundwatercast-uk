"""Tests for scripts/score_flow_seasonal_shadow.py — low-flow build_plan.md
Stage 6b's closed-month scorer. Offline/pure — no pastas needed."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.score_flow_seasonal_shadow import (
    SCORE_COLS,
    is_closed_month,
    observed_month_stats,
    parse_args,
    run,
    score_archive,
    score_row,
)


TODAY = pd.Timestamp("2026-07-15")


# ---------------------------------------------------------------------------
# is_closed_month
# ---------------------------------------------------------------------------

class TestIsClosedMonth:
    def test_past_month_is_closed(self):
        assert is_closed_month("2026-05-01", TODAY) is True

    def test_current_month_is_open(self):
        assert is_closed_month("2026-07-01", TODAY) is False

    def test_future_month_is_open(self):
        assert is_closed_month("2026-08-01", TODAY) is False

    def test_month_end_boundary(self):
        # June ends 2026-06-30; "today" the day after -> closed.
        assert is_closed_month("2026-06-01", pd.Timestamp("2026-07-01")) is True
        # "today" the last day of June itself -> not yet closed.
        assert is_closed_month("2026-06-01", pd.Timestamp("2026-06-30")) is False


# ---------------------------------------------------------------------------
# observed_month_stats
# ---------------------------------------------------------------------------

class TestObservedMonthStats:
    def _shard(self, dates, flows):
        return pd.DataFrame({"date": dates, "Flow_m3s": flows})

    def test_mean_and_sub_q95_indicator(self):
        dates = pd.date_range("2026-05-01", "2026-05-31", freq="D")
        flows = [1.0] * 30 + [0.01]                # one day dips low
        shard = self._shard(dates, flows)
        mean, hit = observed_month_stats(shard, "2026-05-01", q95_m3s=0.5)
        assert mean == pytest.approx(np.mean(flows))
        assert hit is True

    def test_no_sub_q95_day(self):
        dates = pd.date_range("2026-05-01", "2026-05-31", freq="D")
        shard = self._shard(dates, [1.0] * 31)
        mean, hit = observed_month_stats(shard, "2026-05-01", q95_m3s=0.1)
        assert hit is False

    def test_no_data_that_month_returns_none(self):
        dates = pd.date_range("2026-04-01", "2026-04-30", freq="D")
        shard = self._shard(dates, [1.0] * 30)
        assert observed_month_stats(shard, "2026-05-01", q95_m3s=0.1) is None


# ---------------------------------------------------------------------------
# score_row
# ---------------------------------------------------------------------------

class TestScoreRow:
    def _row(self, p10=0.3, p50=0.6, p90=0.9, p_sub=0.2):
        return pd.Series({"gauge_id": "g1", "run": pd.Timestamp("2026-06-01"),
                          "month_ahead": 1, "month_start": "2026-05-01",
                          "q_p10_m3s": p10, "q_p50_m3s": p50, "q_p90_m3s": p90,
                          "p_sub_q95": p_sub})

    def test_coverage_hit_inside_band(self):
        r = score_row(self._row(), observed_mean=0.6, observed_sub_q95=False)
        assert r["coverage_hit"] is True

    def test_coverage_miss_outside_band(self):
        r = score_row(self._row(), observed_mean=5.0, observed_sub_q95=False)
        assert r["coverage_hit"] is False

    def test_tercile_hit_when_observed_and_p50_same_third(self):
        # p10=0.3, p90=0.9 -> thirds at 0.5, 0.7; p50=0.6 is "near".
        r = score_row(self._row(), observed_mean=0.55, observed_sub_q95=False)
        assert r["tercile_hit"] is True

    def test_tercile_miss_when_observed_in_different_third(self):
        r = score_row(self._row(), observed_mean=0.85, observed_sub_q95=False)
        assert r["tercile_hit"] is False

    def test_brier_perfect_forecast(self):
        r = score_row(self._row(p_sub=1.0), observed_mean=0.6, observed_sub_q95=True)
        assert r["brier"] == pytest.approx(0.0)

    def test_brier_worst_forecast(self):
        r = score_row(self._row(p_sub=0.0), observed_mean=0.6, observed_sub_q95=True)
        assert r["brier"] == pytest.approx(1.0)

    def test_schema(self):
        r = score_row(self._row(), observed_mean=0.6, observed_sub_q95=False)
        assert set(r) == set(SCORE_COLS)


# ---------------------------------------------------------------------------
# score_archive: pure combination
# ---------------------------------------------------------------------------

def test_score_archive_scores_only_closed_rows_with_shard_data():
    archive = pd.DataFrame([
        {"gauge_id": "g1", "run": pd.Timestamp("2026-06-01"), "month_ahead": 1,
         "month_start": "2026-05-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
         "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2},
        {"gauge_id": "g1", "run": pd.Timestamp("2026-06-01"), "month_ahead": 2,
         "month_start": "2026-07-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,   # open
         "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2},
        {"gauge_id": "g2", "run": pd.Timestamp("2026-06-01"), "month_ahead": 1,   # no shard
         "month_start": "2026-05-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
         "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2},
    ])
    shard = pd.DataFrame({"date": pd.date_range("2026-05-01", "2026-05-31", freq="D"),
                          "Flow_m3s": [0.5] * 31})
    scored = score_archive(archive, {"g1": shard}, today=TODAY)
    assert len(scored) == 1
    assert scored.iloc[0]["gauge_id"] == "g1"
    assert scored.iloc[0]["month_ahead"] == 1


def test_score_archive_empty_when_no_closed_months():
    archive = pd.DataFrame([
        {"gauge_id": "g1", "run": pd.Timestamp("2026-06-01"), "month_ahead": 1,
         "month_start": "2026-08-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
         "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2},
    ])
    scored = score_archive(archive, {}, today=TODAY)
    assert scored.empty
    assert list(scored.columns) == SCORE_COLS


# ---------------------------------------------------------------------------
# run(): the "no closed months yet" acceptance path + basic I/O wiring
# ---------------------------------------------------------------------------

def _cfg(archive_path) -> dict:
    return {"forecast": {"flow_seasonal": {"archive_cache": str(archive_path)}}}


def test_run_missing_archive_exits_zero(tmp_path, capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(tmp_path / "absent.parquet")) == 0
    assert "not found" in capsys.readouterr().out


def test_run_empty_archive_exits_zero(tmp_path, capsys):
    archive_path = tmp_path / "shadow.parquet"
    pd.DataFrame(columns=["gauge_id", "run", "month_ahead", "month_start",
                          "q_p10_m3s", "q_p50_m3s", "q_p90_m3s", "p_sub_q95",
                          "q95_m3s"]).to_parquet(archive_path, index=False)
    args = parse_args([])
    assert run(args, cfg=_cfg(archive_path)) == 0
    assert "is empty" in capsys.readouterr().out


def test_run_no_closed_months_yet(tmp_path, capsys):
    archive_path = tmp_path / "shadow.parquet"
    future_month = (pd.Timestamp.now().normalize()
                    + pd.offsets.MonthBegin(2)).date().isoformat()
    pd.DataFrame([{"gauge_id": "g1", "run": pd.Timestamp.now(), "month_ahead": 1,
                  "month_start": future_month, "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
                  "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2}]
                ).to_parquet(archive_path, index=False)
    args = parse_args([])
    assert run(args, cfg=_cfg(archive_path)) == 0
    assert "No closed months yet" in capsys.readouterr().out


def test_run_closed_month_no_matching_shard(tmp_path, capsys, monkeypatch):
    import scripts.score_flow_seasonal_shadow as S
    monkeypatch.setattr(S, "FLOW_SHARD_DIR", tmp_path / "no_shards_here")
    archive_path = tmp_path / "shadow.parquet"
    pd.DataFrame([{"gauge_id": "g1", "run": pd.Timestamp("2026-01-01"), "month_ahead": 1,
                  "month_start": "2026-01-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
                  "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2}]
                ).to_parquet(archive_path, index=False)
    args = parse_args([])
    assert run(args, cfg=_cfg(archive_path)) == 0
    assert "none have matching shard observations" in capsys.readouterr().out


def test_run_scores_closed_month_with_shard(tmp_path, capsys, monkeypatch):
    import scripts.score_flow_seasonal_shadow as S
    shard_dir = tmp_path / "flow_by_station"
    shard_dir.mkdir()
    dates = pd.date_range("2026-01-01", "2026-01-31", freq="D")
    pd.DataFrame({"date": dates, "Flow_m3s": [0.6] * 31}).to_parquet(
        shard_dir / "g1.parquet", index=False)
    monkeypatch.setattr(S, "FLOW_SHARD_DIR", shard_dir)

    archive_path = tmp_path / "shadow.parquet"
    pd.DataFrame([{"gauge_id": "g1", "run": pd.Timestamp("2025-12-15"), "month_ahead": 1,
                  "month_start": "2026-01-01", "q_p10_m3s": 0.3, "q_p50_m3s": 0.6,
                  "q_p90_m3s": 0.9, "p_sub_q95": 0.1, "q95_m3s": 0.2}]
                ).to_parquet(archive_path, index=False)
    args = parse_args([])
    assert run(args, cfg=_cfg(archive_path)) == 0
    out = capsys.readouterr().out
    assert "Scored 1 closed" in out
    assert "coverage" in out

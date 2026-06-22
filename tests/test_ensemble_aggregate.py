"""Offline unit tests for Phase 3 aggregation + threshold resolution."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.forecast.ensemble import thresholds, aggregate


# ---------------------------------------------------------------------------
# Threshold resolution (priority order)
# ---------------------------------------------------------------------------

@pytest.fixture
def user_thresholds_file(tmp_path, monkeypatch):
    """Point the module at a temp user-thresholds YAML and clear its caches."""
    fp = tmp_path / "user_thresholds.yaml"

    def write(text: str):
        fp.write_text(text, encoding="utf-8")
        thresholds.reload()
        return fp

    monkeypatch.setattr(thresholds, "_USER_THRESHOLDS", fp)
    thresholds.reload()
    yield write
    thresholds.reload()


class TestUserThresholds:
    def test_missing_file_yields_empty(self, user_thresholds_file):
        assert thresholds.load_user_thresholds() == {}
        assert thresholds.user_threshold_station_ids() == frozenset()

    def test_loads_station_ids_and_levels(self, user_thresholds_file):
        user_thresholds_file(
            "thresholds:\n"
            "  - station_id: a\n    mAOD: 41.2\n    label: cellar\n"
            "  - station_id: b\n    mAOD: 12.0\n"
        )
        assert thresholds.load_user_thresholds() == {"a": 41.2, "b": 12.0}
        assert thresholds.user_threshold_station_ids() == frozenset({"a", "b"})

    def test_duplicate_station_keeps_most_severe(self, user_thresholds_file):
        user_thresholds_file(
            "thresholds:\n"
            "  - station_id: a\n    mAOD: 41.2\n"
            "  - station_id: a\n    mAOD: 43.0\n"
        )
        assert thresholds.load_user_thresholds() == {"a": 43.0}

    def test_malformed_yaml_yields_empty(self, user_thresholds_file):
        user_thresholds_file("thresholds: [unclosed")
        assert thresholds.load_user_thresholds() == {}


class TestResolveThreshold:
    def test_priority_user_over_proxy_over_none(self, user_thresholds_file):
        user_thresholds_file("thresholds:\n  - station_id: a\n    mAOD: 50.0\n")
        assert thresholds.resolve_threshold("a") == (50.0, "user")
        assert thresholds.resolve_threshold("a", gw_p90=80.0) == (50.0, "user")
        assert thresholds.resolve_threshold("c", gw_p90=80.0) == (80.0, "gw_p90_proxy")
        assert thresholds.resolve_threshold("c") == (None, "none")


# ---------------------------------------------------------------------------
# Breach stats + fan
# ---------------------------------------------------------------------------

def _traj(member_paths: dict[int, list[float]], start="2026-01-01"):
    """member_paths: {member: [gw day1..dayH]} -> long traj frame."""
    dates = pd.date_range(start, periods=len(next(iter(member_paths.values()))))
    rows = []
    for m, path in member_paths.items():
        for d, gw in zip(dates, path):
            rows.append({"station_id": "s1", "member": m, "date": d, "gw_pred": gw})
    return pd.DataFrame(rows)


class TestBreachStats:
    def test_probability_and_censoring(self):
        # 2 of 4 members cross T=10 within the window
        traj = _traj({0: [8, 9, 11], 1: [8, 8, 12], 2: [8, 8, 9], 3: [7, 7, 8]})
        s = aggregate.breach_stats(traj, threshold=10.0)
        assert s["p_breach"] == pytest.approx(0.5)
        assert s["censored_frac"] == pytest.approx(0.5)

    def test_first_crossing_lead(self):
        # member 0 crosses on day 3, member 1 on day 2 -> median lead 2.5
        traj = _traj({0: [8, 9, 11], 1: [8, 12, 13]})
        s = aggregate.breach_stats(traj, threshold=10.0)
        assert s["first_cross_median_lead"] == pytest.approx(2.5)
        # rounds to lead 2 (or 3) -> a real date within the window
        assert pd.notna(s["first_cross_median"])

    def test_no_crossers(self):
        traj = _traj({0: [1, 2, 3], 1: [1, 1, 2]})
        s = aggregate.breach_stats(traj, threshold=10.0)
        assert s["p_breach"] == 0.0
        assert pd.isna(s["first_cross_median"])

    def test_none_threshold_returns_fan_only(self):
        traj = _traj({0: [1, 2, 3]})
        s = aggregate.breach_stats(traj, threshold=None)
        assert np.isnan(s["p_breach"])
        assert s["gw_p50_end"] == pytest.approx(3.0)

    def test_fan_quantiles_shape(self):
        traj = _traj({m: [float(m), float(m + 1)] for m in range(11)})
        fan = aggregate.fan_quantiles(traj)
        assert list(fan["lead"]) == [1, 2]
        assert (fan["gw_p90"] >= fan["gw_p50"]).all()
        assert (fan["gw_p50"] >= fan["gw_p10"]).all()


# ---------------------------------------------------------------------------
# Headline sentence + aggregate orchestration
# ---------------------------------------------------------------------------

class TestHeadlineAndAggregate:
    def test_headline_with_breach(self):
        row = {"threshold_source": "user", "threshold": 10.0,
               "horizon_days": 3, "p_breach": 0.62,
               "first_cross_median": pd.Timestamp("2026-02-12"),
               "first_cross_p25": pd.Timestamp("2026-02-05"),
               "first_cross_p75": pd.Timestamp("2026-02-22"),
               "censored_frac": 0.38}
        s = aggregate.headline_sentence(row)
        assert "62%" in s and "10.0 mAOD" in s and "uncalibrated" in s

    def test_headline_dual_window_reconciles_46d_and_14d(self):
        # 46-day horizon: the sentence must state BOTH windows so it can't
        # contradict the 14-day tier the dashboard shows.
        row = {"threshold_source": "user", "threshold": 10.0,
               "horizon_days": 46, "p_breach": 0.62, "p_breach_14d": 0.38,
               "first_cross_median": pd.Timestamp("2026-02-12"),
               "first_cross_p25": pd.Timestamp("2026-02-05"),
               "first_cross_p75": pd.Timestamp("2026-02-22"),
               "censored_frac": 0.38}
        s = aggregate.headline_sentence(row)
        assert "62%" in s and "within 46 days" in s
        assert "38% within the 14-day operational window" in s

    def test_headline_no_dual_window_at_short_horizon(self):
        # At a 14-day horizon the windows coincide → no parenthetical.
        row = {"threshold_source": "user", "threshold": 10.0,
               "horizon_days": 14, "p_breach": 0.5, "p_breach_14d": 0.5,
               "first_cross_median": pd.Timestamp("2026-02-12"),
               "first_cross_p25": pd.Timestamp("2026-02-05"),
               "first_cross_p75": pd.Timestamp("2026-02-22"),
               "censored_frac": 0.5}
        assert "operational window" not in aggregate.headline_sentence(row)

    def test_headline_proxy_labelled(self):
        row = {"threshold_source": "gw_p90_proxy", "threshold": 10.0,
               "horizon_days": 3, "p_breach": 0.0}
        assert "proxy threshold" in aggregate.headline_sentence(row)

    def test_headline_zero_prob_floor_from_members(self):
        row = {"threshold_source": "user", "threshold": 10.0,
               "horizon_days": 14, "p_breach": 0.0, "n_members": 51}
        s = aggregate.headline_sentence(row)
        assert "<2.0%" in s and "no members cross" in s

    def test_headline_zero_prob_floor_from_samples(self):
        # Pastas Monte-Carlo basis: 4000 samples → 1/n below the 0.1% display floor.
        row = {"threshold_source": "user", "threshold": 10.0,
               "horizon_days": 14, "p_breach": 0.0,
               "n_members": 35, "n_samples": 4000}
        s = aggregate.headline_sentence(row)
        assert "<0.1%" in s and "no sampled trajectories cross" in s

    def test_headline_no_threshold(self):
        assert "without a breach probability" in aggregate.headline_sentence(
            {"threshold_source": "none", "threshold": np.nan})

    def test_aggregate_outputs(self, monkeypatch):
        monkeypatch.setattr(thresholds, "load_user_thresholds", lambda: {"s1": 10.0})
        traj = _traj({0: [8, 9, 11], 1: [8, 8, 8]})
        summary, fan = aggregate.aggregate(traj, run=pd.Timestamp("2026-06-07"))
        assert len(summary) == 1
        assert summary.iloc[0]["p_breach"] == pytest.approx(0.5)
        assert summary.iloc[0]["threshold_source"] == "user"
        assert set(["station_id", "lead", "gw_p50"]).issubset(fan.columns)


class TestScopeProvenance:
    def test_scope_column_carried_into_summary(self, monkeypatch):
        monkeypatch.setattr(thresholds, "load_user_thresholds", lambda: {})
        traj = _traj({0: [8, 9, 11], 1: [8, 8, 8]})
        traj["scope"] = "live"
        summary, _ = aggregate.aggregate(traj, run=pd.Timestamp("2026-06-07"))
        assert summary.iloc[0]["scope"] == "live"

    def test_legacy_parquet_without_scope_gets_unknown(self, monkeypatch):
        monkeypatch.setattr(thresholds, "load_user_thresholds", lambda: {})
        traj = _traj({0: [8, 9, 11]})
        summary, _ = aggregate.aggregate(traj, run=pd.Timestamp("2026-06-07"))
        assert summary.iloc[0]["scope"] == "unknown"

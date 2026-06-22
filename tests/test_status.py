"""Tests for the current-status-vs-normal layer (src/dashboard/status.py)
— the risk index's replacement.

Covers: percentile interpolation over the monthly quantile ladder, the
below/near/above classification, the full ``current_status`` path (with a
monkeypatched shard reader), chip formatting, and the tolerant
``attach_current_status`` join contract (missing normals/shards → grey,
never an exception; status is a tie-break only).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dashboard import status as ST


def _qrow(p10=48.0, t1=49.0, median=50.0, t2=51.0, p90=52.0) -> pd.Series:
    return pd.Series({"p10": p10, "t1": t1, "median": median,
                      "t2": t2, "p90": p90})


def _normals(sid="BH1", months=range(1, 13)) -> pd.DataFrame:
    return pd.DataFrame([{"station_id": sid, "month": m, "p10": 48.0,
                          "t1": 49.0, "median": 50.0, "t2": 51.0,
                          "p90": 52.0, "n_years": 10} for m in months])


def _shard(levels, end="2026-06-10"):
    idx = pd.date_range(end=end, periods=len(levels), freq="D")
    return pd.Series(np.asarray(levels, dtype=float), index=idx,
                     name="GW_Level")


@pytest.fixture
def fake_shard(monkeypatch):
    """Patch seeding.freshest_gw with a {sid: Series} lookup."""
    store: dict[str, pd.Series] = {}

    def _fake(sid, fallback=None):
        return store.get(sid, pd.Series(dtype="float64", name="GW_Level"))

    import src.forecast.ensemble.seeding as seeding
    monkeypatch.setattr(seeding, "freshest_gw", _fake)
    return store


# ---------------------------------------------------------------------------
# 1. Percentile interpolation
# ---------------------------------------------------------------------------

class TestPercentileOf:
    def test_at_stored_quantiles(self):
        q = _qrow()
        assert ST.percentile_of(50.0, q) == pytest.approx(50.0)
        assert ST.percentile_of(48.0, q) == pytest.approx(10.0)
        assert ST.percentile_of(52.0, q) == pytest.approx(90.0)

    def test_interpolates_between_quantiles(self):
        # halfway between median (50th) and t2 (66.7th)
        p = ST.percentile_of(50.5, _qrow())
        assert 50.0 < p < 200 / 3

    def test_clamped_in_the_tails(self):
        q = _qrow()
        assert ST.percentile_of(40.0, q) == 2.0
        assert ST.percentile_of(60.0, q) == 98.0

    def test_nan_for_bad_ladder(self):
        # NaN quantile or non-monotone ladder can't yield a percentile
        assert np.isnan(ST.percentile_of(50.0, _qrow(t1=float("nan"))))
        assert np.isnan(ST.percentile_of(50.0, _qrow(t1=51.5)))  # t1 > median


# ---------------------------------------------------------------------------
# 1b. SGI (ladder-based Standardised Groundwater Index approximation)
# ---------------------------------------------------------------------------

class TestSgi:
    def test_midpoint(self):
        assert ST.sgi_from_percentile(50.0) == pytest.approx(0.0, abs=1e-9)

    def test_low_clamp(self):
        # percentile clamps at 2 -> Phi^-1(0.02) ~= -2.0537
        assert ST.sgi_from_percentile(2.0) == pytest.approx(-2.0537, abs=1e-3)

    def test_high_clamp(self):
        assert ST.sgi_from_percentile(98.0) == pytest.approx(2.0537, abs=1e-3)

    def test_none(self):
        assert ST.sgi_from_percentile(None) is None

    def test_nan(self):
        assert ST.sgi_from_percentile(float("nan")) is None


# ---------------------------------------------------------------------------
# 2. Classification
# ---------------------------------------------------------------------------

class TestStatusOf:
    def test_terciles(self):
        q = _qrow()
        assert ST.status_of(48.5, q) == "below"
        assert ST.status_of(50.0, q) == "near"
        assert ST.status_of(51.5, q) == "above"

    def test_boundaries_are_near(self):
        q = _qrow()
        assert ST.status_of(49.0, q) == "near"   # exactly t1
        assert ST.status_of(51.0, q) == "near"   # exactly t2


# ---------------------------------------------------------------------------
# 3. current_status
# ---------------------------------------------------------------------------

class TestCurrentStatus:
    NOW = pd.Timestamp("2026-06-12")

    def test_full_path(self, fake_shard):
        # 30 flat days then a rise — fresh obs, rising trend, above normal
        fake_shard["BH1"] = _shard([50.0] * 25 + [51.2, 51.4, 51.6, 51.8, 52.0])
        st = ST.current_status("BH1", _normals(), now=self.NOW)
        assert st["status"] == "above"
        assert st["trend"] == "rising"
        assert st["level"] == 52.0
        assert st["month"] == 6
        assert st["age_days"] == 2.0
        assert 66.0 < st["percentile"] <= 98.0
        assert st["sgi"] is not None and st["sgi"] > 0   # above-normal -> positive

    def test_trend_stable_and_falling(self, fake_shard):
        fake_shard["BH1"] = _shard([50.0] * 30)
        assert ST.current_status("BH1", _normals(),
                                 now=self.NOW)["trend"] == "stable"
        fake_shard["BH1"] = _shard(list(np.linspace(50.0, 49.0, 30)))
        assert ST.current_status("BH1", _normals(),
                                 now=self.NOW)["trend"] == "falling"

    def test_stale_observation_gets_no_status(self, fake_shard):
        # Last observation 60 days ago — trend/level still reported,
        # but no status claim
        fake_shard["BH1"] = _shard([50.0] * 30, end="2026-04-13")
        st = ST.current_status("BH1", _normals(), now=self.NOW)
        assert st["status"] is None
        assert st["sgi"] is None                 # no status -> no SGI
        assert st["age_days"] == 60.0
        assert st["level"] == 50.0

    def test_missing_shard_all_none(self, fake_shard):
        st = ST.current_status("NOPE", _normals(), now=self.NOW)
        assert st["status"] is None and st["trend"] is None
        assert np.isnan(st["level"])

    def test_missing_month_row_no_status(self, fake_shard):
        fake_shard["BH1"] = _shard([50.0] * 30)
        st = ST.current_status("BH1", _normals(months=[1, 2]), now=self.NOW)
        assert st["status"] is None          # June not in the normals
        assert st["level"] == 50.0           # level still surfaced

    def test_empty_normals_no_status(self, fake_shard):
        fake_shard["BH1"] = _shard([50.0] * 30)
        st = ST.current_status("BH1", pd.DataFrame(), now=self.NOW)
        assert st["status"] is None


# ---------------------------------------------------------------------------
# 4. Chips
# ---------------------------------------------------------------------------

class TestStatusChip:
    def test_full_chip(self):
        chip = ST.status_chip("above", "rising", 84.0)
        assert chip == "🔵 above normal ↑ (84th pct)"

    def test_minimal_chip(self):
        assert ST.status_chip("below") == "🟡 below normal"

    def test_ordinal_suffixes(self):
        assert "51st pct" in ST.status_chip("near", None, 51.0)
        assert "42nd pct" in ST.status_chip("near", None, 42.0)
        assert "33rd pct" in ST.status_chip("near", None, 33.0)
        assert "11th pct" in ST.status_chip("near", None, 11.0)
        assert "12th pct" in ST.status_chip("near", None, 12.4)

    def test_unknown(self):
        assert ST.status_chip(None) == ST.UNKNOWN_CHIP
        assert ST.status_chip(float("nan")) == ST.UNKNOWN_CHIP

    def test_nan_percentile_omitted(self):
        assert "pct" not in ST.status_chip("near", "stable", float("nan"))


# ---------------------------------------------------------------------------
# 5. attach_current_status join contract
# ---------------------------------------------------------------------------

class TestAttachCurrentStatus:
    NOW = pd.Timestamp("2026-06-12")

    def _triage(self, sids):
        return pd.DataFrame({
            "station_id": sids,
            "tier_rank": [1] * len(sids),
            "is_fresh": [True] * len(sids),
            "adjusted_score": list(range(len(sids), 0, -1)),
        })

    def test_adds_columns_and_does_not_mutate(self, fake_shard):
        fake_shard["A"] = _shard([50.0] * 30)
        triage = self._triage(["A", "B"])
        before = triage.copy()
        out = ST.attach_current_status(triage, _normals("A"), now=self.NOW)
        for c in ("status_now", "status_percentile", "status_trend",
                  "status_age_days", "status_rank"):
            assert c in out.columns
        pd.testing.assert_frame_equal(triage, before)
        # A resolved; B (no shard) grey but present
        assert out.loc[out.station_id == "A", "status_now"].iloc[0] == "near"
        assert out.loc[out.station_id == "B", "status_now"].isna().all()

    def test_status_is_tie_break_only(self, fake_shard):
        # Same tier/freshness/score → above-normal sorts first
        fake_shard["LOW"] = _shard([48.5] * 30)   # below
        fake_shard["HIGH"] = _shard([51.5] * 30)  # above
        triage = self._triage(["LOW", "HIGH"])
        triage["adjusted_score"] = [1.0, 1.0]
        norm = pd.concat([_normals("LOW"), _normals("HIGH")],
                         ignore_index=True)
        out = ST.attach_current_status(triage, norm, now=self.NOW)
        assert list(out["station_id"]) == ["HIGH", "LOW"]

    def test_tier_still_dominates_status(self, fake_shard):
        # A worse tier must outrank a better status
        fake_shard["T1"] = _shard([48.5] * 30)    # below normal, tier 1
        fake_shard["T2"] = _shard([51.5] * 30)    # above normal, tier 2
        triage = self._triage(["T1", "T2"])
        triage["tier_rank"] = [1, 2]
        norm = pd.concat([_normals("T1"), _normals("T2")],
                         ignore_index=True)
        out = ST.attach_current_status(triage, norm, now=self.NOW)
        assert list(out["station_id"]) == ["T1", "T2"]

    def test_empty_triage(self):
        out = ST.attach_current_status(pd.DataFrame(), pd.DataFrame())
        assert "status_now" in out.columns and out.empty

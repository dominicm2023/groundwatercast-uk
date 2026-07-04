"""Offline unit tests for the per-member forecast chain (Phase 2)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.build_ensemble_members import resolve_provider_name
from src.features.build import compute_weibull_kernel
from src.forecast.ensemble import bias
from src.forecast.ensemble.members import (
    _FRESH_SEED_DGW_MAX_GAP_DAYS, _seed_gw_dgw, forecast_recharge,
    member_trajectories,
)


# ---------------------------------------------------------------------------
# Bias factor
# ---------------------------------------------------------------------------

class TestBiasFactor:
    def test_mean_ratio(self):
        idx = pd.date_range("2024-01-01", periods=40)
        gauge = pd.Series(4.0, index=idx)
        ref = pd.Series(2.0, index=idx)
        assert bias.fit_bias_factor(gauge, ref) == pytest.approx(2.0)

    def test_zero_reference_guard(self):
        idx = pd.date_range("2024-01-01", periods=40)
        assert bias.fit_bias_factor(pd.Series(4.0, index=idx),
                                    pd.Series(0.0, index=idx)) == 1.0

    def test_insufficient_overlap_returns_one(self):
        idx = pd.date_range("2024-01-01", periods=5)
        assert bias.fit_bias_factor(pd.Series(4.0, index=idx),
                                    pd.Series(2.0, index=idx)) == 1.0

    def test_explosive_ratio_is_clamped(self):
        # Near-zero ref mean that still clears the 1e-6 guard → raw ratio ~50x.
        idx = pd.date_range("2024-01-01", periods=40)
        f = bias.fit_bias_factor(pd.Series(2.5, index=idx),
                                 pd.Series(0.05, index=idx))
        assert f == pytest.approx(bias.F_BH_MAX)

    def test_tiny_ratio_is_clamped(self):
        idx = pd.date_range("2024-01-01", periods=40)
        f = bias.fit_bias_factor(pd.Series(0.01, index=idx),
                                 pd.Series(5.0, index=idx))
        assert f == pytest.approx(bias.F_BH_MIN)


# ---------------------------------------------------------------------------
# Bias factor persistence (upsert — H2 clobber/provenance fix)
# ---------------------------------------------------------------------------

def _seed_bias_csv(tmp_path, monkeypatch):
    """Point bias.BIAS_PATH at a tmp CSV pre-seeded with two stations."""
    path = tmp_path / "ensemble_bias_factors.csv"
    monkeypatch.setattr(bias, "BIAS_PATH", path)
    pd.DataFrame([
        {"station_id": "A", "f_bh": 1.2, "overlap_start": "2023-01-01",
         "overlap_end": "2025-01-01", "fitted_on": "2025-01-15", "note": ""},
        {"station_id": "B", "f_bh": 0.9, "overlap_start": "2023-01-01",
         "overlap_end": "2025-01-01", "fitted_on": "2025-01-15", "note": ""},
    ]).to_csv(path, index=False)
    return path


class TestUpsertBiasFactors:
    def test_preserves_unrelated_rows_and_provenance(self, tmp_path, monkeypatch):
        # A narrow run touching only B + new C must not truncate or re-stamp A.
        path = _seed_bias_csv(tmp_path, monkeypatch)
        bias.upsert_bias_factors(pd.DataFrame([
            {"station_id": "B", "f_bh": 1.5, "overlap_start": "2024-06-01",
             "overlap_end": "2026-05-10", "fitted_on": "2026-06-09", "note": ""},
            {"station_id": "C", "f_bh": 1.1, "overlap_start": "2024-06-01",
             "overlap_end": "2026-05-10", "fitted_on": "2026-06-09", "note": ""},
        ]))
        out = pd.read_csv(path).set_index("station_id")
        assert sorted(out.index) == ["A", "B", "C"]          # nothing truncated
        assert out.loc["A", "f_bh"] == pytest.approx(1.2)    # untouched
        assert out.loc["A", "fitted_on"] == "2025-01-15"     # not re-stamped
        assert out.loc["B", "f_bh"] == pytest.approx(1.5)    # updated
        assert out.loc["B", "fitted_on"] == "2026-06-09"
        assert out.loc["C", "fitted_on"] == "2026-06-09"     # inserted

    def test_creates_file_when_absent(self, tmp_path, monkeypatch):
        path = tmp_path / "ensemble_bias_factors.csv"
        monkeypatch.setattr(bias, "BIAS_PATH", path)
        bias.upsert_bias_factors(pd.DataFrame([
            {"station_id": "X", "f_bh": 1.0, "overlap_start": "2024-06-01",
             "overlap_end": "2026-05-10", "fitted_on": "2026-06-09", "note": ""},
        ]))
        out = pd.read_csv(path)
        assert out["station_id"].tolist() == ["X"]

    def test_empty_rows_leaves_existing_intact(self, tmp_path, monkeypatch):
        path = _seed_bias_csv(tmp_path, monkeypatch)
        bias.upsert_bias_factors(pd.DataFrame())
        assert pd.read_csv(path)["station_id"].tolist() == ["A", "B"]


# ---------------------------------------------------------------------------
# Provider default resolution (H1 — config `provider` must win)
# ---------------------------------------------------------------------------

class TestResolveProviderName:
    def test_config_provider_beats_dev_provider(self):
        ens = {"provider": "ecmwf_opendata", "dev_provider": "open_meteo"}
        assert resolve_provider_name(ens) == ("ecmwf_opendata", False)

    def test_dev_provider_when_no_provider_key(self):
        assert resolve_provider_name({"dev_provider": "dev_x"}) == ("dev_x", False)

    def test_hardcoded_default_when_config_empty(self):
        assert resolve_provider_name({}) == ("open_meteo", False)

    def test_explicit_cli_wins_and_is_flagged_explicit(self):
        ens = {"provider": "ecmwf_opendata", "dev_provider": "open_meteo"}
        assert resolve_provider_name(ens, "open_meteo") == ("open_meteo", True)

    def test_dev_fallback_returns_dev_provider_loudly(self, tmp_path, capsys):
        from scripts.build_ensemble_members import _dev_fallback
        p = _dev_fallback({"dev_provider": "open_meteo"}, "ecmwf_opendata",
                          ImportError("no GRIB stack"), tmp_path)
        assert p.name == "open_meteo"
        assert "WARNING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Forecast recharge (bridge + convolve)
# ---------------------------------------------------------------------------

class TestForecastRecharge:
    def test_lag1_kernel_is_previous_day_rain(self):
        # kernel lag_days=1 -> recharge[t] = rainfall[t-1]
        kernel = compute_weibull_kernel(1.5, 5.0, 1)
        assert kernel.sum() == pytest.approx(1.0)
        observed = pd.Series([5.0, 6.0],
                             index=pd.to_datetime(["2026-06-05", "2026-06-06"]))
        member = pd.Series([7.0, 8.0],
                           index=pd.to_datetime(["2026-06-07", "2026-06-08"]))
        fdates = pd.DatetimeIndex(pd.to_datetime(["2026-06-07", "2026-06-08"]))
        rech = forecast_recharge(observed, member, kernel, fdates)
        # 06-07 recharge = rain(06-06)=6 ; 06-08 recharge = rain(06-07)=7
        assert rech.loc["2026-06-07"] == pytest.approx(6.0)
        assert rech.loc["2026-06-08"] == pytest.approx(7.0)

    def test_forecast_wins_on_overlap(self):
        kernel = compute_weibull_kernel(1.5, 5.0, 1)
        observed = pd.Series([5.0], index=pd.to_datetime(["2026-06-07"]))
        member = pd.Series([99.0], index=pd.to_datetime(["2026-06-07"]))  # overlaps
        fdates = pd.DatetimeIndex(pd.to_datetime(["2026-06-08"]))
        # bridged 06-07 should be the member's 99, so recharge(06-08)=99
        rech = forecast_recharge(observed, member, kernel, fdates)
        assert rech.loc["2026-06-08"] == pytest.approx(99.0)


# ---------------------------------------------------------------------------
# Seed level + one-day momentum (freshest-shard reseed; non-daily-gap fix)
# ---------------------------------------------------------------------------

class TestSeedGwDgw:
    def _hist(self):
        # Joined-feature tail: last true one-day delta = 30.0 - 29.9 = +0.1.
        idx = pd.date_range("2026-05-01", periods=5, freq="D")
        return pd.DataFrame(
            {"GW_Level": [29.5, 29.6, 29.7, 29.9, 30.0],
             "GW_Lag1":  [29.4, 29.5, 29.6, 29.7, 29.9]},
            index=idx)

    def test_no_fresh_uses_hist_daily_delta(self):
        seed_gw, seed_dgw = _seed_gw_dgw(self._hist(), pd.Series(dtype="float64"))
        assert seed_gw == pytest.approx(30.0)
        assert seed_dgw == pytest.approx(0.1)

    def test_stale_fresh_not_more_recent_keeps_hist(self):
        # Fresh tail is older than the feature tail → no reseed at all.
        fresh = pd.Series([20.0, 21.0],
                          index=pd.to_datetime(["2026-04-01", "2026-04-10"]))
        seed_gw, seed_dgw = _seed_gw_dgw(self._hist(), fresh)
        assert seed_gw == pytest.approx(30.0)
        assert seed_dgw == pytest.approx(0.1)

    def test_consecutive_fresh_uses_raw_delta(self):
        # Last two shard obs one day apart → per-day rate == raw delta.
        fresh = pd.Series([31.4, 31.5],
                          index=pd.to_datetime(["2026-06-09", "2026-06-10"]))
        seed_gw, seed_dgw = _seed_gw_dgw(self._hist(), fresh)
        assert seed_gw == pytest.approx(31.5)        # level reseeded to freshest
        assert seed_dgw == pytest.approx(0.1)        # 1-day gap → unchanged

    def test_gapped_fresh_is_divided_to_daily_rate(self):
        # THE BUG: last two obs 8 days apart spanning +0.8 m. Must seed the
        # per-DAY rate 0.1, not the raw 0.8 (which is 8x the true daily momentum).
        fresh = pd.Series([30.7, 31.5],
                          index=pd.to_datetime(["2026-06-02", "2026-06-10"]))
        seed_gw, seed_dgw = _seed_gw_dgw(self._hist(), fresh)
        assert seed_gw == pytest.approx(31.5)
        assert seed_dgw == pytest.approx(0.8 / 8)    # 0.1 m/day, not 0.8

    def test_very_stale_gap_falls_back_to_hist_delta(self):
        # Gap beyond the cap → momentum is stale; keep the hist daily delta,
        # but still reseed the LEVEL to the freshest observation.
        gap = _FRESH_SEED_DGW_MAX_GAP_DAYS + 10
        start = pd.Timestamp("2026-06-10") - pd.Timedelta(days=gap)
        fresh = pd.Series([29.0, 31.5],
                          index=pd.to_datetime([start, pd.Timestamp("2026-06-10")]))
        seed_gw, seed_dgw = _seed_gw_dgw(self._hist(), fresh)
        assert seed_gw == pytest.approx(31.5)        # level still reseeded
        assert seed_dgw == pytest.approx(0.1)        # momentum falls back to hist


# ---------------------------------------------------------------------------
# Member trajectories
# ---------------------------------------------------------------------------

def _history(n=80):
    rng = np.random.RandomState(0)
    recharge = rng.uniform(0, 5, n)
    dgw = 0.1 * recharge                       # GW responds to recharge
    gw = 50 + np.cumsum(dgw - dgw.mean())
    doy = rng.randint(1, 366, n)
    idx = pd.date_range("2025-01-01", periods=n, tz="UTC")
    return pd.DataFrame({
        "GW_Level": gw, "GW_Lag1": np.r_[gw[0], gw[:-1]],
        "Recharge_Weibull": recharge,
        "Sin_DOY": np.sin(2 * np.pi * doy / 365.25),
        "Cos_DOY": np.cos(2 * np.pi * doy / 365.25),
    }, index=idx)


class TestMemberTrajectories:
    def _members(self):
        dates = pd.to_datetime(["2026-06-07", "2026-06-08", "2026-06-09"])
        rows = []
        for m, scale in [(0, 1.0), (1, 5.0)]:        # member 1 much wetter
            for d in dates:
                rows.append({"member": m, "date": d, "precip_mm": scale})
        return pd.DataFrame(rows)

    def test_shape_and_columns(self):
        kernel = compute_weibull_kernel(1.8, 10.0, 45)
        observed = pd.Series(2.0, index=pd.date_range("2026-04-01", "2026-06-06"))
        traj = member_trajectories("s1", self._members(), _history(), kernel,
                                   f_bh=1.0, observed_rain=observed)
        assert list(traj.columns) == ["station_id", "member", "date",
                                       "precip_mm", "recharge_weibull", "gw_pred"]
        assert traj["member"].nunique() == 2
        assert traj["date"].nunique() == 3
        assert traj["gw_pred"].notna().all()

    def test_wetter_member_ends_higher(self):
        # Recharge-driven response (positive coef) -> wetter member -> higher GW.
        kernel = compute_weibull_kernel(1.5, 5.0, 3)
        observed = pd.Series(2.0, index=pd.date_range("2026-04-01", "2026-06-06"))
        traj = member_trajectories("s1", self._members(), _history(), kernel,
                                   f_bh=1.0, observed_rain=observed)
        end = traj["date"].max()
        gw_dry = traj[(traj.member == 0) & (traj.date == end)]["gw_pred"].iloc[0]
        gw_wet = traj[(traj.member == 1) & (traj.date == end)]["gw_pred"].iloc[0]
        assert gw_wet > gw_dry

    def test_bias_factor_scales_precip(self):
        kernel = compute_weibull_kernel(1.8, 10.0, 45)
        observed = pd.Series(2.0, index=pd.date_range("2026-04-01", "2026-06-06"))
        traj = member_trajectories("s1", self._members(), _history(), kernel,
                                   f_bh=2.0, observed_rain=observed)
        # member 0 precip was 1.0 -> *2.0 = 2.0
        assert traj[traj.member == 0]["precip_mm"].iloc[0] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# observed_daily_rainfall — broken-gauge screen
# ---------------------------------------------------------------------------

def test_observed_daily_rainfall_excludes_implausible_gauge(tmp_path):
    """A broken/cumulative 'rainfall' measure (long-run mean far above any real
    UK gauge — seen live at 128 mm/day) must be excluded from the top-3 average,
    not poison the recharge forcing for every downstream consumer."""
    import pandas as pd
    from src.forecast.ensemble.members import observed_daily_rainfall

    raw = tmp_path / "rainfall"
    raw.mkdir(parents=True)
    dates = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")

    def write(mid, values):
        pd.DataFrame({"dateTime": dates, "value": values}).to_csv(
            raw / f"{mid}.csv", index=False)

    write("good-1", [2.0] * 200)          # plausible gauge
    write("good-2", [3.0] * 200)          # plausible gauge
    write("broken", [130.0] * 200)        # cumulative/garbage series

    s = observed_daily_rainfall(["good-1", "good-2", "broken"], str(tmp_path))
    assert len(s) == 200
    # average of the two GOOD gauges only — the broken one is screened out
    assert abs(float(s.mean()) - 2.5) < 1e-9

    # all-broken -> empty series (callers fall back to the joined column)
    s2 = observed_daily_rainfall(["broken"], str(tmp_path))
    assert s2.empty

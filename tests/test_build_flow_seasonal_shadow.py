"""Tests for scripts/build_flow_seasonal_shadow.py — low-flow build_plan.md
Stage 6b (shadow-mode flow seasonal archive; publishes nothing).

Pure-function tests (no pastas needed): the weighted-ESP aggregation on
synthetic trace sets (incl. a winterbourne with zero-flow days), archive
round-trip/dedup, and graceful skips. A separate pastas-gated integration
test (pytest.importorskip) calibrates a REAL flow_2s model and drives
``run()`` end-to-end against a synthetic-but-realistic on-disk layout — the
closest thing to a live run this offline test environment can produce (no
production ERA5/PET/SEAS5 caches for the pilot gauges exist locally; see the
module docstring / PR description for what production does differently on
its first monthly run).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import scripts.build_flow_seasonal_shadow as M
from scripts.build_flow_seasonal_shadow import (
    SHADOW_COLS,
    append_shadow_archive,
    compute_gauge_shadow,
    parse_args,
    run,
    weighted_daily_mc,
)
from src.forecast.seasonal import esp

ORIGIN = pd.Timestamp("2026-06-30")
PERIODS = esp.monthly_anchors(ORIGIN, months=6)
F_DATES = pd.date_range(ORIGIN + pd.Timedelta(days=1), periods=esp.TRACE_DAYS, freq="D")
H = len(F_DATES)


def _rec(*, eps=0.01, sigma=0.3, alpha=40.0) -> dict:
    return {"model_kind": "flow_2s", "eps": eps, "sigma": sigma, "alpha": alpha,
           "param_names": [], "params": [], "rfunc": "Gamma", "recharge": "FlexModel"}


def _seasonal_mu(*, low: float, high: float, n_years: int = 15,
                 seed: int = 0) -> dict[int, np.ndarray]:
    """Synthetic daily logQ mean trajectories: low (dry) Jun-Sep, high (wet)
    the rest of the year, each year's trace lightly perturbed."""
    rng = np.random.default_rng(seed)
    out = {}
    for i, year in enumerate(range(2005, 2005 + n_years)):
        vals = np.array([high if m not in (6, 7, 8, 9) else low
                         for m in F_DATES.month], dtype=float)
        out[year] = vals + rng.normal(0, 0.05, size=H)
    return out


def _uniform_weights(years) -> dict[int, float]:
    return {y: 1.0 / len(years) for y in years}


# ---------------------------------------------------------------------------
# weighted_daily_mc: shape + weighting sanity
# ---------------------------------------------------------------------------

class TestWeightedDailyMc:
    def test_shape(self):
        mu = _seasonal_mu(low=-5.0, high=-1.0, n_years=12)
        sig = np.full(H, 0.2)
        w = _uniform_weights(mu)
        rng = np.random.default_rng(1)
        out = weighted_daily_mc(mu, sig, w, alpha=40.0, n=500, rng=rng)
        assert out.shape == (500, H)
        assert np.isfinite(out).all()

    def test_extreme_weight_concentrates_on_one_trace(self):
        mu = {2010: np.full(H, -1.0), 2011: np.full(H, -10.0)}
        sig = np.full(H, 1e-6)                         # negligible noise
        w = {2010: 0.999999, 2011: 0.000001}
        rng = np.random.default_rng(2)
        out = weighted_daily_mc(mu, sig, w, alpha=40.0, n=2000, rng=rng)
        # overwhelmingly resamples the 2010 trace (~-1.0), not the -10.0 one
        assert np.mean(out[:, 0] > -3.0) > 0.99


# ---------------------------------------------------------------------------
# compute_gauge_shadow: both target statistics, incl. a winterbourne
# ---------------------------------------------------------------------------

class TestComputeGaugeShadow:
    def test_perennial_gauge_low_sub_q95_prob_all_year(self):
        # flow stays well above Q95 all year -> P(sub-Q95) near 0 everywhere,
        # monthly quantiles all comfortably above the threshold.
        rec = _rec(eps=0.01)
        mu = _seasonal_mu(low=1.0, high=2.0, n_years=15)     # never near thr
        sig = np.full(H, 0.15)
        weights = _uniform_weights(mu)
        q95 = 0.05                                            # thr_logq = log(0.06)
        rows = compute_gauge_shadow(
            "g1", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN,
            obs_last=ORIGIN - pd.Timedelta(days=3), q95_m3s=q95,
            run=pd.Timestamp("2026-07-01"), seas5_weighted=False,
            ft_m3s=None, band_mode="additive", mc_samples=1000, seed=7)
        assert len(rows) == 6
        for r in rows:
            assert r["p_sub_q95"] < 0.05
            assert r["q_p10_m3s"] <= r["q_p50_m3s"] <= r["q_p90_m3s"]
            assert r["q_p50_m3s"] > q95
            assert r["band_mode"] == "weighted_quantiles"    # no fan -> fallback
            assert r["days_covered"] == PERIODS[r["month_ahead"] - 1].days_in_month

    def test_winterbourne_high_sub_q95_prob_in_dry_months(self):
        # zero-flow-record gauge: Q95 == 0.0 (compute_q95's documented
        # behaviour), and the trace mean sits right at the epsilon floor in
        # the dry months -> P(sub-Q95) should be high there, low in the wet
        # months, and the dry-month quantiles should sit near/at zero.
        rec = _rec(eps=0.01)
        thr_logq = float(np.log(0.0 + rec["eps"]))
        mu = _seasonal_mu(low=thr_logq, high=thr_logq + 4.0, n_years=15)
        sig = np.full(H, 0.1)
        weights = _uniform_weights(mu)
        rows = compute_gauge_shadow(
            "winterbourne1", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN,
            obs_last=ORIGIN - pd.Timedelta(days=3), q95_m3s=0.0,
            run=pd.Timestamp("2026-07-01"), seas5_weighted=True,
            ft_m3s=None, band_mode="additive", mc_samples=2000, seed=11)
        by_month = {r["month_ahead"]: r for r in rows}
        dry_months = [m for m, p in enumerate(PERIODS, start=1) if p.month in (6, 7, 8, 9)]
        wet_months = [m for m in by_month if m not in dry_months]
        for m in dry_months:
            assert by_month[m]["p_sub_q95"] > 0.5
            assert by_month[m]["q_p50_m3s"] < 0.05        # near the zero floor
        for m in wet_months:
            assert by_month[m]["p_sub_q95"] < by_month[dry_months[0]]["p_sub_q95"]
            assert by_month[m]["q_p50_m3s"] > by_month[dry_months[0]]["q_p50_m3s"]

    def test_exp_q_floors_at_zero(self):
        # A trace far below -eps in logQ space must still exponentiate to a
        # non-negative flow (flow_summary.exp_q's documented clip).
        rec = _rec(eps=0.01)
        mu = {y: np.full(H, -20.0) for y in range(2005, 2020)}
        sig = np.full(H, 0.1)
        weights = _uniform_weights(mu)
        rows = compute_gauge_shadow(
            "deep_dry", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN,
            obs_last=ORIGIN, q95_m3s=0.0, run=pd.Timestamp("2026-07-01"),
            seas5_weighted=False, ft_m3s=None, mc_samples=500, seed=3)
        for r in rows:
            assert r["q_p10_m3s"] >= 0.0
            assert r["q_p50_m3s"] >= 0.0

    def test_additive_band_mode_runs_with_fan_terminal(self):
        rec = _rec(eps=0.01, sigma=0.25, alpha=60.0)
        mu = _seasonal_mu(low=0.0, high=1.5, n_years=15)
        sig = np.full(H, 0.2)
        weights = _uniform_weights(mu)
        # a fan terminal roughly consistent with the trace scale
        ft_m3s = (0.8, 1.0, 1.3)
        rows = compute_gauge_shadow(
            "g_fan", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN,
            obs_last=ORIGIN - pd.Timedelta(days=1), q95_m3s=0.2,
            run=pd.Timestamp("2026-07-01"), seas5_weighted=False,
            ft_m3s=ft_m3s, band_mode="additive", mc_samples=500, seed=5)
        assert len(rows) == 6
        for r in rows:
            assert r["band_mode"] == "additive"
            assert r["q_p10_m3s"] <= r["q_p50_m3s"] <= r["q_p90_m3s"]
            assert 0.0 <= r["p_sub_q95"] <= 1.0

    def test_below_min_traces_returns_empty(self):
        rec = _rec()
        mu = _seasonal_mu(low=0.0, high=1.0, n_years=3)   # < MIN_TRACES
        sig = np.full(H, 0.2)
        weights = _uniform_weights(mu)
        rows = compute_gauge_shadow(
            "thin", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN, obs_last=ORIGIN,
            q95_m3s=0.1, run=pd.Timestamp("2026-07-01"), seas5_weighted=False)
        assert rows == []

    def test_row_schema_matches_shadow_cols(self):
        rec = _rec()
        mu = _seasonal_mu(low=0.0, high=1.0, n_years=12)
        sig = np.full(H, 0.2)
        weights = _uniform_weights(mu)
        rows = compute_gauge_shadow(
            "g_schema", rec, mu_by_year=mu, sig_daily=sig, weights=weights,
            f_dates=F_DATES, periods=PERIODS, origin=ORIGIN, obs_last=ORIGIN,
            q95_m3s=0.1, run=pd.Timestamp("2026-07-01"), seas5_weighted=False,
            mc_samples=200)
        assert rows
        assert set(rows[0]) == set(SHADOW_COLS)


# ---------------------------------------------------------------------------
# append_shadow_archive: append-only, dedup on (gauge_id, run, month_ahead)
# ---------------------------------------------------------------------------

class TestAppendShadowArchive:
    def _row(self, gid, run_ts, month_ahead, p50):
        return pd.DataFrame({"gauge_id": [gid], "run": [run_ts],
                             "month_ahead": [month_ahead], "q_p50_m3s": [p50]})

    def test_same_run_rerun_replaces(self):
        run_ts = pd.Timestamp("2026-07-01")
        prior = pd.concat([self._row("g1", run_ts, 1, 0.1),
                           self._row("g1", run_ts, 2, 0.2)], ignore_index=True)
        out = append_shadow_archive(prior, self._row("g1", run_ts, 1, 0.9))
        assert len(out) == 2
        assert out.loc[out["month_ahead"] == 1, "q_p50_m3s"].iloc[0] == 0.9

    def test_distinct_runs_accumulate(self):
        r1 = pd.Timestamp("2026-06-01")
        r2 = pd.Timestamp("2026-07-01")
        out = append_shadow_archive(self._row("g1", r1, 1, 0.1),
                                    self._row("g1", r2, 1, 0.2))
        assert len(out) == 2

    def test_distinct_gauges_same_run_both_kept(self):
        run_ts = pd.Timestamp("2026-07-01")
        out = append_shadow_archive(self._row("g1", run_ts, 1, 0.1),
                                    self._row("g2", run_ts, 1, 0.2))
        assert len(out) == 2

    def test_no_prior(self):
        run_ts = pd.Timestamp("2026-07-01")
        out = append_shadow_archive(None, self._row("g1", run_ts, 1, 0.1))
        assert len(out) == 1

    def test_empty_prior_frame(self):
        run_ts = pd.Timestamp("2026-07-01")
        empty = pd.DataFrame(columns=["gauge_id", "run", "month_ahead", "q_p50_m3s"])
        out = append_shadow_archive(empty, self._row("g1", run_ts, 1, 0.1))
        assert len(out) == 1


# ---------------------------------------------------------------------------
# run(): graceful exit-0 skips
# ---------------------------------------------------------------------------

def _cfg(*, pilot_path=None, models_cache=None, flow_enabled=True,
        seasonal_enabled=True, flow_seasonal_enabled=True) -> dict:
    return {
        "forecast": {
            "seasonal": {"enabled": seasonal_enabled},
            "flow_seasonal": {"enabled": flow_seasonal_enabled},
            "ensemble": {"flow": {
                "enabled": flow_enabled,
                "pilot_path": str(pilot_path) if pilot_path else "does/not/exist.csv",
                "models_cache": str(models_cache) if models_cache else "does/not/exist.json",
            }},
        },
    }


def test_run_seasonal_disabled_exits_zero(capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(seasonal_enabled=False)) == 0
    assert "forecast.seasonal.enabled" in capsys.readouterr().out


def test_run_flow_seasonal_disabled_exits_zero(capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(flow_seasonal_enabled=False)) == 0
    assert "forecast.flow_seasonal.enabled" in capsys.readouterr().out


def test_run_flow_disabled_exits_zero(capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(flow_enabled=False)) == 0
    assert "forecast.ensemble.flow.enabled" in capsys.readouterr().out


def test_run_missing_pilot_exits_zero(tmp_path, capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(pilot_path=tmp_path / "absent_pilot.csv")) == 0
    assert "not found" in capsys.readouterr().out


def test_run_empty_pilot_exits_zero(tmp_path, capsys):
    pilot_path = tmp_path / "flow_pilot.csv"
    pilot_path.write_text("gauge_id,station_name,floor_skill\n", encoding="utf-8")
    args = parse_args([])
    assert run(args, cfg=_cfg(pilot_path=pilot_path)) == 0
    assert "is empty" in capsys.readouterr().out


def test_run_missing_models_exits_zero(tmp_path, capsys):
    pilot_path = tmp_path / "flow_pilot.csv"
    pilot_path.write_text("gauge_id,station_name,floor_skill\ng1,Gauge One,0.1\n",
                          encoding="utf-8")
    args = parse_args([])
    assert run(args, cfg=_cfg(pilot_path=pilot_path,
                              models_cache=tmp_path / "absent_models.json")) == 0
    out = capsys.readouterr().out
    assert "not found" in out


def test_run_empty_models_exits_zero(tmp_path, capsys):
    pilot_path = tmp_path / "flow_pilot.csv"
    pilot_path.write_text("gauge_id,station_name,floor_skill\ng1,Gauge One,0.1\n",
                          encoding="utf-8")
    models_path = tmp_path / "flow_models.json"
    models_path.write_text("{}", encoding="utf-8")
    args = parse_args([])
    assert run(args, cfg=_cfg(pilot_path=pilot_path, models_cache=models_path)) == 0
    assert "no calibrated flow models" in capsys.readouterr().out


def test_run_missing_links_or_catalogue_exits_zero(tmp_path, capsys, monkeypatch):
    # Point ROOT at an empty tmp dir so the links/catalogue lookup misses.
    # (This originally relied on the REAL worktree lacking flow_links.csv —
    # true on the machine it was written on, false on any machine where the
    # fleet scan has run. Hermetic now.)
    monkeypatch.setattr(M, "ROOT", tmp_path)
    pilot_path = tmp_path / "flow_pilot.csv"
    pilot_path.write_text("gauge_id,station_name,floor_skill\ng1,Gauge One,0.1\n",
                          encoding="utf-8")
    models_path = tmp_path / "flow_models.json"
    models_path.write_text(
        '{"g1": {"model_kind": "flow_2s", "sigma": 0.1, "alpha": 40.0, '
        '"eps": 0.01, "q95_m3s": 0.5, "params": [], "param_names": [], '
        '"rfunc": "Gamma", "recharge": "FlexModel"}}', encoding="utf-8")
    args = parse_args([])
    assert run(args, cfg=_cfg(pilot_path=pilot_path, models_cache=models_path)) == 0
    assert "flow_links.csv" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Integration test: a REAL calibrated flow_2s model driven end-to-end
# through run(), against a synthetic-but-realistic on-disk layout. Skips in
# the main env (no pastas). This is the closest thing to a "live" run
# achievable without production ERA5/PET/SEAS5 caches for the pilot gauges.
# ---------------------------------------------------------------------------

pastas = pytest.importorskip("pastas")


def _write_era5_pet_caches(tmp_path: Path, gauge_id: str, dates: pd.DatetimeIndex,
                           rng: np.random.Generator) -> pd.Series:
    doy = dates.day_of_year.to_numpy()
    precip = np.clip(rng.gamma(0.5, 4.0, len(dates)), 0, None)
    et0 = np.clip(2 + 1.5 * np.sin(2 * np.pi * doy / 365), 0, None)

    era5_dir = tmp_path / "data" / "raw" / "era5_precip"
    era5_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": dates, "precip_mm": precip}).to_csv(
        era5_dir / f"{gauge_id}.csv", index=False)

    pet_dir = tmp_path / "data" / "raw" / "pet"
    pet_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": dates, "et0_mm": et0}).to_csv(
        pet_dir / f"{gauge_id}.csv", index=False)

    return pd.Series(precip, index=dates), pd.Series(et0, index=dates)


def test_run_end_to_end_with_real_calibrated_model(tmp_path, monkeypatch):
    from src.forecast.pastas import recharge as R

    gauge_id = "shadow_test_gauge"
    dates = pd.date_range("2008-01-01", "2026-06-30", freq="D")
    rng = np.random.default_rng(42)
    precip, et0 = _write_era5_pet_caches(tmp_path, gauge_id, dates, rng)

    # Real two-pathway calibration (docs/product/lowflow/analysis.md §3),
    # same synthetic-flow construction as tests/test_pastas_flow.py's
    # synthetic_winterbourne fixture.
    net = np.clip(precip.to_numpy() - 0.7 * et0.to_numpy(), 0, None)
    k = np.exp(-np.arange(30) / 10.0); k /= k.sum()
    baseflow = 0.3 + np.convolve(net, k)[:len(dates)] * 0.02
    q = np.clip(baseflow + 0.01 * rng.normal(0, 1, len(dates)), 0, None)
    q_series = pd.Series(q, index=dates, name="Flow_m3s")
    rec = R.calibrate_flow(gauge_id, q_series, precip, et0)
    assert rec["model_kind"] == "flow_2s"

    from scripts.build_flow_models import compute_q95
    rec["q95_m3s"] = round(compute_q95(q_series), 4)

    models_path = tmp_path / "data" / "model" / "flow_models.json"
    R.save_models({gauge_id: rec}, models_path)

    flow_shard_dir = tmp_path / "data" / "features" / "flow_by_station"
    flow_shard_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": dates, "Flow_m3s": q, "data_source": "flow_logged"}).to_parquet(
        flow_shard_dir / f"{gauge_id}.parquet", index=False)

    processed_dir = tmp_path / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    pilot_path = processed_dir / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": [gauge_id], "station_name": ["Shadow Test Gauge"],
                 "floor_skill": [0.5]}).to_csv(pilot_path, index=False)
    (processed_dir / "flow_catalogue.csv").write_text(
        "station_id,station_name,lat,lon\n"
        f"{gauge_id},Shadow Test Gauge,51.05,-1.31\n", encoding="utf-8")
    (processed_dir / "flow_links.csv").write_text(
        "GaugeID,RainMeasureID_1,RainMeasureID_2,RainMeasureID_3\n"
        f"{gauge_id},,,\n", encoding="utf-8")

    import src.data.era5_precip as era5_precip_mod
    import src.forecast.pastas.io as pastas_io_mod

    monkeypatch.setattr(M, "ROOT", tmp_path)
    monkeypatch.setattr(M, "FLOW_SHARD_DIR", flow_shard_dir)
    monkeypatch.setattr(M, "gauge_rainfall_for",
                        lambda gid, links, raw_root: precip.copy())
    monkeypatch.setattr(pastas_io_mod, "PET_CACHE", tmp_path / "data" / "raw" / "pet")
    monkeypatch.setattr(era5_precip_mod, "PRECIP_CACHE_ROOT",
                        tmp_path / "data" / "raw" / "era5_precip")

    archive_path = tmp_path / "data" / "model" / "flow_seasonal_shadow_archive.parquet"
    cfg = {
        "forecast": {
            "seasonal": {"enabled": True, "months": 6, "trace_start_year": 2010,
                        "seas5_weighting": True, "weight_months": 3,
                        "band_mode": "additive", "max_anchor_age_days": 45},
            "flow_seasonal": {"enabled": True, "archive_cache": str(archive_path),
                             "mc_samples": 300},
            "ensemble": {"flow": {"enabled": True, "pilot_path": str(pilot_path),
                                 "models_cache": str(models_path),
                                 "fan_cache": str(tmp_path / "no_fan.csv")}},
        },
        "download": {"raw_root": str(tmp_path / "data" / "raw")},
    }

    args = parse_args(["--seed", "99"])
    assert run(args, cfg=cfg) == 0
    assert archive_path.exists()

    out = pd.read_parquet(archive_path)
    assert set(SHADOW_COLS) == set(out.columns)
    assert out["gauge_id"].unique().tolist() == [gauge_id]
    assert len(out) == 6                              # 6 outlook months
    assert out["n_traces"].min() >= M.MIN_TRACES
    assert (out["q_p10_m3s"] <= out["q_p50_m3s"]).all()
    assert (out["q_p50_m3s"] <= out["q_p90_m3s"]).all()
    assert out["p_sub_q95"].between(0.0, 1.0).all()
    assert (out["q95_m3s"] == rec["q95_m3s"]).all()
    # no fan cache on disk -> the additive band's fan-terminal branch is
    # unreachable; the fallback weighted_quantiles path is what production
    # will actually use too, on any host where 8h-flow hasn't produced a fan
    # yet before this stage's first monthly run.
    assert (out["band_mode"] == "weighted_quantiles").all()

    # Rerunning the SAME day dedups on (gauge_id, run, month_ahead) rather
    # than doubling the archive.
    assert run(args, cfg=cfg) == 0
    out2 = pd.read_parquet(archive_path)
    assert len(out2) == 6

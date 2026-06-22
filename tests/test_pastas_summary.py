"""Tests for the Pastas aggregator (src/forecast/pastas/summary.py).

Pure numpy/pandas — no pastas — so this runs in the main env too.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.forecast.pastas import summary as S


def _members(sid, dates, n_members=51, level=50.0, slope=-0.05,
             sig0=0.3, sig_slope=0.05, seed=0):
    """Near-identical member point paths (chalk: tiny member spread) with a
    growing per-date predictive sd (gw_sigma), as module 2 emits."""
    rng = np.random.default_rng(seed)
    origin = pd.Timestamp(dates[0]) - pd.Timedelta(days=1)
    rows = []
    for m in range(n_members):
        for i, d in enumerate(dates):
            rows.append({"station_id": sid, "member": m, "date": d,
                         "precip_mm": 0.0,
                         "gw_pred": level + slope * i + rng.normal(0, 0.01),
                         "gw_sigma": sig0 + sig_slope * i, "origin_date": origin})
    return pd.DataFrame(rows)


def test_ar1_noise_matches_marginal_sd_vector():
    rng = np.random.default_rng(0)
    sd = np.array([0.3 + 0.05 * i for i in range(14)])
    phi = np.exp(-1 / 50.0)
    z = S._ar1_noise(60000, sd, phi, rng)
    got = z.std(axis=0)
    # output marginals track the requested per-date sd vector
    assert np.allclose(got, sd, atol=0.03)
    # consecutive leads are positively correlated (AR1)
    assert np.corrcoef(z[:, 5], z[:, 6])[0, 1] > 0.5


def test_fan_widens_with_lead_from_noise():
    dates = pd.date_range("2026-06-07", periods=14, freq="D")
    df = _members("BH1", dates)                       # member spread ≈ 0.01 m
    models = {"BH1": {"sigma": 1.0, "alpha": 50.0}}
    _, fan = S.aggregate_pastas(df, models, run=pd.Timestamp("2026-06-09", tz="UTC"),
                                n_samples=6000, seed=1)
    f = fan[fan.station_id == "BH1"]
    band = (f["gw_p90"] - f["gw_p10"]).to_numpy()
    assert band[0] > 0.2                              # noise band, not member spread
    assert band[-1] > band[0] + 0.2                   # widens with lead


def test_above_p90_prob_responds_to_level():
    dates = pd.date_range("2026-06-01", periods=14, freq="D")   # all June
    p90s = {6: 52.0}
    # Flat trajectories well below the June p90 → never exceed.
    low = np.full((200, 14), 50.0)
    assert S.above_p90_prob(low, dates, p90s) == 0.0
    # Flat trajectories above it → every trajectory exceeds.
    high = np.full((200, 14), 53.0)
    assert S.above_p90_prob(high, dates, p90s) == 1.0
    # Borderline per-trajectory levels (flat paths, as smooth AR1 is) → interior.
    rng = np.random.default_rng(0)
    levels = rng.normal(52.0, 1.0, size=4000)
    border = np.repeat(levels[:, None], 14, axis=1)
    assert 0.05 < S.above_p90_prob(border, dates, p90s) < 0.95


def test_above_p90_prob_uses_calendar_month_bounds():
    # Window straddles a month boundary: June p90 high (never exceeded),
    # July p90 low (always exceeded) → any-day exceedance fires via July.
    dates = pd.date_range("2026-06-28", periods=7, freq="D")    # 3 Jun + 4 Jul
    traj = np.full((50, 7), 51.0)
    assert S.above_p90_prob(traj, dates, {6: 60.0, 7: 50.0}) == 1.0
    assert S.above_p90_prob(traj, dates, {6: 60.0, 7: 55.0}) == 0.0
    # Months without a normal are skipped; none known at all → NaN.
    assert S.above_p90_prob(traj, dates, {6: 60.0}) == 0.0
    assert np.isnan(S.above_p90_prob(traj, dates, {1: 50.0}))


def test_load_monthly_p90s_round_trip(tmp_path):
    """build_gw_normals output → build_pastas_summary._load_monthly_p90s
    gives {station → {month → p90}}; missing/pre-ladder artefacts degrade
    to {} with a visible warning (the cron must not block on normals)."""
    from scripts.build_pastas_summary import _load_monthly_p90s

    normals = pd.DataFrame({
        "station_id": ["S1", "S1", "S2"],
        "month": [1, 2, 6],
        "p10": [48.0, 48.1, 40.0],
        "t1": [49.0, 49.1, 41.0],
        "median": [50.0, 50.1, 42.0],
        "t2": [51.0, 51.1, 43.0],
        "p90": [52.0, 52.1, 44.0],
        "n_years": [10, 10, 8],
    })
    path = tmp_path / "gw_monthly_normals.csv"
    normals.to_csv(path, index=False)
    out = _load_monthly_p90s(path)
    assert out["S1"] == {1: 52.0, 2: 52.1}
    assert out["S2"] == {6: 44.0}


def test_load_monthly_p90s_degrades_with_warning(tmp_path, capsys):
    from scripts.build_pastas_summary import _load_monthly_p90s

    assert _load_monthly_p90s(tmp_path / "absent.csv") == {}
    assert "WARNING" in capsys.readouterr().out
    # Pre-quantile-ladder artefact (no p90 column) → also {} + warning
    legacy = pd.DataFrame({"station_id": ["S1"], "month": [1],
                           "tercile_1": [49.0], "median": [50.0]})
    path = tmp_path / "legacy_normals.csv"
    legacy.to_csv(path, index=False)
    assert _load_monthly_p90s(path) == {}
    assert "WARNING" in capsys.readouterr().out


def test_aggregate_carries_p_above_p90_14d():
    dates = pd.date_range("2026-06-07", periods=14, freq="D")
    df = _members("BH1", dates, level=49.6, slope=0.0, sig0=0.4, sig_slope=0.04)
    models = {"BH1": {"sigma": 1.0, "alpha": 50.0}}
    # June p90 at 50.0 ≈ the trajectory mean+noise → interior probability.
    summ, _ = S.aggregate_pastas(df, models, run=pd.Timestamp("2026-06-09", tz="UTC"),
                                 monthly_p90_by_station={"BH1": {6: 50.0}},
                                 n_samples=4000, seed=3)
    p = summ.iloc[0]["p_above_p90_14d"]
    assert 0.0 < p < 1.0
    # No normals supplied → NaN, never a crash.
    summ2, _ = S.aggregate_pastas(df, models, run=pd.Timestamp("2026-06-09", tz="UTC"),
                                  n_samples=200, seed=3)
    assert np.isnan(summ2.iloc[0]["p_above_p90_14d"])


def test_breach_and_model_spread_interior():
    dates = pd.date_range("2026-06-07", periods=14, freq="D")
    df = _members("BH1", dates, level=49.6, slope=0.0, sig0=0.4, sig_slope=0.04)
    models = {"BH1": {"sigma": 1.0, "alpha": 50.0}}
    roll = {"BH1": pd.Series(np.full(14, 50.0), index=dates)}   # roll P50 = 50
    summ, _ = S.aggregate_pastas(df, models, run=pd.Timestamp("2026-06-09", tz="UTC"),
                                 gw_p90_by_station={"BH1": 50.0},
                                 roll_p50_by_station=roll, n_samples=10000, seed=2)
    r = summ.iloc[0]
    assert r["threshold"] == 50.0 and r["threshold_source"] == "gw_p90_proxy"
    assert r["stale_days"] == 3                        # run 06-09 − origin 06-06
    assert 0.0 < r["p_breach"] < 1.0                  # interior probability
    assert abs(r["censored_frac"] - (1 - r["p_breach"])) < 1e-9
    # |pastas P50 (~49.6) − roll P50 (50)| ≈ 0.4 m
    assert 0.2 < r["model_spread_mean"] < 0.7


def test_scope_provenance_carried_and_defaulted():
    dates = pd.date_range("2026-06-07", periods=5, freq="D")
    models = {"BH1": {"sigma": 0.5, "alpha": 30.0}}
    df = _members("BH1", dates, n_members=5)
    df["scope"] = "live"
    summ, _ = S.aggregate_pastas(df, models, run=pd.Timestamp("2026-06-09", tz="UTC"),
                                 n_samples=200, seed=1)
    assert summ.iloc[0]["scope"] == "live"
    # legacy members parquet without the column -> "unknown"
    summ2, _ = S.aggregate_pastas(_members("BH1", dates, n_members=5), models,
                                  run=pd.Timestamp("2026-06-09", tz="UTC"),
                                  n_samples=200, seed=1)
    assert summ2.iloc[0]["scope"] == "unknown"


class TestArchiveAppendDedup:
    """append_archive (scripts.build_pastas_summary / build_ensemble_summary):
    same-(station, run) re-runs replace rows; distinct runs accumulate."""

    def _row(self, sid, run, p, scope="live"):
        return pd.DataFrame({"station_id": [sid], "run": [run],
                             "scope": [scope], "p_breach": [p]})

    def test_same_run_rerun_replaces(self):
        from scripts.build_pastas_summary import append_archive
        run = pd.Timestamp("2026-06-10T06:00:00Z")
        prior = pd.concat([self._row("x", run, 0.1), self._row("y", run, 0.2)],
                          ignore_index=True)
        out = append_archive(prior, self._row("x", run, 0.5))
        assert len(out) == 2
        assert out.loc[out["station_id"] == "x", "p_breach"].iloc[0] == 0.5

    def test_distinct_runs_accumulate(self):
        from scripts.build_pastas_summary import append_archive
        r1 = pd.Timestamp("2026-06-09T06:00:00Z")
        r2 = pd.Timestamp("2026-06-10T06:00:00Z")
        out = append_archive(self._row("x", r1, 0.1), self._row("x", r2, 0.2))
        assert len(out) == 2

    def test_no_prior_and_legacy_scope_fill(self):
        from scripts.build_pastas_summary import append_archive
        run = pd.Timestamp("2026-06-10T06:00:00Z")
        assert len(append_archive(None, self._row("x", run, 0.1))) == 1
        # prior rows pre-dating the scope column -> filled with "unknown"
        legacy = pd.DataFrame({"station_id": ["old"], "p_breach": [0.3],
                               "run": [pd.Timestamp("2026-06-01T06:00:00Z")]})
        out = append_archive(legacy, self._row("x", run, 0.1))
        assert out.loc[out["station_id"] == "old", "scope"].iloc[0] == "unknown"

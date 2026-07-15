"""Tests for scripts/build_flow_members.py — graceful skips (models absent,
bridge absent/stale), the ENS-bridge reader, the no-network invariant, the
explicit climatological dev mode, and archive append/dedup
(build_plan.md Stage 6). No pastas/network needed.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import scripts.build_flow_members as M
from scripts.build_flow_members import (
    append_archive,
    append_fan_archive,
    climatological_members,
    load_ens_bridge,
    parse_args,
    run,
)


def _bridge_frame(gauge_ids, start, *, days=14, n_members=3):
    rows = []
    for gid in gauge_ids:
        for m in range(n_members):
            for d in pd.date_range(start, periods=days, freq="D"):
                rows.append({"gauge_id": gid, "member": m, "date": d,
                            "precip_mm": 1.0})
    df = pd.DataFrame(rows)
    df["provider"] = pd.Categorical(["ecmwf_opendata"] * len(df))
    return df


# ---------------------------------------------------------------------------
# The no-network invariant: this stage reads ONLY the on-disk ENS bridge.
# A provider fetch here would silently land on Open-Meteo (PASTAS_ENV has no
# GRIB stack; free tier is non-commercial) — the exact regression the bridge
# design exists to prevent, so pin it.
# ---------------------------------------------------------------------------

def test_no_ensemble_provider_import_or_usage():
    # Module namespace: no provider factory, no provider classes.
    assert not hasattr(M, "get_provider")
    assert not hasattr(M, "OpenMeteoEnsemble")
    assert not hasattr(M, "ECMWFOpenDataENS")
    # Source: no provider resolution or fetch path at all (comments/docstring
    # legitimately mention the words; the code must not call them).
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert "get_provider" not in src
    assert "provider.fetch" not in src
    assert "resolve_provider_name" not in src
    assert "_dev_fallback" not in src


# ---------------------------------------------------------------------------
# load_ens_bridge: read + freshness check
# ---------------------------------------------------------------------------

class TestLoadEnsBridge:
    def test_missing_file_returns_none_with_reason(self, tmp_path):
        bridge, why = load_ens_bridge(tmp_path / "absent.parquet")
        assert bridge is None
        assert "not found" in why

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "bridge.parquet"
        pd.DataFrame(columns=["gauge_id", "member", "date", "precip_mm"]).to_parquet(
            p, index=False)
        bridge, why = load_ens_bridge(p)
        assert bridge is None
        assert "empty" in why

    def test_fresh_bridge_loads(self, tmp_path):
        p = tmp_path / "bridge.parquet"
        today = date(2026, 7, 14)
        _bridge_frame(["g1"], pd.Timestamp(today)).to_parquet(p, index=False)
        bridge, why = load_ens_bridge(p, today=today)
        assert bridge is not None and why == ""
        assert bridge["gauge_id"].nunique() == 1

    def test_yesterdays_bridge_still_acceptable(self, tmp_path):
        p = tmp_path / "bridge.parquet"
        today = date(2026, 7, 14)
        _bridge_frame(["g1"], pd.Timestamp("2026-07-13")).to_parquet(p, index=False)
        bridge, why = load_ens_bridge(p, today=today)
        assert bridge is not None

    def test_stale_bridge_returns_none(self, tmp_path):
        p = tmp_path / "bridge.parquet"
        today = date(2026, 7, 14)
        _bridge_frame(["g1"], pd.Timestamp("2026-07-10")).to_parquet(p, index=False)
        bridge, why = load_ens_bridge(p, today=today)
        assert bridge is None
        assert "stale" in why


# ---------------------------------------------------------------------------
# climatological_members: the EXPLICIT dev/offline mode (never a fallback)
# ---------------------------------------------------------------------------

def test_climatological_members_shape_and_zero_spread():
    rain = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0],
                     index=pd.date_range("2026-01-01", periods=5))
    fdates = pd.date_range("2026-06-01", periods=14, freq="D")
    mdf = climatological_members(rain, fdates, n_members=51)
    assert set(mdf["member"]) == set(range(51))
    assert len(mdf) == 51 * 14
    # every member sees the SAME rain on a given date (zero forecast spread —
    # the dev mode documents itself rather than fabricating spread).
    spread = mdf.groupby("date")["precip_mm"].std()
    assert (spread.fillna(0.0) < 1e-9).all()


def test_climatological_members_uses_day_of_year_mean():
    idx = pd.date_range("2020-01-01", "2023-12-31", freq="D")
    rain = pd.Series(0.0, index=idx)
    rain[idx.month == 6] = 10.0     # only June has rain, every year
    fdates = pd.date_range("2026-06-15", periods=3, freq="D")   # all June
    mdf = climatological_members(rain, fdates, n_members=3)
    assert (mdf["precip_mm"] > 9.0).all()


def test_climatological_members_handles_empty_observed_rain():
    rain = pd.Series(dtype=float)
    fdates = pd.date_range("2026-06-01", periods=5, freq="D")
    mdf = climatological_members(rain, fdates, n_members=2)
    assert len(mdf) == 2 * 5
    assert (mdf["precip_mm"] == 0.0).all()


# ---------------------------------------------------------------------------
# append_archive / append_fan_archive: dedup on rerun (mirrors
# scripts.build_pastas_summary's TestArchiveAppendDedup)
# ---------------------------------------------------------------------------

class TestArchiveAppendDedup:
    def _row(self, gid, run_ts, p):
        return pd.DataFrame({"gauge_id": [gid], "run": [run_ts], "p_below_q95": [p]})

    def test_same_run_rerun_replaces(self):
        run_ts = pd.Timestamp("2026-08-10T06:00:00Z")
        prior = pd.concat([self._row("g1", run_ts, 0.1), self._row("g2", run_ts, 0.2)],
                          ignore_index=True)
        out = append_archive(prior, self._row("g1", run_ts, 0.9))
        assert len(out) == 2
        assert out.loc[out["gauge_id"] == "g1", "p_below_q95"].iloc[0] == 0.9

    def test_distinct_runs_accumulate(self):
        r1 = pd.Timestamp("2026-08-09T06:00:00Z")
        r2 = pd.Timestamp("2026-08-10T06:00:00Z")
        out = append_archive(self._row("g1", r1, 0.1), self._row("g1", r2, 0.2))
        assert len(out) == 2

    def test_no_prior(self):
        run_ts = pd.Timestamp("2026-08-10T06:00:00Z")
        assert len(append_archive(None, self._row("g1", run_ts, 0.1))) == 1

    def _fan_row(self, gid, run_ts, lead):
        return pd.DataFrame({"gauge_id": [gid], "run": [run_ts], "lead": [lead],
                             "q_p50_m3s": [1.0]})

    def test_fan_archive_dedups_on_gauge_run_lead(self):
        run_ts = pd.Timestamp("2026-08-10T06:00:00Z")
        prior = pd.concat([self._fan_row("g1", run_ts, 1), self._fan_row("g1", run_ts, 2)],
                          ignore_index=True)
        out = append_fan_archive(prior, self._fan_row("g1", run_ts, 1))
        assert len(out) == 2  # lead=1 replaced, lead=2 untouched

    def test_fan_archive_empty_fan_returns_prior_unchanged(self):
        run_ts = pd.Timestamp("2026-08-10T06:00:00Z")
        prior = self._fan_row("g1", run_ts, 1)
        out = append_fan_archive(prior, pd.DataFrame())
        assert len(out) == 1


# ---------------------------------------------------------------------------
# run(): graceful exit-0 skips — absent models, absent bridge, stale bridge
# ---------------------------------------------------------------------------

def _cfg(models_cache, bridge_cache="does/not/matter.parquet"):
    return {"forecast": {"ensemble": {"flow": {
        "enabled": True,
        "models_cache": str(models_cache),
        "ens_bridge_cache": str(bridge_cache),
    }}}}


def _write_models(tmp_path):
    models_path = tmp_path / "flow_models.json"
    models_path.write_text(
        '{"g1": {"model_kind": "flow_2s", "sigma": 0.1, "alpha": 40.0, '
        '"eps": 0.01, "q95_m3s": 0.5, "params": [], "param_names": [], '
        '"rfunc": "Gamma", "recharge": "FlexModel"}}', encoding="utf-8")
    return models_path


def test_run_missing_models_file_exits_zero(tmp_path, capsys):
    args = parse_args([])
    assert run(args, cfg=_cfg(tmp_path / "absent_models.json")) == 0
    assert "not found" in capsys.readouterr().out


def test_run_empty_models_file_exits_zero(tmp_path, capsys):
    models_path = tmp_path / "flow_models.json"
    models_path.write_text("{}", encoding="utf-8")
    args = parse_args([])
    assert run(args, cfg=_cfg(models_path)) == 0
    assert "no calibrated flow models" in capsys.readouterr().out


def test_run_missing_bridge_exits_zero_with_skip_message(tmp_path, capsys):
    models_path = _write_models(tmp_path)
    args = parse_args([])
    assert run(args, cfg=_cfg(models_path, tmp_path / "absent_bridge.parquet")) == 0
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "yesterday's fans remain" in out


def test_run_stale_bridge_exits_zero_with_skip_message(tmp_path, capsys):
    models_path = _write_models(tmp_path)
    bridge_path = tmp_path / "bridge.parquet"
    old_start = pd.Timestamp(date.today()) - pd.Timedelta(days=10)
    _bridge_frame(["g1"], old_start).to_parquet(bridge_path, index=False)
    args = parse_args([])
    assert run(args, cfg=_cfg(models_path, bridge_path)) == 0
    out = capsys.readouterr().out
    assert "stale" in out
    assert "yesterday's fans remain" in out


def test_run_disabled_via_config_exits_zero(capsys):
    args = parse_args([])
    cfg = {"forecast": {"ensemble": {"flow": {"enabled": False}}}}
    assert run(args, cfg=cfg) == 0
    assert "enabled = false" in capsys.readouterr().out

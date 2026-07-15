"""Tests for scripts/build_ensemble_members.py::build_flow_ens_bridge — the
MAIN_ENV on-disk ENS bridge for the low-flow pilot (build_plan.md Stage 6).

The bridge is how the ENS member forcing crosses the venv boundary to the
pastas-env build_flow_members stage (which has no GRIB stack and must never
fall through to Open-Meteo — free tier is non-commercial). Fixture-only: the
provider is faked; no network, no GRIB.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from scripts.build_ensemble_members import build_flow_ens_bridge
from scripts.build_flow_members import load_ens_bridge


class FakeProvider:
    """Deterministic stand-in for an EnsembleRainfallProvider."""
    name = "fake_ens"

    def __init__(self, n_members=3, days=14, fail_for=()):
        self.n_members = n_members
        self.days = days
        self.fail_for = set(fail_for)          # (lat, lon) pairs that raise
        self.fetch_calls = []

    def fetch(self, *, lat, lon, start, horizon_days):
        self.fetch_calls.append((lat, lon))
        if (lat, lon) in self.fail_for:
            raise RuntimeError("synthetic fetch failure")
        rows = []
        for m in range(self.n_members):
            for d in pd.date_range(pd.Timestamp(start), periods=self.days, freq="D"):
                rows.append({"member": m, "date": d, "precip_mm": float(m)})
        return pd.DataFrame(rows)


def _write_inputs(tmp_path, gauge_ids=("g1", "g2")):
    pilot = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": list(gauge_ids),
                  "station_name": [f"Gauge {g}" for g in gauge_ids],
                  "floor_skill": [0.5] * len(gauge_ids)}).to_csv(pilot, index=False)
    cat = tmp_path / "flow_catalogue.csv"
    pd.DataFrame({"station_id": list(gauge_ids),
                  "station_name": [f"Gauge {g}" for g in gauge_ids],
                  "lat": [51.0 + i for i in range(len(gauge_ids))],
                  "lon": [-1.0 - i for i in range(len(gauge_ids))]}).to_csv(
        cat, index=False)
    return pilot, cat


def _cfg(bridge_path):
    return {"forecast": {"ensemble": {"flow": {
        "enabled": True, "window_days": 14,
        "ens_bridge_cache": str(bridge_path),
    }}}}


def test_bridge_round_trip_through_the_pastas_side_reader(tmp_path):
    """The full seam: MAIN_ENV writer -> parquet -> PASTAS_ENV reader."""
    pilot, cat = _write_inputs(tmp_path)
    out_path = tmp_path / "flow_ens_members.parquet"
    provider = FakeProvider()
    written = build_flow_ens_bridge(provider, _cfg(out_path),
                                    pilot_path=pilot, catalogue_path=cat,
                                    out_path=out_path)
    assert written is not None
    assert out_path.exists()
    assert list(written.columns) == ["gauge_id", "member", "date",
                                     "precip_mm", "provider"]
    assert set(written["gauge_id"]) == {"g1", "g2"}
    assert written["provider"].iloc[0] == "fake_ens"
    # one fetch per gauge, at the gauge's own coords
    assert len(provider.fetch_calls) == 2

    # ...and the pastas-side reader accepts it as fresh today.
    bridge, why = load_ens_bridge(out_path, today=date.today())
    assert bridge is not None and why == ""
    g1 = bridge[bridge["gauge_id"] == "g1"]
    assert g1["member"].nunique() == 3
    assert len(g1) == 3 * 14


def test_missing_pilot_returns_none_and_writes_nothing(tmp_path, capsys):
    out_path = tmp_path / "flow_ens_members.parquet"
    provider = FakeProvider()
    result = build_flow_ens_bridge(provider, _cfg(out_path),
                                   pilot_path=tmp_path / "absent_pilot.csv",
                                   catalogue_path=tmp_path / "cat.csv",
                                   out_path=out_path)
    assert result is None
    assert not out_path.exists()
    assert provider.fetch_calls == []           # never touched the provider
    assert "not found" in capsys.readouterr().out


def test_missing_catalogue_returns_none(tmp_path, capsys):
    pilot, _ = _write_inputs(tmp_path)
    out_path = tmp_path / "flow_ens_members.parquet"
    result = build_flow_ens_bridge(FakeProvider(), _cfg(out_path),
                                   pilot_path=pilot,
                                   catalogue_path=tmp_path / "absent_cat.csv",
                                   out_path=out_path)
    assert result is None
    assert not out_path.exists()


def test_disabled_flow_config_returns_none(tmp_path, capsys):
    pilot, cat = _write_inputs(tmp_path)
    cfg = {"forecast": {"ensemble": {"flow": {"enabled": False}}}}
    result = build_flow_ens_bridge(FakeProvider(), cfg,
                                   pilot_path=pilot, catalogue_path=cat,
                                   out_path=tmp_path / "out.parquet")
    assert result is None
    assert "enabled = false" in capsys.readouterr().out


def test_per_gauge_fetch_failure_skips_that_gauge_only(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("scripts.build_ensemble_members.time.sleep", lambda s: None)
    pilot, cat = _write_inputs(tmp_path, gauge_ids=("g1", "g2"))
    out_path = tmp_path / "flow_ens_members.parquet"
    # g1 sits at (51.0, -1.0) per _write_inputs — make exactly that one fail.
    provider = FakeProvider(fail_for={(51.0, -1.0)})
    written = build_flow_ens_bridge(provider, _cfg(out_path),
                                    pilot_path=pilot, catalogue_path=cat,
                                    out_path=out_path)
    assert written is not None
    assert set(written["gauge_id"]) == {"g2"}
    assert "skipped" in capsys.readouterr().out


def test_all_gauges_failing_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("scripts.build_ensemble_members.time.sleep", lambda s: None)
    pilot, cat = _write_inputs(tmp_path, gauge_ids=("g1",))
    out_path = tmp_path / "flow_ens_members.parquet"
    provider = FakeProvider(fail_for={(51.0, -1.0)})
    result = build_flow_ens_bridge(provider, _cfg(out_path),
                                   pilot_path=pilot, catalogue_path=cat,
                                   out_path=out_path)
    assert result is None
    assert not out_path.exists()
    assert "nothing written" in capsys.readouterr().out

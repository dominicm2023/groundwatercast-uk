"""Tests for scripts/refresh_seasonal_inputs.py's ``_flow_pilot_points`` —
the low-flow build_plan.md Stage 6b change that folds the flow pilot gauges
into the same fleet-wide ERA5/PET/SEAS5 fetch the GW seasonal outlook already
uses. Offline/pure (no network) — only ever reads local CSVs.
"""
from __future__ import annotations

import pandas as pd

from scripts.refresh_seasonal_inputs import _flow_pilot_points


def _cfg(pilot_path) -> dict:
    return {"forecast": {"ensemble": {"flow": {"pilot_path": str(pilot_path)}}}}


def test_missing_pilot_csv_returns_empty(tmp_path, monkeypatch):
    import scripts.refresh_seasonal_inputs as RSI
    monkeypatch.setattr(RSI, "ROOT", tmp_path)
    assert _flow_pilot_points(_cfg(tmp_path / "absent_pilot.csv")) == {}


def test_missing_flow_catalogue_returns_empty(tmp_path, monkeypatch):
    import scripts.refresh_seasonal_inputs as RSI
    monkeypatch.setattr(RSI, "ROOT", tmp_path)
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": ["g1"], "station_name": ["Gauge One"],
                 "floor_skill": [0.1]}).to_csv(pilot_path, index=False)
    # no data/processed/flow_catalogue.csv under ROOT
    assert _flow_pilot_points(_cfg(pilot_path)) == {}


def test_empty_pilot_csv_returns_empty(tmp_path, monkeypatch):
    import scripts.refresh_seasonal_inputs as RSI
    monkeypatch.setattr(RSI, "ROOT", tmp_path)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    pilot_path = tmp_path / "flow_pilot.csv"
    pilot_path.write_text("gauge_id,station_name,floor_skill\n", encoding="utf-8")
    pd.DataFrame({"station_id": ["g1"], "lat": [51.0], "lon": [-1.3]}).to_csv(
        tmp_path / "data" / "processed" / "flow_catalogue.csv", index=False)
    assert _flow_pilot_points(_cfg(pilot_path)) == {}


def test_returns_lat_lon_for_pilot_gauges_in_catalogue(tmp_path, monkeypatch):
    import scripts.refresh_seasonal_inputs as RSI
    monkeypatch.setattr(RSI, "ROOT", tmp_path)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": ["g1", "g2"], "station_name": ["One", "Two"],
                 "floor_skill": [0.1, 0.2]}).to_csv(pilot_path, index=False)
    pd.DataFrame({"station_id": ["g1", "g2", "g3"],
                 "lat": [51.0, 52.0, 53.0],
                 "lon": [-1.3, -2.1, -0.5]}).to_csv(
        tmp_path / "data" / "processed" / "flow_catalogue.csv", index=False)
    points = _flow_pilot_points(_cfg(pilot_path))
    assert points == {"g1": (51.0, -1.3), "g2": (52.0, -2.1)}
    assert "g3" not in points                          # not in the pilot


def test_pilot_gauge_missing_from_catalogue_is_dropped(tmp_path, monkeypatch):
    import scripts.refresh_seasonal_inputs as RSI
    monkeypatch.setattr(RSI, "ROOT", tmp_path)
    (tmp_path / "data" / "processed").mkdir(parents=True)
    pilot_path = tmp_path / "flow_pilot.csv"
    pd.DataFrame({"gauge_id": ["g1", "gX"], "station_name": ["One", "Missing"],
                 "floor_skill": [0.1, 0.2]}).to_csv(pilot_path, index=False)
    pd.DataFrame({"station_id": ["g1"], "lat": [51.0], "lon": [-1.3]}).to_csv(
        tmp_path / "data" / "processed" / "flow_catalogue.csv", index=False)
    points = _flow_pilot_points(_cfg(pilot_path))
    assert points == {"g1": (51.0, -1.3)}

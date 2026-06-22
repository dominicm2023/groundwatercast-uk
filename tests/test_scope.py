"""Tests for forecast borehole-scope selection (src/forecast/ensemble/scope.py)."""
from __future__ import annotations

import pandas as pd

from src.forecast.ensemble import scope as sc


def _setup(tmp_path, monkeypatch):
    cat = tmp_path / "catalogue.csv"
    pd.DataFrame({
        "station_id": ["a", "b", "c", "d", "noco"],
        "measure_type": ["groundwater"] * 5,
        "lat": [51, 51, 51, 51, None], "lon": [-1, -1, -1, -1, None],
    }).to_csv(cat, index=False)
    xref = tmp_path / "xref.csv"
    pd.DataFrame({"station_id": ["a", "b", "noco"],
                  "fm_notation": ["E1", "E2", "E9"]}).to_csv(xref, index=False)
    joined = tmp_path / "joined.csv"
    # a,b,c calibratable (>=5 here); d short; noco has data but no coords
    rows = [("a", 5), ("b", 5), ("c", 5), ("d", 2), ("noco", 5)]
    df = pd.concat([pd.DataFrame({"station_id": [s] * n, "GW_Level": [1.0] * n})
                    for s, n in rows], ignore_index=True)
    df.to_csv(joined, index=False)
    monkeypatch.setattr(sc, "_CATALOGUE", cat)
    monkeypatch.setattr(sc, "_XREF", xref)
    monkeypatch.setattr(sc, "_JOINED", joined)
    monkeypatch.setattr(sc, "user_threshold_station_ids", lambda: frozenset({"c"}))


def test_live_scope_is_live_and_calibratable_union_user(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # live∩calibratable = {a,b} (noco excluded: no coords); ∪ user {c} → {a,b,c}.
    # d excluded (too few rows); noco excluded (no coords).
    assert sc.select_scope("live", min_rows=5) == {"a", "b", "c"}


def test_user_scope(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    assert sc.select_scope("user", min_rows=5) == {"c"}


def test_fleet_scope_is_all_calibratable_union_user(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # calibratable = {a,b,c,noco} (>=5 rows); d excluded; ∪ user {c}
    assert sc.select_scope("fleet", min_rows=5) == {"a", "b", "c", "noco"}


def test_unknown_scope_raises(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        sc.select_scope("bogus", min_rows=5)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_known_bad_stations_dropped_from_every_scope(tmp_path, monkeypatch):
    """Register entries (datum/scaling shifts) are excluded even from the
    user scope — their live readings aren't comparable with the history the
    models/thresholds were built on."""
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sc, "excluded_station_ids", lambda: {"a", "c"})
    assert sc.select_scope("user") == set()             # c was the user-threshold BH
    assert "a" not in sc.select_scope("live", min_rows=5)
    assert sc.select_scope("fleet", min_rows=5) == {"b", "noco"}  # fleet does not require coords

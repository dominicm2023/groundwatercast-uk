"""
Unit tests for src/linking/build.py.
No file I/O — all tests use inline fixture data.
"""

import numpy as np
import pandas as pd
import pytest

from src.linking.build import (
    build_links,
    haversine,
    nearest_n,
    select_groundwater,
    select_predictors_rainfall,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_station(
    station_id, measure_id, lat, lon, measure_type,
    measure_period=None, measure_value_statistic=None,
):
    return {
        "station_id":              station_id,
        "station_name":            station_id,
        "lat":                     lat,
        "lon":                     lon,
        "measure_id":              measure_id,
        "measure_type":            measure_type,
        "measure_period":          measure_period,
        "measure_value_statistic": measure_value_statistic,
    }


@pytest.fixture
def gw_stations():
    return pd.DataFrame([
        _make_station("GW1", "GW1-mAOD", 51.0, -1.3, "groundwater",
                      measure_period=900, measure_value_statistic="instantaneous"),
        _make_station("GW2", "GW2-mAOD", 51.1, -1.1, "groundwater",
                      measure_period=900, measure_value_statistic="instantaneous"),
    ])


@pytest.fixture
def rain_stations():
    # Four stations at known offsets from GW1 — distances increase with index
    return pd.DataFrame([
        _make_station("R1", "R1-mm", 51.01, -1.30, "rainfall",
                      measure_period=900, measure_value_statistic="instantaneous"),
        _make_station("R2", "R2-mm", 51.05, -1.30, "rainfall",
                      measure_period=900, measure_value_statistic="instantaneous"),
        _make_station("R3", "R3-mm", 51.10, -1.30, "rainfall",
                      measure_period=900, measure_value_statistic="instantaneous"),
        _make_station("R4", "R4-mm", 51.20, -1.30, "rainfall",
                      measure_period=900, measure_value_statistic="instantaneous"),
    ])


# ---------------------------------------------------------------------------
# haversine
# ---------------------------------------------------------------------------

def test_haversine_known_distance():
    # London (51.5074, -0.1278) to Paris (48.8566, 2.3522) ≈ 340 km
    dist = haversine(51.5074, -0.1278, 48.8566, 2.3522)
    assert abs(float(dist) - 340) < 5


def test_haversine_zero():
    assert haversine(51.0, -1.3, 51.0, -1.3) == pytest.approx(0.0)


def test_haversine_symmetry():
    d1 = haversine(51.0, -1.3, 50.8, -1.1)
    d2 = haversine(50.8, -1.1, 51.0, -1.3)
    assert float(d1) == pytest.approx(float(d2))


def test_haversine_vectorised():
    lats = np.array([51.01, 51.05, 51.10])
    lons = np.array([-1.30, -1.30, -1.30])
    dists = haversine(51.0, -1.3, lats, lons)
    assert dists.shape == (3,)
    assert (dists > 0).all()
    # distances should be strictly increasing
    assert dists[0] < dists[1] < dists[2]


# ---------------------------------------------------------------------------
# select_groundwater
# ---------------------------------------------------------------------------

def test_select_groundwater_excludes_dipped():
    df = pd.DataFrame([
        _make_station("GW1", "GW1-logged", 51.0, -1.3, "groundwater", measure_period=900),
        _make_station("GW2", "GW2-dipped", 51.1, -1.2, "groundwater", measure_period=None),
    ])
    result = select_groundwater(df)
    assert len(result) == 1
    assert result.iloc[0]["station_id"] == "GW1"


def test_select_groundwater_prefers_lowest_period():
    df = pd.DataFrame([
        _make_station("GW1", "GW1-hourly",  51.0, -1.3, "groundwater", measure_period=3600),
        _make_station("GW1", "GW1-15min",   51.0, -1.3, "groundwater", measure_period=900),
        _make_station("GW1", "GW1-daily",   51.0, -1.3, "groundwater", measure_period=86400),
    ])
    result = select_groundwater(df)
    assert len(result) == 1
    assert result.iloc[0]["measure_id"] == "GW1-15min"


def test_select_groundwater_one_row_per_station():
    df = pd.DataFrame([
        _make_station("GW1", "GW1-a", 51.0, -1.3, "groundwater", measure_period=900),
        _make_station("GW1", "GW1-b", 51.0, -1.3, "groundwater", measure_period=900),
        _make_station("GW2", "GW2-a", 51.1, -1.2, "groundwater", measure_period=900),
    ])
    result = select_groundwater(df)
    assert len(result) == 2
    assert result["station_id"].nunique() == 2


def test_select_groundwater_excludes_other_types():
    df = pd.DataFrame([
        _make_station("GW1", "GW1-mAOD", 51.0, -1.3, "groundwater", measure_period=900),
        _make_station("R1",  "R1-mm",    51.1, -1.2, "rainfall",    measure_period=900),
    ])
    result = select_groundwater(df)
    assert list(result["station_id"]) == ["GW1"]


# ---------------------------------------------------------------------------
# select_predictors_rainfall
# ---------------------------------------------------------------------------

_RAIN_PREFS  = {"preferred_period": 86400}


def test_select_rainfall_prefers_86400():
    df = pd.DataFrame([
        _make_station("R1", "R1-daily-total",  51.0, -1.3, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
        _make_station("R1", "R1-15min-total",  51.0, -1.3, "rainfall",
                      measure_period=900,   measure_value_statistic="total"),
    ])
    result = select_predictors_rainfall(df, _RAIN_PREFS)
    assert len(result) == 1
    assert result.iloc[0]["measure_id"] == "R1-daily-total"
    assert result.iloc[0]["selection_reason"] == "preferred"


def test_select_rainfall_fallback_to_900():
    df = pd.DataFrame([
        _make_station("R1", "R1-15min-total", 51.0, -1.3, "rainfall",
                      measure_period=900, measure_value_statistic="total"),
    ])
    result = select_predictors_rainfall(df, _RAIN_PREFS)
    assert len(result) == 1
    assert result.iloc[0]["measure_id"] == "R1-15min-total"
    assert result.iloc[0]["selection_reason"] == "fallback"


def test_select_rainfall_does_not_require_instantaneous():
    df = pd.DataFrame([
        _make_station("R1", "R1-daily-total", 51.0, -1.3, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
    ])
    result = select_predictors_rainfall(df, _RAIN_PREFS)
    assert len(result) == 1


def test_select_rainfall_one_row_per_station():
    df = pd.DataFrame([
        _make_station("R1", "R1-daily-a", 51.0, -1.3, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
        _make_station("R1", "R1-daily-b", 51.0, -1.3, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
        _make_station("R2", "R2-daily",   51.1, -1.2, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
    ])
    result = select_predictors_rainfall(df, _RAIN_PREFS)
    assert len(result) == 2
    assert result["station_id"].nunique() == 2


def test_select_rainfall_selection_reason_flag_present():
    df = pd.DataFrame([
        _make_station("R1", "R1-daily", 51.0, -1.3, "rainfall",
                      measure_period=86400, measure_value_statistic="total"),
    ])
    result = select_predictors_rainfall(df, _RAIN_PREFS)
    assert "selection_reason" in result.columns
    assert result.iloc[0]["selection_reason"] in ("preferred", "fallback")


# ---------------------------------------------------------------------------
# nearest_n
# ---------------------------------------------------------------------------

def test_nearest_n_returns_correct_order(gw_stations, rain_stations):
    gw_row = gw_stations.iloc[0]  # GW1 at (51.0, -1.3)
    result = nearest_n(gw_row, rain_stations, n=3)
    ids = [r[0] for r in result]
    assert ids == ["R1-mm", "R2-mm", "R3-mm"]


def test_nearest_n_returns_distances(gw_stations, rain_stations):
    gw_row = gw_stations.iloc[0]
    result = nearest_n(gw_row, rain_stations, n=3)
    dists = [r[1] for r in result]
    assert all(d > 0 for d in dists)
    assert dists == sorted(dists), "Distances should be in ascending order"


def test_nearest_n_distinct_stations(gw_stations, rain_stations):
    gw_row = gw_stations.iloc[0]
    result = nearest_n(gw_row, rain_stations, n=3)
    ids = [r[0] for r in result]
    assert len(ids) == len(set(ids)), "Returned duplicate station measure_ids"


def test_nearest_n_pads_with_none_when_insufficient():
    gw_row = pd.Series({"lat": 51.0, "lon": -1.3})
    two_stations = pd.DataFrame([
        _make_station("R1", "R1-mm", 51.01, -1.30, "rainfall"),
        _make_station("R2", "R2-mm", 51.05, -1.30, "rainfall"),
    ])
    result = nearest_n(gw_row, two_stations, n=3)
    assert result[2] == (None, None)


def test_nearest_n_empty_candidates():
    gw_row = pd.Series({"lat": 51.0, "lon": -1.3})
    result = nearest_n(gw_row, pd.DataFrame(columns=["lat", "lon", "measure_id"]), n=3)
    assert result == [(None, None), (None, None), (None, None)]


# ---------------------------------------------------------------------------
# build_links
# ---------------------------------------------------------------------------

def test_build_links_one_row_per_gw_station(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    assert len(links) == len(gw_stations)


def test_build_links_required_columns(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    expected = {
        "GWStationID", "GWMeasureID",
        "RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3",
    }
    assert expected.issubset(set(links.columns))


def test_build_links_top3_rainfall_are_distinct_stations(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    for _, row in links.iterrows():
        rain_ids = [row["RainMeasureID_1"], row["RainMeasureID_2"], row["RainMeasureID_3"]]
        rain_ids = [r for r in rain_ids if r is not None]
        assert len(rain_ids) == len(set(rain_ids)), f"Duplicate rainfall links for {row['GWStationID']}"


def test_build_links_distance_columns_present(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    for col in ["RainDist_1", "RainDist_2", "RainDist_3"]:
        assert col in links.columns
        assert links[col].notna().all(), f"{col} contains unexpected None"


def test_build_links_distances_are_positive(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    for col in ["RainDist_1", "RainDist_2", "RainDist_3"]:
        assert (links[col] > 0).all(), f"{col} has non-positive distance"


def test_build_links_rain_distances_ascending(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    for _, row in links.iterrows():
        assert row["RainDist_1"] <= row["RainDist_2"] <= row["RainDist_3"]


def test_build_links_gw_measure_ids_match_input(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    assert set(links["GWMeasureID"]) == set(gw_stations["measure_id"])


def test_build_links_gwstationid_never_null(gw_stations, rain_stations):
    links = build_links(gw_stations, rain_stations)
    assert links["GWStationID"].notna().all()
    assert (links["GWStationID"] != "").all()

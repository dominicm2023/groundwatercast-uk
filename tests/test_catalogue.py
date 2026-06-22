"""
Unit tests for src/catalogue/build.py.
No network calls — all tests use inline fixture data.
"""

import pandas as pd
import pytest

from src.catalogue.build import (
    classify_measure,
    classify_measures,
    derive_station_id,
    expand_measures,
    parse_period_from_measure_id,
    parse_stations,
    validate_classification,
)


# ---------------------------------------------------------------------------
# parse_period_from_measure_id
# ---------------------------------------------------------------------------

class TestParsePeriodFromMeasureId:
    def test_rainfall_86400(self):
        mid = "2df719bf-0e40-4725-a3be-e674e76d669e-rainfall-t-86400-mm-qualified"
        assert parse_period_from_measure_id(mid) == 86400

    def test_river_level_900(self):
        mid = "f3d486b4-085b-4deb-836d-2acb20933188-level-i-900-m-qualified"
        assert parse_period_from_measure_id(mid) == 900

    def test_river_flow_900(self):
        mid = "38131efd-7c79-4c3d-8657-6a6b18ba4b5c-flow-i-900-m3s-qualified"
        assert parse_period_from_measure_id(mid) == 900

    def test_gw_logged_subdaily_inferred_as_900(self):
        # EA CSV omits period for these; JSON API consistently reports 900.
        mid = "9d91f19f-567a-4286-a378-ecc7c2553223-gw-logged-i-subdaily-mAOD-qualified"
        assert parse_period_from_measure_id(mid) == 900

    def test_gw_dipped_returns_none(self):
        mid = "367b1196-a7e2-4781-b2f1-b301ff3c9f68-gw-dipped-i-mAOD-qualified"
        assert parse_period_from_measure_id(mid) is None

    def test_unknown_pattern_returns_none(self):
        assert parse_period_from_measure_id("foo-bar-baz") is None

    def test_empty_and_none_inputs(self):
        assert parse_period_from_measure_id("") is None
        assert parse_period_from_measure_id(None) is None

    def test_compound_id_with_reference_number(self):
        # Composite IDs (e.g. "<guid>_<reference>-...") must still parse correctly
        mid = "1c578eb1-9f07-448e-8aa4-5e7ce09c392e_644610006-gw-logged-i-subdaily-mAOD-qualified"
        assert parse_period_from_measure_id(mid) == 900

    def test_river_3600(self):
        mid = "abc-level-i-3600-m-qualified"
        assert parse_period_from_measure_id(mid) == 3600

    def test_does_not_match_arbitrary_digits(self):
        # Reference numbers like '644610006' must NOT be parsed as a period
        mid = "1c578eb1-9f07-448e-8aa4-5e7ce09c392e_644610006-gw-dipped-i-mAOD-qualified"
        assert parse_period_from_measure_id(mid) is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_stations_df():
    """Minimal fixture using stationGuid as the ID source."""
    return pd.DataFrame({
        "stationGuid": ["guid-001", "guid-002"],
        "label":       ["Borehole A", "Gauge B"],
        "lat":         [51.0, 50.9],
        "long":        [-1.3, -1.5],
        "measures": [
            "https://environment.data.gov.uk/hydrology/id/measures/ST001-gw-mAOD-15_min-mAOD",
            "https://environment.data.gov.uk/hydrology/id/measures/ST002-rainfall-mm-15_min|"
            "https://environment.data.gov.uk/hydrology/id/measures/ST002-level-m-15_min",
        ],
    })


@pytest.fixture
def raw_stations_with_metadata_df():
    """Fixture with pipe-delimited period and valueStatistic metadata."""
    return pd.DataFrame({
        "stationGuid": ["guid-003"],
        "label":       ["River Station"],
        "lat":         [51.0],
        "long":        [-1.3],
        "measures": [
            "https://example.com/measures/ST003-flow-i-900-m3s|"
            "https://example.com/measures/ST003-flow-m-86400-m3s|"
            "https://example.com/measures/ST003-flow-max-86400-m3s"
        ],
        "measures.period": ["900|86400|86400"],
        "measures.valueStatistic": [
            "https://example.com/def/core/instantaneous|"
            "https://example.com/def/core/mean|"
            "https://example.com/def/core/maximum"
        ],
    })


# ---------------------------------------------------------------------------
# derive_station_id
# ---------------------------------------------------------------------------

def test_station_id_derived_from_guid():
    row = pd.Series({"stationGuid": "abc-123", "notation": "NOTATION", "@id": "https://x/y/Z"})
    assert derive_station_id(row) == "abc-123"


def test_station_id_derived_from_notation():
    row = pd.Series({"stationGuid": None, "notation": "E6410", "@id": "https://x/y/Z"})
    assert derive_station_id(row) == "E6410"


def test_station_id_derived_from_at_id():
    row = pd.Series({"stationGuid": None, "notation": None, "@id": "https://x/y/E6410"})
    assert derive_station_id(row) == "E6410"


def test_station_id_empty_when_all_missing():
    row = pd.Series({"stationGuid": None, "notation": None, "@id": None})
    assert derive_station_id(row) == ""


# ---------------------------------------------------------------------------
# parse_stations
# ---------------------------------------------------------------------------

def test_parse_stations_columns(raw_stations_df):
    result = parse_stations(raw_stations_df)
    assert "station_id" in result.columns
    assert "station_name" in result.columns
    assert "lat" in result.columns
    assert "lon" in result.columns
    assert "measures_raw" in result.columns


def test_station_id_never_null(raw_stations_df):
    result = parse_stations(raw_stations_df)
    assert result["station_id"].notna().all()
    assert (result["station_id"] != "").all()


def test_station_id_uses_guid(raw_stations_df):
    result = parse_stations(raw_stations_df)
    assert list(result["station_id"]) == ["guid-001", "guid-002"]


def test_parse_stations_types(raw_stations_df):
    result = parse_stations(raw_stations_df)
    assert result["lat"].dtype == float
    assert result["lon"].dtype == float


def test_parse_stations_missing_column_raises():
    bad_df = pd.DataFrame({"irrelevant": [1, 2]})
    with pytest.raises(ValueError, match="Could not find column"):
        parse_stations(bad_df)


def test_parse_stations_raises_if_station_id_undeducible():
    df = pd.DataFrame({
        "label": ["X"],
        "lat": [51.0],
        "long": [-1.3],
        "measures": ["https://example.com/measures/X-mAOD"],
        # no stationGuid, notation, or @id
    })
    with pytest.raises(ValueError, match="no derivable station_id"):
        parse_stations(df)


# ---------------------------------------------------------------------------
# expand_measures
# ---------------------------------------------------------------------------

def test_expand_measures_one_row_per_measure(raw_stations_df):
    parsed = parse_stations(raw_stations_df)
    expanded = expand_measures(parsed)
    assert len(expanded) == 3


def test_expand_measures_has_measure_id(raw_stations_df):
    parsed = parse_stations(raw_stations_df)
    expanded = expand_measures(parsed)
    assert "measure_id" in expanded.columns
    assert expanded["measure_id"].notna().all()


def test_expand_measures_no_raw_columns(raw_stations_df):
    parsed = parse_stations(raw_stations_df)
    expanded = expand_measures(parsed)
    assert "measures_raw" not in expanded.columns
    assert "measures_period_raw" not in expanded.columns
    assert "measures_statistic_raw" not in expanded.columns


def test_expand_measures_period_aligned(raw_stations_with_metadata_df):
    """
    Period is now derived from each measure_id slug (not the EA CSV's
    unreliable scalar ``measures.period`` column), so the values are
    typed integers and align 1-to-1 with each measure.
    """
    parsed = parse_stations(raw_stations_with_metadata_df)
    expanded = expand_measures(parsed)
    assert len(expanded) == 3
    assert list(expanded["measure_period"]) == [900, 86400, 86400]


def test_expand_measures_value_statistic_aligned(raw_stations_with_metadata_df):
    parsed = parse_stations(raw_stations_with_metadata_df)
    expanded = expand_measures(parsed)
    assert list(expanded["measure_value_statistic"]) == [
        "instantaneous", "mean", "maximum"
    ]


def test_expand_measures_multiple_periods_present(raw_stations_with_metadata_df):
    parsed = parse_stations(raw_stations_with_metadata_df)
    expanded = expand_measures(parsed)
    assert expanded["measure_period"].nunique() > 1, \
        "Expected multiple distinct period values for this station"


def test_expand_measures_length_mismatch_fills_none():
    """If period list length mismatches measures, all period values become None."""
    df = pd.DataFrame({
        "stationGuid": ["guid-x"],
        "label": ["X"],
        "lat": [51.0],
        "long": [-1.3],
        "measures": ["https://example.com/m/A|https://example.com/m/B"],
        "measures.period": ["900"],            # 1 value but 2 measures
        "measures.valueStatistic": [None],
    })
    parsed = parse_stations(df)
    expanded = expand_measures(parsed)
    assert expanded["measure_period"].isna().all()


# ---------------------------------------------------------------------------
# classify_measure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("measure_id,expected", [
    ("ST001-mAOD-15_min",                "groundwater"),
    ("ST001-gw-15_min",                  "unknown"),
    ("ST002-rainfall-mm-15_min",         "rainfall"),
    ("ST002-precip-mm-15_min",           "rainfall"),
    ("ST003-flow-m3s-15_min",            "river_flow"),
    ("ST003-m3-15_min",                  "river_flow"),
    ("ST004-level-m-15_min",             "river_level"),
    ("ST004-stage-15_min",               "river_level"),
    ("ST005-temperature-degC-15_min",    "unknown"),
])
def test_classify_measure_fast(measure_id, expected):
    assert classify_measure(measure_id, mode="fast") == expected


def test_classify_measure_strict_raises():
    with pytest.raises(NotImplementedError):
        classify_measure("any-measure-id", mode="strict")


# ---------------------------------------------------------------------------
# validate_classification
# ---------------------------------------------------------------------------

def test_validate_classification_ok():
    df = pd.DataFrame({"measure_type": ["groundwater"] * 100})
    validate_classification(df)


def test_validate_classification_warning():
    types = ["groundwater"] * 97 + ["unknown"] * 3
    df = pd.DataFrame({"measure_type": types})
    with pytest.warns(UserWarning, match="3.0%"):
        validate_classification(df)


def test_validate_classification_raises():
    types = ["groundwater"] * 90 + ["unknown"] * 10
    df = pd.DataFrame({"measure_type": types})
    with pytest.raises(ValueError, match="10.0%"):
        validate_classification(df)


def test_validate_classification_empty():
    validate_classification(pd.DataFrame({"measure_type": []}))

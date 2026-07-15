"""
Unit tests for src/catalogue/flow.py (low-flow build_plan.md Stage 1).

No network calls — fixtures model the real EA Hydrology
``stations.csv?observedProperty=waterFlow`` response shape (columns
verified live 2026-07-14: stationGuid, label, lat, long, catchmentName,
riverName, dateOpened, dateClosed, status.label, measures).
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.catalogue.flow import (
    build_flow_links,
    daily_mean_measure_id,
    flow_catalogue_config,
    is_open,
    parse_flow_catalogue,
)


# ---------------------------------------------------------------------------
# daily_mean_measure_id
# ---------------------------------------------------------------------------

def _measures_uri(guid: str, suffixes: list[str]) -> str:
    base = "http://environment.data.gov.uk/hydrology/id/measures"
    return "|".join(f"{base}/{guid}-{s}" for s in suffixes)


class TestDailyMeanMeasureId:
    def test_finds_daily_mean_qualified(self):
        guid = "e9c72be8-dea1-4a5d-8af7-05dce5f419ee"
        measures = _measures_uri(guid, [
            "flow-i-900-m3s-qualified",
            "flow-m-86400-m3s-qualified",
            "flow-max-86400-m3s-qualified",
            "flow-min-86400-m3s-qualified",
        ])
        assert daily_mean_measure_id(measures) == f"{guid}-flow-m-86400-m3s-qualified"

    def test_returns_none_when_absent(self):
        guid = "abc"
        measures = _measures_uri(guid, ["flow-i-900-m3s-qualified"])
        assert daily_mean_measure_id(measures) is None

    def test_ignores_unqualified_daily_mean(self):
        # An "unqualified" daily mean must NOT be picked up as the qualified one
        guid = "abc"
        measures = _measures_uri(guid, ["flow-m-86400-m3s-unqualified"])
        assert daily_mean_measure_id(measures) is None

    def test_empty_and_nan_inputs(self):
        assert daily_mean_measure_id("") is None
        assert daily_mean_measure_id(None) is None
        assert daily_mean_measure_id(float("nan")) is None

    def test_compound_guid_with_reference_suffix(self):
        # EA occasionally suffixes guids with "_<reference>" (seen live on
        # Medway@Teston in the validation fixtures) — must still parse.
        guid = "eba748a3-ebd6-4141-a671-5ef94b896ffa_453202901"
        measures = _measures_uri(guid, ["flow-m-86400-m3s-qualified"])
        assert daily_mean_measure_id(measures) == f"{guid}-flow-m-86400-m3s-qualified"


# ---------------------------------------------------------------------------
# is_open
# ---------------------------------------------------------------------------

class TestIsOpen:
    def test_active_with_no_close_date_is_open(self):
        row = pd.Series({"dateClosed": None, "status.label": "Active"})
        assert is_open(row) is True

    def test_date_closed_present_is_not_open(self):
        row = pd.Series({"dateClosed": "2020-01-01", "status.label": "Active"})
        assert is_open(row) is False

    def test_status_label_closed_is_not_open(self):
        row = pd.Series({"dateClosed": None, "status.label": "Closed"})
        assert is_open(row) is False

    def test_suspended_still_counts_as_open(self):
        # Data-flow-suspended gauges remain catalogued; only "Closed" excludes.
        row = pd.Series({"dateClosed": None,
                         "status.label": "Flow Suspended|Suspended"})
        assert is_open(row) is True

    def test_nan_close_date_is_open(self):
        row = pd.Series({"dateClosed": float("nan"), "status.label": "Active"})
        assert is_open(row) is True


# ---------------------------------------------------------------------------
# parse_flow_catalogue
# ---------------------------------------------------------------------------

@pytest.fixture
def raw_flow_stations_df():
    """Modelled on the live EA waterFlow CSV response (columns + a subset of
    real rows: Itchen@Highbridge, Test@Chilbolton, a closed gauge, and a
    gauge missing the daily-mean qualified measure)."""
    itchen_guid = "e9c72be8-dea1-4a5d-8af7-05dce5f419ee"
    test_guid = "d96cf58d-21f6-49bd-85e1-8db63a693645"
    closed_guid = "closed-0001"
    no_daily_guid = "no-daily-0001"

    def measures_for(guid, suffixes):
        return _measures_uri(guid, suffixes)

    full_suffixes = [
        "flow-i-900-m3s-qualified",
        "flow-m-86400-m3s-qualified",
        "flow-max-86400-m3s-qualified",
        "flow-min-86400-m3s-qualified",
    ]

    return pd.DataFrame([
        {
            "stationGuid": itchen_guid, "label": "Highbridge",
            "lat": 50.990385, "long": -1.335534,
            "riverName": "River Itchen", "catchmentName": "Itchen",
            "dateOpened": "1971-01-01", "dateClosed": None,
            "status.label": "Active",
            "measures": measures_for(itchen_guid, full_suffixes),
        },
        {
            "stationGuid": test_guid, "label": "Chilbolton Total",
            "lat": 51.15252, "long": -1.450838,
            "riverName": "River Test", "catchmentName": "Test",
            "dateOpened": "1963-06-01", "dateClosed": None,
            "status.label": "Active",
            "measures": measures_for(test_guid, full_suffixes),
        },
        {
            "stationGuid": closed_guid, "label": "Closed Gauge",
            "lat": 51.0, "long": -1.0,
            "riverName": "River Nowhere", "catchmentName": "Nowhere",
            "dateOpened": "1980-01-01", "dateClosed": "2015-01-01",
            "status.label": "Closed",
            "measures": measures_for(closed_guid, full_suffixes),
        },
        {
            "stationGuid": no_daily_guid, "label": "Instantaneous Only",
            "lat": 51.2, "long": -1.2,
            "riverName": "River Instant", "catchmentName": "Instant",
            "dateOpened": "1990-01-01", "dateClosed": None,
            "status.label": "Active",
            "measures": measures_for(no_daily_guid, ["flow-i-900-m3s-qualified"]),
        },
    ])


class TestParseFlowCatalogue:
    def test_keeps_only_open_gauges_with_daily_mean_measure(self, raw_flow_stations_df):
        result = parse_flow_catalogue(raw_flow_stations_df)
        assert set(result["station_id"]) == {
            "e9c72be8-dea1-4a5d-8af7-05dce5f419ee",
            "d96cf58d-21f6-49bd-85e1-8db63a693645",
        }

    def test_output_columns(self, raw_flow_stations_df):
        result = parse_flow_catalogue(raw_flow_stations_df)
        expected = {
            "station_id", "station_name", "lat", "lon", "river_name",
            "catchment_name", "flow_measure_id", "record_start",
        }
        assert expected.issubset(set(result.columns))

    def test_flow_measure_id_is_daily_mean_qualified(self, raw_flow_stations_df):
        result = parse_flow_catalogue(raw_flow_stations_df)
        itchen = result[result["station_id"] == "e9c72be8-dea1-4a5d-8af7-05dce5f419ee"].iloc[0]
        assert itchen["flow_measure_id"] == (
            "e9c72be8-dea1-4a5d-8af7-05dce5f419ee-flow-m-86400-m3s-qualified"
        )

    def test_itchen_and_test_spot_check(self, raw_flow_stations_df):
        """Matches the gauge ids used in
        docs/product/lowflow/scripts/validation_fetch.py."""
        result = parse_flow_catalogue(raw_flow_stations_df).set_index("station_id")
        assert result.loc["e9c72be8-dea1-4a5d-8af7-05dce5f419ee", "river_name"] == "River Itchen"
        assert result.loc["d96cf58d-21f6-49bd-85e1-8db63a693645", "river_name"] == "River Test"

    def test_lat_lon_numeric(self, raw_flow_stations_df):
        result = parse_flow_catalogue(raw_flow_stations_df)
        assert result["lat"].dtype == float
        assert result["lon"].dtype == float

    def test_no_duplicate_station_ids(self, raw_flow_stations_df):
        doubled = pd.concat([raw_flow_stations_df, raw_flow_stations_df.iloc[[0]]],
                            ignore_index=True)
        result = parse_flow_catalogue(doubled)
        assert result["station_id"].is_unique

    def test_colocated_split_channel_gauges_kept_as_distinct_rows(self):
        """EA reuses one stationGuid across colocated split-flow channels
        (live-verified 2026-07-14, e.g. 'Coolham Total'/'Main'/'Side' all
        share one stationGuid). station_id must be derived from the
        (per-channel-unique) flow_measure_id, not the bare stationGuid, or
        distinct real gauges collapse into one row."""
        guid = "26e91f00-1139-4775-aac4-76c88f1bf1e6"
        full_suffixes = ["flow-m-86400-m3s-qualified"]
        df = pd.DataFrame([
            {
                "stationGuid": guid, "label": "Coolham Total",
                "lat": 50.999521, "long": -0.402618,
                "riverName": "River Adur", "catchmentName": "Adur",
                "dateOpened": "2003-04-03", "dateClosed": None,
                "status.label": "Active",
                "measures": _measures_uri(guid, full_suffixes),
            },
            {
                "stationGuid": guid, "label": "Coolham Main",
                "lat": 50.999521, "long": -0.402618,
                "riverName": "River Adur", "catchmentName": "Adur",
                "dateOpened": "2014-04-14", "dateClosed": None,
                "status.label": "Active",
                "measures": _measures_uri(f"{guid}_w1", full_suffixes),
            },
            {
                "stationGuid": guid, "label": "Coolham Side",
                "lat": 50.999521, "long": -0.402618,
                "riverName": "River Adur", "catchmentName": "Adur",
                "dateOpened": "2014-04-14", "dateClosed": None,
                "status.label": "Active",
                "measures": _measures_uri(f"{guid}_w2", full_suffixes),
            },
        ])
        result = parse_flow_catalogue(df)
        assert len(result) == 3
        assert result["station_id"].is_unique
        assert set(result["station_id"]) == {guid, f"{guid}_w1", f"{guid}_w2"}


# ---------------------------------------------------------------------------
# build_flow_links — nearest-3 selection (reuses src.linking.build.nearest_n)
# ---------------------------------------------------------------------------

@pytest.fixture
def flow_catalogue_df():
    return pd.DataFrame([
        {"station_id": "GAUGE1", "flow_measure_id": "GAUGE1-flow-m-86400-m3s-qualified",
         "lat": 51.0, "lon": -1.3},
        {"station_id": "GAUGE2", "flow_measure_id": "GAUGE2-flow-m-86400-m3s-qualified",
         "lat": 51.1, "lon": -1.1},
    ])


@pytest.fixture
def rain_candidates_df():
    # Distances from GAUGE1 (51.0, -1.3) increase with index, as in test_linking.py
    return pd.DataFrame([
        {"station_id": "R1", "measure_id": "R1-mm", "lat": 51.01, "lon": -1.30},
        {"station_id": "R2", "measure_id": "R2-mm", "lat": 51.05, "lon": -1.30},
        {"station_id": "R3", "measure_id": "R3-mm", "lat": 51.10, "lon": -1.30},
        {"station_id": "R4", "measure_id": "R4-mm", "lat": 51.20, "lon": -1.30},
    ])


class TestBuildFlowLinks:
    def test_one_row_per_gauge(self, flow_catalogue_df, rain_candidates_df):
        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        assert len(links) == len(flow_catalogue_df)

    def test_required_columns_match_station_links_semantics(
        self, flow_catalogue_df, rain_candidates_df
    ):
        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        expected = {
            "GaugeID", "FlowMeasureID",
            "RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3",
            "RainDist_1", "RainDist_2", "RainDist_3",
        }
        assert expected.issubset(set(links.columns))

    def test_nearest_3_order(self, flow_catalogue_df, rain_candidates_df):
        links = build_flow_links(flow_catalogue_df, rain_candidates_df).set_index("GaugeID")
        row = links.loc["GAUGE1"]
        assert [row["RainMeasureID_1"], row["RainMeasureID_2"], row["RainMeasureID_3"]] == [
            "R1-mm", "R2-mm", "R3-mm",
        ]

    def test_distances_ascending(self, flow_catalogue_df, rain_candidates_df):
        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        for _, row in links.iterrows():
            assert row["RainDist_1"] <= row["RainDist_2"] <= row["RainDist_3"]

    def test_gaugeid_never_null(self, flow_catalogue_df, rain_candidates_df):
        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        assert links["GaugeID"].notna().all()

    def test_pads_with_none_when_insufficient_candidates(self, flow_catalogue_df):
        one_candidate = pd.DataFrame([
            {"station_id": "R1", "measure_id": "R1-mm", "lat": 51.01, "lon": -1.30},
        ])
        links = build_flow_links(flow_catalogue_df, one_candidate).set_index("GaugeID")
        row = links.loc["GAUGE1"]
        assert row["RainMeasureID_1"] == "R1-mm"
        assert pd.isna(row["RainMeasureID_2"])
        assert pd.isna(row["RainMeasureID_3"])


# ---------------------------------------------------------------------------
# flow_links.csv -> gauge_rainfall_for consumption (the "links-frame swap")
# ---------------------------------------------------------------------------

class TestGaugeRainfallForLinksFrameSwap:
    def test_gauge_rainfall_for_consumes_flow_links_shape(
        self, flow_catalogue_df, rain_candidates_df, tmp_path
    ):
        """gauge_rainfall_for (src/forecast/ensemble/members.py:29) must work
        unchanged when handed a flow_links-shaped frame indexed by GaugeID —
        the build_plan's 'links-frame swap' requirement."""
        from src.forecast.ensemble.members import gauge_rainfall_for

        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        links = links.set_index("GaugeID")

        raw = tmp_path / "rainfall"
        raw.mkdir(parents=True)
        dates = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
        for mid, val in [("R1-mm", 1.0), ("R2-mm", 2.0), ("R3-mm", 3.0)]:
            pd.DataFrame({"dateTime": dates, "value": [val] * 10}).to_csv(
                raw / f"{mid}.csv", index=False
            )

        series = gauge_rainfall_for("GAUGE1", links, str(tmp_path))
        assert not series.empty
        # average of the 3 nearest linked gauges (1.0, 2.0, 3.0)
        assert abs(float(series.mean()) - 2.0) < 1e-9

    def test_broken_gauge_screen_reused_for_flow_links(
        self, flow_catalogue_df, rain_candidates_df, tmp_path
    ):
        """The >12 mm/day broken-gauge screen lives in
        observed_daily_rainfall and is reused unchanged — a flow_links-
        sourced rain id with an implausible long-run mean is excluded the
        same way a station_links-sourced one is."""
        from src.forecast.ensemble.members import gauge_rainfall_for

        links = build_flow_links(flow_catalogue_df, rain_candidates_df)
        links = links.set_index("GaugeID")

        raw = tmp_path / "rainfall"
        raw.mkdir(parents=True)
        dates = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")
        for mid, val in [("R1-mm", 2.0), ("R2-mm", 3.0), ("R3-mm", 130.0)]:
            pd.DataFrame({"dateTime": dates, "value": [val] * 200}).to_csv(
                raw / f"{mid}.csv", index=False
            )

        series = gauge_rainfall_for("GAUGE1", links, str(tmp_path))
        assert not series.empty
        # R3-mm (130 mm/day) is screened out — average of the two good gauges only
        assert abs(float(series.mean()) - 2.5) < 1e-9


# ---------------------------------------------------------------------------
# flow_catalogue_config
# ---------------------------------------------------------------------------

class TestFlowCatalogueConfig:
    def test_defaults_when_absent(self):
        cfg = flow_catalogue_config({})
        assert cfg["output_path"] == "data/processed/flow_catalogue.csv"
        assert cfg["links_output_path"] == "data/processed/flow_links.csv"
        assert cfg["observed_property"] == "waterFlow"

    def test_overrides_merge(self):
        cfg = flow_catalogue_config({"flow_catalogue": {"stations_limit": 5}})
        assert cfg["stations_limit"] == 5
        assert cfg["observed_property"] == "waterFlow"

"""
Tests for the national-catalogue dry-run.

Covers:
  1. england_wales.geojson validity (loads, named features, valid polygons)
     + provenance sidecar.
  2. Point-in-polygon nation classification on synthetic points
     (Hampshire → England, Wales → Wales, Scotland/offshore → excluded).
  3. CLI arg parsing + the outputs/-only guard on --out.

No network calls — the live fetch path is exercised manually, not in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon

from scripts.national_catalogue_dryrun import (
    DEFAULT_BOUNDARY,
    OUTSIDE_LABEL,
    assign_nation,
    build_summary_table,
    load_nation_geometries,
    parse_args,
    validate_out_path,
)
from src.catalogue.build import filter_to_region, load_region_geometry

ROOT = Path(__file__).parents[1]
BOUNDARY_PATH = ROOT / "data" / "regions" / "england_wales.geojson"
PROVENANCE_PATH = ROOT / "data" / "regions" / "england_wales.source.json"

# Synthetic probe points (lon, lat)
WINCHESTER = (-1.31, 51.06)   # Hampshire — England
CARDIFF = (-3.18, 51.48)      # Wales
EDINBURGH = (-3.19, 55.95)    # Scotland — excluded
OFFSHORE = (-1.00, 49.50)     # mid-Channel — excluded


# ---------------------------------------------------------------------------
# 1. Boundary GeoJSON validity
# ---------------------------------------------------------------------------

class TestBoundaryGeoJSON:
    def test_file_exists(self):
        assert BOUNDARY_PATH.exists(), f"Missing boundary file: {BOUNDARY_PATH}"

    def test_is_default_boundary(self):
        assert DEFAULT_BOUNDARY == BOUNDARY_PATH

    def test_under_one_megabyte(self):
        # Coverage filter, not cartography — keep it light
        assert BOUNDARY_PATH.stat().st_size < 1_000_000

    def test_loads_as_feature_collection_with_named_nations(self):
        with open(BOUNDARY_PATH) as f:
            gj = json.load(f)
        assert gj["type"] == "FeatureCollection"
        names = {feat["properties"]["name"] for feat in gj["features"]}
        assert names == {"England", "Wales"}

    def test_geometries_are_valid_polygons(self):
        geoms = load_nation_geometries(BOUNDARY_PATH)
        assert set(geoms) == {"England", "Wales"}
        for name, geom in geoms.items():
            assert isinstance(geom, (Polygon, MultiPolygon)), name
            assert geom.is_valid, f"{name} geometry invalid"

    def test_loads_via_catalogue_machinery(self):
        # The same loader build_catalogue uses must accept this file
        geom = load_region_geometry(str(BOUNDARY_PATH))
        assert geom.is_valid
        lon_min, lat_min, lon_max, lat_max = geom.bounds
        # England+Wales footprint: Scilly to Berwick
        assert -7.0 < lon_min < -5.0 and 1.0 < lon_max < 2.5
        assert 49.5 < lat_min < 50.2 and 55.5 < lat_max < 56.0

    def test_provenance_sidecar(self):
        with open(PROVENANCE_PATH) as f:
            prov = json.load(f)
        for key in ("dataset_name", "source_url", "licence",
                    "fetch_date_utc", "crs"):
            assert key in prov, f"Missing provenance key: {key}"
        assert prov["crs"] == "EPSG:4326"
        assert "OGL" in prov["licence"]


# ---------------------------------------------------------------------------
# 2. Point-in-polygon nation classification
# ---------------------------------------------------------------------------

class TestNationClassification:
    @pytest.fixture(scope="class")
    def nations(self):
        return load_nation_geometries(BOUNDARY_PATH)

    def test_hampshire_point_is_england(self, nations):
        assert assign_nation(*WINCHESTER, nations) == "England"

    def test_wales_point_is_wales(self, nations):
        assert assign_nation(*CARDIFF, nations) == "Wales"

    def test_scotland_point_is_outside(self, nations):
        assert assign_nation(*EDINBURGH, nations) == OUTSIDE_LABEL

    def test_offshore_point_is_outside(self, nations):
        assert assign_nation(*OFFSHORE, nations) == OUTSIDE_LABEL

    def test_filter_to_region_keeps_england_and_wales_only(self):
        """The unchanged catalogue filter must work against this boundary."""
        df = pd.DataFrame({
            "station_id": ["hants", "wales", "scot", "sea"],
            "lon": [WINCHESTER[0], CARDIFF[0], EDINBURGH[0], OFFSHORE[0]],
            "lat": [WINCHESTER[1], CARDIFF[1], EDINBURGH[1], OFFSHORE[1]],
        })
        out = filter_to_region(df, str(BOUNDARY_PATH))
        assert set(out["station_id"]) == {"hants", "wales"}

    def test_no_name_features_yield_empty_dict(self, tmp_path):
        anon = tmp_path / "anon.geojson"
        anon.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                },
            }],
        }))
        assert load_nation_geometries(anon) == {}


# ---------------------------------------------------------------------------
# 3. CLI parsing + output guard
# ---------------------------------------------------------------------------

class TestCLI:
    def test_defaults(self):
        args = parse_args([])
        assert args.boundary == DEFAULT_BOUNDARY
        assert args.no_polygon is False
        assert args.limit == 20000
        assert args.out is None

    def test_overrides(self):
        args = parse_args([
            "--boundary", "data/regions/england_wales.geojson",
            "--no-polygon",
            "--limit", "5000",
            "--out", "outputs/dryrun.txt",
        ])
        # Relative --boundary resolves against the repo root, not the CWD
        assert args.boundary == ROOT / "data/regions/england_wales.geojson"
        assert args.no_polygon is True
        assert args.limit == 5000
        assert args.out == Path("outputs/dryrun.txt")

    def test_out_under_outputs_accepted(self):
        resolved = validate_out_path(Path("outputs/national_dryrun.txt"))
        assert resolved == (ROOT / "outputs" / "national_dryrun.txt").resolve()

    def test_out_under_data_rejected(self):
        with pytest.raises(ValueError, match="must be under"):
            validate_out_path(Path("data/processed/catalogue.csv"))

    def test_out_escaping_outputs_rejected(self):
        with pytest.raises(ValueError, match="must be under"):
            validate_out_path(Path("outputs/../config/config.json"))


# ---------------------------------------------------------------------------
# 4. Summary table (pure formatting — no network)
# ---------------------------------------------------------------------------

class TestSummaryTable:
    def test_counts_rows_and_unique_stations_per_nation(self):
        nations = load_nation_geometries(BOUNDARY_PATH)
        df = pd.DataFrame({
            "station_id":   ["h", "h", "w", "s"],
            "lon": [WINCHESTER[0], WINCHESTER[0], CARDIFF[0], EDINBURGH[0]],
            "lat": [WINCHESTER[1], WINCHESTER[1], CARDIFF[1], EDINBURGH[1]],
            "measure_type": ["groundwater", "groundwater", "rainfall", "groundwater"],
        })
        table = build_summary_table(df, nations)
        lines = {ln.split()[0]: ln.split() for ln in table.splitlines()
                 if ln and not ln.startswith("-")}
        # groundwater: 3 measure rows, 2 stations (h, s); h=England, s=outside
        assert lines["groundwater"][1:] == ["3", "2", "1", "0", "1"]
        # rainfall: 1 row, 1 station in Wales
        assert lines["rainfall"][1:] == ["1", "1", "0", "1", "0"]
        assert lines["TOTAL"][1:] == ["4", "3", "1", "1", "1"]

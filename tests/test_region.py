"""
Tests for the regional scoping (England+Wales boundary).

Covers:
  1. GeoJSON file validity (loads, correct CRS, Polygon/MultiPolygon).
  2. Point-in-region behaviour (known-in vs known-out).
  3. Catalogue spatial filter retains only stations inside the polygon.
  4. Map rendering embeds the region GeoJSON layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Point, Polygon, shape

from src.catalogue.build import (
    REGION_CONTAINS_BUFFER_DEG,
    filter_to_region,
    load_region_geometry,
    region_bbox,
)
from src.dashboard.map_builder import _load_region_geojson, build_map

ROOT = Path(__file__).parents[1]
REGION_PATH = ROOT / "data" / "regions" / "england_wales.geojson"
PROVENANCE_PATH = ROOT / "data" / "regions" / "england_wales.source.json"


# ---------------------------------------------------------------------------
# 1. GeoJSON validity
# ---------------------------------------------------------------------------

class TestGeoJSONValidity:
    def test_file_exists(self):
        assert REGION_PATH.exists(), f"Missing region file: {REGION_PATH}"

    def test_provenance_file_exists(self):
        assert PROVENANCE_PATH.exists(), (
            f"Missing provenance sidecar: {PROVENANCE_PATH}"
        )

    def test_provenance_contents(self):
        prov = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        for key in ("source_url", "dataset_name", "licence", "fetch_date_utc"):
            assert key in prov, f"Provenance missing '{key}'"

    def test_loads_as_geojson(self):
        data = json.loads(REGION_PATH.read_text(encoding="utf-8"))
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) >= 1

    def test_geometry_is_polygon_or_multipolygon(self):
        data = json.loads(REGION_PATH.read_text(encoding="utf-8"))
        for feat in data["features"]:
            g = shape(feat["geometry"])
            assert isinstance(g, (Polygon, MultiPolygon)), (
                f"Unexpected geometry type: {g.geom_type}"
            )

    def test_load_region_geometry_returns_valid_shape(self):
        geom = load_region_geometry(str(REGION_PATH))
        assert isinstance(geom, (Polygon, MultiPolygon))
        assert geom.is_valid

    def test_crs_is_wgs84_envelope(self):
        """Coordinates should be in lat/lon (EPSG:4326), not projected metres."""
        geom = load_region_geometry(str(REGION_PATH))
        lon_min, lat_min, lon_max, lat_max = geom.bounds
        # England + Wales — lon roughly -6..+2, lat roughly 49..56
        assert -7 < lon_min < 0, f"lon_min={lon_min} not in WGS84 range"
        assert 49 < lat_min < 51, f"lat_min={lat_min} not in WGS84 range"
        assert 0 < lon_max < 3
        assert 54 < lat_max < 56.5


# ---------------------------------------------------------------------------
# 2. Point-in-region
# ---------------------------------------------------------------------------

class TestPointInRegion:
    @pytest.fixture(scope="class")
    def geom(self):
        return load_region_geometry(str(REGION_PATH))

    def test_brighton_is_inside(self, geom):
        assert geom.contains(Point(-0.14, 50.82))

    def test_manchester_is_inside(self, geom):
        # Manchester — inside England (was outside the old SW-region scope)
        assert geom.contains(Point(-2.24, 53.48))

    def test_edinburgh_is_outside(self, geom):
        assert not geom.contains(Point(-3.19, 55.95))

    def test_dublin_is_outside(self, geom):
        assert not geom.contains(Point(-6.26, 53.35))

    def test_cardiff_is_inside(self, geom):
        # Cardiff — inside the Wales feature
        assert geom.contains(Point(-3.18, 51.48))

    def test_offshore_is_outside(self, geom):
        # Mid-Channel point, south of the coastline
        assert not geom.contains(Point(-1.0, 49.9))

    def test_coastal_station_inside_buffered_polygon(self):
        """The ultra-generalised 500 m coastline can drop near-coast
        stations; the membership buffer must admit them. Newport (IoW) sits
        on an island feature/fringe — regression guard for the buffer."""
        geom_buffered = load_region_geometry(
            str(REGION_PATH), buffer_deg=REGION_CONTAINS_BUFFER_DEG
        )
        assert geom_buffered.contains(Point(-1.29, 50.70))

    def test_buffer_is_positive(self):
        assert REGION_CONTAINS_BUFFER_DEG > 0
        # Guard only against gross errors (under ~20 km).
        assert REGION_CONTAINS_BUFFER_DEG <= 0.2

    def test_bbox_envelope_makes_sense(self, geom):
        lon_min, lat_min, lon_max, lat_max = region_bbox(geom)
        # England+Wales footprint: Land's End/Welsh coast to East Anglia,
        # south coast to the Scottish border
        assert lon_min < -4.0 and lon_max > 1.0
        assert lat_min < 50.5 and lat_max > 54.5


# ---------------------------------------------------------------------------
# 3. Catalogue filtering
# ---------------------------------------------------------------------------

class TestCatalogueFilter:
    def test_filter_to_region_retains_only_inside(self):
        # Mock stations: three inside England+Wales, two outside
        df = pd.DataFrame({
            "station_id":   ["a", "b", "c", "d", "e"],
            "station_name": ["Brighton", "Southampton", "Manchester",
                             "Edinburgh", "Dublin"],
            "lat":          [50.82, 50.91, 53.48, 55.95, 53.35],
            "lon":          [-0.14, -1.40, -2.24, -3.19, -6.26],
            "measure_id":   ["m1", "m2", "m3", "m4", "m5"],
            "measure_type": ["groundwater"] * 5,
        })
        out = filter_to_region(df, str(REGION_PATH))
        kept = set(out["station_id"])
        assert kept == {"a", "b", "c"}, f"Expected {{a, b, c}}, got {kept}"

    def test_filter_empty_input(self):
        df = pd.DataFrame({"station_id": [], "lat": [], "lon": []})
        out = filter_to_region(df, str(REGION_PATH))
        assert len(out) == 0

    def test_filter_all_outside(self):
        df = pd.DataFrame({
            "station_id": ["x", "y"],
            "lat": [55.95, 53.35],
            "lon": [-3.19, -6.26],
        })
        out = filter_to_region(df, str(REGION_PATH))
        assert len(out) == 0

    def test_filter_all_inside(self):
        df = pd.DataFrame({
            "station_id": ["a", "b"],
            "lat": [50.82, 53.48],
            "lon": [-0.14, -2.24],
        })
        out = filter_to_region(df, str(REGION_PATH))
        assert len(out) == 2


# ---------------------------------------------------------------------------
# 4. Map rendering — region layer present in HTML
# ---------------------------------------------------------------------------

class TestMapOverlay:
    def test_load_region_geojson_returns_dict(self):
        gj = _load_region_geojson()
        assert gj is not None
        assert gj["type"] == "FeatureCollection"

    def test_build_map_html_contains_region_layer(self):
        snapshot = pd.DataFrame({
            "station_id":      ["s1"],
            "station_name":    ["Test station"],
            "lat":             [50.85],
            "lon":             [-0.5],
            "risk_raw":        ["LOW"],
            "risk_score":      [0.1],
            "trend":           ["STABLE"],
            "persistence_days": [0],
            "action_category": ["STABLE_LOW"],
            "reason_text":     ["Normal conditions"],
            "suggested_action": ["Continue routine monitoring"],
        })
        m = build_map(snapshot)
        html = m.get_root().render()

        # GeoJSON layer should be embedded as a Leaflet GeoJSON object
        assert "geo_json" in html.lower() or "geojson" in html.lower()
        # The fill / stroke colour we picked should appear
        assert "#1f77b4" in html
        # Legend should mention the configured region
        assert "England region" in html

    def test_build_map_marker_inside_region(self):
        """Sanity: a station inside the region renders without error."""
        snapshot = pd.DataFrame({
            "station_id":      ["s1"],
            "station_name":    ["Brighton"],
            "lat":             [50.82],
            "lon":             [-0.14],
            "risk_raw":        ["HIGH"],
            "risk_score":      [1.8],
            "trend":           ["RISING"],
            "persistence_days": [3],
            "action_category": ["IMMEDIATE_ACTION"],
            "reason_text":     ["Rising trend"],
            "suggested_action": ["Deploy"],
        })
        m = build_map(snapshot)
        # CircleMarker added (children include at least 1 CircleMarker)
        names = [type(c).__name__ for c in m._children.values()]
        assert any("CircleMarker" in n for n in names)

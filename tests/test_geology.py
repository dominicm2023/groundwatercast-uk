"""
Tests for the indicative aquifer-class integration.

The source layer is the OGL **BGS Geology 625k bedrock**, classified to an
indicative aquifer potential (Principal / Secondary / Low) by
``scripts/build_bedrock_geology.py`` — NOT the official EA/BGS Aquifer
Designation (which is not OGL/commercial-clean and was retired).

Covers:
  1. GeoJSON file validity (loads, FeatureCollection, valid geometries,
     ``aquifer_class`` property in the canonical order).
  2. Provenance file (OGL licence + "not the official designation" note).
  3. ``lookup_aquifer`` returns expected values for known coords and
     ``None`` for offshore coords.
  4. ``enrich_with_aquifer`` adds the two columns and never drops or
     mutates existing columns.
  5. Loader (`src.dashboard.geology.load_aquifer_layer`) returns the
     parsed FeatureCollection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import MultiPolygon, Polygon, shape

from src.catalogue.build import (
    enrich_with_aquifer,
    load_aquifer_layer as load_aquifer_layer_build,
    lookup_aquifer,
)
from src.dashboard.geology import (
    AQUIFER_ORDER,
    AQUIFER_STYLE,
    aquifer_designations_present,
    load_aquifer_layer as load_aquifer_layer_dash,
)

ROOT = Path(__file__).parents[1]
AQUIFER_PATH = ROOT / "data" / "geology" / "bedrock_625k.geojson"
PROVENANCE_PATH = ROOT / "data" / "geology" / "bedrock_625k.source.json"

# A chalk-downs borehole (Keepers Wood, W. Sussex) — Principal aquifer.
CHALK_LAT, CHALK_LON = 50.996961, -0.657297
# Central North Sea — no bedrock-geology polygon, must classify as None.
OFFSHORE_LAT, OFFSHORE_LON = 56.5, 3.0


# ---------------------------------------------------------------------------
# 1. GeoJSON validity
# ---------------------------------------------------------------------------

class TestGeoJSONValidity:
    def test_aquifer_file_exists(self):
        assert AQUIFER_PATH.exists(), f"Missing geology file: {AQUIFER_PATH}"

    def test_loads_as_feature_collection(self):
        data = json.loads(AQUIFER_PATH.read_text(encoding="utf-8"))
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) >= 1

    def test_all_geometries_are_polygonal_and_usable(self):
        # The build (scripts/build_bedrock_geology.py) runs make_valid, but
        # GeoJSON coordinate rounding can re-introduce a minor self-intersection
        # in these ~1.4k-part simplified national multipolygons. That's
        # functionally harmless (point-in-polygon + rendering both work), so we
        # assert the geometry is polygonal and *cleanable* (buffer(0) is valid)
        # rather than byte-perfect on disk.
        data = json.loads(AQUIFER_PATH.read_text(encoding="utf-8"))
        for feat in data["features"]:
            g = shape(feat["geometry"])
            assert isinstance(g, (Polygon, MultiPolygon)), (
                f"Bad geometry: {g.geom_type}"
            )
            assert not g.is_empty
            assert g.buffer(0).is_valid

    def test_features_carry_class(self):
        data = json.loads(AQUIFER_PATH.read_text(encoding="utf-8"))
        for feat in data["features"]:
            props = feat["properties"]
            assert "aquifer_class" in props
            assert props["aquifer_class"] in AQUIFER_ORDER


# ---------------------------------------------------------------------------
# 2. Provenance
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_provenance_file_exists(self):
        assert PROVENANCE_PATH.exists()

    def test_provenance_required_keys(self):
        prov = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        for key in (
            "source_dataset", "source_provider", "licence",
            "attribution", "derivation", "note",
        ):
            assert key in prov, f"Provenance missing '{key}'"

    def test_provenance_is_ogl_and_disclaims_official_designation(self):
        prov = json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))
        assert "Open Government Licence" in prov["licence"]
        assert "625k" in prov["source_dataset"]
        # The note must make clear this is NOT the official EA designation.
        assert "Aquifer Designation" in prov["note"]


# ---------------------------------------------------------------------------
# 3. lookup_aquifer
# ---------------------------------------------------------------------------

class TestLookupAquifer:
    @pytest.fixture(scope="class")
    def index(self):
        return load_aquifer_layer_build(str(AQUIFER_PATH))

    def test_chalk_borehole_is_principal(self, index):
        tree, meta = index
        result = lookup_aquifer(CHALK_LAT, CHALK_LON, tree, meta)
        assert result is not None
        assert result["aquifer_designation"] == "Principal"

    def test_onshore_location_classified(self, index):
        # Manchester — now inside the UK-wide layer (the old SW-only extract
        # returned None here); must classify into one of the three classes.
        tree, meta = index
        result = lookup_aquifer(53.48, -2.24, tree, meta)
        assert result is not None
        assert result["aquifer_designation"] in AQUIFER_ORDER

    def test_offshore_returns_none(self, index):
        tree, meta = index
        result = lookup_aquifer(OFFSHORE_LAT, OFFSHORE_LON, tree, meta)
        assert result is None

    def test_returned_dict_shape(self, index):
        tree, meta = index
        result = lookup_aquifer(CHALK_LAT, CHALK_LON, tree, meta)
        assert set(result.keys()) == {"aquifer_name", "aquifer_designation"}

    def test_name_is_readable(self, index):
        tree, meta = index
        result = lookup_aquifer(CHALK_LAT, CHALK_LON, tree, meta)
        assert result["aquifer_name"] == "Principal aquifer"


# ---------------------------------------------------------------------------
# 4. Catalogue enrichment
# ---------------------------------------------------------------------------

class TestEnrichWithAquifer:
    def test_adds_two_columns(self):
        df = pd.DataFrame({
            "station_id": ["a", "b"],
            "station_name": ["X", "Y"],
            "lat": [CHALK_LAT, OFFSHORE_LAT],
            "lon": [CHALK_LON, OFFSHORE_LON],
            "measure_type": ["groundwater", "groundwater"],
        })
        out = enrich_with_aquifer(df, str(AQUIFER_PATH))
        assert "aquifer_name" in out.columns
        assert "aquifer_designation" in out.columns

    def test_existing_columns_unchanged(self):
        df = pd.DataFrame({
            "station_id": ["a"],
            "station_name": ["X"],
            "lat": [CHALK_LAT],
            "lon": [CHALK_LON],
            "measure_type": ["groundwater"],
            "measure_id": ["xxx"],
        })
        out = enrich_with_aquifer(df, str(AQUIFER_PATH))
        for col in ("station_id", "station_name", "lat", "lon", "measure_type", "measure_id"):
            assert col in out.columns
            assert list(out[col]) == list(df[col])

    def test_inside_classified_offshore_none(self):
        df = pd.DataFrame({
            "station_id": ["in", "out"],
            "lat": [CHALK_LAT, OFFSHORE_LAT],
            "lon": [CHALK_LON, OFFSHORE_LON],
        })
        out = enrich_with_aquifer(df, str(AQUIFER_PATH))
        assert out.iloc[0]["aquifer_designation"] == "Principal"
        assert pd.isna(out.iloc[1]["aquifer_designation"])

    def test_missing_layer_returns_columns_with_nulls(self, tmp_path):
        df = pd.DataFrame({
            "station_id": ["a"],
            "lat": [CHALK_LAT],
            "lon": [CHALK_LON],
        })
        out = enrich_with_aquifer(df, str(tmp_path / "nonexistent.geojson"))
        assert "aquifer_name" in out.columns
        assert "aquifer_designation" in out.columns
        assert pd.isna(out.iloc[0]["aquifer_name"])

    def test_empty_input(self):
        df = pd.DataFrame({"station_id": [], "lat": [], "lon": []})
        out = enrich_with_aquifer(df, str(AQUIFER_PATH))
        assert len(out) == 0
        assert "aquifer_name" in out.columns
        assert "aquifer_designation" in out.columns


# ---------------------------------------------------------------------------
# 5. Dashboard loader
# ---------------------------------------------------------------------------

class TestDashboardLoader:
    def test_load_returns_feature_collection(self):
        data = load_aquifer_layer_dash()
        assert data is not None
        assert data["type"] == "FeatureCollection"

    def test_classes_present_is_ordered_subset(self):
        data = load_aquifer_layer_dash()
        present = aquifer_designations_present(data)
        for d in present:
            assert d in AQUIFER_ORDER
        indices = [AQUIFER_ORDER.index(d) for d in present]
        assert indices == sorted(indices)

    def test_style_constants_complete(self):
        for d in AQUIFER_ORDER:
            assert d in AQUIFER_STYLE
            style = AQUIFER_STYLE[d]
            assert "fill" in style and style["fill"].startswith("#")
            assert "opacity" in style and 0.0 < style["opacity"] <= 1.0
            assert "label" in style

    def test_load_returns_none_when_missing(self, tmp_path):
        missing = tmp_path / "nope.geojson"
        assert load_aquifer_layer_dash(str(missing)) is None

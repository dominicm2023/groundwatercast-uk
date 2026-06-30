"""Offline county lookup — point-in-polygon ray casting against a GeoJSON, with
graceful None when the boundary file is absent or the point is outside."""
import json

import scripts.geo_region as geo


def _write(path, features):
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}),
                    encoding="utf-8")


def _square(name, x0, y0, x1, y1):
    ring = [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]   # [lon, lat], closed
    return {"type": "Feature", "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_region_for(tmp_path, monkeypatch):
    f = tmp_path / "counties.geojson"
    _write(f, [_square("Westshire", 0, 0, 10, 10), _square("Eastshire", 10, 0, 20, 10)])
    monkeypatch.setattr(geo, "GEOJSON_PATH", f)
    geo.reset_cache()
    try:
        assert geo.region_for(5, 5) == "Westshire"     # lat 5, lon 5
        assert geo.region_for(5, 15) == "Eastshire"    # lat 5, lon 15
        assert geo.region_for(50, 50) is None          # outside both
        assert geo.region_for(None, 5) is None
        assert geo.region_for(5, "bad") is None
    finally:
        geo.reset_cache()


def test_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(geo, "GEOJSON_PATH", tmp_path / "nope.geojson")
    geo.reset_cache()
    try:
        assert geo.region_for(5, 5) is None            # graceful: no boundary file
    finally:
        geo.reset_cache()


def test_hole(tmp_path, monkeypatch):
    f = tmp_path / "holed.geojson"
    outer = [[0, 0], [20, 0], [20, 20], [0, 20], [0, 0]]
    hole = [[8, 8], [12, 8], [12, 12], [8, 12], [8, 8]]
    feat = {"type": "Feature", "properties": {"name": "Holed"},
            "geometry": {"type": "Polygon", "coordinates": [outer, hole]}}
    _write(f, [feat])
    monkeypatch.setattr(geo, "GEOJSON_PATH", f)
    geo.reset_cache()
    try:
        assert geo.region_for(2, 2) == "Holed"         # inside outer, outside hole
        assert geo.region_for(10, 10) is None          # inside the hole
    finally:
        geo.reset_cache()

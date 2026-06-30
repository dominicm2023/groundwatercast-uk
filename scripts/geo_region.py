"""Offline borehole → English ceremonial county lookup.

The artifact pack has no region/county field (station keys are only aquifer,
aquifer_designation, lat, lon, name, station_id), but the per-borehole pages
want a county in the title / description / Open Graph / JSON-LD / share card.
This resolves one from coordinates by point-in-polygon (ray casting, stdlib-only
— no shapely dependency) against an in-repo boundary file at
``data/geo/english_ceremonial_counties.geojson``.

Graceful by design: if the boundary file is absent, or a point falls outside all
polygons (offshore nudge, boundary gap, missing coords), ``region_for`` returns
None and every downstream region token collapses to empty — stubs ship
region-less rather than blocking the build. Build the index once per process;
callers cache ``station_id -> region`` separately (coords are stable).
"""
from __future__ import annotations

import json
from pathlib import Path

GEOJSON_PATH = (Path(__file__).resolve().parents[1]
                / "data" / "geo" / "english_ceremonial_counties.geojson")

# Candidate property keys for the county name (varies by boundary source —
# ONS uses CTYUA*, OS Boundary-Line ceremonial uses 'NAME' / 'name').
_NAME_KEYS = ("name", "NAME", "CTYUA23NM", "ctyua_name", "county", "REGION", "long_name")

# Cached index: list[(name, list[polygon])]; polygon = list[ring]; ring = list[[lon, lat]].
_index = None


def _name_of(props: dict):
    for k in _NAME_KEYS:
        v = props.get(k)
        if v:
            return str(v)
    return None


def _polys_of(geom: dict):
    t = (geom or {}).get("type")
    c = (geom or {}).get("coordinates")
    if t == "Polygon":
        return [c]
    if t == "MultiPolygon":
        return c
    return []


def _load():
    global _index
    if _index is not None:
        return _index
    _index = []
    if not GEOJSON_PATH.exists():
        return _index
    try:
        gj = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _index
    for feat in gj.get("features", []):
        name = _name_of(feat.get("properties") or {})
        polys = _polys_of(feat.get("geometry") or {})
        if name and polys:
            _index.append((name, polys))
    return _index


def _in_ring(lon, lat, ring) -> bool:
    """Even-odd ray casting; ring = list of [lon, lat]."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _in_polygon(lon, lat, polygon) -> bool:
    """polygon = [outer_ring, hole1, hole2, ...]: inside the outer ring and not in
    any hole."""
    if not polygon or not _in_ring(lon, lat, polygon[0]):
        return False
    for hole in polygon[1:]:
        if _in_ring(lon, lat, hole):
            return False
    return True


def region_for(lat, lon):
    """The English ceremonial county containing (lat, lon), or None."""
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    for name, polys in _load():
        for poly in polys:
            if _in_polygon(lon, lat, poly):
                return name
    return None


def reset_cache():
    """Drop the in-memory index — used by tests after pointing GEOJSON_PATH at a
    fixture."""
    global _index
    _index = None

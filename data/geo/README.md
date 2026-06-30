# `data/geo/` — boundary asset for offline region lookup

`scripts/geo_region.py` resolves each borehole's English **ceremonial county**
from its lat/lon by point-in-polygon, for the Phase-2 per-borehole pages (title /
description / Open Graph / JSON-LD `addressRegion` / share card).

## Needed file (not committed yet)
`english_ceremonial_counties.geojson` — a GeoJSON `FeatureCollection` of England's
ceremonial counties, each feature carrying a county **name** property.

- `geo_region.py` looks for the name in these property keys (first match wins):
  `name`, `NAME`, `CTYUA23NM`, `ctyua_name`, `county`, `REGION`, `long_name`.
  If your source uses a different key, add it to `_NAME_KEYS`.
- WGS84 lon/lat coordinates (standard GeoJSON order `[lon, lat]`).
- `Polygon` and `MultiPolygon` geometries are both handled (incl. holes).

## Sourcing (free, OGL)
- **OS Boundary-Line** (Ordnance Survey OpenData, OGL v3) — "Ceremonial Counties";
  or **ONS Geoportal** boundaries. Both are Open Government Licence, so they sit
  comfortably alongside our other open data.
- Convert to GeoJSON and **simplify** so the repo file stays ~1–2 MB, e.g. with
  [mapshaper](https://mapshaper.org/): `-simplify 5% keep-shapes` then
  `-o format=geojson`. (Coastline precision doesn't matter for a borehole that's
  inland; simplification keeps the build fast and the file small.)

## Graceful degradation
Until this file is present, `region_for()` returns `None` for every borehole and
the pages ship **region-less** (every region token collapses to empty) — the
build is not blocked. Once added, rebuild to populate the county everywhere; the
build logs the count of stations that resolve to no region.

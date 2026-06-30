# `data/geo/` — boundary asset for offline region lookup

`scripts/geo_region.py` resolves each borehole's English **ceremonial county**
from its lat/lon by point-in-polygon, for the Phase-2 per-borehole pages (title /
description / Open Graph / JSON-LD `addressRegion` / share card).

## `english_ceremonial_counties.geojson` (committed)
A simplified `FeatureCollection` of the UK's **73 ceremonial counties**, each
feature carrying a `county` name property. WGS84 `[lon, lat]`; `Polygon` and
`MultiPolygon` (incl. holes) both handled.

- **Size:** ~650 KB (simplified from a ~11 MB source).
- **Coverage:** whole UK (lat ~49.9–60.9, lon ~−10.7–1.8); England boreholes only
  ever match English counties.
- **Resolves correctly:** Wilgate Green → Kent, Devon, Cumbria, North Yorkshire,
  etc.; all 688 local-pack boreholes resolve. A point inside a county "hole"
  (e.g. exactly on the City-of-London boundary) returns `None` and degrades
  cleanly — this matches the source data, not a bug.

## How it was built (reproducible)
1. **Source** — [`evansd/uk-ceremonial-counties`](https://github.com/evansd/uk-ceremonial-counties)
   (`uk-ceremonial-counties.geojson`, derived from OS Boundary-Line ceremonial
   counties, **OGL v3** — sits comfortably with our other open data; county name
   in the `county` property).
2. **Simplify** —
   ```
   curl -sL https://raw.githubusercontent.com/evansd/uk-ceremonial-counties/master/uk-ceremonial-counties.geojson -o uk-cc.geojson
   python scripts/build_counties_geojson.py uk-cc.geojson
   ```
   `scripts/build_counties_geojson.py` applies Douglas-Peucker (~200 m) + 4-dp
   coordinate rounding + minify (stdlib only), writing this file. Re-run only to
   refresh.

## Graceful degradation
If this file is ever missing, `region_for()` returns `None` for every borehole
and the pages ship **region-less** (region tokens collapse to empty) — the build
is never blocked. The stub builder logs the count of stations that resolve to no
region.

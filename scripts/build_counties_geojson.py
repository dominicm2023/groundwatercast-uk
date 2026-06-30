"""One-time build tool: simplify the UK ceremonial-counties GeoJSON to a
repo-friendly size for ``scripts/geo_region.py`` (borehole → county lookup).

Source (OGL v3 / OS Boundary-Line ceremonial counties):
  https://github.com/evansd/uk-ceremonial-counties
  raw: https://raw.githubusercontent.com/evansd/uk-ceremonial-counties/master/uk-ceremonial-counties.geojson
  (~11 MB; the county name is in the ``county`` property).

Usage (download the source first, then):
  python scripts/build_counties_geojson.py path/to/uk-ceremonial-counties.geojson

Writes ``data/geo/english_ceremonial_counties.geojson`` — Douglas-Peucker
simplified (~200 m), 4-dp coordinates (~11 m), ``area`` dropped, minified.
Covers the whole UK (74 counties); England boreholes only ever match English
ones. Stdlib-only; re-run only to refresh the asset.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EPS = 0.002   # Douglas-Peucker tolerance in degrees (~200 m) — fine for county membership
DP = 4        # coordinate decimal places (~11 m)
OUT = (Path(__file__).resolve().parents[1]
       / "data" / "geo" / "english_ceremonial_counties.geojson")


def _dp(points, eps):
    """Iterative Douglas-Peucker on a list of [lon, lat]; keeps the endpoints."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    e2 = eps * eps
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        denom = (dx * dx + dy * dy) or 1e-18
        dmax, idx = 0.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            t = ((px - ax) * dx + (py - ay) * dy) / denom
            cx, cy = ax + t * dx, ay + t * dy
            d = (px - cx) ** 2 + (py - cy) ** 2
            if d > dmax:
                dmax, idx = d, i
        if dmax > e2 and idx != -1:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [points[i] for i in range(n) if keep[i]]


def _ring(ring):
    r = _dp(ring, EPS)
    r = [[round(x, DP), round(y, DP)] for x, y in r]
    out = [r[0]]
    for p in r[1:]:
        if p != out[-1]:
            out.append(p)
    if out[0] != out[-1]:
        out.append(out[0])
    return out if len(out) >= 4 else None


def _poly(poly):
    rings = [_ring(r) for r in poly]
    rings = [r for r in rings if r]
    return rings or None


def _geom(geom):
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Polygon":
        p = _poly(c)
        return {"type": "Polygon", "coordinates": p} if p else None
    if t == "MultiPolygon":
        polys = [_poly(p) for p in c]
        polys = [p for p in polys if p]
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return None


def main(src):
    gj = json.loads(Path(src).read_text(encoding="utf-8"))
    feats = []
    for f in gj.get("features", []):
        name = (f.get("properties") or {}).get("county")
        g = _geom(f.get("geometry") or {})
        if name and g:
            feats.append({"type": "Feature", "properties": {"county": name}, "geometry": g})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                              separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} — {len(feats)} counties, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "uk-ceremonial-counties.geojson")

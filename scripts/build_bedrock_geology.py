"""Build the explorer's national bedrock-geology context layer from BGS 625k.

Source: BGS Geology 625k (DiGMapGB-625) Bedrock — UK-wide, 1:625,000,
**Open Government Licence** (free, commercial use permitted). This replaces
the EA/BGS Aquifer Designation layer, which is NOT commercial-clean
(BGS-licensed ~£0.35/km²) — see docs/free_data_migration.md / the launch
notes.

The BGS layer is rock geology, not the EA aquifer designation, so we derive
an **indicative aquifer-potential** class from the lithology (RCS_D) into
three groups — Principal / Secondary / Low — clearly labelled in the
explorer as lithology-derived, not the official designation. Output:
``data/geology/bedrock_625k.geojson`` (WGS84, property ``aquifer_class``),
dissolved + simplified to a few MB for a faded national context layer.

    python -m scripts.build_bedrock_geology [--tolerance 250] [--src PATH]

Re-run when the BGS GeoPackage is updated or the classification changes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = ROOT / "data/geology_src/625k_V5_Geology_UK_EPSG27700.gpkg"
OUT = ROOT / "data/geology/bedrock_625k.geojson"
BEDROCK_LAYER = "625k_V5_BEDROCK_Geology"

# Indicative aquifer-potential from BGS rock-composition (RCS_D) keywords.
# Crystalline/igneous/metamorphic and argillaceous-dominant = low; chalk and
# clean arenaceous/carbonate-dominant = principal; interbedded/secondary
# elsewhere. A first-order hydrogeological approximation — refine the keyword
# lists as needed.
_CRYSTALLINE = ("IGNEOUS", "GNEISS", "FELSIC", "MAFIC", "LAVA", "TUFF",
                "GRANITE", "BASALT", "GABBRO", "DIORITE", "RHYOLITE", "ANDESITE",
                "PSAMMITE", "PELITE", "SCHIST", "SLATE", "PHYLLITE", "WACKE",
                "MYLONIT", "QUARTZITE", "CALCSILICATE", "HORNFELS", "MIGMATITE")
_AQUIFER_ROCK = ("SANDSTONE", "LIMESTONE", "METALIMESTONE", "DOLOSTONE",
                 "DOLOMITE", "CHALK", "SAND", "GRAVEL", "CONGLOMERATE")


def classify(rcs_d: str | None) -> str:
    s = (rcs_d or "").upper()
    if any(k in s for k in _CRYSTALLINE):
        return "Low"
    if "CHALK" in s:
        return "Principal"
    lead = s.split(",")[0].split(" AND ")[0].strip()
    if (lead.startswith("SANDSTONE") or lead.startswith("LIMESTONE")
            or lead.startswith("METALIMESTONE") or lead.startswith("DOLO")
            or lead in ("SAND", "GRAVEL")):
        return "Principal"
    has_aquifer = any(k in s for k in _AQUIFER_ROCK)
    if (lead.startswith("MUDSTONE") or lead.startswith("CLAY")
            or lead.startswith("SILTSTONE") or "ARGILLACEOUS" in lead):
        return "Secondary" if has_aquifer else "Low"
    return "Secondary" if has_aquifer else "Low"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--tolerance", type=float, default=250.0,
                    help="simplify tolerance in metres (BNG) before reprojection")
    args = ap.parse_args(argv)

    import geopandas as gpd

    src = Path(args.src)
    if not src.exists():
        print(f"BGS GeoPackage not found at {src}")
        return 1
    print(f"reading {src.name} ({BEDROCK_LAYER}) ...")
    gdf = gpd.read_file(src, layer=BEDROCK_LAYER)
    print(f"  {len(gdf)} bedrock polygons, CRS {gdf.crs.to_epsg()}")

    gdf["aquifer_class"] = gdf["RCS_D"].map(classify)
    print("  class polygon counts:", gdf["aquifer_class"].value_counts().to_dict())

    # dissolve to one (multi)polygon per class, simplify in metres, reproject
    print(f"dissolving + simplifying (tolerance {args.tolerance} m) ...")
    diss = gdf.dissolve(by="aquifer_class", as_index=False)[["aquifer_class", "geometry"]]
    diss["geometry"] = diss.geometry.simplify(args.tolerance, preserve_topology=True)
    diss = diss.to_crs(4326)
    # Topology cleanup in the FINAL CRS: simplify (and the reprojection's
    # coordinate-precision changes near coastlines) can leave self-intersections
    # in the dissolved multipolygons (invalid geometry → GEOS warnings + brittle
    # point-in-polygon / rendering). make_valid restores OGC validity; keep only
    # the polygonal parts in case it emits a mixed GeometryCollection.
    from shapely import get_parts
    from shapely.geometry import MultiPolygon as _MP, Polygon as _P

    def _polygonal(geom):
        valid = geom if geom.is_valid else geom.buffer(0)
        if valid.is_valid and valid.geom_type in ("Polygon", "MultiPolygon"):
            return valid
        polys = [g for g in get_parts(valid) if isinstance(g, (_P, _MP))]
        return _MP([p for g in polys for p in get_parts(g)]) if polys else valid

    diss["geometry"] = diss.geometry.make_valid().apply(_polygonal)
    n_invalid = int((~diss.geometry.is_valid).sum())
    print(f"  invalid geometries after make_valid: {n_invalid}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    diss.to_file(OUT, driver="GeoJSON")
    size_mb = OUT.stat().st_size / 1e6
    print(f"wrote {OUT.relative_to(ROOT)}  ({size_mb:.1f} MB)")
    for _, r in diss.iterrows():
        print(f"  {r['aquifer_class']:<12} geom parts: {len(getattr(r.geometry, 'geoms', [r.geometry]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

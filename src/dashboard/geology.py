"""
Aquifer / hydrogeology data loader for the dashboard.

Single responsibility: load the precomputed aquifer GeoJSON from disk
once per Streamlit process and return it as a Python dict for Folium to
consume.

Spatial joins happen at catalogue build time (see
``src.catalogue.build.enrich_with_aquifer``); this module is read-only.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parents[2]
# OGL BGS Geology 625k bedrock, classified to an indicative aquifer potential
# (Principal / Secondary / Low) — NOT the official EA/BGS Aquifer Designation,
# which is not OGL/commercial-clean and was retired from this product.
AQUIFER_GEOJSON_PATH = _ROOT / "data" / "geology" / "bedrock_625k.geojson"

# Colour scheme — keep in sync with map_builder._make_legend() and the
# explorer's web/config.js geologyColors.
AQUIFER_STYLE: dict[str, dict] = {
    "Principal": {"fill": "#4c9f8a", "opacity": 0.25, "label": "Principal aquifer"},
    "Secondary": {"fill": "#9ec79b", "opacity": 0.25, "label": "Secondary aquifer"},
    "Low":       {"fill": "#d7d2c4", "opacity": 0.20, "label": "Low productivity"},
}

# Canonical render order — Principal underneath so Secondary / Low don't
# visually obscure water-bearing strata.
AQUIFER_ORDER: list[str] = ["Principal", "Secondary", "Low"]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def load_aquifer_layer(path: str | None = None) -> dict | None:
    """
    Load the aquifer GeoJSON once per process (LRU-cached).

    Returns the parsed FeatureCollection dict, or ``None`` if the file is
    missing — callers must handle the missing case gracefully so the
    dashboard still renders when the geology layer hasn't been generated.

    The ``path`` argument is accepted for testability; in normal use the
    default constant ``AQUIFER_GEOJSON_PATH`` is correct and lets the
    LRU cache do its job (one entry per process).
    """
    p = Path(path) if path else AQUIFER_GEOJSON_PATH
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def aquifer_designations_present(geojson: dict | None) -> list[str]:
    """
    Return the distinct ``aquifer_class`` values present in the loaded
    layer, in canonical render order.  Useful for building legends that
    only mention classes actually present.
    """
    if not geojson:
        return []
    seen: set[str] = set()
    for feat in geojson.get("features", []):
        props = feat.get("properties") or {}
        des = props.get("aquifer_class") or props.get("aquifer_designation")
        if des:
            seen.add(des)
    return [d for d in AQUIFER_ORDER if d in seen]

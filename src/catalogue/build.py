"""
Station catalogue builder.

Downloads EA Hydrology stations within the configured region's bounding box,
expands measures, classifies by type, applies a precise point-in-polygon
filter to the region boundary, and writes data/processed/catalogue.csv.

Region boundary is taken from ``config.region.geojson_path``
(default: ``data/regions/england_wales.geojson``).

Usage:
    python -m src.catalogue.build
"""

import json
import re
import sys
import warnings
from pathlib import Path

import pandas as pd
import requests
from shapely.geometry import Point, shape
from shapely.ops import unary_union
from shapely.strtree import STRtree

from src.utils.io_encoding import force_utf8_stdio


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parents[2] / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_stations(url: str, limit: int = 10000) -> pd.DataFrame:
    """Download stations.csv from EA Hydrology API and return as DataFrame."""
    from io import StringIO
    response = requests.get(url, params={"_limit": limit}, timeout=60)
    response.raise_for_status()
    return pd.read_csv(StringIO(response.text))


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

_REQUIRED_COLUMNS = {
    "station_name": ["label", "name"],
    "lat":          ["lat", "latitude"],
    "lon":          ["long", "longitude"],
    "measures_raw": ["measures", "measure"],
}

_OPTIONAL_COLUMNS = {
    "measures_period_raw":    ["measures.period"],
    "measures_statistic_raw": ["measures.valueStatistic"],
}


def _pick_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def derive_station_id(row: pd.Series) -> str:
    """Derive a non-null station ID from available fields.

    Priority: stationGuid → notation → last segment of @id URI.
    """
    for field in ("stationGuid", "notation"):
        val = row.get(field)
        if pd.notna(val) and str(val).strip():
            return str(val).strip()
    at_id = row.get("@id")
    if pd.notna(at_id) and str(at_id).strip():
        return str(at_id).rstrip("/").split("/")[-1]
    return ""


def parse_stations(df: pd.DataFrame) -> pd.DataFrame:
    """Extract and normalise required fields from raw stations DataFrame."""
    # Required columns — raise if absent
    mapping = {}
    for target, candidates in _REQUIRED_COLUMNS.items():
        found = _pick_column(df, candidates)
        if found is None:
            raise ValueError(
                f"Could not find column for '{target}'. "
                f"Available columns: {list(df.columns)}"
            )
        mapping[found] = target

    # Optional metadata columns — fill with None if absent
    optional_mapping = {}
    for target, candidates in _OPTIONAL_COLUMNS.items():
        found = _pick_column(df, candidates)
        if found:
            optional_mapping[found] = target

    keep_cols = list(mapping.keys()) + list(optional_mapping.keys())
    # Keep id-source columns for derive_station_id
    for id_col in ("stationGuid", "notation", "@id"):
        if id_col in df.columns and id_col not in keep_cols:
            keep_cols.append(id_col)

    parsed = df[keep_cols].rename(columns={**mapping, **optional_mapping}).copy()

    # Derive station_id
    parsed["station_id"] = parsed.apply(derive_station_id, axis=1)

    # Drop id-source columns now that station_id is built
    parsed = parsed.drop(
        columns=[c for c in ("stationGuid", "notation", "@id") if c in parsed.columns],
        errors="ignore",
    )

    # Fill absent optional columns
    for col in ("measures_period_raw", "measures_statistic_raw"):
        if col not in parsed.columns:
            parsed[col] = None

    parsed = parsed.dropna(subset=["lat", "lon", "measures_raw"])
    parsed["lat"] = pd.to_numeric(parsed["lat"], errors="coerce")
    parsed["lon"] = pd.to_numeric(parsed["lon"], errors="coerce")
    parsed = parsed.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    # Validate: station_id must be non-null and non-empty for every row
    bad = (parsed["station_id"].isna()) | (parsed["station_id"] == "")
    if bad.any():
        raise ValueError(
            f"{bad.sum()} rows have no derivable station_id. "
            "Check stationGuid, notation, and @id columns."
        )

    return parsed


# ---------------------------------------------------------------------------
# Expand measures
# ---------------------------------------------------------------------------

def _split_pipe(val) -> list[str]:
    """Split a pipe-delimited string, stripping whitespace. Returns [] for null."""
    if pd.isna(val) or str(val).strip() == "":
        return []
    return [s.strip() for s in str(val).split("|") if s.strip()]


# Recognised numeric period tokens encoded in EA measure_id slugs.
# These match the EA Hydrology API's period values (seconds) and are
# explicit enough to be safe against accidental matches elsewhere in the slug.
_KNOWN_PERIODS: tuple[int, ...] = (
    900,     # 15 min  — typical continuous logger
    1800,    # 30 min
    3600,    # 1 h
    86400,   # daily   — typical rainfall totals
    604800,  # weekly
)

# Pattern: one of the known period values surrounded by hyphens
_PERIOD_TOKEN_RE = re.compile(
    r"-(" + "|".join(str(p) for p in _KNOWN_PERIODS) + r")-"
)


def parse_period_from_measure_id(measure_id: str) -> int | None:
    """
    Infer the sampling period (in seconds) from an EA measure_id slug.

    The EA CSV endpoint reports ``measures.period`` as a station-level
    scalar that cannot be reliably aligned back to individual measures
    when a station has more than one.  The period is, however, always
    encoded in the measure_id itself — this function recovers it.

    Resolution order
    ----------------
    1. Explicit numeric token: a known period flanked by hyphens
       (e.g. ``"-900-"``, ``"-86400-"``).
    2. ``"subdaily"`` keyword (groundwater logged loggers): treated as 900 s.
       The EA JSON API confirms these always report ``period=900``.
    3. ``None`` otherwise — typical for ``gw-dipped-i`` (irregular manual
       readings) and any other measure type without a period in its slug.
    """
    if not measure_id:
        return None
    s = str(measure_id).lower()

    m = _PERIOD_TOKEN_RE.search(s)
    if m:
        return int(m.group(1))

    # Continuous logger groundwater measures: '...-gw-logged-i-subdaily-...'
    if "subdaily" in s:
        return 900

    return None


def expand_measures(df: pd.DataFrame) -> pd.DataFrame:
    """Explode measures in parallel with period and valueStatistic.

    Each output row = one station + one measure.

    ``measure_period`` is derived authoritatively from the measure_id
    slug (see ``parse_period_from_measure_id``).  The EA CSV endpoint's
    ``measures.period`` column is unreliable: it returns a station-level
    scalar that cannot be aligned to individual measures when the station
    has more than one, which previously caused continuous loggers to be
    silently dropped at the linking stage.

    ``measure_value_statistic`` is still extracted from the pipe-delimited
    CSV column when it has the right length; length mismatches fall back
    to ``None`` rather than silently misaligning.
    """
    rows = []
    for _, station in df.iterrows():
        measures   = _split_pipe(station["measures_raw"])
        statistics = _split_pipe(station.get("measures_statistic_raw"))

        if not measures:
            continue

        n = len(measures)
        if statistics and len(statistics) != n:
            statistics = [None] * n
        statistics = statistics or [None] * n

        for measure_uri, statistic in zip(measures, statistics):
            measure_id = measure_uri.rstrip("/").split("/")[-1]
            stat_label = (
                statistic.rstrip("/").split("/")[-1] if statistic else None
            )
            rows.append({
                **{k: station[k] for k in station.index
                   if k not in ("measures_raw", "measures_period_raw",
                                "measures_statistic_raw")},
                "measure_id":              measure_id,
                "measure_period":          parse_period_from_measure_id(measure_id),
                "measure_value_statistic": stat_label,
            })

    return pd.DataFrame(rows).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------

def classify_measure(measure_id: str, mode: str = "fast") -> str:
    """Classify a measure_id string into a measure type.

    mode="fast"   — substring matching on measure_id (default)
    mode="strict" — metadata fetch per measure (not yet implemented)
    """
    if mode == "strict":
        raise NotImplementedError(
            "Strict classification (unitName metadata fetch) is not yet implemented. "
            "Set classification_mode to 'fast' in config/config.json."
        )

    m = measure_id.lower()

    # groundwater: mAOD unit only — do not match on keyword "gw" or "groundwater"
    if "maod" in m:
        return "groundwater"

    if "rainfall" in m or "-mm-" in m or "precip" in m:
        return "rainfall"

    if "m3" in m or "flow" in m:
        return "river_flow"

    # river_level: explicit keywords only — no bare "-m-" to avoid false matches
    if "level" in m or "stage" in m or "wstage" in m or "river" in m:
        return "river_level"

    return "unknown"


def validate_classification(df: pd.DataFrame) -> None:
    """Warn or raise based on the proportion of unknown classifications.

    <1%  : silent
    1-5% : warnings.warn
    >5%  : raises ValueError
    """
    total = len(df)
    if total == 0:
        return
    n_unknown = (df["measure_type"] == "unknown").sum()
    pct = n_unknown / total * 100

    if pct > 5:
        raise ValueError(
            f"{pct:.1f}% of measures could not be classified ({n_unknown}/{total}). "
            "Review measure_id patterns or switch to classification_mode 'strict'."
        )
    if pct >= 1:
        warnings.warn(
            f"{pct:.1f}% of measures unclassified ({n_unknown}/{total}). "
            "These rows will be dropped.",
            UserWarning,
            stacklevel=2,
        )


def classify_measures(df: pd.DataFrame, mode: str = "fast") -> pd.DataFrame:
    """Add measure_type column, validate unknown rate, then drop unknowns."""
    df = df.copy()
    df["measure_type"] = df["measure_id"].apply(classify_measure, mode=mode)
    validate_classification(df)
    return df[df["measure_type"] != "unknown"].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Pre-filter: remove non-hydrology measures
# ---------------------------------------------------------------------------

_KEEP_PATTERNS = ["maod", "mm", "m3", "flow", "level", "stage"]
_DROP_PATTERNS = ["ph", "temp", "amm", "cond", "oxygen", "do-", "turb", "cphyll"]


def filter_relevant_measures(df: pd.DataFrame) -> pd.DataFrame:
    """Drop non-hydrology measures (water quality etc.) before classification."""
    m = df["measure_id"].str.lower()
    keep = m.str.contains("|".join(_KEEP_PATTERNS), regex=True)
    drop = m.str.contains("|".join(_DROP_PATTERNS), regex=True)
    return df[keep & ~drop].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Spatial filter
# ---------------------------------------------------------------------------

# Buffer applied to the region polygon when testing whether a station
# falls "inside" the configured region. A generous buffer (~15 km, the
# value below) pulls in stations just outside the polygon edge: aquifers
# don't respect administrative boundaries, so a borehole just over the
# line can still be the best driver for a location inside the region.
# Candidate linking still applies its own distance caps, so the extra
# stations only matter when they're geographically and hydrogeologically
# relevant.
#
# The dashboard overlay continues to use the un-buffered geometry — only
# the catalogue's membership test uses this buffer.
REGION_CONTAINS_BUFFER_DEG: float = 0.15


def load_region_geometry(geojson_path: str, buffer_deg: float = 0.0):
    """
    Load a region GeoJSON and return a single (Multi)Polygon.

    Handles both single-feature and multi-feature collections; multiple
    features are unioned into one geometry.  Caller can use the result
    directly with shapely ``contains`` / ``within``.

    Parameters
    ----------
    geojson_path : Path to the region GeoJSON.
    buffer_deg   : Optional positive buffer applied to the unioned geometry
                   in WGS84 degrees.  Use ``REGION_CONTAINS_BUFFER_DEG`` for
                   membership-test calls; leave at 0.0 (the default) for
                   bbox derivation and the dashboard overlay so the visible
                   region remains exactly as published.
    """
    with open(geojson_path) as f:
        geojson = json.load(f)

    polygons = [shape(feat["geometry"]) for feat in geojson["features"]]
    if not polygons:
        raise ValueError(f"No features in region GeoJSON: {geojson_path}")
    merged = unary_union(polygons)
    if buffer_deg > 0:
        merged = merged.buffer(buffer_deg)
    return merged


def region_bbox(geometry) -> tuple[float, float, float, float]:
    """
    Return the (lon_min, lat_min, lon_max, lat_max) envelope of a geometry.
    Used to derive a bounding box for the upstream EA API query.
    """
    minx, miny, maxx, maxy = geometry.bounds
    return (minx, miny, maxx, maxy)


def filter_to_boundary(df: pd.DataFrame, geojson_path: str) -> pd.DataFrame:
    """Keep only stations whose point falls within the boundary polygon."""
    geom = load_region_geometry(
        geojson_path, buffer_deg=REGION_CONTAINS_BUFFER_DEG
    )

    def _inside(lon: float, lat: float) -> bool:
        return geom.contains(Point(lon, lat))

    mask = [_inside(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]
    return df[mask].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Aquifer enrichment (build-time spatial join)
# ---------------------------------------------------------------------------

# Readable per-borehole labels for the indicative aquifer classes carried by
# the OGL BGS 625k layer (``aquifer_class``). "Low aquifer" reads oddly, so the
# low-productivity class gets a descriptive name instead.
_AQUIFER_CLASS_NAME = {
    "Principal": "Principal aquifer",
    "Secondary": "Secondary aquifer",
    "Low": "Low productivity",
}


def load_aquifer_layer(geojson_path: str) -> tuple[STRtree, list[dict]]:
    """
    Load the aquifer GeoJSON and build a spatial index for fast lookups.

    Returns
    -------
    tree     : ``shapely.strtree.STRtree`` over the polygon geometries.
    metadata : List of dicts, one per polygon, with keys
               ``"geom"``, ``"aquifer_name"``, ``"aquifer_designation"``.
               Ordered to match the indices returned by ``tree.query``.

    The layer is the OGL BGS Geology 625k bedrock, classified to an
    indicative aquifer potential per feature in the ``aquifer_class``
    property (``Principal`` / ``Secondary`` / ``Low``). Legacy property
    names (``aquifer_designation`` / ``typology`` / ``TYPOLOGY``) are
    accepted as fallbacks so the loader still reads older layers. The
    per-borehole ``aquifer_designation`` column therefore now holds the
    *indicative* class — NOT the official EA/BGS Aquifer Designation
    (which is not OGL/commercial-clean and was retired from this product).
    """
    with open(geojson_path, encoding="utf-8") as f:
        gj = json.load(f)

    metadata: list[dict] = []
    geoms: list = []
    for feat in gj.get("features", []):
        geom = shape(feat["geometry"])
        props = feat.get("properties") or {}
        designation = (
            props.get("aquifer_class")
            or props.get("aquifer_designation")
            or props.get("typology")
            or props.get("TYPOLOGY")
        )
        name = (
            props.get("aquifer_name")
            or _AQUIFER_CLASS_NAME.get(designation)
            or (f"{designation} aquifer" if designation else None)
        )
        if designation is None:
            # Skip features with no class — they cannot be classified.
            continue
        metadata.append({
            "geom": geom,
            "aquifer_name": name,
            "aquifer_designation": designation,
        })
        geoms.append(geom)

    tree = STRtree(geoms)
    return tree, metadata


def lookup_aquifer(
    lat: float,
    lon: float,
    tree: STRtree,
    metadata: list[dict],
) -> dict | None:
    """
    Return ``{"aquifer_name": ..., "aquifer_designation": ...}`` for the
    polygon containing ``(lat, lon)``, or ``None`` if no polygon contains
    the point.

    The STRtree prunes candidates by bounding box (~10x faster than a
    linear scan); the exact ``contains`` test is then applied to each
    candidate, in input order, so deterministic precedence is preserved
    if polygons happen to overlap.
    """
    pt = Point(lon, lat)
    # tree.query returns positional indices in shapely 2.x
    candidate_idx = tree.query(pt)
    for idx in candidate_idx:
        idx = int(idx)
        rec = metadata[idx]
        if rec["geom"].contains(pt):
            return {
                "aquifer_name": rec["aquifer_name"],
                "aquifer_designation": rec["aquifer_designation"],
            }
    return None


def enrich_with_aquifer(
    df: pd.DataFrame,
    aquifer_path: str,
) -> pd.DataFrame:
    """
    Append ``aquifer_name`` and ``aquifer_designation`` columns to every
    row of ``df`` using a precomputed STRtree spatial join.

    Stations outside every polygon get ``None`` for both columns
    (typically a few coastal / outlier boreholes).
    """
    if not Path(aquifer_path).exists():
        # Missing layer is non-fatal: keep the columns so downstream
        # consumers always see a stable schema.
        df = df.copy()
        df["aquifer_name"] = None
        df["aquifer_designation"] = None
        return df

    tree, metadata = load_aquifer_layer(aquifer_path)
    names: list[str | None] = []
    designations: list[str | None] = []
    for lat, lon in zip(df["lat"], df["lon"]):
        hit = lookup_aquifer(lat, lon, tree, metadata)
        if hit is None:
            names.append(None)
            designations.append(None)
        else:
            names.append(hit["aquifer_name"])
            designations.append(hit["aquifer_designation"])

    out = df.copy()
    out["aquifer_name"] = names
    out["aquifer_designation"] = designations
    return out


def filter_to_region(
    df: pd.DataFrame,
    geojson_path: str,
) -> pd.DataFrame:
    """
    Precise point-in-polygon filter against the configured region boundary.

    Two-stage for efficiency:
      1. Coarse bbox filter (uses the un-buffered polygon's envelope).
      2. Exact shapely ``contains`` against the polygon, slightly buffered
         (``REGION_CONTAINS_BUFFER_DEG``) so simplification-induced edge
         losses don't drop legitimate boundary boreholes.
    """
    # Bbox uses the un-buffered geometry — buffer would just enlarge the box
    geom_raw = load_region_geometry(geojson_path)
    lon_min, lat_min, lon_max, lat_max = region_bbox(geom_raw)

    # Buffered geom for the precise contains test
    geom_buf = geom_raw.buffer(REGION_CONTAINS_BUFFER_DEG)

    # Stage 1: bbox prefilter (slightly widened to match the buffer)
    bbox_mask = (
        (df["lon"] >= lon_min - REGION_CONTAINS_BUFFER_DEG)
        & (df["lon"] <= lon_max + REGION_CONTAINS_BUFFER_DEG)
        & (df["lat"] >= lat_min - REGION_CONTAINS_BUFFER_DEG)
        & (df["lat"] <= lat_max + REGION_CONTAINS_BUFFER_DEG)
    )
    coarse = df[bbox_mask]

    # Stage 2: exact polygon test
    inside = [
        geom_buf.contains(Point(lon, lat))
        for lon, lat in zip(coarse["lon"], coarse["lat"])
    ]
    return coarse[inside].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_catalogue(config: dict) -> pd.DataFrame:
    stations_url = config["api"]["stations_url"]
    output_path = Path(__file__).parents[2] / config["catalogue"]["output_path"]

    # Region boundary (mandatory): drives both bbox and precise filter.
    region_cfg = config.get("region")
    if not region_cfg or "geojson_path" not in region_cfg:
        raise ValueError(
            "config.region.geojson_path is required. "
            "Add a 'region' block to config/config.json pointing at the region GeoJSON."
        )
    region_path = Path(__file__).parents[2] / region_cfg["geojson_path"]
    region_name = region_cfg.get("name", "region")
    region_geom = load_region_geometry(str(region_path))
    lon_min, lat_min, lon_max, lat_max = region_bbox(region_geom)
    print(
        f"Region: {region_name}  ·  bbox=({lon_min:.3f},{lat_min:.3f},"
        f"{lon_max:.3f},{lat_max:.3f})  ·  geom={region_geom.geom_type}"
    )

    limit = config["api"].get("stations_limit", 10000)

    print("Fetching stations...")
    raw = fetch_stations(stations_url, limit=limit)
    print(f"  {len(raw)} stations downloaded")

    parsed = parse_stations(raw)
    print(f"  {len(parsed)} stations after parsing")

    # Coarse bbox prefilter — derived dynamically from the region polygon
    in_bbox = (
        (parsed["lon"] >= lon_min) & (parsed["lon"] <= lon_max)
        & (parsed["lat"] >= lat_min) & (parsed["lat"] <= lat_max)
    )
    parsed = parsed[in_bbox].reset_index(drop=True)
    print(f"  {len(parsed)} stations after region bbox filter")

    expanded = expand_measures(parsed)
    print(f"  {len(expanded)} rows after expanding measures")

    relevant = filter_relevant_measures(expanded)
    print(f"  {len(relevant)} rows after pre-filtering (non-hydrology dropped)")

    mode = config["catalogue"].get("classification_mode", "fast")
    classified = classify_measures(relevant, mode=mode)
    print(f"  {len(classified)} rows after classifying (unknowns dropped)")

    # Precise point-in-polygon filter against the region boundary
    filtered = filter_to_region(classified, str(region_path))
    print(f"  {len(filtered)} rows after filtering to {region_name} polygon")

    # Aquifer enrichment (build-time spatial join using STRtree) against the
    # OGL BGS 625k bedrock layer — indicative aquifer class, commercial-clean.
    aquifer_path = Path(__file__).parents[2] / "data" / "geology" / "bedrock_625k.geojson"
    if aquifer_path.exists():
        print(f"Enriching with indicative aquifer class from {aquifer_path.name}...")
        filtered = enrich_with_aquifer(filtered, str(aquifer_path))
        n_classified = int(filtered["aquifer_designation"].notna().sum())
        n_total = len(filtered)
        print(
            f"  {n_classified}/{n_total} rows classified into an aquifer "
            f"({100 * n_classified / max(n_total, 1):.1f}%)"
        )
    else:
        print(f"  Skipping aquifer enrichment — {aquifer_path} not found")
        filtered["aquifer_name"] = None
        filtered["aquifer_designation"] = None

    final_cols = [
        "station_id", "station_name", "lat", "lon",
        "measure_id", "measure_type",
        "measure_period", "measure_value_statistic",
        "aquifer_name", "aquifer_designation",
    ]
    catalogue = filtered[final_cols].reset_index(drop=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    catalogue.to_csv(output_path, index=False)
    print(f"\nCatalogue written to {output_path}")
    print(catalogue["measure_type"].value_counts().to_string())

    summary = {
        "region": region_name,
        "region_bbox": [lon_min, lat_min, lon_max, lat_max],
        "total_rows": len(catalogue),
        "by_measure_type": catalogue["measure_type"].value_counts().to_dict(),
        "lat_min": catalogue["lat"].min(),
        "lat_max": catalogue["lat"].max(),
        "lon_min": catalogue["lon"].min(),
        "lon_max": catalogue["lon"].max(),
    }
    summary_path = Path(__file__).parents[2] / "outputs" / "catalogue_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Summary written to {summary_path}")

    return catalogue


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    force_utf8_stdio()
    config = load_config()
    try:
        build_catalogue(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

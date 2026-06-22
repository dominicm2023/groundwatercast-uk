"""
Boundary debug tool.

Runs the catalogue pipeline up to (but not including) spatial filtering,
then reports coordinate bounds, inside/outside counts by measure type,
and saves outputs/debug_station_bbox.csv.

Usage:
    python -m src.catalogue.debug_boundary
"""

import json
import sys
from pathlib import Path

from shapely.geometry import Point, shape

from src.catalogue.build import (
    classify_measures,
    expand_measures,
    fetch_stations,
    filter_relevant_measures,
    load_config,
    parse_stations,
)


def load_boundary_polygons(geojson_path: str):
    with open(geojson_path) as f:
        geojson = json.load(f)
    return [shape(feature["geometry"]) for feature in geojson["features"]]


def tag_inside_boundary(df, polygons) -> list[bool]:
    result = []
    for lon, lat in zip(df["lon"], df["lat"]):
        pt = Point(lon, lat)
        result.append(any(pt.within(poly) for poly in polygons))
    return result


def run_debug(config: dict) -> None:
    import pandas as pd

    stations_url = config["api"]["stations_url"]
    boundary_path = Path(__file__).parents[2] / config["catalogue"]["boundary_path"]
    output_path = Path(__file__).parents[2] / "outputs" / "debug_station_bbox.csv"
    mode = config["catalogue"].get("classification_mode", "fast")

    limit = config["api"].get("stations_limit", 10000)

    print("Fetching stations...")
    raw = fetch_stations(stations_url, limit=limit)
    parsed = parse_stations(raw)
    expanded = expand_measures(parsed)
    relevant = filter_relevant_measures(expanded)
    print(f"  {len(relevant)} rows after pre-filtering")
    classified = classify_measures(relevant, mode=mode)
    print(f"  {len(classified)} classified rows (unknowns dropped)\n")

    # --- Coordinate bounds ---
    print("Coordinate bounds (all classified stations):")
    print(f"  Lat range : {classified['lat'].min():.4f} -> {classified['lat'].max():.4f}")
    print(f"  Lon range : {classified['lon'].min():.4f} -> {classified['lon'].max():.4f}\n")

    # --- Hampshire polygon bounds ---
    polygons = load_boundary_polygons(str(boundary_path))
    bounds = [poly.bounds for poly in polygons]
    min_lon = min(b[0] for b in bounds)
    min_lat = min(b[1] for b in bounds)
    max_lon = max(b[2] for b in bounds)
    max_lat = max(b[3] for b in bounds)
    print("Hampshire polygon bounds:")
    print(f"  Lat range : {min_lat:.4f} -> {max_lat:.4f}")
    print(f"  Lon range : {min_lon:.4f} -> {max_lon:.4f}\n")

    # --- Tag inside/outside ---
    classified = classified.copy()
    classified["inside_boundary"] = tag_inside_boundary(classified, polygons)

    # --- Summary table ---
    summary = (
        classified.groupby(["measure_type", "inside_boundary"])
        .size()
        .unstack(fill_value=0)
        .rename(columns={True: "inside", False: "outside"})
    )
    if "inside" not in summary.columns:
        summary["inside"] = 0
    if "outside" not in summary.columns:
        summary["outside"] = 0
    summary = summary[["inside", "outside"]]
    summary["total"] = summary["inside"] + summary["outside"]

    print("Stations inside vs outside Hampshire boundary:")
    print(summary.to_string())
    print(f"\nTotal inside : {summary['inside'].sum()}")
    print(f"Total outside: {summary['outside'].sum()}\n")

    # --- Save CSV ---
    out_cols = ["station_id", "station_name", "lat", "lon", "measure_type", "inside_boundary"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    classified[out_cols].to_csv(output_path, index=False)
    print(f"Debug CSV written to {output_path}")


if __name__ == "__main__":
    config = load_config()
    try:
        run_debug(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

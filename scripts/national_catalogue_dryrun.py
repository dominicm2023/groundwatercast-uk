"""
National catalogue dry-run.

Demonstrates that the existing catalogue machinery (src.catalogue.build)
scales to national England+Wales coverage, WITHOUT changing any default
behaviour. Reuses the download / parse / expand / classify / polygon-filter
functions from src.catalogue.build unchanged.

This script is strictly read-only with respect to the repo's data:
- it never touches data/processed/catalogue.csv or config/config.json;
- it writes NOTHING under data/ — output is stdout plus an optional
  ``--out`` text file which must live under outputs/ (gitignored area).

Usage:
    python -m scripts.national_catalogue_dryrun
    python -m scripts.national_catalogue_dryrun --no-polygon
    python -m scripts.national_catalogue_dryrun --boundary data/regions/england_wales.geojson --limit 20000
    python -m scripts.national_catalogue_dryrun --out outputs/national_dryrun.txt
"""

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
from shapely.geometry import Point, shape

from src.catalogue.build import (
    classify_measure,
    expand_measures,
    fetch_stations,
    filter_relevant_measures,
    filter_to_region,
    load_config,
    parse_stations,
    REGION_CONTAINS_BUFFER_DEG,
)
from src.utils.io_encoding import force_utf8_stdio

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOUNDARY = REPO_ROOT / "data" / "regions" / "england_wales.geojson"

# Display order for the summary table
_MEASURE_TYPES = ["groundwater", "rainfall", "river_level", "river_flow"]

# Nation label for stations not inside any named nation polygon. With the
# polygon filter on, these are stations admitted only via the +0.15 deg
# membership buffer (Scottish-border fringe, near-offshore); in --no-polygon
# mode it is everything outside England+Wales (e.g. any Scottish stations
# the API happens to return).
OUTSIDE_LABEL = "outside/fringe"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run the station catalogue machinery at national "
                    "(England+Wales) scale. Read-only: writes nothing under data/."
    )
    parser.add_argument(
        "--boundary",
        type=Path,
        default=DEFAULT_BOUNDARY,
        help=f"Boundary GeoJSON for the polygon filter and the nation split "
             f"(default: {DEFAULT_BOUNDARY.relative_to(REPO_ROOT)}).",
    )
    parser.add_argument(
        "--no-polygon",
        action="store_true",
        help="National mode: skip the polygon membership filter entirely. "
             "The boundary is still used (read-only) for the nation split.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20000,
        help="_limit for the EA stations endpoint (default 20000 — high "
             "enough for national; the configured catalogue default is 10000).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path for a copy of the summary text. Must be under "
             "outputs/ — this script never writes under data/.",
    )
    args = parser.parse_args(argv)
    # Relative boundary paths resolve against the repo root (matching how
    # src.catalogue.build treats config paths), not the caller's CWD.
    if not args.boundary.is_absolute():
        args.boundary = REPO_ROOT / args.boundary
    return args


def validate_out_path(out: Path) -> Path:
    """Resolve --out and refuse anything outside outputs/."""
    resolved = out if out.is_absolute() else (REPO_ROOT / out)
    resolved = resolved.resolve()
    outputs_root = (REPO_ROOT / "outputs").resolve()
    if not resolved.is_relative_to(outputs_root):
        raise ValueError(
            f"--out must be under {outputs_root} (got {resolved}). "
            "This dry-run never writes under data/."
        )
    return resolved


# ---------------------------------------------------------------------------
# Nation split
# ---------------------------------------------------------------------------

def load_nation_geometries(boundary_path: Path) -> dict[str, object]:
    """
    Per-feature (un-buffered) geometries keyed by feature ``properties.name``.

    The england_wales.geojson keeps England and Wales as separate features
    precisely so this split is a real point-in-polygon test rather than a
    crude lon/lat box. Returns {} when features carry no usable name (the
    caller then skips the nation split and says so).
    """
    with open(boundary_path) as f:
        gj = json.load(f)

    geoms: dict[str, object] = {}
    for feat in gj.get("features", []):
        props = feat.get("properties") or {}
        name = props.get("name") or props.get("CTRY24NM")
        if not name:
            continue
        geom = shape(feat["geometry"])
        if name in geoms:
            geom = geoms[name].union(geom)
        geoms[name] = geom
    return geoms


def assign_nation(lon: float, lat: float, nation_geoms: dict[str, object]) -> str:
    """First nation polygon (un-buffered) containing the point, else OUTSIDE_LABEL."""
    pt = Point(lon, lat)
    for name, geom in nation_geoms.items():
        if geom.contains(pt):
            return name
    return OUTSIDE_LABEL


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary_table(df: pd.DataFrame, nation_geoms: dict[str, object]) -> str:
    """Counts by measure type (measure rows + unique stations) x nation."""
    # One nation lookup per unique station, then broadcast to measure rows
    stations = df.drop_duplicates("station_id")[["station_id", "lon", "lat"]]
    if nation_geoms:
        nation_by_station = {
            row.station_id: assign_nation(row.lon, row.lat, nation_geoms)
            for row in stations.itertuples()
        }
    else:
        nation_by_station = {sid: "n/a" for sid in stations["station_id"]}
    df = df.copy()
    df["nation"] = df["station_id"].map(nation_by_station)

    nations = list(nation_geoms.keys()) + [OUTSIDE_LABEL] if nation_geoms else ["n/a"]
    header = (
        f"{'measure_type':<14}{'measures':>10}{'stations':>10}"
        + "".join(f"{('stn ' + n[:10]):>16}" for n in nations)
    )
    lines = [header, "-" * len(header)]
    for mtype in _MEASURE_TYPES + sorted(
        set(df["measure_type"]) - set(_MEASURE_TYPES)
    ):
        sub = df[df["measure_type"] == mtype]
        if sub.empty:
            continue
        stn = sub.drop_duplicates("station_id")
        row = f"{mtype:<14}{len(sub):>10}{len(stn):>10}"
        for n in nations:
            row += f"{(stn['nation'] == n).sum():>16}"
        lines.append(row)
    all_stn = df.drop_duplicates("station_id")
    total = f"{'TOTAL':<14}{len(df):>10}{len(all_stn):>10}"
    for n in nations:
        total += f"{(all_stn['nation'] == n).sum():>16}"
    lines.append("-" * len(header))
    lines.append(total)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> str:
    out_path = validate_out_path(args.out) if args.out else None

    config = load_config()  # read-only: only api.stations_url is used
    stations_url = config["api"]["stations_url"]

    report: list[str] = []

    def emit(line: str = "") -> None:
        print(line)
        report.append(line)

    emit("National catalogue dry-run — England+Wales spike")
    emit("=" * 60)
    emit(f"API:       {stations_url}  (_limit={args.limit})")
    emit(f"Boundary:  {args.boundary}"
         + ("  [polygon filter SKIPPED — --no-polygon]" if args.no_polygon else ""))
    emit("")

    t0 = time.perf_counter()
    raw = fetch_stations(stations_url, limit=args.limit)
    t_fetch = time.perf_counter() - t0
    limit_hit = len(raw) >= args.limit
    emit(f"Downloaded {len(raw)} stations in {t_fetch:.1f} s"
         + ("  ⚠ row count == _limit: response may be TRUNCATED — raise "
            "--limit or page the API" if limit_hit else
            "  (below _limit — no paging needed)"))

    parsed = parse_stations(raw)
    emit(f"Parsed:    {len(parsed)} stations with coords + measures")

    expanded = expand_measures(parsed)
    emit(f"Expanded:  {len(expanded)} measure rows")

    relevant = filter_relevant_measures(expanded)
    emit(f"Relevant:  {len(relevant)} rows after hydrology pre-filter")

    # Classification. We reuse classify_measure per-row instead of the
    # classify_measures wrapper: its validate gate RAISES above 5% unknown,
    # which would turn a national-coverage finding into a crash. A dry-run
    # should report the unknown rate, not die on it.
    relevant = relevant.copy()
    relevant["measure_type"] = relevant["measure_id"].apply(
        classify_measure, mode=config["catalogue"].get("classification_mode", "fast")
    )
    n_unknown = int((relevant["measure_type"] == "unknown").sum())
    pct_unknown = 100 * n_unknown / max(len(relevant), 1)
    classified = relevant[relevant["measure_type"] != "unknown"].reset_index(drop=True)
    emit(f"Classified: {len(classified)} rows "
         f"({n_unknown} unknown = {pct_unknown:.1f}%, dropped"
         + ("; exceeds the 5% gate src.catalogue.build enforces — "
            "investigate before productionising" if pct_unknown > 5 else "")
         + ")")

    if args.no_polygon:
        final = classified
        emit("Polygon:   skipped (--no-polygon) — counting everything the API returned")
    else:
        final = filter_to_region(classified, str(args.boundary))
        n_stn_in = final["station_id"].nunique()
        emit(f"Polygon:   {len(final)} rows / {n_stn_in} stations inside boundary "
             f"(+{REGION_CONTAINS_BUFFER_DEG} deg membership buffer)")

    nation_geoms = load_nation_geometries(args.boundary)
    emit("")
    if nation_geoms:
        emit(f"Nation split: point-in-polygon against per-feature geometries "
             f"({', '.join(nation_geoms)}); '{OUTSIDE_LABEL}' = not inside any "
             "un-buffered nation polygon")
    else:
        emit("Nation split: UNAVAILABLE — boundary features carry no name "
             "property; counts below are boundary-wide only")
    emit("")
    emit(build_summary_table(final, nation_geoms))
    emit("")
    emit(f"Total runtime: {time.perf_counter() - t0:.1f} s")
    emit("Dry-run only — nothing written under data/, catalogue.csv untouched.")

    text = "\n".join(report)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(f"\nSummary copy written to {out_path}")
    return text


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        run(parse_args())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

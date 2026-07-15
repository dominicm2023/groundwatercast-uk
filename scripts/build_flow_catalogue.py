"""
Stage 1 of the low-flow Rivers layer build
(``docs/product/lowflow/build_plan.md``): gauge catalogue + links.

Builds, end-to-end against the live EA API:
  data/processed/flow_catalogue.csv — one row per open EA flow gauge with a
    daily-mean qualified measure (name, lat/lon, river name, catchment,
    record start).
  data/processed/flow_links.csv     — each gauge's 3 nearest EA rainfall
    gauges (RainMeasureID_1..3 / RainDist_1..3), same columns/semantics as
    the borehole station_links.csv, built by reusing
    src.linking.build.nearest_n unchanged.

See src/catalogue/flow.py for the reusable, fixture-testable functions this
script orchestrates.

Usage:
    python -m scripts.build_flow_catalogue
    python -m scripts.build_flow_catalogue --limit 200   # quick smoke test
"""

import argparse
import sys
import time

from src.catalogue.flow import (
    build_flow_catalogue,
    build_flow_links_from_config,
    load_config,
)
from src.utils.io_encoding import force_utf8_stdio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build data/processed/flow_catalogue.csv and "
                    "flow_links.csv from the live EA Hydrology API."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Override flow_catalogue.stations_limit (EA API _limit param) "
             "for a quick smoke test.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    config = load_config()
    if args.limit is not None:
        config = dict(config)
        config["flow_catalogue"] = {
            **(config.get("flow_catalogue") or {}),
            "stations_limit": args.limit,
        }

    t0 = time.perf_counter()
    catalogue = build_flow_catalogue(config)
    t_catalogue = time.perf_counter() - t0
    print(f"\nflow_catalogue.csv: {len(catalogue)} gauges in {t_catalogue:.1f}s")

    t1 = time.perf_counter()
    links = build_flow_links_from_config(config, catalogue)
    t_links = time.perf_counter() - t1
    print(f"flow_links.csv: {len(links)} gauges linked in {t_links:.1f}s")

    print(f"\nTotal runtime: {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        run(parse_args())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

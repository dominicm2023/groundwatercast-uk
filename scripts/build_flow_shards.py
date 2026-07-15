"""Flow-gauge ingest + daily shard top-up — Stage 2 of the low-flow Rivers
layer build (``docs/product/lowflow/build_plan.md``).

For every gauge in ``flow_links.csv``: ensure its raw EA readings archive is
current (first download, or a tail top-up — ``src.download.build``'s HTTP
primitives, reused unchanged) and build/extend its per-gauge Parquet shard
under ``data/features/flow_by_station/`` (see ``src/download/flow.py`` for
the shard writer). Idempotent and safe to run daily: the first run per gauge
does the full historical download + shard build; every later run only fetches
and appends the new tail.

Meant to run daily, before the forecast refresh (wired into
``scripts/run_chain.py``'s "ingest"/"freshness" groups):

    python -m scripts.build_flow_shards
    python -m scripts.run_chain --freshness --forecast --publish

Usage:
    python -m scripts.build_flow_shards
    python -m scripts.build_flow_shards --limit 50   # pilot-scope smoke test
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from src.download.build import load_config
from src.download.flow import FLOW_LINKS_PATH, load_flow_measure_map, refresh_flow_gauge
from src.utils.io_encoding import force_utf8_stdio


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--limit", type=int, default=None,
        help="only process the first N gauges, sorted by gauge_id "
             "(pilot-scope smoke test; the real pilot list comes from the "
             "Stage-5 fleet scan)",
    )
    ap.add_argument(
        "--links", default=str(FLOW_LINKS_PATH),
        help="path to flow_links.csv (default: %(default)s)",
    )
    return ap.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    config = load_config()
    links_path = Path(args.links)
    if not links_path.exists():
        # GRACEFUL SKIP, exit 0 — this stage runs inside run_chain's freshness
        # group, and run_chain aborts the WHOLE chain (forecast + publish
        # included) on any non-zero stage exit. flow_links.csv is a gitignored
        # local build artefact, so a host that hasn't run the Stage-1 flow
        # catalogue (e.g. the production VPS today) must skip flow ingest,
        # not kill the groundwater forecast chain.
        print(f"{links_path} not found — flow ingest skipped (run "
              f"'python -m scripts.build_flow_catalogue' to enable it on this host).")
        return 0

    measure_map = load_flow_measure_map(links_path)
    gauge_ids = sorted(measure_map)
    if args.limit is not None:
        gauge_ids = gauge_ids[: args.limit]

    print(f"Flow shard refresh: {len(gauge_ids)} gauge(s)"
          + (f" (--limit {args.limit})" if args.limit is not None else ""))

    dl_tally: Counter = Counter()
    shard_tally: Counter = Counter()
    n_rows = 0
    failed: list[str] = []

    for i, gauge_id in enumerate(gauge_ids, 1):
        result = refresh_flow_gauge(gauge_id, measure_map[gauge_id], config)
        dl_tally[result["download"]] += 1
        shard_tally[result["shard"]] += 1
        n_rows += result["n_rows"]
        if result["shard"] == "error" or result["download"] == "failed":
            failed.append(gauge_id)
        if i % 25 == 0 or i == len(gauge_ids):
            print(f"  {i}/{len(gauge_ids)}  download={dict(dl_tally)}  "
                  f"shard={dict(shard_tally)}  (+{n_rows} daily rows)")

    print(f"\nDone. download={dict(dl_tally)}  shard={dict(shard_tally)}  "
          f"— {n_rows} daily rows written/appended")
    if failed:
        shown = failed[:10]
        more = "..." if len(failed) > 10 else ""
        print(f"  WARNING: {len(failed)} gauge(s) failed: {shown}{more}")

    return 0


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        sys.exit(run(parse_args()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

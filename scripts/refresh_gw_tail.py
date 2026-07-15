"""Incrementally top up the raw GW readings tail (freshness refresh).

``src.download.build.download_measure`` only ever CREATES raw CSVs — it skips any
that already exist — so between full rebuilds the ingested copy drifts months
stale while the EA Hydrology archive stays ~weeks fresh. This tops up every
already-downloaded measure's tail to today, so the downstream observed series
(v15 dipped-daily → per-station parquet → gw_freshness → pack status) can be
rebuilt current.

Meant to run daily BEFORE the observed rebuild + publish, e.g.:
  python -m scripts.refresh_gw_tail
  python -m scripts.run_chain --freshness --forecast --publish

The first run is the catch-up (large tails, year-chunked as needed); subsequent
daily runs fetch only the ~week of new data since the archive last advanced.
"""
from __future__ import annotations

import argparse
import sys

from src.download.build import load_config, refresh_tails


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--types", default="groundwater",
                    help="comma-separated measure types to top up "
                         "(groundwater[,rainfall]); default groundwater")
    ap.add_argument("--overlap-days", type=int, default=14,
                    help="re-fetch this many days before each CSV's last date, "
                         "to absorb late-arriving / revised readings (default 14)")
    args = ap.parse_args()

    config = load_config()
    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    tally = refresh_tails(config, types=types, overlap_days=args.overlap_days)
    print(f"\nDone. {tally}")
    # 'advanced' + 'current' are healthy; 'absent' just means not yet fully
    # downloaded; only 'failed' is a problem — surface it without failing the run.
    if tally.get("failed"):
        print(f"  ⚠ {tally['failed']} measures failed to top up (transient — "
              f"the next run retries).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

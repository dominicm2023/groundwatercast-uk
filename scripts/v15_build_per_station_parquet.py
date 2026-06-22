"""Build per-station Parquet files from the joined GW timeseries.

Why: load_gw_for_bh() previously read the full 1.1M-row CSV and filtered
to one station. Per-station Parquet collapses that to a ~5-10 ms file
read, ~20× faster on cold paths.

Output: data/features/gw_by_station/<bh_id>.parquet  (one per station)
        data/features/gw_by_station/_MANIFEST.json   (build metadata)

Granularity: DAILY mean. All call-sites consume daily; sub-daily fidelity
is preserved in the source CSV (canonical) and used by the separate GW
forecast pipeline if needed.

Compression: snappy (default; optimised for read speed).

Idempotency: if the manifest's `source_mtime` matches the joined CSV's
mtime, the script skips rebuild and exits cleanly.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.utils.io_encoding import force_utf8_stdio

SRC = Path("data/features/joined_timeseries.csv")
OUT_DIR = Path("data/features/gw_by_station")
MANIFEST = OUT_DIR / "_MANIFEST.json"


def main(force: bool = False) -> None:
    force_utf8_stdio()
    if not SRC.exists():
        print(f"Source not found: {SRC}")
        return

    src_mtime = SRC.stat().st_mtime
    if MANIFEST.exists() and not force:
        try:
            m = json.loads(MANIFEST.read_text())
            if m.get("source_mtime") == src_mtime:
                print(f"Up-to-date — manifest matches source_mtime "
                      f"({datetime.fromtimestamp(src_mtime).isoformat()}). "
                      f"Pass force=True to rebuild.")
                return
        except (json.JSONDecodeError, OSError):
            pass

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # The shards also carry live flood-monitoring rows appended hourly by
    # v16_refresh_live_gw (data_source="logged_live") that are NOT in the
    # joined CSV — a rebuild drops them. They repopulate from the next hourly
    # v16 run, but only within its ~7-day fetch window.
    if MANIFEST.exists():
        print("NOTE: rebuilding from the joined CSV drops live-tail rows newer "
              "than it; the next hourly v16_refresh_live_gw run restores the "
              "last ~7 days.")
    print(f"Reading {SRC} ({SRC.stat().st_size / 1024 / 1024:.1f} MB)...")
    t0 = time.time()
    df = pd.read_csv(SRC, parse_dates=["dateTime"])
    print(f"  loaded {len(df):,} rows in {time.time()-t0:.1f}s")

    # Ensure columns we know about exist (older CSV variants may lack
    # is_interpolated / data_source — fill them in).
    if "is_interpolated" not in df.columns:
        df["is_interpolated"] = 0
    if "data_source" not in df.columns:
        df["data_source"] = "logged"

    # Aggregate to daily mean per station (every consumer uses daily).
    df["date"] = df["dateTime"].dt.tz_convert("UTC").dt.normalize()
    daily = (df.dropna(subset=["GW_Level"])
             .groupby(["station_id", "date"], as_index=False)
             .agg(GW_Level=("GW_Level", "mean"),
                  is_interpolated=("is_interpolated", "max"),
                  data_source=("data_source", "first")))
    # Store `date` as tz-naive datetime64[ns] (UTC midnight already from
    # the groupby key). Loader code expects this shape directly — no
    # per-call tz arithmetic needed.
    if daily["date"].dt.tz is not None:
        daily["date"] = daily["date"].dt.tz_convert("UTC").dt.tz_localize(None)

    n_stations = daily["station_id"].nunique()
    print(f"  daily-aggregated to {len(daily):,} rows for {n_stations} stations")

    # Write one Parquet file per station
    t0 = time.time()
    written = 0
    for sid, grp in daily.groupby("station_id", sort=False):
        fp = OUT_DIR / f"{sid}.parquet"
        out = grp[["date", "GW_Level", "is_interpolated", "data_source"]]
        out.to_parquet(fp, compression="snappy", index=False)
        written += 1
        if written % 100 == 0:
            print(f"  wrote {written}/{n_stations}...")
    elapsed = time.time() - t0
    print(f"  wrote {written} Parquet files in {elapsed:.1f}s")

    total_bytes = sum(f.stat().st_size for f in OUT_DIR.glob("*.parquet"))
    MANIFEST.write_text(json.dumps({
        "source": str(SRC),
        "source_mtime": src_mtime,
        "source_mtime_iso": datetime.fromtimestamp(src_mtime).isoformat(),
        "built_at": datetime.now().isoformat(),
        "n_stations": int(n_stations),
        "n_rows": int(len(daily)),
        "n_files": written,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1024 / 1024, 2),
        "compression": "snappy",
        "granularity": "daily",
    }, indent=2))
    print(f"Wrote manifest: {MANIFEST}")
    print(f"Total size: {total_bytes / 1024 / 1024:.1f} MB across {written} files")


if __name__ == "__main__":
    import sys
    main(force="--force" in sys.argv)

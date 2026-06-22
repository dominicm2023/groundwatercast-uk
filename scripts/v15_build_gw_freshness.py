"""Compute per-BH freshness from the joined timeseries.

For each station we record the most recent REAL (non-interpolated) reading
date, the age in days, the data source (logged / dipped_interp), and a
categorical freshness label.

Output: data/processed/gw_freshness.csv
"""
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.utils.io_encoding import force_utf8_stdio

JOINED = Path("data/features/joined_timeseries.csv")
OUT = Path("data/processed/gw_freshness.csv")

# Universal thresholds. Same scale for logged + dipped — but expectations
# differ (a "Recent" logged station already means telemetry issue; a "Recent"
# dipped station is normal). The UI surfaces the data_source so both can be
# read in context.
THRESHOLDS = [
    ("fresh",       0,    7),
    ("recent",      8,    30),
    ("stale",       31,   90),
    ("very_stale",  91,   365),
    ("no_data",     366,  10_000_000),
]


def label_for(age_days: float) -> str:
    for lab, lo, hi in THRESHOLDS:
        if lo <= age_days <= hi:
            return lab
    return "no_data"


def main():
    """Build per-BH freshness, reading from the per-station Parquet shards.

    Reading from the shards (not the canonical CSV) means v16's live
    refresh — which appends recent readings into shards but doesn't
    touch the CSV — is correctly reflected in the freshness labels.
    Falls back to scanning the joined CSV when the shard directory is
    absent (development environments without v1.5+ infrastructure).
    """
    force_utf8_stdio()
    shard_dir = Path("data/features/gw_by_station")
    if shard_dir.exists():
        print(f"Loading from per-station Parquet shards ({shard_dir})...")
        rows = []
        for fp in shard_dir.glob("*.parquet"):
            sid = fp.stem
            df = pd.read_parquet(fp, columns=["date", "is_interpolated",
                                              "data_source"])
            real = df[df["is_interpolated"] == 0]
            if real.empty:
                continue
            rows.append({
                "station_id":        sid,
                "last_real_reading": real["date"].max(),
                "data_source":       real["data_source"].iloc[-1],
            })
        latest = pd.DataFrame(rows)
    else:
        print("Per-station Parquet missing — falling back to joined CSV.")
        cols = ["dateTime", "GW_Level", "station_id"]
        header = pd.read_csv(JOINED, nrows=0).columns
        if "is_interpolated" in header:
            cols.append("is_interpolated")
        if "data_source" in header:
            cols.append("data_source")
        df = pd.read_csv(JOINED, parse_dates=["dateTime"], usecols=cols)
        if "is_interpolated" not in df.columns:
            df["is_interpolated"] = 0
        if "data_source" not in df.columns:
            df["data_source"] = "logged"
        real = df[(df["is_interpolated"] == 0) & df["GW_Level"].notna()].copy()
        real["date"] = real["dateTime"].dt.tz_convert("UTC").dt.normalize()
        latest = (real.sort_values("date")
                  .groupby("station_id", as_index=False)
                  .agg(last_real_reading=("date", "max"),
                       data_source=("data_source", "first")))

    # Normalise last_real_reading to a tz-naive Timestamp before computing age
    latest["last_real_reading"] = pd.to_datetime(latest["last_real_reading"])
    if latest["last_real_reading"].dt.tz is not None:
        latest["last_real_reading"] = latest["last_real_reading"].dt.tz_convert("UTC").dt.tz_localize(None)
    today = pd.Timestamp.now().normalize()
    latest["days_since"] = (today - latest["last_real_reading"]).dt.days
    latest["freshness_label"] = latest["days_since"].apply(label_for)
    latest["last_real_reading"] = latest["last_real_reading"].dt.strftime("%Y-%m-%d")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    latest.to_csv(OUT, index=False)

    print(f"Wrote {OUT} ({len(latest)} stations)")
    print()
    print("Freshness breakdown:")
    print(latest["freshness_label"].value_counts().to_string())
    print()
    print("Logged vs dipped:")
    print(latest.groupby(["data_source", "freshness_label"]).size().unstack(fill_value=0).to_string())


if __name__ == "__main__":
    main()

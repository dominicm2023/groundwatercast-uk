"""Append the topped-up raw GW archive tail into the per-station Parquet shards.

The sibling of ``v16_refresh_live_gw``, for the *audited* EA Hydrology archive:
``refresh_gw_tail`` keeps the raw CSVs current, but nothing propagated that to
the shards the pack reads — rebuilding the joined series from logged raw is the
full features build (``src.features.build``, ~40 min), far too heavy for a daily
cron. This does the O(new data) version: for each station whose raw archive now
extends past its shard's last audited reading, resample just that tail to daily
means (mirroring the features build), screen it against the station's own
history (the same median ± fence·IQR rule), and append it to the shard.

Merge hierarchy (the inverse of v16's, same principle — audited is canonical):
  - archive rows REPLACE live rows (``logged_live*``) on the same date: the
    audited value supersedes the sensor-grade one;
  - archive rows never clobber existing non-live rows (belt-and-braces — the
    tail starts strictly after the last audited date, so overlap is live-only);
  - rows are labelled ``logged`` or ``dipped`` from the measure id, so the
    freshness semantics (v15_build_gw_freshness) stay honest.

Cheap-skip: each raw CSV's last data line is read via a tail seek (no full
parse) and compared against the shard — on a normal daily run most stations
skip after ~1 KB of I/O. Top-up-written CSVs are date-sorted (topup_measure
sorts on merge), so the last line is the max; an unparseable tail falls back
to the safe full read.

Side effects (mirrors v16): touched shards atomically replaced;
``gw_freshness.csv`` rebuilt at the end.

Run daily AFTER ``refresh_gw_tail`` (the run_chain "freshness" group wires
this): raw tail top-up → this → the pack build reads fresh shards.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from src.download.build import last_datetime_from_tail
from src.utils.io_encoding import force_utf8_stdio

LINKS = Path("data/processed/station_links.csv")
RAW_DIR = Path("data/raw/groundwater")
PARQUET_DIR = Path("data/features/gw_by_station")
FRESHNESS_CSV = Path("data/processed/gw_freshness.csv")

# Live (sensor-grade) sources the audited archive supersedes — mirrors v16.
LIVE_SOURCES = {"logged_live", "logged_live_stuck"}
# Same outlier screen the features build applies (median ± fence·IQR), here
# computed from the station's own shard history rather than the raw series.
IQR_FENCE = 20.0
SHARD_COLS = ["date", "GW_Level", "is_interpolated", "data_source"]


def _source_label(measure_id: str) -> str:
    """Label archive rows by measure kind so freshness reads them correctly."""
    return "dipped" if "-dipped-" in measure_id else "logged"


def raw_tail_date(path: Path) -> pd.Timestamp | None:
    """The raw CSV's final reading as a tz-naive UTC DATE (shard-date grain),
    via src.download.build.last_datetime_from_tail — no full parse. None when
    unparseable (caller then falls back to the safe full read)."""
    ts = last_datetime_from_tail(path)
    return None if ts is None else ts.tz_localize(None).normalize()


def _daily_tail(raw_path: Path, after: pd.Timestamp | None,
                source: str) -> pd.DataFrame:
    """Read the raw CSV and return daily-mean rows for dates strictly after
    ``after`` (all dates when ``after`` is None), in the shard schema. Mirrors
    the features build: dateTime → UTC → naive date, daily mean of value."""
    df = pd.read_csv(raw_path, usecols=["dateTime", "value"], low_memory=False)
    dt = pd.to_datetime(df["dateTime"], utc=True, errors="coerce")
    df = pd.DataFrame({
        "date": dt.dt.tz_localize(None).dt.normalize(),
        "value": pd.to_numeric(df["value"], errors="coerce"),
    }).dropna()
    if after is not None:
        df = df[df["date"] > after]
    if df.empty:
        return pd.DataFrame(columns=SHARD_COLS)
    daily = (df.groupby("date", as_index=False)["value"].mean()
             .rename(columns={"value": "GW_Level"}))
    daily["is_interpolated"] = 0
    daily["data_source"] = source
    return daily[SHARD_COLS]


def _iqr_screen(new: pd.DataFrame, hist: pd.Series) -> tuple[pd.DataFrame, int]:
    """Drop tail values outside the station's own median ± IQR_FENCE·IQR
    (the features build's clean_groundwater_series rule, from shard history).
    Zero IQR (flat station) → no screen, matching the features build."""
    v = pd.to_numeric(hist, errors="coerce").dropna()
    if len(v) < 4:
        return new, 0
    q1, q3 = float(v.quantile(0.25)), float(v.quantile(0.75))
    iqr = q3 - q1
    if iqr == 0.0:
        return new, 0
    med = float(v.median())
    ok = new["GW_Level"].between(med - IQR_FENCE * iqr, med + IQR_FENCE * iqr)
    return new[ok], int((~ok).sum())


def update_one_shard(station_id: str, measure_id: str) -> tuple[str, int]:
    """Append the archive tail for one station. Returns (status, n_added):
    'advanced', 'current', 'no_shard', 'no_raw', or 'failed'."""
    fp = PARQUET_DIR / f"{station_id}.parquet"
    raw = RAW_DIR / f"{measure_id}.csv"
    if not fp.exists():
        return "no_shard", 0
    if not raw.exists():
        return "no_raw", 0

    existing = pd.read_parquet(fp)
    for col, default in [("is_interpolated", 0), ("data_source", "logged")]:
        if col not in existing.columns:
            existing[col] = default
    audited = existing[~existing["data_source"].isin(LIVE_SOURCES)]
    last_audited = audited["date"].max() if not audited.empty else None

    # Cheap-skip: tail-line date vs the shard. A parse failure (None) falls
    # through to the full read — never skips on uncertainty.
    hint = raw_tail_date(raw)
    if (hint is not None and last_audited is not None
            and pd.notna(last_audited) and hint <= last_audited):
        return "current", 0

    new = _daily_tail(raw, last_audited if pd.notna(last_audited) else None,
                      _source_label(measure_id))
    if new.empty:
        return "current", 0
    new, n_outliers = _iqr_screen(new, existing["GW_Level"])
    if n_outliers:
        print(f"  QC {station_id[:8]}: {n_outliers} tail value(s) outside "
              f"median±{IQR_FENCE:.0f}·IQR — dropped")
    if new.empty:
        return "current", 0

    # Belt-and-braces: never clobber an existing non-live row (by construction
    # the tail is beyond last_audited, so any overlap is live-only) …
    nonlive_dates = set(audited["date"])
    new = new[~new["date"].isin(nonlive_dates)]
    if new.empty:
        return "current", 0
    # … and audited REPLACES live rows on the dates it now covers.
    covered = set(new["date"])
    keep = existing[~(existing["data_source"].isin(LIVE_SOURCES)
                      & existing["date"].isin(covered))]
    merged = (pd.concat([keep, new], ignore_index=True)
              .sort_values("date").reset_index(drop=True))

    tmp = fp.with_suffix(fp.suffix + ".tmp")   # atomic swap, mirrors v16
    merged.to_parquet(tmp, compression="snappy", index=False)
    os.replace(tmp, fp)
    return "advanced", len(new)


def main() -> int:
    force_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", help="single station_id (testing)")
    ap.add_argument("--no-freshness", action="store_true",
                    help="skip the gw_freshness.csv rebuild (testing)")
    args = ap.parse_args()

    if not LINKS.exists():
        print(f"ERROR: {LINKS} not found"); return 2
    links = (pd.read_csv(LINKS, usecols=["GWStationID", "GWMeasureID"])
             .dropna().drop_duplicates("GWStationID"))
    if args.station:
        links = links[links["GWStationID"] == args.station]
    print(f"Shard tail refresh: {len(links)} stations")

    from collections import Counter
    tally: Counter = Counter()
    n_rows = 0
    for i, r in enumerate(links.itertuples(index=False), 1):
        try:
            status, n = update_one_shard(r.GWStationID, r.GWMeasureID)
        except Exception as exc:
            status, n = "failed", 0
            print(f"  ! {r.GWStationID[:8]}: {type(exc).__name__}: {exc}")
        tally[status] += 1
        n_rows += n
        if i % 200 == 0 or i == len(links):
            print(f"  {i}/{len(links)}  {dict(tally)}  (+{n_rows} daily rows)")

    print(f"\nDone. {dict(tally)}  — {n_rows} new daily rows appended")

    if not args.no_freshness and tally.get("advanced"):
        print("\nRefreshing data/processed/gw_freshness.csv …")
        import subprocess
        rc = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "scripts.v15_build_gw_freshness"],
            check=False).returncode
        if rc != 0:
            print(f"  freshness build returned exit code {rc}")
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

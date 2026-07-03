"""v1.9 — extend the raw rainfall record's tail with live flood-monitoring data.

Uses src.forecast.live_levels.fetch_live_rainfall (return_raw=True, so the raw
payload can be kept) to pull recent EA
flood-monitoring readings for every rainfall gauge that has a match in
rainfall_monitoring_xref.csv (102 of 113 used gauges as of the v1.9
build), sums them to daily totals, and merges them into the existing raw
rainfall CSVs at data/raw/rainfall/<rain_measure_id>.csv. Each gauge is
bridged from its own archive tail (so the live data is contiguous with the
archive) up to the flood-monitoring readings-retention ceiling (~28 days).

Why
---
The hydrology archive that feeds `Rainfall` (and thus `Recharge_Weibull`)
lags weeks to months. The flood-monitoring feed for the same tipping-bucket
gauges is ~15-min fresh. Extending the raw tail closes the 0-2 day
staleness in the recharge signal. Uplift is modest by design — chalk
responds slowly and the 45-day Weibull kernel gives the most-recent days
the least weight — but it removes the "flat tail" at the right-hand edge
of the rainfall record.

Conventions / known limitations
--------------------------------
- **Rain-day offset**: the archive labels daily totals on the EA "09:00
  rain-day" (dateTime stamped 09:00). Live totals here are summed over the
  UTC calendar day (00:00-00:00). The ~9 h attribution difference is
  negligible under the 45-day kernel and never causes double-counting
  because the live window (7 days) is always newer than the archive's
  tail. Revisit if/when the Stage D recharge-recompute lands.
- **Partial days**: the oldest day in the fetch window is dropped (it
  starts mid-day at the `since` boundary). Today's total is kept but is
  necessarily partial until midnight UTC; hourly re-runs overwrite it
  (live wins) so it converges to the final daily total after the day ends.
- **Superseded by full rebuilds**: `src.download.build` re-downloads and
  overwrites these raw CSVs from the hydrology archive. That wipes the
  live tail (same lifecycle as the v15 dipped-data gotcha). Re-run this
  script after any full pipeline rebuild to re-extend the tail. It is
  idempotent and self-healing.

Cadence: hourly via cron / Task Scheduler. Running twice within the same
15-min window is a near-no-op (only today's partial total nudges).

Raw API payloads are stored under data/raw/live_rainfall/<YYYY-MM-DD>/ for
audit (one file per gauge per UTC day; hourly re-runs overwrite —
best-effort, a failed audit write never aborts the refresh).

Run with:
    python -m scripts.v19_refresh_live_rainfall
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from src.forecast.live_levels import fetch_live_rainfall
from src.utils.io_encoding import force_utf8_stdio

XREF = Path("data/processed/rainfall_monitoring_xref.csv")
RAINFALL_DIR = Path("data/raw/rainfall")
RAW_AUDIT_DIR = Path("data/raw/live_rainfall")

# The flood-monitoring /readings?since= endpoint retains roughly the last
# month. We bridge from each gauge's archive tail up to this ceiling so the
# live tail stays *contiguous* with the archive (no mid-series hole that
# would understate the Weibull convolution). When the archive lag exceeds
# this, a hole remains until the next full pipeline rebuild — unavoidable
# without re-downloading the hydrology archive.
MAX_BRIDGE_DAYS = 28


def _persist_raw_payload(payload: dict | None, rain_measure_id: str) -> None:
    """Best-effort audit copy of the raw API response (CLAUDE.md: raw data
    must be stored). One file per gauge per UTC day — hourly re-runs
    overwrite within the day, so growth is bounded at one file/gauge/day.
    A failure here must never abort the refresh (live data path wins).
    """
    if payload is None:
        return
    try:
        day_dir = RAW_AUDIT_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(day_dir / f"{rain_measure_id}.json.gz",
                       "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        print(f"  WARN: raw audit write failed for {rain_measure_id}: {exc}")


def _to_daily_totals(live: pd.DataFrame) -> pd.DataFrame:
    """Sum interval readings to one row per UTC date, dropping the partial
    leading day.

    Returns columns: dateTime (UTC midnight), date (YYYY-MM-DD), value (mm).
    Empty frame when nothing usable remains.
    """
    if live.empty:
        return pd.DataFrame(columns=["dateTime", "date", "value"])

    daily = (live.set_index("dateTime")["value"]
             .resample("1D").sum(min_count=1)
             .dropna())
    if daily.empty:
        return pd.DataFrame(columns=["dateTime", "date", "value"])

    # The earliest day starts at the `since` boundary (mid-day) so its total
    # is partial — drop it. Today stays (partial but converges on re-run).
    daily = daily.iloc[1:]
    if daily.empty:
        return pd.DataFrame(columns=["dateTime", "date", "value"])

    out = daily.rename("value").reset_index()
    out["date"] = out["dateTime"].dt.tz_localize(None).dt.normalize()
    out["data_source"] = "rainfall_live"
    return out[["dateTime", "date", "value", "data_source"]]


def update_one_file(rain_measure_id: str, fm_notation: str,
                    now: pd.Timestamp) -> tuple[int, int]:
    """Pull live rainfall for one gauge and merge into its raw CSV.

    Bridges from the gauge's own archive tail (so the live data stays
    contiguous with the archive) but never reaches back further than
    MAX_BRIDGE_DAYS, the flood-monitoring readings-retention ceiling.

    Returns (n_dates_written, n_rows_total). n_dates_written is 0 when the
    gauge returns no usable live days.
    """
    fp = RAINFALL_DIR / f"{rain_measure_id}.csv"
    if not fp.exists():
        return (0, 0)

    existing = pd.read_csv(fp)
    if "data_source" not in existing.columns:
        existing["data_source"] = "archive"

    existing_dt = pd.to_datetime(existing["dateTime"], utc=True, errors="coerce")
    existing_dates = existing_dt.dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()

    # Anchor the bridge on the *archive* tail only — not previously-written
    # live rows. Anchoring on live rows would make each re-run start at
    # "today" and drop it as the partial leading day, so today's total would
    # never refresh on later hourly runs.
    is_live = existing["data_source"].eq("rainfall_live")
    archive_max = existing_dt[~is_live].max()

    # Start right after the archive's last day to keep the series contiguous,
    # but no earlier than the API's retention ceiling.
    floor = now - pd.Timedelta(days=MAX_BRIDGE_DAYS)
    since = max(archive_max, floor) if pd.notna(archive_max) else floor

    live, payload = fetch_live_rainfall(fm_notation, since=since, return_raw=True)
    _persist_raw_payload(payload, rain_measure_id)
    new_daily = _to_daily_totals(live)
    if new_daily.empty:
        return (0, len(existing))

    # Live wins: drop any existing rows on dates the live data covers. Safe
    # because the live window is always newer than the archive tail, so this
    # only ever removes prior live rows from earlier runs.
    overlap = set(new_daily["date"])
    keep_mask = ~existing_dates.isin(overlap)

    new_rows = pd.DataFrame({col: pd.NA for col in existing.columns},
                            index=range(len(new_daily)))
    new_rows["dateTime"] = new_daily["dateTime"].dt.strftime("%Y-%m-%dT%H:%M:%S").values
    new_rows["value"] = new_daily["value"].values
    if "date" in new_rows.columns:
        new_rows["date"] = new_daily["date"].dt.strftime("%Y-%m-%d").values
    if "quality" in new_rows.columns:
        new_rows["quality"] = "Live"
    if "completeness" in new_rows.columns:
        new_rows["completeness"] = "Live"
    new_rows["data_source"] = "rainfall_live"

    merged = pd.concat([existing[keep_mask], new_rows], ignore_index=True)
    merged = merged.sort_values("dateTime").reset_index(drop=True)
    # Atomic replace — same rationale as v16's shard write: to_csv truncates
    # first, so never leave a half-written file where a concurrent reader
    # (feature build / pack) can see it.
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    merged.to_csv(tmp, index=False)
    os.replace(tmp, fp)

    n_written = len(new_daily)
    return (n_written, len(merged))


def main() -> int:
    force_utf8_stdio()
    if not XREF.exists():
        print(f"ERROR: {XREF} not found — run "
              f"src.diagnostics.rainfall_monitoring_xref first.")
        return 2

    xref = pd.read_csv(XREF)
    matched = xref[xref["match_method"] != "none"].copy()
    matched = matched.dropna(subset=["fm_notation"])
    print(f"Matched rainfall gauges to refresh: {len(matched)}")

    now = pd.Timestamp.now(tz="UTC")

    n_touched = 0
    n_dates_total = 0
    n_skipped = 0
    n_errored = 0

    for _, r in matched.iterrows():
        rain_id = r["rain_measure_id"]
        notation = r["fm_notation"]
        try:
            n_written, _ = update_one_file(rain_id, notation, now)
            if n_written > 0:
                n_touched += 1
                n_dates_total += n_written
            else:
                n_skipped += 1
        except Exception as exc:
            print(f"  ! {rain_id} ({r.get('station_name', '?')}): {exc}")
            n_errored += 1

    print("\nDone:")
    print(f"  gauges touched:   {n_touched}")
    print(f"  live daily rows:  {n_dates_total}")
    print(f"  unchanged/no data:{n_skipped}")
    print(f"  errors:           {n_errored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

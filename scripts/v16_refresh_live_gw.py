"""v1.6 — refresh per-station Parquet shards with live flood-monitoring readings.

Uses src.forecast.live_levels.fetch_live_readings (return_raw=True, so the raw
payload can be kept) to pull the last 7 days of EA flood-monitoring data
for every BH that has a match in flood_monitoring_xref.csv (27 stations
as of v1.6 build).

Writes back into the existing per-station Parquet shards at
data/features/gw_by_station/<bh_id>.parquet, deduplicating by date and
marking live rows with data_source="logged_live" (so they're distinguishable
from the audited "logged" history in the canonical CSV).

Cadence: hourly via cron / Task Scheduler. Idempotent — running twice
within a 15-min window is a no-op (no new readings).

Quality handling: flood-monitoring readings are real-time and not all
have explicit quality codes. We accept any non-NaN value the API returns
(this matches the user's "Good + Unchecked" decision — the API itself
filters out obvious bad data upstream).

Side effects:
- Touched stations' Parquet shards updated in place.
- gw_freshness.csv re-built from scratch at the end (cheap — ~5 s).
- The _MANIFEST.json's `last_live_refresh` field is updated.
- Raw API payloads stored under data/raw/live_gw/<YYYY-MM-DD>/ for audit
  (one file per station per UTC day; hourly re-runs overwrite — best-effort,
  a failed audit write never aborts the refresh).

NOT a substitute for the full data-pipeline rebuild — this only writes
the most recent ~7 days. Periodic full rebuilds (src.pipeline.run +
v15_build_per_station_parquet) remain the canonical refresh path.
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/v16_refresh_live_gw.py` (path-form, as documented in
# CLAUDE.md and as cron/Task Scheduler typically invoke it). Without this,
# `from src...` below fails with ModuleNotFoundError because the project
# root isn't on sys.path when Python is launched against a file path.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd

from src.forecast.live_levels import apply_qc, fetch_live_readings
from src.utils.io_encoding import force_utf8_stdio

XREF = Path("data/processed/flood_monitoring_xref.csv")
PARQUET_DIR = Path("data/features/gw_by_station")
MANIFEST = PARQUET_DIR / "_MANIFEST.json"
FRESHNESS_CSV = Path("data/processed/gw_freshness.csv")
RAW_AUDIT_DIR = Path("data/raw/live_gw")
LIVE_WINDOW_DAYS = 7


def _persist_raw_payload(payload: dict | None, bh_id: str) -> None:
    """Best-effort audit copy of the raw API response (CLAUDE.md: raw data
    must be stored). One file per station per UTC day — hourly re-runs
    overwrite within the day, so growth is bounded at one file/station/day.
    A failure here must never abort the refresh (live data path wins).
    """
    if payload is None:
        return
    try:
        day_dir = RAW_AUDIT_DIR / datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(day_dir / f"{bh_id}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception as exc:
        print(f"  WARN: raw audit write failed for {bh_id}: {exc}")


def _historical_stats(existing: pd.DataFrame) -> tuple[float, float]:
    """Station's own GW_Level mean/std from the existing shard — the basis for
    the live |z|-score outlier cap in apply_qc. NaN-safe: returns (nan, nan)
    when there's too little history, which makes apply_qc skip the z-filter
    (rather than crash or drop everything)."""
    if existing.empty or "GW_Level" not in existing.columns:
        return float("nan"), float("nan")
    gw = pd.to_numeric(existing["GW_Level"], errors="coerce").dropna()
    if len(gw) < 2:
        return float("nan"), float("nan")
    return float(gw.mean()), float(gw.std())


def _normalise_to_daily(live: pd.DataFrame, *, stuck: bool = False) -> pd.DataFrame:
    """Aggregate sub-daily readings to one row per UTC date (mean).

    When ``stuck`` is True (apply_qc flagged the live window as a frozen
    sensor — value unchanged for > STUCK_THRESHOLD_H), the new daily rows are
    marked ``logged_live_stuck`` instead of ``logged_live`` so downstream
    readers (v15 freshness → pack → web) can warn that the reading may not
    reflect the true level. Additive new value — schema unchanged.
    """
    if live.empty:
        return live
    out = live.copy()
    # Stored Parquet schema uses tz-naive `date` (UTC midnight)
    out["date"] = (out["dateTime"].dt.tz_convert("UTC")
                   .dt.tz_localize(None).dt.normalize())
    daily = (out.dropna(subset=["value"])
             .groupby("date", as_index=False)["value"].mean()
             .rename(columns={"value": "GW_Level"}))
    daily["is_interpolated"] = 0
    daily["data_source"]     = "logged_live_stuck" if stuck else "logged_live"
    return daily[["date", "GW_Level", "is_interpolated", "data_source"]]


def update_one_shard(bh_id: str, fm_notation: str,
                     since: pd.Timestamp) -> tuple[int, int]:
    """Pull live readings for one BH and merge into its Parquet shard.

    Returns (n_rows_added, n_rows_total). n_rows_added is 0 when no
    new dates appear in the live window.
    """
    fp = PARQUET_DIR / f"{bh_id}.parquet"
    if not fp.exists():
        return (0, 0)

    live, payload = fetch_live_readings(fm_notation, since=since, return_raw=True)
    _persist_raw_payload(payload, bh_id)
    if live.empty:
        return (0, _row_count(fp))

    existing = pd.read_parquet(fp)
    # Ensure schema parity — add missing columns gracefully
    for col, default in [("is_interpolated", 0), ("data_source", "logged")]:
        if col not in existing.columns:
            existing[col] = default

    # QC the live window against the station's OWN history BEFORE it seeds the
    # status chip / forecast origin. Previously apply_qc was dead code (defined
    # + unit-tested but never called in production), so a single telemetry spike
    # or stuck sensor flowed straight into freshest_gw() and the forecast.
    hist_mean, hist_std = _historical_stats(existing)
    live, qc_flags = apply_qc(live, historical_mean=hist_mean,
                              historical_std=hist_std)
    if qc_flags:
        print(f"  QC {bh_id}: {', '.join(qc_flags)}")

    new_daily = _normalise_to_daily(live, stuck="stuck_sensor" in qc_flags)
    if new_daily.empty:
        return (0, len(existing))

    # Live wins over prior LIVE rows, but must NOT clobber audited readings on
    # the same date — the audited archive is canonical and quality-checked,
    # whereas live is sensor-grade. So drop the live value for any date that
    # already carries an audited row, then replace only the existing live rows
    # the (filtered) live window now covers. (H3: live overlap with audited is
    # rare — live is the recent tail — but when it happens, audited must stand.)
    _LIVE_SOURCES = {"logged_live", "logged_live_stuck"}
    audited_dates = set(existing.loc[~existing["data_source"].isin(_LIVE_SOURCES),
                                     "date"])
    new_daily = new_daily[~new_daily["date"].isin(audited_dates)]
    if new_daily.empty:
        return (0, len(existing))
    overlapping_dates = set(new_daily["date"])
    keep_mask = ~existing["date"].isin(overlapping_dates)
    merged = pd.concat([existing[keep_mask], new_daily], ignore_index=True)
    merged = merged.sort_values("date").reset_index(drop=True)

    merged.to_parquet(fp, compression="snappy", index=False)
    n_added = len(new_daily) - (len(existing) - int(keep_mask.sum()))
    return (max(n_added, 0), len(merged))


def _row_count(fp: Path) -> int:
    return len(pd.read_parquet(fp, columns=["date"]))


def main() -> int:
    force_utf8_stdio()
    if not XREF.exists():
        print(f"ERROR: {XREF} not found — run "
              f"src.diagnostics.flood_monitoring_xref first.")
        return 2

    xref = pd.read_csv(XREF)
    matched = xref[xref["match_method"] != "none"].copy()
    matched = matched.dropna(subset=["fm_notation"])
    print(f"Matched stations to refresh: {len(matched)}")

    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=LIVE_WINDOW_DAYS)

    n_touched = 0
    n_added_total = 0
    n_skipped = 0
    n_errored = 0

    for _, r in matched.iterrows():
        bh_id = r["station_id"]
        notation = r["fm_notation"]
        try:
            n_added, _ = update_one_shard(bh_id, notation, since)
            if n_added > 0:
                n_touched += 1
                n_added_total += n_added
            else:
                n_skipped += 1
        except Exception as exc:
            print(f"  ! {bh_id} ({r.get('station_name','?')}): {exc}")
            n_errored += 1

    # Update the manifest's live_refresh field
    if MANIFEST.exists():
        try:
            m = json.loads(MANIFEST.read_text())
        except (json.JSONDecodeError, OSError):
            m = {}
    else:
        m = {}
    m["last_live_refresh"] = datetime.now(timezone.utc).isoformat()
    m["last_live_refresh_n_touched"] = n_touched
    m["last_live_refresh_n_added"]   = n_added_total
    MANIFEST.write_text(json.dumps(m, indent=2))

    print(f"\nDone:")
    print(f"  shards touched: {n_touched}")
    print(f"  new daily rows: {n_added_total}")
    print(f"  unchanged:      {n_skipped}")
    print(f"  errors:         {n_errored}")

    # Refresh the freshness artefact so downstream readers see new labels.
    # Use `-m scripts.v15_build_gw_freshness` (module form) rather than the
    # path form — module form sets sys.path correctly and works regardless of
    # the subprocess's cwd.
    print("\nRefreshing data/processed/gw_freshness.csv …")
    import subprocess
    rc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "scripts.v15_build_gw_freshness"],
        check=False,
    ).returncode
    if rc != 0:
        print(f"  freshness build returned exit code {rc}")
        return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

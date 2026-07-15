"""
Time series downloader.

Downloads historical EA Hydrology readings for all measure IDs in
station_links.csv. Streams to file, retries with exponential backoff,
detects truncation and falls back to year-by-year chunking, writes
a manifest on completion.

Usage:
    python -m src.download.build
"""

import json
import sys
import time
from datetime import date, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

_USER_AGENT = "GroundwaterForecast/1.0 (research; contact: see project README)"

_TYPE_COLUMNS = {
    "groundwater": ["GWMeasureID"],
    "rainfall":    ["RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"],
    # Flow gauges (low-flow build_plan.md Stage 2): ids come from
    # flow_links.csv's FlowMeasureID column, not station_links.csv, so this
    # entry is inert for the borehole pipeline's download_all/refresh_tails
    # (which read station_links.csv) — src.download.flow reuses
    # extract_measure_ids against flow_links.csv to pick these up.
    "flow":        ["FlowMeasureID"],
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parents[2] / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Extract unique measure IDs
# ---------------------------------------------------------------------------

def extract_measure_ids(links_df: pd.DataFrame) -> dict[str, list[str]]:
    """Return unique measure IDs grouped by type. None values are excluded.

    A measure_id appearing in multiple rows or columns is kept only once.
    """
    seen: set[str] = set()
    result: dict[str, list[str]] = {t: [] for t in _TYPE_COLUMNS}

    for measure_type, cols in _TYPE_COLUMNS.items():
        for col in cols:
            if col not in links_df.columns:
                continue
            for val in links_df[col].dropna().unique():
                if val not in seen:
                    seen.add(val)
                    result[measure_type].append(val)

    return result


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_with_retry(
    url: str,
    params: dict,
    max_retries: int,
    backoff_base: int,
) -> requests.Response:
    """GET with exponential backoff. Raises on final failure."""
    headers = {"User-Agent": _USER_AGENT}
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                stream=True, timeout=60)
            resp.raise_for_status()
            return resp
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = backoff_base ** (attempt + 1)   # 2, 4, 8 s
                print(f"      Retry {attempt + 1}/{max_retries - 1} after {wait}s ({exc})")
                time.sleep(wait)

    raise RuntimeError(f"All {max_retries} attempts failed") from last_exc


def _stream_to_file(response: requests.Response, path: Path) -> int:
    """Write streamed response to path. Returns number of bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                fh.write(chunk)
                written += len(chunk)
    return written


# ---------------------------------------------------------------------------
# Truncation detection and chunked download
# ---------------------------------------------------------------------------

def _row_count(path: Path) -> int:
    """Count data rows in a CSV (excluding header)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return sum(1 for line in fh) - 1


def _date_chunks(min_date: str, chunk_years: int) -> list[tuple[str, str]]:
    """Split [min_date, today] into chunks of chunk_years years."""
    start = date.fromisoformat(min_date)
    end   = date.today()
    chunks = []
    cursor = start
    while cursor < end:
        next_cursor = date(cursor.year + chunk_years, cursor.month, cursor.day)
        chunk_end = min(next_cursor - timedelta(days=1), end)
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = next_cursor
    return chunks


def _download_chunked(
    url: str,
    path: Path,
    min_date: str,
    limit: int,
    max_retries: int,
    backoff_base: int,
    chunk_years: int,
) -> None:
    """Download in year-chunks, merge, deduplicate on dateTime, sort, save."""
    chunks = _date_chunks(min_date, chunk_years)
    frames: list[pd.DataFrame] = []

    for chunk_min, chunk_max in chunks:
        params = {"min-date": chunk_min, "max-date": chunk_max, "_limit": limit}
        resp = _get_with_retry(url, params, max_retries, backoff_base)
        text = resp.content.decode("utf-8", errors="replace")
        df = pd.read_csv(StringIO(text))
        if not df.empty:
            frames.append(df)

    if not frames:
        path.write_text("")
        return

    combined = pd.concat(frames, ignore_index=True)
    rows_before = len(combined)

    if "dateTime" not in combined.columns:
        # Fallback: use whichever column most looks like a timestamp
        dt_col = next(
            (c for c in combined.columns if "date" in c.lower() or "time" in c.lower()),
            None,
        )
        if dt_col:
            combined = combined.rename(columns={dt_col: "dateTime"})

    if "dateTime" in combined.columns:
        combined["dateTime"] = pd.to_datetime(
            combined["dateTime"], utc=True, errors="coerce"
        )
        combined = (
            combined
            .drop_duplicates(subset=["dateTime"])
            .sort_values("dateTime")
            .reset_index(drop=True)
        )

    rows_after = len(combined)
    dupes_removed = rows_before - rows_after
    print(f"      Chunk merge: {rows_before} rows in, "
          f"{dupes_removed} duplicates removed, {rows_after} rows saved")

    combined.to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Single measure download
# ---------------------------------------------------------------------------

def download_measure(
    measure_id: str,
    measure_type: str,
    config: dict,
) -> tuple[str, str]:
    """Download one measure. Returns (measure_id, status) where status is
    'downloaded', 'chunked', 'skipped', or 'failed'.
    """
    dl  = config["download"]
    raw_root    = Path(__file__).parents[2] / dl["raw_root"]
    out_path    = raw_root / measure_type / f"{measure_id}.csv"
    url         = config["api"]["readings_url_template"].format(measure_id=measure_id)
    limit       = dl["limit"]
    max_retries = dl["max_retries"]
    backoff     = dl["backoff_base"]
    min_date    = dl["min_date"]
    chunk_years = dl.get("chunk_years", 2)

    if out_path.exists():
        return measure_id, "skipped"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        params = {"min-date": min_date, "_limit": limit}
        resp = _get_with_retry(url, params, max_retries, backoff)
        _stream_to_file(resp, out_path)

        # Truncation check: if rows == limit, fall back to chunked download
        if _row_count(out_path) >= limit:
            print(f"      Truncation detected ({limit} rows) — switching to chunked download")
            _download_chunked(url, out_path, min_date, limit,
                              max_retries, backoff, chunk_years)
            return measure_id, "chunked"

        return measure_id, "downloaded"

    except Exception as exc:
        print(f"      FAILED: {exc}")
        if out_path.exists():
            out_path.unlink()   # remove partial file
        return measure_id, "failed"


# ---------------------------------------------------------------------------
# Incremental tail top-up (keeps existing CSVs current between full rebuilds)
# ---------------------------------------------------------------------------
# download_measure SKIPS any measure whose CSV already exists, so raw readings
# freeze at first download and only refresh on a full rebuild — leaving the
# ingested copy months stale while the EA archive stays ~weeks fresh. topup_*
# closes that gap: fetch each existing CSV's tail from its last date (minus a
# small overlap, to absorb late-arriving/revised readings) and merge it in.

def _merge_readings(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Union two readings frames, parse dateTime, drop duplicate timestamps
    (new wins on overlap), sort. Pure — unit-tested."""
    combined = pd.concat([existing, new], ignore_index=True)
    if "dateTime" not in combined.columns:
        return existing
    combined["dateTime"] = pd.to_datetime(combined["dateTime"], utc=True, errors="coerce")
    return (combined.dropna(subset=["dateTime"])
            .drop_duplicates(subset=["dateTime"], keep="last")
            .sort_values("dateTime").reset_index(drop=True))


def _fetch_since(url: str, min_date: str, limit: int, max_retries: int,
                 backoff: int, chunk_years: int) -> pd.DataFrame:
    """Readings from ``min_date`` to today as a DataFrame; falls back to
    year-chunking if the single request truncates (large catch-up gaps)."""
    resp = _get_with_retry(url, {"min-date": min_date, "_limit": limit},
                           max_retries, backoff)
    df = pd.read_csv(StringIO(resp.content.decode("utf-8", errors="replace")))
    if len(df) < limit:
        return df
    frames = []
    for cmin, cmax in _date_chunks(min_date, chunk_years):
        r = _get_with_retry(url, {"min-date": cmin, "max-date": cmax, "_limit": limit},
                            max_retries, backoff)
        d = pd.read_csv(StringIO(r.content.decode("utf-8", errors="replace")))
        if not d.empty:
            frames.append(d)
    return pd.concat(frames, ignore_index=True) if frames else df


def last_datetime_from_tail(path: Path, tail_bytes: int = 4096):
    """dateTime of a readings CSV's final data line via a tail seek — no full
    parse (the CSVs are date-sorted: chunked downloads and top-up merges both
    sort). Returns a tz-aware UTC Timestamp, or None when unparseable — the
    caller must then fall back to a full read, never skip."""
    import csv as _csv
    import io as _io
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
            fh.seek(0)
            header = fh.readline().decode("utf-8", errors="replace")
        cols = next(_csv.reader(_io.StringIO(header)))
        if "dateTime" not in cols:
            return None
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if not lines or lines[-1] == header.strip():
            return None
        last = next(_csv.reader(_io.StringIO(lines[-1])))
        ts = pd.to_datetime(last[cols.index("dateTime")], utc=True, errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def topup_measure(measure_id: str, measure_type: str, config: dict,
                  overlap_days: int = 14) -> tuple[str, str]:
    """Extend an existing measure CSV to today. Returns (measure_id, status):
    'advanced' (newer rows added), 'current' (already up to date), 'absent'
    (no file, or an existing file with a valid header but zero data rows —
    nothing to top up from yet, but nothing broken either), 'failed', or one
    of download_measure's statuses ('downloaded'/'chunked'/'failed') when a
    0-byte/unparseable raw file is healed by falling through to a full
    download (see below).

    The existing CSV is only fully read (and rewritten) when the fetch actually
    extends past its last date — the common no-new-data day costs one tail seek
    plus one small HTTP request. Trade-off: a revision inside the overlap window
    with NO new dates isn't merged until the series next advances (archive
    revisions ship with the batch that extends it, so this is theoretical).
    """
    dl = config["download"]
    raw_root = Path(__file__).parents[2] / dl["raw_root"]
    out_path = raw_root / measure_type / f"{measure_id}.csv"
    if not out_path.exists():
        return measure_id, "absent"

    existing = None
    last = last_datetime_from_tail(out_path)
    if last is None:                       # odd/unsorted file → safe full read
        try:
            existing = pd.read_csv(out_path, low_memory=False)
        except Exception:
            # 0-byte or otherwise unparseable file — not a real archive.
            # The EA API occasionally returns 200 OK with an empty body for a
            # catalogued-but-not-yet-populated qualified measure; if that got
            # saved verbatim as the raw CSV, every future top-up would land
            # here and previously returned "failed" forever (see BUGS.md —
            # fixed here), wedging the measure until a human deleted the
            # file. Treat it as "never downloaded": drop the junk file and
            # fall through to a full download, which overwrites it.
            #
            # Contrast with a file that parses fine but has zero data rows
            # (just a header) — download_measure/_stream_to_file themselves
            # write that shape for a legitimately empty-so-far measure (the
            # truncation check below the streamed write treats 0 rows as a
            # normal "downloaded" outcome, not an error). That's a valid,
            # intentional state, not corruption, so it is left alone and
            # reported as "absent" below rather than deleted/re-fetched.
            out_path.unlink(missing_ok=True)
            return download_measure(measure_id, measure_type, config)
        if existing.empty or "dateTime" not in existing.columns:
            return measure_id, "absent"
        last = pd.to_datetime(existing["dateTime"], utc=True, errors="coerce").max()
        if pd.isna(last):
            return measure_id, "failed"

    frm = (last - timedelta(days=overlap_days)).date().isoformat()
    url = config["api"]["readings_url_template"].format(measure_id=measure_id)
    try:
        new = _fetch_since(url, frm, dl["limit"], dl["max_retries"],
                           dl["backoff_base"], dl.get("chunk_years", 2))
    except Exception:
        return measure_id, "failed"
    if new is None or new.empty or "dateTime" not in new.columns:
        return measure_id, "current"
    new_max = pd.to_datetime(new["dateTime"], utc=True, errors="coerce").max()
    if pd.isna(new_max) or new_max <= last:
        return measure_id, "current"       # nothing beyond the tail — no rewrite

    if existing is None:
        try:
            existing = pd.read_csv(out_path, low_memory=False)
        except Exception:
            return measure_id, "failed"
    merged = _merge_readings(existing, new)
    merged.to_csv(out_path, index=False)
    return measure_id, "advanced"


def refresh_tails(config: dict, types: tuple[str, ...] = ("groundwater",),
                  overlap_days: int = 14) -> dict[str, int]:
    """Top up every EXISTING raw CSV for the given measure types (default
    groundwater). Returns a status tally. Absent files are left for a full
    download — this only keeps already-ingested series current."""
    from collections import Counter
    links_path = Path(__file__).parents[2] / config["linking"]["output_path"]
    grouped = extract_measure_ids(pd.read_csv(links_path))
    tally: Counter = Counter()
    for t in types:
        ids = grouped.get(t, [])
        print(f"Top-up {t}: {len(ids)} measures (overlap {overlap_days}d)")
        for i, mid in enumerate(ids, 1):
            _, status = topup_measure(mid, t, config, overlap_days)
            tally[status] += 1
            if i % 100 == 0 or i == len(ids):
                print(f"  {i}/{len(ids)}  {dict(tally)}")
    return dict(tally)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _write_manifest(
    results: list[tuple[str, str]],
    manifest_path: Path,
) -> None:
    manifest: dict[str, list[str]] = {
        "downloaded": [],
        "chunked":    [],
        "skipped":    [],
        "failed":     [],
        "timestamp":  [date.today().isoformat()],
    }
    for measure_id, status in results:
        manifest[status].append(measure_id)

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def download_all(config: dict) -> None:
    links_path    = Path(__file__).parents[2] / config["linking"]["output_path"]
    manifest_path = Path(__file__).parents[2] / config["download"]["manifest_path"]

    links_df = pd.read_csv(links_path)
    grouped  = extract_measure_ids(links_df)

    total = sum(len(v) for v in grouped.values())
    print(f"Measures to download: {total}")
    for t, ids in grouped.items():
        print(f"  {t}: {len(ids)}")
    print()

    results: list[tuple[str, str]] = []
    counter = 0

    for measure_type, ids in grouped.items():
        for measure_id in ids:
            counter += 1
            print(f"  [{counter:>3}/{total}] {measure_type} / {measure_id}", end=" ... ")
            _, status = download_measure(measure_id, measure_type, config)
            print(status.upper())
            results.append((measure_id, status))

    downloaded = sum(1 for _, s in results if s == "downloaded")
    chunked    = sum(1 for _, s in results if s == "chunked")
    skipped    = sum(1 for _, s in results if s == "skipped")
    failed     = sum(1 for _, s in results if s == "failed")

    print(f"\nDone. downloaded={downloaded}  chunked={chunked}  "
          f"skipped={skipped}  failed={failed}")

    _write_manifest(results, manifest_path)
    print(f"Manifest written to {manifest_path}")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config = load_config()
    try:
        download_all(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

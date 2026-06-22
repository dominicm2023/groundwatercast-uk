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

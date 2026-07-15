"""Low-flow Rivers layer — Stage 5 fleet scan
(``docs/product/lowflow/build_plan.md``).

Runs the Stage-4 admission gate (``src.forecast.pastas.flow_gate.admit_gauge``)
over every gauge in ``data/processed/flow_links.csv`` (~1,087) and writes one
row per gauge to ``outputs/flow_fleet_scan.csv``. Per-gauge data assembly
(flow shard build-if-missing, rainfall raw archives, PET cache) is
``src.download.flow.load_gauge_series`` — the SAME function
``scripts/flow_gate_check.py`` (Stage 4) uses, refactored out to
``src.download.flow`` rather than duplicated here. Most gauges have no flow
shard yet (only the ~49 pilot gauges do), so the shard build happens per-gauge
inside the scan loop — this script IS the fleet's first ingest.

Resumable: on startup, existing rows in ``--out`` are read and their
``gauge_id``s skipped; every completed row is appended and flushed
immediately, so a killed run loses at most the one in-flight gauge per
worker. A row whose ``error`` column is non-empty (a ``load_error``/
``gate_error``/etc from a prior session, e.g. a 429) is normally treated as
DONE too — resume is cheap by default — but ``--retry-errors`` flips that:
those rows are dropped (rewritten out of ``--out``, every non-error row
preserved byte-for-byte) before scanning, so the failed gauges are retried
this session instead of being skipped forever.

Parallel via ``ProcessPoolExecutor`` (``--workers``, default
``min(3, cpu_count - 2)`` — capped the same way ``build_flow_models.py``
caps its own default, see that module's docstring: per-gauge data assembly
hits ``src.data.pet.fetch_station_pet`` (Open-Meteo archive HTTP), which is
429-prone at high parallelism); a lock shared across worker processes
serialises raw-file downloads for rain gauges shared by nearby flow gauges
(the only concurrent-write hazard — flow shards and PET cache are
one-file-per-gauge). That lock only covers workers WITHIN one invocation's
pool, though — a second concurrent invocation (a second manual scan, or a
scan racing another flow stage's ingest) has no shared lock and can download
the same rain-gauge raw CSV at the same time and corrupt it. A coarse
single-instance lockfile (``--lock``, same stale-steal pattern as
``scripts/run_chain.py``'s repo-level lock) makes one-scan-at-a-time a hard
guarantee instead of a race.

If the pool breaks (Windows/pastas/numba misbehaving), the remaining gauges
run out serially in the same process rather than aborting the fleet run.

A single bad gauge never aborts the batch: both data assembly and the gate
call are wrapped so a per-gauge exception degrades to an error row.

Usage:
    python -m scripts.flow_fleet_scan                    # full fleet, resumable
    python -m scripts.flow_fleet_scan --limit 15 --workers 4   # smoke test
    python -m scripts.flow_fleet_scan --workers 1         # force serial

429 recovery (a prior session's error rows, e.g. rate-limited PET fetches):
    python -m scripts.flow_fleet_scan --retry-errors --workers 1

Pastas lives in the dedicated ``.venv-pastas`` environment:
    .venv-pastas\\Scripts\\python.exe -m scripts.flow_fleet_scan --workers 6
"""
from __future__ import annotations

import os

# Set BEFORE numpy/pastas import in every process (main AND each spawned
# worker re-executes this module top-to-bottom): one pastas fit per origin is
# already CPU-bound, and BLAS/numba each defaulting to multi-threaded inside
# EVERY worker process oversubscribes the machine's cores badly under
# ProcessPoolExecutor. Pin each worker to one thread; the parallelism comes
# from the process pool, not from threads inside each process.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import argparse
import csv
import multiprocessing as mp
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pandas as pd

from src.download.build import load_config
from src.download.flow import (
    FLOW_CATALOGUE_PATH,
    FLOW_LINKS_PATH,
    load_flow_measure_map,
    load_gauge_series,
)
from src.forecast.pastas import flow_gate as G
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
FLOW_CATALOGUE = ROOT / FLOW_CATALOGUE_PATH
OUT_PATH = ROOT / "outputs" / "flow_fleet_scan.csv"
LOG_PATH = ROOT / "outputs" / "flow_fleet_scan.log"
LOCK_PATH = ROOT / "outputs" / "flow_fleet_scan.lock"

# PET fetch (network, Open-Meteo archive) is 429-prone at high parallelism —
# discovered live running this exact script. build_flow_models.py (the
# monthly recalibration sibling, same load_gauge_series data assembly) caps
# its default the same way; mirrored here rather than the older
# ``max(2, cpu_count - 2)`` this script used before the 429 was diagnosed.
_DEFAULT_WORKERS = min(3, max(1, (os.cpu_count() or 4) - 2))

# Same columns as flow_gate_check.csv, plus record_start (earliest date in the
# gauge's flow shard, once assembled) and error (exception text, when a row
# came from a failure rather than a completed gate run).
ROW_FIELDS = [
    "gauge_id", "station_name", "gate_pass", "tier", "rain_dependent",
    "n_origins", "n_years", "range_logq",
    "floor_skill", "floor_cov14", "floor_band_frac",
    "ceiling_skill", "ceiling_cov14", "ceiling_band_frac",
    "reason", "record_start", "error", "elapsed_s",
]

PROGRESS_EVERY = 25   # stdout tier-count summary cadence


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--links", default=str(FLOW_LINKS_PATH))
    ap.add_argument("--catalogue", default=str(FLOW_CATALOGUE))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--log", default=str(LOG_PATH))
    ap.add_argument("--lock", default=str(LOCK_PATH),
                    help="single-instance lockfile — refuses to start a "
                         "second concurrent scan (shared rain-gauge raw-CSV "
                         "downloads are not safe across processes)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only run the first N not-yet-done gauges this "
                         "session, sorted by id (smoke-test flag)")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                    help="ProcessPoolExecutor worker count; <=1 forces serial "
                         "(default: %(default)s, capped for PET-fetch "
                         "rate-limit safety — see module docstring)")
    ap.add_argument("--retry-errors", action="store_true",
                    help="treat rows with a non-empty 'error' column (a "
                         "prior session's load_error/gate_error/etc, e.g. a "
                         "429) as not-done: drop them from --out and retry "
                         "this session, instead of skipping them forever. "
                         "Default off so a plain resume stays cheap; 429 "
                         "recovery is '--retry-errors --workers 1'.")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Resumability: skip-already-done set
# ---------------------------------------------------------------------------

def load_done_gauge_ids(out_path: Path) -> set[str]:
    """``gauge_id``s already present in an existing scan CSV. Empty set if the
    file doesn't exist, is empty, or is corrupt (e.g. the process was killed
    mid-line last time) — resuming from a damaged tail must not crash the
    rerun, it just re-does slightly more work than strictly necessary."""
    if not out_path.exists() or out_path.stat().st_size == 0:
        return set()
    try:
        df = pd.read_csv(out_path, usecols=["gauge_id"], dtype=str)
    except Exception:
        return set()
    return set(df["gauge_id"].dropna())


def rewrite_dropping_error_rows(out_path: Path) -> int:
    """``--retry-errors`` startup step: rewrite ``out_path`` keeping only rows
    whose ``error`` column is empty, so ``load_done_gauge_ids`` (called right
    after this) no longer sees the failed gauges as done and re-scans them
    this session — without this, a gauge that failed once (e.g. a 429) is
    skipped forever and the operator has to hand-delete its row to retry.

    Reads/writes through the stdlib ``csv`` module (not pandas) specifically
    so every kept row's field values pass through as the exact strings
    already on disk — no dtype coercion, no float re-formatting — matching
    the "preserving all non-error rows byte-for-byte" requirement. Returns
    the number of rows dropped (0 if the file doesn't exist, is empty, has
    no ``error`` column, or is corrupt — mirrors ``load_done_gauge_ids``'s
    never-crash-a-resume discipline; a genuinely corrupt tail is left for
    that function's own fallback rather than risked here).
    """
    if not out_path.exists() or out_path.stat().st_size == 0:
        return 0
    try:
        with open(out_path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames
            rows = list(reader)
    except Exception:
        return 0
    if not fieldnames or "error" not in fieldnames:
        return 0
    keep_rows = [r for r in rows if not (r.get("error") or "").strip()]
    n_dropped = len(rows) - len(keep_rows)
    if n_dropped == 0:
        return 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(keep_rows)
    return n_dropped


# ---------------------------------------------------------------------------
# Single-instance lock — outputs/flow_fleet_scan.lock, same coarse
# create-exclusive-with-stale-steal pattern as scripts/run_chain.py's
# repo-level lock (see that module for the full rationale). Scoped to this
# script only: the per-worker multiprocessing.Manager().Lock() elsewhere in
# this file serialises rain-raw downloads WITHIN one invocation's pool, but
# does nothing for a SECOND concurrent invocation (a second manual scan, or
# this scan racing another flow stage's ingest) — both could download the
# same shared rain-gauge raw CSV at once and corrupt it. One-scan-at-a-time
# is the right guarantee for a manual tool; making the per-file lock
# cross-process is not attempted here.
# ---------------------------------------------------------------------------
LOCK_STALE_S = 6 * 3600


def _acquire_lock(lock_path: Path) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"pid={os.getpid()} started="
                         f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                continue                      # vanished between open and stat — retry
            if age <= LOCK_STALE_S:
                return False
            print(f"WARNING: stealing stale flow_fleet_scan lock (age "
                  f"{age / 3600:.1f} h) — a previous scan died without "
                  f"releasing it.", file=sys.stderr)
            try:
                lock_path.unlink()
            except OSError:
                pass
    return False


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Row shape helper (used by both the success and failure paths)
# ---------------------------------------------------------------------------

def _empty_row(gauge_id: str, name, reason: str, error: str = "") -> dict:
    return {
        "gauge_id": gauge_id, "station_name": name, "gate_pass": False,
        "tier": "status_only", "rain_dependent": False,
        "n_origins": 0, "n_years": 0, "range_logq": None,
        "floor_skill": None, "floor_cov14": None, "floor_band_frac": None,
        "ceiling_skill": None, "ceiling_cov14": None, "ceiling_band_frac": None,
        "reason": reason, "record_start": None, "error": error, "elapsed_s": None,
    }


# ---------------------------------------------------------------------------
# Worker process state — populated ONCE per process via ProcessPoolExecutor's
# initializer, not per task: the links/catalogue CSV reads and (for the
# gate itself) pastas/numba's JIT warmup both amortise across every gauge a
# given worker handles over the run, instead of repeating per gauge.
# ---------------------------------------------------------------------------

_W_LINKS_DF: pd.DataFrame | None = None
_W_CAT_DF: pd.DataFrame | None = None
_W_MEASURE_MAP: dict[str, str] | None = None
_W_CONFIG: dict | None = None
_W_LOCK = None   # multiprocessing lock guarding shared rain-raw downloads


def _worker_init(links_path: str, catalogue_path: str, download_lock) -> None:
    """Runs once per worker process (module-level function -> picklable by
    reference, required for Windows spawn)."""
    global _W_LINKS_DF, _W_CAT_DF, _W_MEASURE_MAP, _W_CONFIG, _W_LOCK
    _W_CONFIG = load_config()
    _W_LINKS_DF = pd.read_csv(links_path, dtype=str).set_index("GaugeID")
    _W_CAT_DF = pd.read_csv(catalogue_path, dtype={"station_id": str}).set_index("station_id")
    _W_MEASURE_MAP = load_flow_measure_map(Path(links_path))
    _W_LOCK = download_lock


def _scan_one_gauge_inner(gauge_id: str) -> dict:
    t0 = time.time()
    name = gauge_id
    try:
        if _W_CAT_DF is not None and gauge_id in _W_CAT_DF.index:
            name = _W_CAT_DF.loc[gauge_id, "station_name"]
    except Exception:
        pass

    try:
        loaded = load_gauge_series(gauge_id, _W_LINKS_DF, _W_CAT_DF, _W_MEASURE_MAP,
                                   _W_CONFIG, download_lock=_W_LOCK)
    except Exception as exc:
        row = _empty_row(gauge_id, name, "load_error", f"{type(exc).__name__}: {exc}")
        row["elapsed_s"] = round(time.time() - t0, 1)
        return row

    if loaded is None:
        row = _empty_row(gauge_id, name, "no_data")
        row["elapsed_s"] = round(time.time() - t0, 1)
        return row

    q, prec, evap = loaded
    record_start = pd.Timestamp(q.index.min()).date().isoformat() if len(q) else None

    try:
        result = G.admit_gauge(gauge_id, q, prec, evap)
    except Exception as exc:
        row = _empty_row(gauge_id, name, "gate_error", f"{type(exc).__name__}: {exc}")
        row["record_start"] = record_start
        row["elapsed_s"] = round(time.time() - t0, 1)
        return row

    floor = result.get("floor") or {}
    ceiling = result.get("ceiling") or {}
    return {
        "gauge_id": gauge_id, "station_name": name,
        "gate_pass": result["gate_pass"], "tier": result["tier"],
        "rain_dependent": result["rain_dependent"],
        "n_origins": result["n_origins"], "n_years": result["n_years"],
        "range_logq": result["range_logq"],
        "floor_skill": floor.get("skill_ratio"),
        "floor_cov14": floor.get("cov14"),
        "floor_band_frac": floor.get("band_frac"),
        "ceiling_skill": ceiling.get("skill_ratio"),
        "ceiling_cov14": ceiling.get("cov14"),
        "ceiling_band_frac": ceiling.get("band_frac"),
        "reason": result["reason"],
        "record_start": record_start,
        "error": "",
        "elapsed_s": round(time.time() - t0, 1),
    }


def scan_one_gauge(gauge_id: str) -> dict:
    """Module-level, picklable worker entry point. Belt-and-braces outer
    catch on top of ``_scan_one_gauge_inner``'s own guards (and
    ``admit_gauge``'s own never-raises contract) — NOTHING escapes this
    function as an exception; a bad gauge is always an error row."""
    try:
        return _scan_one_gauge_inner(gauge_id)
    except Exception as exc:                                     # pragma: no cover
        return _empty_row(gauge_id, gauge_id, "worker_exception",
                          f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# CSV + log writer (main process only — single writer keeps append+flush
# resumability simple regardless of worker count)
# ---------------------------------------------------------------------------

class ScanWriter:
    def __init__(self, out_path: Path, log_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not out_path.exists() or out_path.stat().st_size == 0
        self._csv_fh = open(out_path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._csv_fh, fieldnames=ROW_FIELDS)
        if write_header:
            self._writer.writeheader()
            self._csv_fh.flush()
        self._log_fh = open(log_path, "a", encoding="utf-8")

    def write_row(self, row: dict, *, done: int, total: int) -> None:
        self._writer.writerow({k: row.get(k) for k in ROW_FIELDS})
        self._csv_fh.flush()
        try:
            os.fsync(self._csv_fh.fileno())
        except OSError:                                           # pragma: no cover
            pass
        self._log_fh.write(
            f"[{done}/{total}] {row.get('gauge_id')} "
            f"{str(row.get('station_name'))[:30]} tier={row.get('tier')} "
            f"reason={row.get('reason')} {row.get('elapsed_s')}s\n"
        )
        self._log_fh.flush()

    def close(self) -> None:
        self._csv_fh.close()
        self._log_fh.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """Acquires the single-instance lock (``--lock``) before doing anything
    else, refuses to start with a clear message if another scan already
    holds it, and always releases on the way out (success, exception, or
    early return) — see the lock section above for why this scan needs its
    own lockfile on top of the intra-pool download lock."""
    lock_path = Path(args.lock)
    if not _acquire_lock(lock_path):
        print(f"ERROR: another flow_fleet_scan is already running "
              f"({lock_path}) — refusing to start a second one (two "
              f"concurrent scans can download the same shared rain-gauge "
              f"raw CSV at once and corrupt it). If you're sure no other "
              f"scan is actually running, delete the lock file and retry.",
              file=sys.stderr)
        return 3
    try:
        return _run_locked(args)
    finally:
        _release_lock(lock_path)


def _run_locked(args: argparse.Namespace) -> int:
    links_path = Path(args.links)
    catalogue_path = Path(args.catalogue)
    if not links_path.exists():
        print(f"ERROR: {links_path} not found.", file=sys.stderr)
        return 2
    if not catalogue_path.exists():
        print(f"ERROR: {catalogue_path} not found.", file=sys.stderr)
        return 2

    measure_map = load_flow_measure_map(links_path)
    all_gauge_ids = sorted(measure_map)
    total_fleet = len(all_gauge_ids)

    out_path = Path(args.out)
    if getattr(args, "retry_errors", False):
        n_dropped = rewrite_dropping_error_rows(out_path)
        if n_dropped:
            print(f"--retry-errors: dropped {n_dropped} error row(s) from "
                 f"{out_path} — those gauge(s) will be retried this session.")
    done_ids = load_done_gauge_ids(out_path)
    todo = [g for g in all_gauge_ids if g not in done_ids]
    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"Flow fleet scan: {total_fleet} gauge(s) in fleet, "
         f"{len(done_ids)} already done, {len(todo)} to run this session "
         f"(workers={args.workers})")
    if not todo:
        print("Nothing to do.")
        return 0

    writer = ScanWriter(out_path, Path(args.log))
    done_count = len(done_ids)
    recorded_ids: set[str] = set(done_ids)
    tier_counts: dict[str, int] = {}

    def _record(row: dict) -> None:
        nonlocal done_count
        done_count += 1
        recorded_ids.add(row.get("gauge_id"))
        t = row.get("tier", "?")
        tier_counts[t] = tier_counts.get(t, 0) + 1
        writer.write_row(row, done=done_count, total=total_fleet)
        if done_count % PROGRESS_EVERY == 0 or done_count == total_fleet:
            print(f"  progress: {done_count}/{total_fleet}  session_tiers={tier_counts}")

    t0 = time.time()
    try:
        if args.workers <= 1:
            print("Running serial (--workers <= 1).")
            _worker_init(str(links_path), str(catalogue_path), None)
            for gauge_id in todo:
                _record(scan_one_gauge(gauge_id))
        else:
            manager = mp.Manager()
            download_lock = manager.Lock()
            try:
                with ProcessPoolExecutor(
                    max_workers=args.workers,
                    initializer=_worker_init,
                    initargs=(str(links_path), str(catalogue_path), download_lock),
                ) as ex:
                    futures = {ex.submit(scan_one_gauge, g): g for g in todo}
                    for fut in as_completed(futures):
                        gauge_id = futures[fut]
                        try:
                            row = fut.result()
                        except BrokenProcessPool:
                            # Must NOT become a per-gauge error row: a broken
                            # pool fails EVERY pending future, and an error
                            # row is skipped forever on resume — one breakage
                            # would poison the CSV for hundreds of un-run
                            # gauges. Re-raise to the serial-fallback handler.
                            raise
                        except Exception as exc:
                            row = _empty_row(gauge_id, gauge_id, "future_exception",
                                             f"{type(exc).__name__}: {exc}")
                        _record(row)
            except BrokenProcessPool as exc:
                print(f"WARNING: worker pool broke ({exc}) — falling back to "
                     f"serial for the remaining gauges. This run just takes "
                     f"longer; nothing is lost (resumable).", file=sys.stderr)
                remaining = [g for g in todo if g not in recorded_ids]
                _worker_init(str(links_path), str(catalogue_path), None)
                for gauge_id in remaining:
                    _record(scan_one_gauge(gauge_id))
    finally:
        writer.close()

    elapsed = time.time() - t0
    n_this_session = done_count - len(done_ids)
    per_gauge = elapsed / n_this_session if n_this_session else float("nan")
    print(f"\nSession done: {n_this_session} gauge(s) in {elapsed:.1f}s "
         f"({per_gauge:.2f}s/gauge at workers={args.workers})")

    full = pd.read_csv(out_path, dtype={"gauge_id": str})
    print(f"Fleet total so far: {len(full)}/{total_fleet}")
    print(f"Tiers: {full['tier'].value_counts().to_dict()}")
    return 0


if __name__ == "__main__":
    force_utf8_stdio()
    try:
        sys.exit(run(parse_args()))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

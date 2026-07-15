"""Calibrate & cache the two-pathway flow ModelRecs for the low-flow Rivers
layer pilot — Stage 6 of ``docs/product/lowflow/build_plan.md``.

Monthly-recalibration sibling of ``scripts/build_pastas_models.py``: for each
pilot gauge (``data/processed/flow_pilot.csv``, from
``scripts/select_flow_pilot.py``), assembles its full flow/rain/PET series
(``src.download.flow.load_gauge_series`` — the same per-gauge data assembly
Stage 4/5 use) and calibrates the two-pathway model
(``src.forecast.pastas.recharge.calibrate_flow``), then caches the ModelRec
JSON (single-file store, ``R.save_models``/``R.load_models`` — pattern-matches
the GW model store exactly: one JSON dict of ``{gauge_id: ModelRec}``, not a
directory of per-gauge files).

Also computes each gauge's Q95 threshold — the 5th percentile of the gauge's
FULL daily flow record (m3/s), i.e. the flow exceeded 95% of the time, the
low-flow breach threshold ``build_flow_members.py`` gates on — and stores it
on the rec as ``rec["q95_m3s"]`` (alongside the rec, not a separate file: it
round-trips through the same JSON store the rec already uses).

GRACEFUL SKIP (exit 0) when ``data/processed/flow_pilot.csv`` is absent —
this stage sits in run_chain's monthly recalibration group ("pastas"), and a
host that hasn't run ``select_flow_pilot.py`` yet (or has the low-flow build
disabled) must not kill the whole recalibration run (see
``scripts/build_flow_shards.py``'s links-missing path, PR #126, for the same
discipline applied to the daily ingest stage).

Parallel via ``ProcessPoolExecutor`` (``--workers``), same discipline as
``scripts/flow_fleet_scan.py`` (Stage 5): module-level picklable worker
functions, a per-process initializer that loads the links/catalogue CSVs
ONCE per worker (not per gauge), a ``multiprocessing.Manager`` lock guarding
the shared rain-raw download hazard (nearby gauges commonly share a nearest
rain gauge — see ``src.download.flow.ensure_rain_raw``), and a
BrokenProcessPool handler that falls back to serial for the remaining gauges
rather than losing the run. A single bad gauge always degrades to a skip
entry, never aborts the batch.

Default ``--workers`` is capped at ``min(3, cpu_count - 2)`` rather than the
fleet-scan's ``max(2, cpu_count - 2)``: each worker's ``load_gauge_series``
call can hit ``src.data.pet.fetch_station_pet`` (Open-Meteo archive HTTP),
which the Stage-5 fleet scan proved goes 429-prone under higher parallelism.
Once the PET cache is warm (every gauge after its first monthly run), the
workload is compute-bound and this cap just leaves a little parallelism on
the table in exchange for not tripping the rate limit — acceptable for a
monthly job.

Run with the pastas venv python from the repo root:
    .venv-pastas\\Scripts\\python.exe -m scripts.build_flow_models
    .venv-pastas\\Scripts\\python.exe -m scripts.build_flow_models --limit 5   # smoke test
    .venv-pastas\\Scripts\\python.exe -m scripts.build_flow_models --workers 1 # force serial
"""
from __future__ import annotations

import os

# Set BEFORE numpy/pastas import in every process (main AND each spawned
# worker re-executes this module top-to-bottom): one pastas fit per gauge is
# already CPU-bound, and BLAS/numba each defaulting to multi-threaded inside
# EVERY worker process oversubscribes the machine's cores badly under
# ProcessPoolExecutor. Pin each worker to one thread; the parallelism comes
# from the process pool, not from threads inside each process.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import argparse
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
    resolve_flow_pilot_path,
)
from src.forecast.pastas import recharge as R
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
PILOT_PATH = ROOT / "data" / "processed" / "flow_pilot.csv"

# PET fetch (network, Open-Meteo archive) is 429-prone at high parallelism —
# proven live during the Stage-5 fleet scan. Capped lower than the fleet
# scan's default so a monthly recalibration run doesn't hammer the archive;
# steady state (PET already cached from a prior run) is compute-bound and
# would tolerate more, but there is no cheap way to tell "warm" from "cold"
# up front, so the cap is unconditional.
_DEFAULT_WORKERS = min(3, max(1, (os.cpu_count() or 4) - 2))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pilot", default=None,
                    help="pilot CSV path; defaults to config's "
                         "forecast.ensemble.flow.pilot_path, falling back "
                         f"to {PILOT_PATH} — resolved in run() once cfg is "
                         "loaded, matching build_flow_seasonal_shadow.py / "
                         "refresh_seasonal_inputs.py / the flow ENS bridge "
                         "(all four flow-pilot consumers agree)")
    ap.add_argument("--links", default=str(ROOT / FLOW_LINKS_PATH))
    ap.add_argument("--catalogue", default=str(ROOT / FLOW_CATALOGUE_PATH))
    ap.add_argument("--limit", type=int, default=None,
                    help="only calibrate the first N pilot gauges, sorted by "
                         "gauge_id (smoke test)")
    ap.add_argument("--workers", type=int, default=_DEFAULT_WORKERS,
                    help="ProcessPoolExecutor worker count; <=1 forces serial "
                         "(default: %(default)s, capped for PET-fetch "
                         "rate-limit safety — see module docstring)")
    return ap.parse_args(argv)


def compute_q95(q: pd.Series) -> float:
    """5th percentile of the gauge's FULL daily flow record (m3/s) — the
    low-flow threshold (the flow exceeded 95% of the time). A winterbourne
    whose record is all-zero (or mostly zero) legitimately yields Q95==0.0 —
    not an error; an all-NaN/empty series also returns 0.0 rather than NaN so
    a downstream breach test degrades to "always below" instead of silently
    disabling itself.
    """
    qn = pd.to_numeric(pd.Series(q), errors="coerce").dropna()
    if qn.empty:
        return 0.0
    return float(qn.quantile(0.05))


# ---------------------------------------------------------------------------
# Worker process state — populated ONCE per process via ProcessPoolExecutor's
# initializer, not per task (mirrors scripts/flow_fleet_scan.py).
# ---------------------------------------------------------------------------

_W_LINKS_DF: pd.DataFrame | None = None
_W_CAT_DF: pd.DataFrame | None = None
_W_MEASURE_MAP: dict[str, str] | None = None
_W_CFG: dict | None = None
_W_FCFG: dict | None = None
_W_LOCK = None   # multiprocessing lock guarding shared rain-raw downloads


def _worker_init(links_path: str, catalogue_path: str, cfg: dict, fcfg: dict,
                 download_lock) -> None:
    """Runs once per worker process (module-level function -> picklable by
    reference, required for Windows spawn)."""
    global _W_LINKS_DF, _W_CAT_DF, _W_MEASURE_MAP, _W_CFG, _W_FCFG, _W_LOCK
    _W_LINKS_DF = pd.read_csv(links_path, dtype=str).set_index("GaugeID")
    _W_CAT_DF = pd.read_csv(catalogue_path, dtype={"station_id": str}).set_index("station_id")
    _W_MEASURE_MAP = load_flow_measure_map(Path(links_path))
    _W_CFG = cfg
    _W_FCFG = fcfg
    _W_LOCK = download_lock


def _calibrate_one_gauge_inner(gauge_id: str) -> dict:
    name = gauge_id
    try:
        if _W_CAT_DF is not None and gauge_id in _W_CAT_DF.index:
            name = _W_CAT_DF.loc[gauge_id, "station_name"]
    except Exception:
        pass

    try:
        loaded = load_gauge_series(gauge_id, _W_LINKS_DF, _W_CAT_DF, _W_MEASURE_MAP,
                                   _W_CFG, download_lock=_W_LOCK)
    except Exception as exc:
        return {"gauge_id": gauge_id, "name": name, "rec": None,
               "skip_reason": f"load error: {exc}"}

    if loaded is None:
        return {"gauge_id": gauge_id, "name": name, "rec": None,
               "skip_reason": "no_data"}
    q, prec, evap = loaded

    try:
        rec = R.calibrate_flow(gauge_id, q, prec, evap,
                               rfunc=_W_FCFG.get("rfunc", "Gamma"),
                               recharge=_W_FCFG.get("recharge", "FlexModel"),
                               precip_source="gauge")
    except Exception as exc:
        return {"gauge_id": gauge_id, "name": name, "rec": None,
               "skip_reason": f"calibration error: {exc}"}

    rec["q95_m3s"] = round(compute_q95(q), 4)
    return {"gauge_id": gauge_id, "name": name, "rec": rec, "skip_reason": None}


def calibrate_one_flow_gauge(gauge_id: str) -> dict:
    """Module-level, picklable worker entry point. Belt-and-braces outer
    catch on top of ``_calibrate_one_gauge_inner``'s own guards — NOTHING
    escapes this function as an exception; a bad gauge always degrades to a
    skip entry, it never aborts the batch."""
    try:
        return _calibrate_one_gauge_inner(gauge_id)
    except Exception as exc:                                     # pragma: no cover
        return {"gauge_id": gauge_id, "name": gauge_id, "rec": None,
               "skip_reason": f"worker error: {type(exc).__name__}: {exc}"}


def run(args: argparse.Namespace, cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    fcfg = cfg.get("forecast", {}).get("ensemble", {}).get("flow", {})
    if not fcfg.get("enabled", True):
        print("forecast.ensemble.flow.enabled = false — nothing to do")
        return 0

    # --pilot explicit > config forecast.ensemble.flow.pilot_path > the
    # on-disk default (PILOT_PATH) — resolve_flow_pilot_path is the same
    # helper build_flow_seasonal_shadow.py / refresh_seasonal_inputs.py /
    # the flow ENS bridge call site use, so all four flow-pilot consumers
    # agree on where the pilot CSV lives.
    pilot_path = (Path(args.pilot) if args.pilot is not None
                 else resolve_flow_pilot_path(cfg, ROOT))
    if not pilot_path.exists():
        print(f"{pilot_path} not found — flow model calibration skipped "
              f"(run 'python -m scripts.select_flow_pilot' to enable it on "
              f"this host).")
        return 0

    links_path = Path(args.links)
    catalogue_path = Path(args.catalogue)
    if not links_path.exists() or not catalogue_path.exists():
        print(f"{links_path} / {catalogue_path} not found — flow model "
              f"calibration skipped (run 'python -m scripts.build_flow_catalogue').")
        return 0

    pilot = pd.read_csv(pilot_path, dtype={"gauge_id": str})
    if pilot.empty:
        print(f"{pilot_path} is empty — nothing to calibrate.")
        return 0
    gauge_ids = sorted(pilot["gauge_id"])
    if args.limit is not None:
        gauge_ids = gauge_ids[: args.limit]

    workers = getattr(args, "workers", _DEFAULT_WORKERS)
    print(f"Flow model calibration: {len(gauge_ids)} pilot gauge(s)"
          + (f" (--limit {args.limit})" if args.limit is not None else "")
          + f"  (workers={workers})")

    results: dict[str, dict] = {}

    def _run_serial(ids: list[str]) -> None:
        _worker_init(str(links_path), str(catalogue_path), cfg, fcfg, None)
        for gauge_id in ids:
            results[gauge_id] = calibrate_one_flow_gauge(gauge_id)

    t0 = time.time()
    if workers <= 1:
        print("Running serial (--workers <= 1).")
        _run_serial(gauge_ids)
    else:
        manager = mp.Manager()
        download_lock = manager.Lock()
        try:
            with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_worker_init,
                initargs=(str(links_path), str(catalogue_path), cfg, fcfg, download_lock),
            ) as ex:
                futures = {ex.submit(calibrate_one_flow_gauge, g): g for g in gauge_ids}
                done_count = 0
                for fut in as_completed(futures):
                    gauge_id = futures[fut]
                    try:
                        result = fut.result()
                    except BrokenProcessPool:
                        # Must NOT become a per-gauge skip entry: a broken pool
                        # fails EVERY pending future at once — re-raise so the
                        # caller falls back to serial for whatever is left,
                        # instead of quietly losing the rest of the fleet.
                        raise
                    except Exception as exc:
                        result = {"gauge_id": gauge_id, "name": gauge_id, "rec": None,
                                 "skip_reason": f"future exception: {type(exc).__name__}: {exc}"}
                    results[gauge_id] = result
                    done_count += 1
                    print(f"  [{done_count}/{len(gauge_ids)}] {gauge_id[:8]} done")
        except BrokenProcessPool as exc:
            print(f"WARNING: worker pool broke ({exc}) — falling back to "
                 f"serial for the remaining gauges.", file=sys.stderr)
            remaining = [g for g in gauge_ids if g not in results]
            _run_serial(remaining)
    elapsed = time.time() - t0
    per_gauge = elapsed / len(gauge_ids) if gauge_ids else float("nan")
    print(f"Session done: {len(gauge_ids)} gauge(s) in {elapsed:.1f}s "
         f"({per_gauge:.2f}s/gauge at workers={workers})")

    # Build the final recs/skipped in the same canonical (sorted gauge_id)
    # order regardless of completion order, so the model store and console
    # output stay identical between serial and parallel runs.
    recs: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []
    for gauge_id in gauge_ids:
        result = results.get(gauge_id) or {
            "gauge_id": gauge_id, "name": gauge_id, "rec": None,
            "skip_reason": "missing result (internal error)",
        }
        rec = result.get("rec")
        if rec is None:
            skipped.append((gauge_id, result.get("skip_reason") or "unknown"))
            continue
        recs[gauge_id] = rec
        name = result.get("name", gauge_id)
        print(f"  {gauge_id[:8]}  {str(name)[:24]:<24}  n={rec['n_obs']:5d}  "
             f"EVP={rec['evp']:5.1f}%  Q95={rec['q95_m3s']:.4f} m3/s")

    models_cache = ROOT / fcfg.get("models_cache", "data/model/flow_models.json")
    out = R.save_models(recs, models_cache)
    print(f"\nCalibrated {len(recs)} flow model(s) -> {out.relative_to(ROOT)}")
    for gauge_id, why in skipped:
        print(f"  skipped {gauge_id[:8]}: {why}")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(parse_args())


if __name__ == "__main__":
    force_utf8_stdio()
    raise SystemExit(main())

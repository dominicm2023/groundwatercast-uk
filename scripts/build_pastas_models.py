"""Calibrate & cache production Pastas TFN recharge models for every borehole
in the configured forecast scope (config ``forecast.ensemble.pastas.scope``).

Module 1 of the "Production Pastas recharge" build (docs/backlog.md → In progress).
Calibrates one Pastas FlexModel per in-scope borehole on ALL available history
and writes the small JSON model cache that downstream stages (ensemble driver,
aggregation) will load — the main GW-pipeline env never needs pastas.

Run with the pastas venv python from the repo root:
  .venv-pastas\\Scripts\\python -m scripts.build_pastas_models
  .venv-pastas\\Scripts\\python -m scripts.build_pastas_models --workers 1  # force serial

Prereq: PET cached per borehole in data/raw/pet/<sid>.csv (main-env step:
``python -m scripts.refresh_pet --scope <scope>``). Boreholes without a PET
cache are skipped.

Parallel via ``ProcessPoolExecutor``, same discipline as
``scripts/flow_fleet_scan.py`` (Stage 5) and its sibling
``scripts/build_flow_models.py``: module-level picklable worker functions, a
per-process initializer that loads the joined timeseries / catalogue /
station-links CSVs ONCE per worker (not per borehole — each borehole's rain
and PET series are then read independently by the worker from disk, so no
DataFrame needs to cross the pickled task boundary), and a BrokenProcessPool
handler that falls back to serial for the remaining boreholes rather than
losing the run. A single bad borehole always degrades to a skip entry, never
aborts the batch. Default ``--workers`` matches the fleet scan
(``max(2, cpu_count - 2)``) — unlike ``build_flow_models``, borehole
calibration has no network call in the hot loop (PET is pre-cached by
``refresh_pet``, rainfall is read from already-downloaded raw archives), so
there is no rate-limit reason to cap it lower.
"""
from __future__ import annotations

import os

# Set BEFORE numpy/pastas import in every process (main AND each spawned
# worker re-executes this module top-to-bottom): one pastas fit per borehole
# is already CPU-bound, and BLAS/numba each defaulting to multi-threaded
# inside EVERY worker process oversubscribes the machine's cores badly under
# ProcessPoolExecutor. Pin each worker to one thread; the parallelism comes
# from the process pool, not from threads inside each process.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pandas as pd

from src.forecast.ensemble.members import gauge_rainfall_for
from src.forecast.ensemble.scope import (MIN_ROWS, MIN_ROWS_FAN, select_scope,
                                         short_record_ids)
from src.forecast.pastas import recharge as R
from src.forecast.pastas import screen
from src.forecast.pastas.io import load_pet

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "data/features/joined_timeseries.csv"
CATALOGUE = ROOT / "data/processed/catalogue.csv"
LINKS = ROOT / "data/processed/station_links.csv"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["user", "live", "fleet"], default=None,
                    help="borehole scope (default: config forecast.ensemble.pastas.scope)")
    ap.add_argument("--workers", type=int,
                    default=max(2, (os.cpu_count() or 4) - 2),
                    help="ProcessPoolExecutor worker count; <=1 forces serial")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Worker process state — populated ONCE per process via ProcessPoolExecutor's
# initializer, not per task (mirrors scripts/flow_fleet_scan.py): the joined
# timeseries CSV read (the single biggest per-process cost besides the fit
# itself) amortises across every borehole a given worker handles over the
# run, instead of repeating per borehole.
# ---------------------------------------------------------------------------

_W_JOINED: pd.DataFrame | None = None
_W_CAT: pd.DataFrame | None = None
_W_LINKS: pd.DataFrame | None = None
_W_RAW_ROOT: str | None = None
_W_PCFG: dict | None = None
_W_GCFG: dict | None = None


def _worker_init(joined_path: str, catalogue_path: str, links_path: str | None,
                 raw_root: str, pcfg: dict, gcfg: dict) -> None:
    """Runs once per worker process (module-level function -> picklable by
    reference, required for Windows spawn)."""
    global _W_JOINED, _W_CAT, _W_LINKS, _W_RAW_ROOT, _W_PCFG, _W_GCFG
    _W_JOINED = pd.read_csv(joined_path, index_col=0, parse_dates=True)
    _W_CAT = (pd.read_csv(catalogue_path).query("measure_type == 'groundwater'")
             .drop_duplicates("station_id").set_index("station_id"))
    _W_LINKS = (pd.read_csv(links_path).drop_duplicates("GWStationID")
               .set_index("GWStationID")
               if links_path and Path(links_path).exists() else None)
    _W_RAW_ROOT = raw_root
    _W_PCFG = pcfg
    _W_GCFG = gcfg


def _calibrate_one_inner(sid: str) -> dict:
    joined, cat, links = _W_JOINED, _W_CAT, _W_LINKS
    pcfg, gcfg = _W_PCFG, _W_GCFG

    g = joined[joined["station_id"] == sid].sort_index()
    head = g["GW_Level"].dropna()
    if len(head) < MIN_ROWS_FAN or sid not in cat.index:
        return {"sid": sid, "rec": None,
               "skip_reason": f"insufficient history ({len(head)})"}
    is_short = len(head) < MIN_ROWS
    evap = load_pet(sid)
    if evap is None:
        return {"sid": sid, "rec": None, "skip_reason": "no PET cache"}
    prec = gauge_rainfall_for(sid, links, _W_RAW_ROOT)
    precip_source = "gauge" if not prec.empty else "joined"
    if prec.empty:
        prec = g["Rainfall"]

    # Short-record admission gate: gauge rainfall only (the joined-fallback
    # zero-fills gaps → fabricates drought → far worse fits), then a
    # leakage-safe hindcast must clear the coverage/error bar. A failure is
    # dropped to status-only rather than publishing an unbacktested fan.
    hindcast = None
    if is_short:
        if precip_source != "gauge":
            return {"sid": sid, "rec": None,
                   "skip_reason": "short-record: no gauge rainfall"}
        hindcast = screen.leakage_safe_hindcast(
            sid, head, prec, evap, rfunc=pcfg["rfunc"],
            recharge=pcfg["recharge"], precip_source=precip_source,
            origins_back=tuple(gcfg.get("origins_back", screen.ORIGINS_BACK)),
            window=gcfg.get("window", screen.WINDOW),
            min_origins=gcfg.get("min_origins", screen.MIN_ORIGINS),
            target_coverage_pct=gcfg.get("target_coverage_pct", screen.TARGET_COVERAGE_PCT),
            min_coverage_pct=gcfg.get("min_coverage_pct", screen.MIN_COVERAGE_PCT),
            max_skill_ratio=gcfg.get("max_skill_ratio", screen.MAX_SKILL_RATIO),
            max_band_frac=gcfg.get("max_band_frac", screen.MAX_BAND_FRAC))
        if not hindcast["gate_pass"]:
            return {"sid": sid, "rec": None,
                   "skip_reason": f"short-record gate fail: {hindcast['reason']}"}

    try:
        rec = R.calibrate(sid, head, prec, evap,
                          rfunc=pcfg["rfunc"], recharge=pcfg["recharge"],
                          precip_source=precip_source)
    except Exception as exc:
        return {"sid": sid, "rec": None, "skip_reason": f"calibration error: {exc}"}
    rec["short_record"] = is_short
    if hindcast is not None:
        rec["hindcast"] = hindcast
        # Band-widening: the published fan uses the calibrated σ-inflation
        # (recharge.simulate_path reads rec["sigma_inflation"]). Full-record
        # models carry none → 1.0 → unchanged.
        rec["sigma_inflation"] = hindcast.get("sigma_inflation", 1.0)
    return {"sid": sid, "rec": rec, "skip_reason": None}


def calibrate_one_borehole(sid: str) -> dict:
    """Module-level, picklable worker entry point. Belt-and-braces outer
    catch on top of ``_calibrate_one_inner``'s own guards — NOTHING escapes
    this function as an exception; a bad borehole always degrades to a skip
    entry, it never aborts the batch. (The serial script this replaces let
    an unexpected exception outside the ``R.calibrate`` call — e.g. inside
    ``gauge_rainfall_for`` or ``screen.leakage_safe_hindcast`` — crash the
    whole run; this outer guard is a deliberate robustness improvement that
    applies equally in the serial (``--workers 1``) path, since both paths
    call this same function.)"""
    try:
        return _calibrate_one_inner(sid)
    except Exception as exc:                                     # pragma: no cover
        return {"sid": sid, "rec": None,
               "skip_reason": f"worker error: {type(exc).__name__}: {exc}"}


def run(args: argparse.Namespace, cfg: dict | None = None, *,
       ids: list[str] | None = None,
       joined_path: Path | None = None,
       catalogue_path: Path | None = None,
       links_path: Path | None = None) -> int:
    """Driver, separated from ``main()`` for testability.

    ``ids``/``joined_path``/``catalogue_path``/``links_path`` are test-only
    overrides (no CLI flag): production always resolves ``ids`` from
    ``select_scope`` and reads the module-level ``JOINED``/``CATALOGUE``/
    ``LINKS`` paths, exactly as the pre-parallel script did.
    """
    cfg = cfg if cfg is not None else json.loads((ROOT / "config/config.json").read_text())
    pcfg = cfg["forecast"]["ensemble"]["pastas"]
    if not pcfg.get("enabled", True):
        print("forecast.ensemble.pastas.enabled = false — nothing to do"); return 0
    scope = args.scope or pcfg.get("scope", "live")

    # Short-record fan tier: also calibrate [MIN_ROWS_FAN, MIN_ROWS) boreholes,
    # each admitted only behind the gauge-rainfall + leakage-safe hindcast gate
    # (src.forecast.pastas.screen). Config toggle so it can be disabled wholesale.
    srcfg = pcfg.get("short_record", {})
    short_enabled = bool(srcfg.get("enabled", True))
    gcfg = srcfg.get("gate", {})

    if ids is None:
        ids = sorted(select_scope(scope, include_short=short_enabled))
        short_cands = short_record_ids() if short_enabled else set()
    else:
        ids = sorted(ids)
        short_cands = set()
    print(f"Scope={scope}: {len(ids)} boreholes ({len(short_cands & set(ids))} "
          f"short-record candidates)  |  rfunc={pcfg['rfunc']} "
          f"recharge={pcfg['recharge']}  (workers={args.workers})")

    # Calibrate on the SAME rainfall the fan is driven with — the raw
    # top-3-gauge series (src.forecast.ensemble.members.observed_daily_rainfall,
    # reaches ~today), not the joined CSV's GW-date-limited Rainfall column
    # (indexed on groundwater-observation dates, so every gap — including the
    # weeks-stale archive tail — was zero-filled by recharge._daily and driven
    # a fitted recharge gain against a fabricated drought). Falls back to the
    # joined column only when a station has no rain-gauge link, so it stays
    # calibratable rather than silently dropping out.
    raw_root = cfg["download"]["raw_root"]
    joined_path = joined_path or JOINED
    catalogue_path = catalogue_path or CATALOGUE
    if links_path is None:
        links_path = LINKS if LINKS.exists() else None

    if not ids:
        print("Nothing in scope.")
        return 0

    results: dict[str, dict] = {}

    def _run_serial(sids: list[str]) -> None:
        _worker_init(str(joined_path), str(catalogue_path),
                    str(links_path) if links_path else None,
                    raw_root, pcfg, gcfg)
        for sid in sids:
            results[sid] = calibrate_one_borehole(sid)

    t0 = time.time()
    if args.workers <= 1:
        print("Running serial (--workers <= 1).")
        _run_serial(ids)
    else:
        try:
            with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=_worker_init,
                initargs=(str(joined_path), str(catalogue_path),
                         str(links_path) if links_path else None,
                         raw_root, pcfg, gcfg),
            ) as ex:
                futures = {ex.submit(calibrate_one_borehole, sid): sid for sid in ids}
                done_count = 0
                for fut in as_completed(futures):
                    sid = futures[fut]
                    try:
                        result = fut.result()
                    except BrokenProcessPool:
                        # Must NOT become a per-borehole skip entry: a broken
                        # pool fails EVERY pending future at once — re-raise
                        # so the caller falls back to serial for whatever is
                        # left, instead of quietly losing the rest of the run.
                        raise
                    except Exception as exc:
                        result = {"sid": sid, "rec": None,
                                 "skip_reason": f"future exception: {type(exc).__name__}: {exc}"}
                    results[sid] = result
                    done_count += 1
                    print(f"  [{done_count}/{len(ids)}] {sid[:8]} done")
        except BrokenProcessPool as exc:
            print(f"WARNING: worker pool broke ({exc}) — falling back to "
                 f"serial for the remaining boreholes.", file=sys.stderr)
            remaining = [sid for sid in ids if sid not in results]
            _run_serial(remaining)
    elapsed = time.time() - t0
    per_sid = elapsed / len(ids) if ids else float("nan")
    print(f"Session done: {len(ids)} borehole(s) in {elapsed:.1f}s "
         f"({per_sid:.2f}s/borehole at workers={args.workers})")

    # Build the final recs/skipped in the same canonical (sorted station-id)
    # order regardless of completion order, so the model store and console
    # output stay identical between serial and parallel runs.
    recs: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []
    n_gauge = n_joined = 0
    n_short = 0
    for sid in ids:
        result = results.get(sid) or {"sid": sid, "rec": None,
                                      "skip_reason": "missing result (internal error)"}
        rec = result.get("rec")
        if rec is None:
            skipped.append((sid, result.get("skip_reason") or "unknown"))
            continue
        precip_source = rec.get("precip_source", "joined")
        n_gauge += precip_source == "gauge"
        n_joined += precip_source == "joined"
        is_short = bool(rec.get("short_record"))
        n_short += is_short
        recs[sid] = rec
        hindcast = rec.get("hindcast")
        tag = (f"  SHORT gate✓ σ×{hindcast['sigma_inflation']:.1f} "
               f"cov {hindcast['base_cov14']:.0f}→{hindcast['cov14']:.0f}% "
               f"band/rng={hindcast['band_frac']:.0%}"
               if is_short and hindcast else "")
        print(f"  {sid[:8]}  n={rec['n_obs']:5d}  EVP={rec['evp']:5.1f}%  "
              f"sigma={rec['sigma']:.3f}m  alpha={rec['alpha']:.0f}d  "
              f"precip={precip_source}{tag}")
    print(f"\nPrecip source: {n_gauge} gauge, {n_joined} joined-fallback "
          f"(no gauge link or no gauge data)")
    print(f"Short-record fan tier: {n_short} admitted (gated); "
          f"full-record: {len(recs) - n_short}")

    out = R.save_models(recs, ROOT / pcfg["models_cache"])
    print(f"\nCalibrated {len(recs)} models -> {out.relative_to(ROOT)}")
    for sid, why in skipped:
        print(f"  skipped {sid[:8]}: {why}")

    # AR1 residual-fit diagnostic (roadmap 0.3) — report-only. Flags stations
    # whose calibrated noise breaks the AR1 assumption the predictive band rests
    # on (autocorrelated / seasonal / heteroscedastic innovations).
    qa_rows = [{"station_id": sid, **rec["noise_qa"]}
               for sid, rec in recs.items() if rec.get("noise_qa")]
    if qa_rows:
        qa_df = pd.DataFrame(qa_rows)
        qa_path = ROOT / "outputs" / "noise_qa.csv"
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        qa_df.to_csv(qa_path, index=False)
        flagged = qa_df[~qa_df["passes"]]
        print(f"\nNoise QA -> {qa_path.relative_to(ROOT)}  "
              f"({len(flagged)}/{len(qa_df)} flagged)")
        for _, r in flagged.iterrows():
            print(f"  ! {r['station_id'][:8]}: {r['flags']}  "
                  f"(lag1={r['lag1_autocorr']:.2f} seas={r['seasonal_frac']:.2f} "
                  f"het={r['hetero_corr']:.2f}, basis={r['basis']})")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

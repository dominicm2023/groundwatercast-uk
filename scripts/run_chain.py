"""Deterministic stage runner for the post-pipeline refresh chain.

The refresh scripts that run on top of the GW pipeline have a strict
order — ``STAGES`` encodes it declaratively: the list IS the executable
documentation of the dependency chain (each stage's ``why`` field says
what it depends on). Prose overview: docs/architecture.md §2.

Usage (always from the repo root, main venv)::

    python -m scripts.run_chain --list             # show the default (core) plan
    python -m scripts.run_chain --all --dry-run    # print every command, run nothing
    python -m scripts.run_chain --core             # the default
    python -m scripts.run_chain --live --ensemble  # combine groups
    python -m scripts.run_chain --all --from build_ensemble_members

Groups (step numbers in brackets):

    --core      [1-4]      derived-artefact rebuild after joined_timeseries.csv
                changes (shards, freshness, monthly normals)
    --xref      [7, 7b]    EA flood-monitoring cross-refs (after catalogue rebuild)
    --live      [8, 8b]    hourly live-feed refresh (GW -> shards; rainfall tail)
    --ensemble  [8d, 8e]   probabilistic ensemble (daily)
    --pastas    [8e-pre, 8f, 8g, 8h]  Pastas TFN forecast (8f-8h need .venv-pastas)
    --seasonal  [9, 9b]    seasonal outlook, months 1-6 (MONTHLY cadence —
                run after SEAS5's update on the 5th; 9b needs .venv-pastas)
    --publish   [10]       assemble the published artifact pack
                (docs/artifact_contract.md) — pure read, run LAST
    --forecast  [8d, 8e, 8e-pre, 8g, 8h]  the DAILY forecast refresh — ensemble +
                pastas WITHOUT 8f (build_pastas_models recalibration is a
                retrain, not a refresh; run --pastas for that). Cron pairs it
                with the pack: `run_chain --forecast --publish`.
    --all       everything above, in documented order

Stages run with ``cwd`` = repo root, streaming output, and stop on the
first failure (downstream stages are never run after a failure).
Stdlib-only on purpose: this must run before any environment is rebuilt.
"""

from __future__ import annotations

import argparse
import atexit
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Repo root = parent of the scripts/ directory containing this file.
REPO_ROOT = Path(__file__).resolve().parents[1]

# Cross-run mutual exclusion. The cron schedule can overlap itself (an hourly
# --live against a long daily --forecast --publish, or a slow previous instance
# of the same job): the refresh scripts rewrite parquet shards / CSVs in place,
# so two concurrent chains race — a torn shard read aborts the pack build, and
# a stale-read write-back silently clobbers a fresh rebuild. One repo-level
# lock serialises them; a colliding run exits 3 (the next scheduled run
# catches up). Stale locks (a killed run) are stolen after LOCK_STALE_S.
LOCK_PATH = REPO_ROOT / "outputs" / "run_chain.lock"
LOCK_STALE_S = 6 * 3600


def _acquire_lock() -> bool:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"pid={os.getpid()} started="
                         f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
            except OSError:
                continue                      # vanished between open and stat — retry
            if age <= LOCK_STALE_S:
                return False
            print(f"WARNING: stealing stale run_chain lock (age {age / 3600:.1f} h) "
                  f"— a previous run died without releasing it.", file=sys.stderr)
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass
    return False


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass

# Interpreter markers — resolved to a real executable at run time.
MAIN_ENV = "main"      # the interpreter running this script (main GW-pipeline venv)
PASTAS_ENV = "pastas"  # .venv-pastas — numba/llvmlite stack, never the main env

# Relative locations of the dedicated pastas venv interpreter (CLAUDE.md
# "Pastas TFN forecast" setup snippet creates it with `python -m venv .venv-pastas`).
PASTAS_VENV_CANDIDATES = (
    Path(".venv-pastas") / "Scripts" / "python.exe",  # Windows
    Path(".venv-pastas") / "bin" / "python",          # POSIX fallback
)


@dataclass(frozen=True)
class Stage:
    """One step of the documented refresh chain.

    name:  unique short name (the script/module basename) — used by --from/--to
    step:  the CLAUDE.md step number, for traceability back to the prose spec
    argv:  arguments passed to the interpreter (module-form ``-m pkg.mod`` or
           path-form ``scripts/foo.py`` — preserved exactly as documented)
    group: which selection flag pulls this stage in (core/xref/live/ensemble/pastas)
    env:   MAIN_ENV or PASTAS_ENV — which interpreter runs it
    why:   one-line reason this stage sits at this position in the order
    """

    name: str
    step: str
    argv: tuple
    group: str
    env: str
    why: str


# ---------------------------------------------------------------------------
# THE CHAIN.  Order here == documented run order in CLAUDE.md "How to run".
# Do not reorder without updating CLAUDE.md (and vice versa).
# ---------------------------------------------------------------------------
STAGES = (
    # -- core [steps 1-6]: full rebuild order any time joined_timeseries.csv changes
    Stage("v15_build_dipped_daily_series", "1",
          ("-m", "scripts.v15_build_dipped_daily_series"), "core", MAIN_ENV,
          "MUST run first: re-merge dipped data wiped by the GW features stage (the documented v1.6 landmine)"),
    Stage("v15_build_per_station_parquet", "2",
          ("-m", "scripts.v15_build_per_station_parquet"), "core", MAIN_ENV,
          "performance-critical fast path; shards the (now dipped-complete) joined CSV per borehole"),
    Stage("v15_build_gw_freshness", "3",
          ("-m", "scripts.v15_build_gw_freshness"), "core", MAIN_ENV,
          "reads from the Parquet shards built in step 2"),
    Stage("build_gw_normals", "4",
          ("-m", "scripts.build_gw_normals"), "core", MAIN_ENV,
          "monthly quantile ladder per borehole — the vs-normal yardstick for status, terciles and tiers"),

    # -- diagnostics [step D1]: data-quality screen over the joined series.
    #    Report-only (Tier 1) — writes outputs/trend_flags.csv, changes nothing
    #    in the forecast. In --all (and --diagnostics), NOT in --core. Re-run
    #    after any joined_timeseries rebuild / retrain. See docs/trend_screen.md.
    Stage("build_trend_screen", "D1",
          ("-m", "scripts.build_trend_screen"), "diagnostics", MAIN_ENV,
          "flag boreholes with strong multi-year trends that break the stationary forecast; reads the joined series (needs core)"),

    # -- xref [steps 7, 7b]: only needed after the catalogue changes
    Stage("flood_monitoring_xref", "7",
          ("-m", "src.diagnostics.flood_monitoring_xref"), "xref", MAIN_ENV,
          "rebuild EA flood-monitoring GW matches after a catalogue rebuild"),
    Stage("rainfall_monitoring_xref", "7b",
          ("-m", "src.diagnostics.rainfall_monitoring_xref"), "xref", MAIN_ENV,
          "rebuild EA RAINFALL matches (v1.9); prerequisite for the live rainfall refresh (8b)"),

    # -- live [steps 8, 8b, 8c]: hourly cron chain
    Stage("v16_refresh_live_gw", "8",
          ("scripts/v16_refresh_live_gw.py",), "live", MAIN_ENV,  # path-form invocation, per CLAUDE.md
          "pull live GW readings (hourly); feeds the live tail seed"),
    Stage("v19_refresh_live_rainfall", "8b",
          ("-m", "scripts.v19_refresh_live_rainfall"), "live", MAIN_ENV,
          "extend raw rainfall tail (closes Recharge_Weibull staleness); needs xref 7b"),

    # -- ensemble [steps 8d, 8e]: daily probabilistic forecast
    Stage("build_ensemble_members", "8d",
          ("-m", "scripts.build_ensemble_members"), "ensemble", MAIN_ENV,
          "per-member GW trajectories; needs live rainfall (8b)"),
    Stage("build_ensemble_summary", "8e",
          ("-m", "scripts.build_ensemble_summary"), "ensemble", MAIN_ENV,
          "aggregates 8d into breach prob + fan; run AFTER 8d"),

    # -- pastas [steps 8e-pre, 8f, 8g, 8h]: calibrated TFN forecast.
    #    8f-8h run in the DEDICATED .venv-pastas interpreter; 8e-pre is main-env.
    Stage("refresh_pet", "8e-pre",
          ("-m", "scripts.refresh_pet"), "pastas", MAIN_ENV,
          "main-env: cache PET (ET0) for the CONFIGURED scope (refresh_pet defaults "
          "to forecast.ensemble.pastas.scope); prerequisite for calibration (8f)"),
    Stage("build_pastas_models", "8f",
          ("-m", "scripts.build_pastas_models"), "pastas", PASTAS_ENV,
          "calibrate per-BH Pastas models; needs joined timeseries + PET cache (8e-pre)"),
    Stage("build_pastas_members", "8g",
          ("-m", "scripts.build_pastas_members"), "pastas", PASTAS_ENV,
          "drive models with ensemble member rainfall; run AFTER 8d (member rainfall) + 8f (models)"),
    Stage("build_pastas_summary", "8h",
          ("-m", "scripts.build_pastas_summary"), "pastas", PASTAS_ENV,
          "fan + breach prob + roll/Pastas spread; run AFTER 8g; also reads 8d members + gw_monthly_normals.csv"),

    # -- seasonal [steps 9, 9b]: monthly cadence (after SEAS5's update on the
    #    5th). Step 9 fetches/caches (main env); 9b is pure compute (pastas env).
    Stage("refresh_seasonal_inputs", "9",
          ("-m", "scripts.refresh_seasonal_inputs"), "seasonal", MAIN_ENV,
          "ERA5 precip + PET backfill + SEAS5 payloads for the ESP traces (monthly)"),
    Stage("build_seasonal_outlook", "9b",
          ("-m", "scripts.build_seasonal_outlook"), "seasonal", PASTAS_ENV,
          "ESP traces through the calibrated models, SEAS5-weighted terciles (monthly; AFTER 9)"),

    # -- publish [step 10]: the versioned static pack (docs/artifact_contract.md).
    #    Pure read of existing artefacts — run LAST so it packages this run's
    #    outputs (cron: `run_chain --forecast --publish`).
    Stage("build_artifact_pack", "10",
          ("-m", "scripts.build_artifact_pack"), "publish", MAIN_ENV,
          "assemble outputs/pack (geojson + per-station JSON) from existing artefacts; pure-read"),

    # -- OG share cards [step 10b]: status-neutral 1200x630 PNGs per borehole,
    #    content-hash filenames + manifest. BEFORE the stubs (they embed the
    #    og:image URLs). Soft-skips (exit 0, no manifest) when resvg_py absent.
    Stage("build_og_cards", "10b",
          ("-m", "scripts.build_og_cards"), "publish", MAIN_ENV,
          "render per-borehole OG share cards + manifest (pure-read, AFTER 10, BEFORE 11)"),

    # -- SEO stubs [step 11]: per-borehole static pages + /browse + sitemap +
    #    robots, from the pack. Pure read; runs LAST so a stub issue can't undo
    #    the already-written pack (the chain aborts after it, not before).
    Stage("build_seo_stubs", "11",
          ("-m", "scripts.build_seo_stubs"), "publish", MAIN_ENV,
          "emit per-borehole /b/<slug>/ SEO stubs + /browse + sitemap.xml + robots.txt (pure-read, AFTER 10)"),
)

GROUPS = ("core", "diagnostics", "xref", "live", "ensemble", "pastas", "seasonal", "publish")

# Virtual groups: named stage SUBSETS that cut across the physical groups
# above (still executed in STAGES order). "forecast" is the daily forecast
# refresh — ensemble + the daily pastas stages, deliberately EXCLUDING
# build_pastas_models (8f): recalibration is a retrain, not a refresh.
VIRTUAL_GROUPS = {
    "forecast": ("build_ensemble_members", "build_ensemble_summary",
                 "refresh_pet", "build_pastas_members", "build_pastas_summary"),
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_run_chain.py — keep them I/O-free)
# ---------------------------------------------------------------------------

def select_stages(groups, from_name=None, to_name=None):
    """Return the ordered stage plan for the requested groups.

    ``groups`` is an iterable of group names ("core", "pastas", ...).
    ``from_name``/``to_name`` slice the selected plan inclusively by stage
    name.  Raises ValueError on unknown groups or names not in the plan.
    """
    groups = set(groups)
    unknown = groups - set(GROUPS) - set(VIRTUAL_GROUPS)
    if unknown:
        raise ValueError(f"unknown stage group(s): {sorted(unknown)}")

    virtual_names = {n for g in groups & set(VIRTUAL_GROUPS)
                     for n in VIRTUAL_GROUPS[g]}
    plan = [s for s in STAGES if s.group in groups or s.name in virtual_names]
    names = [s.name for s in plan]

    start = 0
    end = len(plan)
    if from_name is not None:
        if from_name not in names:
            raise ValueError(
                f"--from {from_name!r} is not in the selected plan "
                f"(selected stages: {names})")
        start = names.index(from_name)
    if to_name is not None:
        if to_name not in names:
            raise ValueError(
                f"--to {to_name!r} is not in the selected plan "
                f"(selected stages: {names})")
        end = names.index(to_name) + 1
    if start >= end:
        raise ValueError(f"--from {from_name!r} comes after --to {to_name!r}")
    return plan[start:end]


def find_pastas_python(root=REPO_ROOT):
    """Resolve the .venv-pastas interpreter, or None if it doesn't exist."""
    for rel in PASTAS_VENV_CANDIDATES:
        candidate = Path(root) / rel
        if candidate.exists():
            return candidate
    return None


def stage_command(stage, pastas_python=None):
    """Full argv for a stage. ``pastas_python`` may be None for display only."""
    if stage.env == PASTAS_ENV:
        interpreter = str(pastas_python) if pastas_python else "<pastas-py>"
    else:
        interpreter = sys.executable
    return [interpreter, *stage.argv]


# ---------------------------------------------------------------------------
# CLI / execution
# ---------------------------------------------------------------------------

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m scripts.run_chain",
        description="Run the post-pipeline refresh chain in the documented order "
                    "(see CLAUDE.md 'How to run' — the numbered steps are the spec).")
    for g in (*GROUPS, *VIRTUAL_GROUPS):
        p.add_argument(f"--{g}", action="store_true",
                       help=f"include the '{g}' stage group")
    p.add_argument("--all", action="store_true",
                   help="include every (physical) stage group")
    p.add_argument("--from", dest="from_name", metavar="NAME",
                   help="start the selected plan at this stage name")
    p.add_argument("--to", dest="to_name", metavar="NAME",
                   help="stop the selected plan after this stage name (inclusive)")
    p.add_argument("--list", action="store_true", dest="list_only",
                   help="print the plan and exit without running anything")
    p.add_argument("--dry-run", action="store_true",
                   help="print the exact commands without executing them")
    return p.parse_args(argv)


def _selected_groups(args):
    if args.all:
        return list(GROUPS)   # virtual groups are subsets — already covered
    chosen = [g for g in (*GROUPS, *VIRTUAL_GROUPS) if getattr(args, g)]
    return chosen or ["core"]  # --core is the default


def _print_plan(plan, pastas_python):
    width = max(len(s.name) for s in plan)
    print(f"Plan ({len(plan)} stage(s)), in order:")
    for i, s in enumerate(plan, 1):
        cmd = " ".join(stage_command(s, pastas_python))
        print(f"  {i:>2}. [{s.step:>6}] {s.name:<{width}}  ({s.group}, {s.env} env)")
        print(f"      $ {cmd}")
        print(f"      why here: {s.why}")


def _fmt_secs(seconds):
    return f"{seconds:7.1f}s"


def main(argv=None):
    args = _parse_args(argv)
    groups = _selected_groups(args)
    try:
        plan = select_stages(groups, args.from_name, args.to_name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not plan:
        print("error: nothing selected", file=sys.stderr)
        return 2

    pastas_python = find_pastas_python()
    needs_pastas = [s for s in plan if s.env == PASTAS_ENV]
    pastas_missing = bool(needs_pastas) and pastas_python is None

    if args.list_only or args.dry_run:
        _print_plan(plan, pastas_python)
        if pastas_missing:
            print("\nnote: .venv-pastas interpreter not found — pastas stages "
                  "(shown as <pastas-py>) would be SKIPPED. See CLAUDE.md "
                  "'Pastas TFN forecast' for the one-time venv setup.")
        if args.list_only:
            return 0
        print("\n--dry-run: no commands were executed.")
        return 0

    if pastas_missing:
        # Don't fail the whole chain up front: run what we can, skip pastas
        # stages, and exit non-zero at the end so cron/operators notice.
        print("WARNING: pastas stages requested but the dedicated interpreter "
              "was not found at .venv-pastas/Scripts/python.exe (or bin/python).",
              file=sys.stderr)
        print("Set it up once (CLAUDE.md 'Pastas TFN forecast'):\n"
              "    python -m venv .venv-pastas\n"
              "    .venv-pastas\\Scripts\\python -m pip install -r requirements-pastas.txt",
              file=sys.stderr)
        print(f"Skipping: {[s.name for s in needs_pastas]}", file=sys.stderr)

    if not _acquire_lock():
        print("Another run_chain is already in progress (outputs/run_chain.lock) "
              "— exiting so we don't race its in-place shard/CSV writes. The "
              "next scheduled run will catch up.", file=sys.stderr)
        return 3
    atexit.register(_release_lock)

    results = []  # (stage, status, seconds)
    failed = False
    for i, stage in enumerate(plan, 1):
        if stage.env == PASTAS_ENV and pastas_python is None:
            print(f"\n[{i}/{len(plan)}] SKIP {stage.name} (no .venv-pastas interpreter)")
            results.append((stage, "SKIPPED", 0.0))
            continue
        cmd = stage_command(stage, pastas_python)
        print(f"\n[{i}/{len(plan)}] RUN  {stage.name}  (step {stage.step})")
        print(f"      $ {' '.join(cmd)}")
        t0 = time.monotonic()
        # No capture: child stdout/stderr stream straight to the console.
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
        elapsed = time.monotonic() - t0
        if proc.returncode != 0:
            results.append((stage, f"FAILED ({proc.returncode})", elapsed))
            print(f"\nFAILED at stage {stage.name} (exit {proc.returncode}) "
                  f"— downstream stages not run (stage {i} of {len(plan)} in the plan).",
                  file=sys.stderr)
            for skipped in plan[i:]:
                results.append((skipped, "not run", 0.0))
            failed = True
            break
        results.append((stage, "OK", elapsed))
        print(f"      done in {elapsed:.1f}s")

    # Final summary table.
    width = max(len(s.name) for s, _, _ in results)
    print("\n" + "=" * (width + 32))
    print(f"{'stage':<{width}}  {'step':>6}  {'status':<12}  time")
    print("-" * (width + 32))
    total = 0.0
    for stage, status, elapsed in results:
        total += elapsed
        print(f"{stage.name:<{width}}  {stage.step:>6}  {status:<12}  {_fmt_secs(elapsed)}")
    print("-" * (width + 32))
    print(f"{'total':<{width}}  {'':>6}  {'':<12}  {_fmt_secs(total)}")

    if failed:
        return 1
    if pastas_missing:
        return 1  # something requested was skipped — surface it to cron
    return 0


if __name__ == "__main__":
    sys.exit(main())

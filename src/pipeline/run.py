"""
Pipeline orchestrator.

Chains the six build stages of the Groundwater Forecasting system in order:

    1. catalogue       — build station catalogue (region-filtered)
    2. linking         — link GW stations to rainfall predictors
    3. download        — fetch raw time series from EA Hydrology API   [SLOW]
    4. features        — resample + engineer features
    5. model           — train pooled delta-RF model
    6. risk            — build the operational risk index

Each stage is invoked as a subprocess via ``python -m <module>`` so the
stages' own error handling, logging, and exit codes remain authoritative.
Pipeline aborts immediately on the first failing stage.

Usage
-----
Full rebuild (re-runs every stage including download):
    python -m src.pipeline.run

Skip the slow download stage (iterative re-runs after the raw cache is warm):
    python -m src.pipeline.run --skip-download

Run a subset of stages by name (inclusive endpoints):
    python -m src.pipeline.run --from features --to risk

List stages and exit:
    python -m src.pipeline.run --list

Dry-run (print the commands that would be run, but execute nothing):
    python -m src.pipeline.run --dry-run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from src.utils.io_encoding import force_utf8_stdio


# ---------------------------------------------------------------------------
# Stage registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage:
    name: str           # short slug used by --from / --to / --skip-*
    module: str         # full dotted module path passed to ``python -m``
    description: str
    slow: bool = False  # marked True only for stages that are unusually slow


STAGES: tuple[Stage, ...] = (
    Stage("catalogue",    "src.catalogue.build",
          "Build station catalogue (region-filtered)"),
    Stage("linking",      "src.linking.build",
          "Link GW stations to rainfall predictors"),
    Stage("download",     "src.download.build",
          "Fetch raw time series from EA Hydrology API", slow=True),
    Stage("features",     "src.features.build",
          "Resample + engineer features"),
    # The forecast chain (derived artefacts, normals, live levels, ensemble,
    # Pastas, seasonal) is scripts/run_chain.py's job — the risk-index/RF
    # stages that used to follow here were retired with the vocabulary
    # unification (see docs/ensemble_forecast_design.md amendments log).
)

_STAGE_BY_NAME: dict[str, Stage] = {s.name: s for s in STAGES}
_STAGE_NAMES:   list[str]       = [s.name for s in STAGES]


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _print_banner(stage: Stage, idx: int, total: int) -> None:
    bar = "─" * 70
    marker = " [SLOW]" if stage.slow else ""
    print(f"\n{bar}")
    print(f"  [{idx}/{total}] {stage.name.upper():<10}{marker}  {stage.description}")
    print(f"  $ python -m {stage.module}")
    print(bar, flush=True)


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(seconds, 60)
    return f"{int(mins)}m {secs:.0f}s"


def _raw_cache_youngest_age_days(raw_root: "Path") -> float | None:
    """
    Return the youngest (smallest) raw-file age across all station folders,
    in days. ``None`` if no raw cache exists yet (forces a full download).
    """
    import time
    from pathlib import Path as _P
    raw_root = _P(raw_root)
    if not raw_root.exists():
        return None
    ages = []
    for sid_dir in raw_root.iterdir():
        if not sid_dir.is_dir():
            continue
        for f in sid_dir.glob("*.csv"):
            ages.append((time.time() - f.stat().st_mtime) / 86400.0)
    if not ages:
        return None
    return min(ages)


def _select_stages(args: argparse.Namespace) -> list[Stage]:
    """Apply --from / --to / --skip-download / --since to the stage list."""
    start = 0
    end = len(STAGES) - 1

    if args.from_stage:
        if args.from_stage not in _STAGE_BY_NAME:
            sys.exit(f"Unknown --from stage: {args.from_stage!r}. "
                     f"Valid: {_STAGE_NAMES}")
        start = _STAGE_NAMES.index(args.from_stage)

    if args.to_stage:
        if args.to_stage not in _STAGE_BY_NAME:
            sys.exit(f"Unknown --to stage: {args.to_stage!r}. "
                     f"Valid: {_STAGE_NAMES}")
        end = _STAGE_NAMES.index(args.to_stage)

    if start > end:
        sys.exit(f"--from {args.from_stage} comes after --to {args.to_stage}")

    selected = list(STAGES[start:end + 1])

    skip_download = args.skip_download

    # --since latest: skip the download stage when the local raw cache is
    # already younger than --max-raw-age-days.  This is an orchestrator-level
    # heuristic; it does NOT change the download module's own logic.
    if args.since == "latest" and not skip_download:
        from pathlib import Path as _P
        raw_root = _P(__file__).parents[2] / "data" / "raw"
        youngest = _raw_cache_youngest_age_days(raw_root)
        if youngest is None:
            print(
                "  --since latest: no local raw cache found — running a full "
                "download.",
                flush=True,
            )
        elif youngest <= args.max_raw_age_days:
            print(
                f"  --since latest: youngest raw file is "
                f"{youngest:.2f} days old (<= {args.max_raw_age_days}) — "
                "skipping download stage.",
                flush=True,
            )
            skip_download = True
        else:
            print(
                f"  --since latest: youngest raw file is "
                f"{youngest:.2f} days old (> {args.max_raw_age_days}) — "
                "running download.",
                flush=True,
            )

    if skip_download:
        selected = [s for s in selected if s.name != "download"]

    return selected


def _run_stage(stage: Stage, dry_run: bool) -> tuple[int, float]:
    """
    Run a single stage as a subprocess.  Returns (returncode, elapsed_seconds).
    On dry-run, prints the command and returns (0, 0.0).
    """
    if dry_run:
        print(f"  (dry-run) would execute: python -m {stage.module}")
        return 0, 0.0

    t0 = time.perf_counter()
    # Force UTF-8 stdio in child stages so any unicode they print (em-dashes,
    # arrows, ≤, …) doesn't crash under non-TTY stdout on Windows.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # ``check=False`` so we can decide how to report failures ourselves.
    # Inherit stdout/stderr so each stage's progress streams live.
    result = subprocess.run(
        [sys.executable, "-m", stage.module],
        check=False,
        env=env,
    )
    elapsed = time.perf_counter() - t0
    return result.returncode, elapsed


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()

    parser = argparse.ArgumentParser(
        prog="python -m src.pipeline.run",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Skip the slow EA download stage (use existing data/raw/ cache)",
    )
    parser.add_argument(
        "--since", choices=["latest", "all"], default="all",
        help=(
            "Download policy. 'all' (default) re-runs the download stage "
            "exactly as before. 'latest' skips the download stage entirely "
            "when every station's local raw cache is younger than "
            "--max-raw-age-days (default 1). For finer per-station "
            "incremental fetch see the runbook."
        ),
    )
    parser.add_argument(
        "--max-raw-age-days", type=int, default=1,
        help=(
            "With --since latest: a raw cache older than this triggers a "
            "full download. Default 1 day."
        ),
    )
    parser.add_argument(
        "--from", dest="from_stage", metavar="STAGE",
        help=f"Start at this stage (inclusive). Choices: {_STAGE_NAMES}",
    )
    parser.add_argument(
        "--to", dest="to_stage", metavar="STAGE",
        help=f"Stop after this stage (inclusive). Choices: {_STAGE_NAMES}",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List the pipeline stages and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run; execute nothing",
    )

    args = parser.parse_args(argv)

    if args.list:
        print("Pipeline stages (in execution order):")
        for i, s in enumerate(STAGES, 1):
            tag = " [SLOW]" if s.slow else ""
            print(f"  {i}. {s.name:<10} → {s.module}{tag}")
            print(f"        {s.description}")
        return 0

    selected = _select_stages(args)
    if not selected:
        print("No stages selected. Nothing to do.")
        return 0

    total = len(selected)
    print(f"Running {total} stage(s): {[s.name for s in selected]}")
    if args.skip_download:
        print("  (download stage skipped — using existing data/raw/ cache)")
    if args.dry_run:
        print("  (dry-run — no commands will be executed)")

    pipeline_t0 = time.perf_counter()
    timings: list[tuple[str, float]] = []

    for idx, stage in enumerate(selected, 1):
        _print_banner(stage, idx, total)
        rc, elapsed = _run_stage(stage, dry_run=args.dry_run)
        timings.append((stage.name, elapsed))

        if rc != 0:
            print(
                f"\n[FAIL] Stage '{stage.name}' exited with code {rc} "
                f"after {_format_elapsed(elapsed)}. Aborting pipeline.",
                file=sys.stderr,
            )
            _print_summary(timings, success=False)
            return rc

        print(f"\n[OK] {stage.name} finished in {_format_elapsed(elapsed)}",
              flush=True)

    total_elapsed = time.perf_counter() - pipeline_t0
    print(f"\nAll stages complete in {_format_elapsed(total_elapsed)}.")
    _print_summary(timings, success=True)
    return 0


def _print_summary(timings: list[tuple[str, float]], success: bool) -> None:
    if not timings:
        return
    print("\nStage timings:")
    for name, secs in timings:
        print(f"  {name:<10} {_format_elapsed(secs):>10}")
    if success:
        print("\nNext: open the dashboard with `python -m streamlit run app.py`")


if __name__ == "__main__":
    sys.exit(main())

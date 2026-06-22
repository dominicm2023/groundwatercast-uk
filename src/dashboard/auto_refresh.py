"""In-app data auto-refresh — the local deployment has no cron, so while
the dashboard is being used it keeps its own data fresh.

Two jobs (see ``build_jobs``), each staleness-gated on its chain's END
artefact and launched as a detached background subprocess:

  live      hourly   run_chain --live      (live GW -> shards; rainfall tail)
  forecast  daily    run_chain --forecast  (roll + Pastas builds; NOT the
                     recalibration — that's a retrain, run --pastas manually)

The dashboard renders immediately on the existing artefacts; the
mtime-keyed caches pick refreshed files up on the next interaction.

Stampede / thrash control, per job:
  - a lock file written before launch and removed by the completion
    watcher; a lock younger than its TTL blocks re-launch (TTL self-heals
    if the app dies mid-refresh);
  - an attempt-stamp written by the watcher on EVERY exit — a failing job
    (API down, missing prerequisite) retries only after its cooldown
    rather than on every interaction;
  - the forecast job defers while a live refresh is in flight (8d reads
    the rainfall CSVs that v19 rewrites);
  - each browser session re-checks at most every ``CHECK_EVERY_MIN``.

Disable everything with ``GWC_APP_START_REFRESH=0`` (recommended when a
real scheduler runs the commands — docs/deploy.md).

Decision logic (``should_kick``) is pure and unit-tested
(tests/test_auto_refresh.py); only ``maybe_kick_refreshes`` touches
streamlit / subprocess. All console output is ASCII-only: a cp1252
console (bare mode / tests) raises on non-ASCII print and would abort
the kick.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_MODEL = ROOT / "data" / "model"
XREF = ROOT / "data" / "processed" / "flood_monitoring_xref.csv"
LIVE_LOCK = _MODEL / "_live_refresh.lock"

CHECK_EVERY_MIN = 15      # per-session re-check interval
_SESSION_KEY = "_gwc_auto_refresh_last_check"


@dataclass(frozen=True)
class Job:
    """One staleness-gated background refresh.

    marker          : the chain's END artefact — its mtime is the freshness signal
    requires        : optional prerequisite file; missing -> never kick
    kick_if_missing : kick when the marker doesn't exist yet?
    defer_on        : optional lock of another job that must NOT be in flight
    """
    name: str
    marker: Path
    lock: Path
    stamp: Path
    argv: tuple                     # passed to sys.executable
    max_age_min: float
    lock_ttl_min: float
    cooldown_min: float             # min gap between attempts (thrash guard)
    requires: Path | None = None
    kick_if_missing: bool = True
    defer_on: Path | None = None


def _pastas_venv_exists(root: Path = ROOT) -> bool:
    return ((root / ".venv-pastas" / "Scripts" / "python.exe").exists()
            or (root / ".venv-pastas" / "bin" / "python").exists())


def build_jobs(root: Path = ROOT) -> tuple[Job, ...]:
    """The job table. Pure — safe to call from tests with a fake root."""
    model = root / "data" / "model"
    # Without the pastas venv the forecast job still refreshes the roll
    # ensemble — marker and argv shrink to the part that can actually run,
    # so a missing venv doesn't read as "always stale".
    pastas = _pastas_venv_exists(root)
    return (
        Job("live",
            marker=root / "data" / "features" / "gw_by_station" / "_MANIFEST.json",
            lock=model / "_live_refresh.lock",
            stamp=model / "_live_refresh.last",
            argv=("-m", "scripts.run_chain", "--live"),
            max_age_min=60, lock_ttl_min=30, cooldown_min=30,
            requires=root / "data" / "processed" / "flood_monitoring_xref.csv"),
        Job("forecast",
            marker=model / ("forecast_pastas_summary.csv" if pastas
                            else "forecast_ensemble_summary.csv"),
            lock=model / "_forecast_refresh.lock",
            stamp=model / "_forecast_refresh.last",
            argv=("-m", "scripts.run_chain",
                  "--forecast" if pastas else "--ensemble"),
            max_age_min=24 * 60, lock_ttl_min=60, cooldown_min=6 * 60,
            defer_on=model / "_live_refresh.lock"),
    )


def _age_min(fp: Path, now: float) -> float | None:
    try:
        return (now - fp.stat().st_mtime) / 60
    except OSError:
        return None


def should_kick(now: float, job: Job) -> tuple[bool, str]:
    """(kick?, reason). Pure decision — no side effects."""
    if job.requires is not None and not job.requires.exists():
        return False, f"prerequisite missing ({job.requires.name})"
    if job.defer_on is not None:
        other = _age_min(job.defer_on, now)
        if other is not None and other < job.lock_ttl_min:
            return False, f"deferred ({job.defer_on.name} in flight)"
    own = _age_min(job.lock, now)
    if own is not None and own < job.lock_ttl_min:
        return False, "refresh already in flight (young lock)"
    last = _age_min(job.stamp, now)
    if last is not None and last < job.cooldown_min:
        return False, f"attempted {last:.0f} min ago (cooldown {job.cooldown_min:.0f})"
    age = _age_min(job.marker, now)
    if age is None:
        if job.kick_if_missing:
            return True, f"no {job.marker.name} yet"
        return False, f"no baseline {job.marker.name} - leave to manual"
    if age >= job.max_age_min:
        return True, f"{job.marker.name} {age:.0f} min old (threshold {job.max_age_min:.0f})"
    return False, f"fresh ({age:.0f} min old)"


def _kick(job: Job, now: float) -> None:
    """Launch one job detached, with a lock + completion watcher."""
    try:
        job.lock.parent.mkdir(parents=True, exist_ok=True)
        job.lock.write_text(f"pid={os.getpid()} started={now:.0f}\n")
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", *job.argv],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )

        def _watch(p: subprocess.Popen, started: float) -> None:
            rc = p.wait()
            took = time.time() - started
            try:
                job.stamp.write_text(f"rc={rc} took={took:.0f}s\n")
                job.lock.unlink(missing_ok=True)
            except OSError:
                pass
            status = "completed" if rc == 0 else f"exited with code {rc}"
            print(f"[auto-refresh] {job.name} refresh {status} in {took:.0f}s.")

        threading.Thread(target=_watch, args=(proc, now), daemon=True).start()
    except Exception as exc:
        try:
            job.lock.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[auto-refresh] could not kick {job.name} refresh: {exc}")


def maybe_kick_refreshes() -> None:
    """Streamlit-facing entry point — call once per script run (cheap)."""
    import streamlit as st

    if os.environ.get("GWC_APP_START_REFRESH", "1") == "0":
        return
    now = time.time()
    last = st.session_state.get(_SESSION_KEY, 0.0)
    if (now - last) / 60 < CHECK_EVERY_MIN:
        return
    st.session_state[_SESSION_KEY] = now

    for job in build_jobs():
        kick, reason = should_kick(now, job)
        if kick:
            print(f"[auto-refresh] {job.name}: {reason} - kicking "
                  f"'{' '.join(job.argv)}' in the background.")
            _kick(job, now)

"""Tests for the in-app auto-refresh decision logic (src/dashboard/auto_refresh)."""
from __future__ import annotations

import os
import time

from src.dashboard.auto_refresh import Job, build_jobs, should_kick


def _touch(fp, age_min, now):
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text("x")
    ts = now - age_min * 60
    os.utime(fp, (ts, ts))
    return fp


def _job(tmp_path, **overrides):
    defaults = dict(
        name="live",
        marker=tmp_path / "marker.csv",
        lock=tmp_path / "job.lock",
        stamp=tmp_path / "job.last",
        argv=("-m", "scripts.run_chain", "--live"),
        max_age_min=60, lock_ttl_min=30, cooldown_min=30,
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestShouldKick:
    def test_kicks_when_marker_stale(self, tmp_path):
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 90, now)
        kick, reason = should_kick(now, job)
        assert kick and "90 min old" in reason

    def test_no_kick_when_fresh(self, tmp_path):
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 10, now)
        kick, reason = should_kick(now, job)
        assert not kick and "fresh" in reason

    def test_kicks_when_marker_missing(self, tmp_path):
        now = time.time()
        kick, reason = should_kick(now, _job(tmp_path))
        assert kick and "yet" in reason

    def test_missing_marker_respects_kick_if_missing(self, tmp_path):
        """Semantics: no baseline CSV -> never auto-kick."""
        now = time.time()
        kick, reason = should_kick(now, _job(tmp_path, kick_if_missing=False))
        assert not kick and "leave to manual" in reason

    def test_missing_prerequisite_blocks(self, tmp_path):
        now = time.time()
        job = _job(tmp_path, requires=tmp_path / "xref.csv")
        _touch(job.marker, 999, now)
        kick, reason = should_kick(now, job)
        assert not kick and "prerequisite" in reason

    def test_young_lock_blocks(self, tmp_path):
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 90, now)
        _touch(job.lock, 5, now)
        kick, reason = should_kick(now, job)
        assert not kick and "in flight" in reason

    def test_dead_lock_ignored(self, tmp_path):
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 90, now)
        _touch(job.lock, 45, now)
        assert should_kick(now, job)[0]

    def test_recent_attempt_cooldown_blocks(self, tmp_path):
        """A failed run leaves a fresh stamp -> no thrash on every check."""
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 90, now)
        _touch(job.stamp, 10, now)
        kick, reason = should_kick(now, job)
        assert not kick and "cooldown" in reason
        # cooldown elapsed -> retry
        _touch(job.stamp, 45, now)
        assert should_kick(now, job)[0]

    def test_defer_on_other_lock(self, tmp_path):
        """Forecast defers while the live refresh is in flight."""
        now = time.time()
        live_lock = _touch(tmp_path / "live.lock", 5, now)
        job = _job(tmp_path, name="forecast", defer_on=live_lock,
                   max_age_min=24 * 60)
        _touch(job.marker, 48 * 60, now)
        kick, reason = should_kick(now, job)
        assert not kick and "deferred" in reason
        # live lock gone -> forecast proceeds
        live_lock.unlink()
        assert should_kick(now, job)[0]

    def test_threshold_boundary(self, tmp_path):
        now = time.time()
        job = _job(tmp_path)
        _touch(job.marker, 59, now)
        assert not should_kick(now, job)[0]
        _touch(job.marker, 61, now)
        assert should_kick(now, job)[0]


class TestJobTable:
    def test_two_jobs_with_expected_cadences(self, tmp_path):
        jobs = {j.name: j for j in build_jobs(tmp_path)}
        assert set(jobs) == {"live", "forecast"}
        assert jobs["live"].max_age_min == 60
        assert jobs["forecast"].max_age_min == 24 * 60
        assert jobs["forecast"].defer_on == jobs["live"].lock

    def test_forecast_degrades_without_pastas_venv(self, tmp_path):
        """No .venv-pastas -> the forecast job shrinks to the roll ensemble
        (marker + argv), instead of reading as permanently stale."""
        jobs = {j.name: j for j in build_jobs(tmp_path)}      # no venv in tmp
        assert "--ensemble" in jobs["forecast"].argv
        assert jobs["forecast"].marker.name == "forecast_ensemble_summary.csv"

    def test_forecast_full_chain_with_pastas_venv(self, tmp_path):
        py = tmp_path / ".venv-pastas" / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True)
        py.write_text("")
        jobs = {j.name: j for j in build_jobs(tmp_path)}
        assert "--forecast" in jobs["forecast"].argv
        assert jobs["forecast"].marker.name == "forecast_pastas_summary.csv"

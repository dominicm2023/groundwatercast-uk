"""Roadmap 0.1 — the per-day fan must accrue to an append-only archive instead
of being overwritten each run (it's the raw material for spread-skill / PIT /
CRPS verification). Tests the run-stamped, (station, run, lead)-deduped append in
both summary builders.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scripts.build_ensemble_summary import append_fan_archive as ens_append
from scripts.build_pastas_summary import append_fan_archive as pas_append


def _fan(run, station="s1", leads=(1, 2, 3), p50=0.0):
    return pd.DataFrame({
        "station_id": station, "run": run,
        "lead": list(leads), "date": pd.date_range("2026-06-12", periods=len(leads)),
        "gw_p10": p50 - 1, "gw_p50": p50, "gw_p90": p50 + 1,
    })


@pytest.mark.parametrize("append", [ens_append, pas_append])
class TestAppendFanArchive:
    def test_first_write_returns_fan(self, append):
        f = _fan("2026-06-16T09")
        out = append(None, f)
        assert len(out) == 3

    def test_new_run_accumulates(self, append):
        a = _fan("2026-06-16T09", p50=10.0)
        b = _fan("2026-06-17T09", p50=11.0)
        out = append(a, b)
        assert len(out) == 6
        assert out["run"].nunique() == 2

    def test_same_run_rerun_replaces_not_duplicates(self, append):
        a = _fan("2026-06-16T09", p50=10.0)
        rerun = _fan("2026-06-16T09", p50=99.0)        # same run, new values
        out = append(a, rerun)
        assert len(out) == 3                            # replaced, not doubled
        assert (out["gw_p50"] == 99.0).all()            # kept the last write

    def test_distinct_leads_within_run_all_kept(self, append):
        out = append(None, _fan("2026-06-16T09", leads=(-2, -1, 1, 2)))
        assert sorted(out["lead"]) == [-2, -1, 1, 2]    # nowcast + forecast leads

    def test_empty_fan_is_safe(self, append):
        prior = _fan("2026-06-16T09")
        assert len(append(prior, pd.DataFrame())) == 3  # prior preserved
        assert append(None, pd.DataFrame()).empty

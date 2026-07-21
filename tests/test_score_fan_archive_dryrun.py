"""Verification dry-run scorer: era split, coverage flags, pinball loss.
Pure-function tests on synthetic rows; the real archive run is recorded in
docs/phase3_verification_scope.md."""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.score_fan_archive_dryrun import ERA_CUT, pinball, score


def _rows(run, obs, p10=9.0, p50=10.0, p90=11.0):
    return dict(station_id="s1", run=run, lead=1,
                date=pd.Timestamp("2026-07-10"),
                gw_p10=p10, gw_p50=p50, gw_p90=p90, obs=obs)


def test_era_split_on_boundary():
    df = pd.DataFrame([
        _rows("2026-07-04T08", 10.0),   # before the 12:00Z cutover
        _rows("2026-07-04T12", 10.0),   # at/after
        _rows("2026-06-20T09", 10.0),
    ])
    s = score(df)
    assert list(s["era"]) == ["joined-calibration", "gauge-calibration",
                              "joined-calibration"]
    assert ERA_CUT == "2026-07-04T12"


def test_coverage_flags():
    df = pd.DataFrame([
        _rows("2026-07-05T09", 10.0),   # in band
        _rows("2026-07-05T09", 8.5),    # below P10
        _rows("2026-07-05T09", 11.5),   # above P90
        _rows("2026-07-05T09", np.nan),  # unobserved -> dropped
    ])
    s = score(df)
    assert len(s) == 3
    assert s["inband"].tolist() == [True, False, False]
    assert s["below"].tolist() == [False, True, False]
    assert s["above"].tolist() == [False, False, True]
    assert np.allclose(s["width"], 2.0)


def test_pinball_is_proper_quantile_loss():
    # over-prediction penalised by (1-tau), under- by tau
    assert pinball(np.array([10.0]), np.array([12.0]), 0.1)[0] == 1.8
    assert pinball(np.array([10.0]), np.array([8.0]), 0.1)[0] == 0.2
    assert pinball(np.array([10.0]), np.array([10.0]), 0.5)[0] == 0.0

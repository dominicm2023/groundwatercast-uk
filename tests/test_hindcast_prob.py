"""Probabilistic-hindcast aggregation (Phase 3 / A): the pure summarise + PIT
helpers. The driver (run_prob_hindcast) is smoke-run on real data, not unit-tested."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.diagnostics import hindcast_prob as H


def _rows(n_per_lead=50, horizon=3, seed=0):
    """Synthetic scored rows: a well-calibrated model (crps < baselines, PIT ~U)."""
    rng = np.random.default_rng(seed)
    out = []
    for lead in range(1, horizon + 1):
        for _ in range(n_per_lead):
            out.append({
                "lead": lead,
                "crps": 0.2 + 0.05 * lead,            # model
                "crps_persist": 0.4 + 0.05 * lead,    # worse baseline
                "crps_clim": 0.6,                     # worst baseline
                "pit": float(rng.uniform()),          # calibrated → uniform
                "sq_err": (0.25 + 0.05 * lead) ** 2,
                "sd": 0.25 + 0.05 * lead,             # spread ≈ rmse
            })
    return pd.DataFrame(out)


class TestSummarise:
    def test_shape_and_columns(self):
        s = H.summarise(_rows(horizon=3), 3)
        assert list(s["lead"]) == [1, 2, 3]
        for c in ("n", "mean_crps", "crpss_persist", "crpss_clim",
                  "spread", "rmse", "spread_skill", "pit_mean"):
            assert c in s.columns

    def test_skill_positive_against_worse_baselines(self):
        s = H.summarise(_rows(horizon=2), 2)
        assert (s["crpss_persist"] > 0).all()      # model beats persistence
        assert (s["crpss_clim"] > 0).all()         # and climatology
        assert (s["crpss_clim"] > s["crpss_persist"]).all()  # clim is the worse ref

    def test_spread_skill_near_one_when_spread_matches_error(self):
        s = H.summarise(_rows(horizon=2), 2)
        assert s["spread_skill"].between(0.9, 1.1).all()

    def test_skips_missing_leads(self):
        df = _rows(horizon=3)
        df = df[df["lead"] != 2]                    # drop a lead
        s = H.summarise(df, 3)
        assert list(s["lead"]) == [1, 3]


class TestPITHistogram:
    def test_uniform_is_flat(self):
        rng = np.random.default_rng(1)
        pit = rng.uniform(size=20000)
        h = H.pit_histogram(pit, bins=10)
        assert len(h) == 10
        assert h["frac"].sum() == pytest.approx(1.0)
        assert h["dev"].abs().sum() < 0.05         # ~flat

    def test_overconfident_band_piles_at_the_tails(self):
        # an over-confident forecast → observations fall in the PIT tails
        pit = np.concatenate([np.full(1000, 0.02), np.full(1000, 0.98)])
        h = H.pit_histogram(pit, bins=10)
        assert h.iloc[0]["frac"] > 0.4 and h.iloc[-1]["frac"] > 0.4
        assert h["dev"].abs().sum() > 0.5          # strongly miscalibrated


class TestBandSd:
    def test_per_lead_sd_floored_and_nan_safe(self):
        sd = H._band_sd_by_lead({1: [0.0, 0.0], 2: [1.0, -1.0, 0.0], 3: [0.5]}, 3)
        assert sd[0] == pytest.approx(H._SD_FLOOR)   # zero-variance → floor
        assert sd[1] > 0                             # real spread
        assert np.isnan(sd[2])                       # <2 points → NaN

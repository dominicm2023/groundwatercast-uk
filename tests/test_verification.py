"""Verification primitives (Phase 3 foundation): CRPS / PIT / Brier / RPS + skill
scores and baseline builders. Pinned against closed-form / analytic values."""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.diagnostics import verification as V


# ---------------------------------------------------------------------------
# CRPS (continuous)
# ---------------------------------------------------------------------------

class TestCRPSGaussian:
    def test_standard_value_at_mean(self):
        # CRPS(N(0,1), 0) = 2φ(0) − 1/√π  (the ω=0 case) ≈ 0.233692
        val = float(V.crps_gaussian(0.0, 1.0, 0.0))
        assert val == pytest.approx(2 / math.sqrt(2 * math.pi) - 1 / math.sqrt(math.pi),
                                    rel=1e-9)
        assert val == pytest.approx(0.233692, abs=1e-5)

    def test_degenerate_sigma_is_absolute_error(self):
        assert float(V.crps_gaussian(5.0, 0.0, 3.0)) == pytest.approx(2.0)

    def test_nonneg_and_symmetric(self):
        a = float(V.crps_gaussian(0.0, 1.0, 2.0))
        b = float(V.crps_gaussian(0.0, 1.0, -2.0))
        assert a >= 0 and a == pytest.approx(b)

    def test_vectorised(self):
        out = V.crps_gaussian([0, 0], [1, 1], [0, 2])
        assert out.shape == (2,)
        assert out[1] > out[0]   # observation further from the mean scores worse

    def test_sharper_calibrated_beats_wider(self):
        # at the mean, a tighter (correct) forecast has lower CRPS than a vague one
        assert float(V.crps_gaussian(0, 0.5, 0)) < float(V.crps_gaussian(0, 3.0, 0))


class TestCRPSEnsemble:
    def test_degenerate_ensemble_is_absolute_error(self):
        assert V.crps_ensemble([4.0, 4.0, 4.0], 1.0) == pytest.approx(3.0)

    def test_matches_gaussian_for_large_normal_sample(self):
        rng = np.random.default_rng(0)
        s = rng.normal(0.0, 1.0, 40000)
        emp = V.crps_ensemble(s, 0.5)
        closed = float(V.crps_gaussian(0.0, 1.0, 0.5))
        assert emp == pytest.approx(closed, abs=0.01)

    def test_empty_is_nan(self):
        assert math.isnan(V.crps_ensemble([], 1.0))


class TestPIT:
    def test_at_mean_is_half(self):
        assert float(V.pit_gaussian(0.0, 1.0, 0.0)) == pytest.approx(0.5)

    def test_calibrated_forecast_is_uniform(self):
        rng = np.random.default_rng(1)
        y = rng.normal(2.0, 1.5, 30000)        # obs drawn FROM the forecast
        pit = V.pit_gaussian(np.full_like(y, 2.0), np.full_like(y, 1.5), y)
        # uniform → mean ~0.5, and quartile mass ~0.25 each
        assert pit.mean() == pytest.approx(0.5, abs=0.01)
        assert np.mean(pit < 0.25) == pytest.approx(0.25, abs=0.02)


# ---------------------------------------------------------------------------
# Skill scores
# ---------------------------------------------------------------------------

class TestSkill:
    def test_better_model_positive(self):
        assert V.skill_score([0.5, 0.5], [1.0, 1.0]) == pytest.approx(0.5)

    def test_equal_is_zero(self):
        assert V.skill_score([1.0, 2.0], [1.0, 2.0]) == pytest.approx(0.0)

    def test_worse_is_negative(self):
        assert V.skill_score([2.0, 2.0], [1.0, 1.0]) == pytest.approx(-1.0)

    def test_zero_reference_is_nan(self):
        assert math.isnan(V.skill_score([0.1], [0.0]))


# ---------------------------------------------------------------------------
# Categorical: Brier + RPS
# ---------------------------------------------------------------------------

class TestBrier:
    def test_perfect_zero(self):
        assert float(V.brier_score(1.0, 1.0)) == 0.0
        assert float(V.brier_score(0.0, 0.0)) == 0.0

    def test_half_on_event(self):
        assert float(V.brier_score(0.5, 1.0)) == pytest.approx(0.25)


class TestRPS:
    def test_perfect_confident_is_zero(self):
        assert V.rps([0, 0, 1], 2) == pytest.approx(0.0)

    def test_climatology_terciles_analytic(self):
        clim = [1 / 3, 1 / 3, 1 / 3]
        assert V.rps(clim, 0) == pytest.approx(5 / 9)
        assert V.rps(clim, 1) == pytest.approx(2 / 9)
        assert V.rps(clim, 2) == pytest.approx(5 / 9)

    def test_near_miss_beats_far_miss(self):
        # forecasting "above" when truth is "near" beats forecasting it when truth
        # is "below" (ordered-category distance matters — that's the point of RPS)
        near = V.rps([0, 0, 1], 1)
        far = V.rps([0, 0, 1], 0)
        assert near < far

    def test_rps_mean(self):
        P = np.array([[1 / 3, 1 / 3, 1 / 3], [0, 0, 1]])
        assert V.rps_mean(P, [0, 2]) == pytest.approx((5 / 9 + 0.0) / 2)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

class TestBaselines:
    def test_climatology_terciles(self):
        assert np.allclose(V.climatology_terciles(3), [1 / 3, 1 / 3, 1 / 3])

    def test_climatology_gaussian(self):
        mu, sd = V.climatology_gaussian([1.0, 2.0, 3.0, 4.0])
        assert mu == pytest.approx(2.5)
        assert sd == pytest.approx(np.std([1, 2, 3, 4], ddof=1))

    def test_persistence_flat_mean_growing_sd(self):
        mu, sigma = V.persistence_gaussian(10.0, 0.5, [1, 4, 9])
        assert np.allclose(mu, 10.0)
        assert np.allclose(sigma, [0.5, 1.0, 1.5])   # 0.5·√lead

    def test_damped_persistence_reverts_to_climatology(self):
        mu, _ = V.damped_persistence_gaussian(10.0, 0.0, 0.5, [1, 200], phi=0.9)
        assert mu[0] == pytest.approx(9.0)           # 0 + 0.9^1·(10−0)
        assert mu[1] == pytest.approx(0.0, abs=1e-6)  # far lead → climatology

"""Forecast-verification primitives (roadmap Phase 3 foundation, report-only).

Pure scoring functions shared by the verification work — the "baselines +
CRPS-skill primitive" that makes any later skill number interpretable (a raw CRPS
or Brier score means nothing without a baseline). Used by the probabilistic
hindcast layer (A) and the as-of seasonal Brier/RPS hindcast (B); see
``docs/phase3_verification_scope.md``.

Two families:
  * **continuous** — CRPS (Gaussian closed-form + ensemble estimator), PIT, and
    Gaussian baseline builders (climatology / persistence / damped-persistence).
  * **categorical** — Brier (binary) and the Ranked Probability Score (ordered
    categories, e.g. below/near/above terciles), plus their skill scores.

Skill scores are all ``1 - mean(model)/mean(reference)`` (positive = better than
the baseline, 0 = no better, negative = worse).

numpy-only (no scipy/pandas), so it imports in any environment — matching the
other ``src/diagnostics`` modules. Φ/φ are implemented from ``math.erf`` to avoid
a scipy dependency.
"""
from __future__ import annotations

import math

import numpy as np

_INV_SQRT_PI = 1.0 / math.sqrt(math.pi)
_SQRT2 = math.sqrt(2.0)
_erf = np.vectorize(math.erf, otypes=[float])


def _phi(x: np.ndarray) -> np.ndarray:
    """Standard-normal pdf."""
    x = np.asarray(x, dtype=float)
    return np.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _Phi(x: np.ndarray) -> np.ndarray:
    """Standard-normal cdf (via erf, no scipy)."""
    x = np.asarray(x, dtype=float)
    return 0.5 * (1.0 + _erf(x / _SQRT2))


# ---------------------------------------------------------------------------
# Continuous: CRPS + PIT
# ---------------------------------------------------------------------------

def crps_gaussian(mu, sigma, y) -> np.ndarray:
    """CRPS of a Gaussian predictive N(mu, sigma) against observation(s) y.

    Closed form (Gneiting & Raftery 2007):
        CRPS = σ·[ ω(2Φ(ω) − 1) + 2φ(ω) − 1/√π ],  ω = (y − μ)/σ.
    Degenerate σ ≤ 0 falls back to the absolute error |y − μ| (a point forecast).
    Returns a float array; NaN where any input is NaN. Always ≥ 0."""
    mu = np.asarray(mu, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    y = np.asarray(y, dtype=float)
    out = np.abs(y - mu)  # σ→0 limit (point forecast)
    pos = sigma > 0
    if np.any(pos):
        s = np.where(pos, sigma, 1.0)            # avoid /0 in the masked-off cells
        w = (y - mu) / s
        crps = s * (w * (2.0 * _Phi(w) - 1.0) + 2.0 * _phi(w) - _INV_SQRT_PI)
        out = np.where(pos, crps, out)
    out = np.where(np.isfinite(out), out, np.nan)
    return np.maximum(out, 0.0)


def crps_ensemble(samples, y) -> float:
    """Empirical CRPS of a finite ensemble/sample against a scalar observation.

    CRPS ≈ mean|Xᵢ − y| − ½·mean_{i,j}|Xᵢ − Xⱼ|  (the energy-form estimator).
    The pairwise term is the Gini mean difference, computed from the SORTED sample
    in O(n log n) / O(n) memory — mean_{i,j}|Xᵢ−Xⱼ| = (2/n²)·Σᵢ(2i−n−1)·x₍ᵢ₎ —
    so it scales to large ensembles (the naïve N×N matrix is ~12 GiB at n=40k).
    `samples` is 1-D. Returns NaN for an empty/all-NaN sample."""
    x = np.asarray(samples, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n == 0 or not np.isfinite(y):
        return float("nan")
    term1 = float(np.mean(np.abs(x - float(y))))
    xs = np.sort(x)
    i = np.arange(1, n + 1, dtype=float)
    gini_mean_diff = (2.0 / (n * n)) * float(np.sum((2.0 * i - n - 1.0) * xs))
    return term1 - 0.5 * gini_mean_diff


def pit_gaussian(mu, sigma, y) -> np.ndarray:
    """Probability Integral Transform for a Gaussian forecast: Φ((y − μ)/σ).

    A calibrated forecast yields PIT values ~Uniform(0,1); systematic deviation
    of the PIT histogram from flat diagnoses miscalibration. σ ≤ 0 → NaN."""
    mu = np.asarray(mu, dtype=float)
    sigma = np.asarray(sigma, dtype=float)
    y = np.asarray(y, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (y - mu) / np.where(sigma > 0, sigma, np.nan)
    return _Phi(z)


def skill_score(score_model, score_ref) -> float:
    """Generic skill score 1 − mean(model)/mean(ref) over finite paired values.

    Positive = model beats the reference; 0 = no better; negative = worse.
    NaN if the reference mean is ~0 or no finite pairs remain."""
    m = np.asarray(score_model, dtype=float)
    r = np.asarray(score_ref, dtype=float)
    ok = np.isfinite(m) & np.isfinite(r)
    if not np.any(ok):
        return float("nan")
    ref_mean = float(np.mean(r[ok]))
    if abs(ref_mean) < 1e-12:
        return float("nan")
    return 1.0 - float(np.mean(m[ok])) / ref_mean


# Aliases — same formula, named for the metric for call-site clarity.
crpss = skill_score


# ---------------------------------------------------------------------------
# Categorical: Brier + Ranked Probability Score
# ---------------------------------------------------------------------------

def brier_score(prob, outcome) -> np.ndarray:
    """Per-event Brier score (prob − outcome)² for a binary event.

    `prob` ∈ [0,1] is the forecast probability of the event; `outcome` ∈ {0,1}.
    Mean it for the aggregate score (0 = perfect, 0.25 = always-0.5, 1 = worst)."""
    p = np.asarray(prob, dtype=float)
    o = np.asarray(outcome, dtype=float)
    return (p - o) ** 2


def rps(probs, outcome_idx) -> float:
    """Ranked Probability Score for ONE forecast over ordered categories.

    `probs` is the K-vector of category probabilities (e.g. [p_below, p_near,
    p_above]); `outcome_idx` is the realised category (0..K−1). RPS is the sum of
    squared differences of the cumulative distributions:
        RPS = Σ_k ( CDF_pred[k] − CDF_obs[k] )².
    0 = a perfect, confident forecast of the right category. For 3 equal-tercile
    categories (1/3 each), RPS = 5/9 at an extreme category, 2/9 in the middle."""
    p = np.asarray(probs, dtype=float)
    k = p.size
    if k == 0 or not np.isfinite(p).all():
        return float("nan")
    obs = np.zeros(k)
    if not (0 <= int(outcome_idx) < k):
        return float("nan")
    obs[int(outcome_idx)] = 1.0
    cdf_p = np.cumsum(p)
    cdf_o = np.cumsum(obs)
    return float(np.sum((cdf_p - cdf_o) ** 2))


def rps_mean(probs_matrix, outcome_indices) -> float:
    """Mean RPS over many forecasts. `probs_matrix` is (N, K); `outcome_indices`
    is length N. NaN rows are skipped."""
    P = np.asarray(probs_matrix, dtype=float)
    idx = np.asarray(outcome_indices)
    if P.ndim != 2 or P.shape[0] != idx.shape[0]:
        raise ValueError("probs_matrix must be (N, K) aligned with outcome_indices")
    vals = [rps(P[i], idx[i]) for i in range(P.shape[0])]
    vals = [v for v in vals if np.isfinite(v)]
    return float(np.mean(vals)) if vals else float("nan")


# ---------------------------------------------------------------------------
# Baseline predictive distributions (the skill denominators)
# ---------------------------------------------------------------------------

def climatology_terciles(k: int = 3) -> np.ndarray:
    """The reference categorical forecast: equal probability on each of K
    categories (1/3 each for terciles). The floor any tercile forecast must beat."""
    return np.full(int(k), 1.0 / int(k))


def climatology_gaussian(history) -> tuple[float, float]:
    """(mean, sd) of the climatological distribution from a history sample — the
    'no-information' continuous baseline. NaN sd if < 2 finite points."""
    h = np.asarray(history, dtype=float)
    h = h[np.isfinite(h)]
    if h.size < 2:
        return (float(np.mean(h)) if h.size else float("nan"), float("nan"))
    return float(np.mean(h)), float(np.std(h, ddof=1))


def persistence_gaussian(last_value: float, innov_sd: float, leads):
    """Persistence baseline as a Gaussian per lead: mean = last observed value
    (flat), sd = innov_sd·√lead (a random walk's spread grows with √horizon).

    Returns (mu[L], sigma[L]) for the given lead array (1-based lead numbers)."""
    leads = np.asarray(leads, dtype=float)
    mu = np.full(leads.shape, float(last_value))
    sigma = float(innov_sd) * np.sqrt(np.maximum(leads, 0.0))
    return mu, sigma


def damped_persistence_gaussian(last_value: float, clim_mean: float,
                                innov_sd: float, leads, *, phi: float = 0.9):
    """Damped-persistence baseline: the mean reverts geometrically from the last
    observation toward the climatological mean — mean[L] = clim + φ^L·(last − clim)
    — a stronger floor than flat persistence for a mean-reverting series. sd grows
    as innov_sd·√lead. 0 < φ < 1 sets the reversion speed."""
    leads = np.asarray(leads, dtype=float)
    decay = np.power(float(phi), np.maximum(leads, 0.0))
    mu = float(clim_mean) + decay * (float(last_value) - float(clim_mean))
    sigma = float(innov_sd) * np.sqrt(np.maximum(leads, 0.0))
    return mu, sigma

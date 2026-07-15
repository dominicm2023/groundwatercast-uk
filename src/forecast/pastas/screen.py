"""Leakage-safe admission gate for the short-record fan tier.

A borehole with fewer than ``scope.MIN_ROWS`` (full-record) but at least
``scope.MIN_ROWS_FAN`` observations can still yield a useful 14-day fan — but
only if it *demonstrably* backtests. We calibrate on ``[start, last-gap_days]``,
forecast the held-out gap with the standard seeded AR1 model, and admit the
borehole only when the 14-day forecast:

  (a) covers observations at roughly the nominal rate (coverage floor — an
      over-wide band is safe, just less sharp, so there is NO upper cap; the
      danger is UNDER-coverage: a narrow band the truth falls outside), and
  (b) has a small median error relative to the borehole's observed range.

National testing (2026-07, 56 boreholes): gauge-rainfall short records pass
~65%; without this gate ~36% publish overconfident fans, and EVP cannot screen
them (EVP≈98% overfit stations miss; EVP≈0% flat stations forecast perfectly).
The gate is therefore mandatory and gauge-rainfall-only — the joined-fallback
rainfall (zero-filled gaps) fabricates droughts and fails far more often.

Pure calibration/forecast reuse — imports pastas lazily via ``recharge``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import recharge as R

# BAND-WIDENING + three criteria. The AR1 predictive band captures residual
# noise but NOT the recharge/rainfall forecast error on a rising limb, so
# short-record fans UNDER-cover there (overconfident). So we first CALIBRATE a
# per-borehole σ-inflation from the backtest — widen the band until it covers
# TARGET_COVERAGE of the held-out obs — and then gate the honestly-widened fan on:
#   SKILL   — the 14-day P50 must beat a persistence baseline (hold the last obs
#             flat). Unchanged by inflation (P50 doesn't move). Catches a wandering
#             point forecast (Pack Lane: P50 off ~3 m while the level barely moved).
#   CALIBRATION — coverage ≥ a floor. After inflation this is met by construction
#             (that's the point); the floor only guards the k=1 / capped cases.
#   SHARPNESS — the WIDENED band must still be a modest fraction of the record
#             range. This is what now rejects the genuinely unpredictable: a
#             borehole needing a huge σ-inflation to cover blows past this.
MAX_SKILL_RATIO = 1.15      # mean |obs−P50| / persistence MAE (≤1 = beats it)
TARGET_COVERAGE_PCT = 80.0  # widen σ so the band covers this fraction of held-out obs
MIN_COVERAGE_PCT = 50.0     # post-inflation coverage floor (guards k=1 / capped)
MAX_BAND_FRAC = 0.60        # WIDENED mean (P90−P10) as a fraction of observed range
MIN_EVAL_OBS = 7            # need this many held-out obs in a window to score it
MIN_TRAIN_ROWS = 600        # a hindcast on < this leaves too little to fit
# Evaluate at SEVERAL origins (days before the last obs) and average — a single
# 14-point window is season-sensitive (a steep winter rising limb can dip
# coverage even when the point forecast is spot-on), so the gate would otherwise
# reject good boreholes by luck of the backtest date.
ORIGINS_BACK = (45, 90, 135)
MIN_ORIGINS = 2             # need this many evaluable origins to reach a verdict
WINDOW = 14                 # the operational (tankering) horizon we gate on
_Z90 = 1.2815515594         # P90 of the standard normal
_EPS = 1e-6


def _inflation_factor(abs_z: np.ndarray, target_frac: float) -> float:
    """σ-multiplier that makes the P10–P90 band cover ``target_frac`` of the
    held-out obs: k = quantile(|standardised residual|, target) / Z90.

    |standardised residual| = |obs − P50| / σ_base(lead). An obs is inside the
    inflated band iff |z| ≤ k·Z90, so k = P_target(|z|)/Z90 covers exactly
    ``target_frac`` of them. Floored at 1.0 — only WIDEN an over-confident band,
    never NARROW a safe one (a limited backtest can't justify sharpening)."""
    if abs_z.size == 0:
        return 1.0
    q = float(np.quantile(abs_z, target_frac))
    return max(1.0, q / _Z90)


def gate_pass(cov: float | None, skill_ratio: float | None, band_frac: float | None,
              *, min_coverage_pct: float = MIN_COVERAGE_PCT,
              max_skill_ratio: float = MAX_SKILL_RATIO,
              max_band_frac: float = MAX_BAND_FRAC) -> bool:
    """Admit iff the forecast beats persistence, is not over-confident, and is
    sharp enough to be useful:

      skill_ratio  = mean|obs−P50| / persistence-MAE   ≤ max_skill_ratio
      cov          = mean P10–P90 coverage (%)          ≥ min_coverage_pct
      band_frac    = mean(P90−P10) / observed range     ≤ max_band_frac

    Any None (not evaluable) fails."""
    if cov is None or skill_ratio is None or band_frac is None:
        return False
    return bool(skill_ratio <= max_skill_ratio
                and cov >= min_coverage_pct
                and band_frac <= max_band_frac)


def _score_origin(station_id: str, h: pd.Series, prec: pd.Series, evap: pd.Series,
                  *, rfunc: str, recharge: str, precip_source: str,
                  back: int, window: int) -> dict | None:
    """One leakage-safe origin: calibrate on ``head ≤ last-back``, forecast the
    next ``window`` days, and score coverage + MAE. None if not evaluable."""
    cutoff = h.index.max() - pd.Timedelta(days=back)
    train = h[h.index <= cutoff]
    if len(train) < MIN_TRAIN_ROWS:
        return None
    try:
        rec = R.calibrate(station_id, train, prec, evap, train_max=cutoff,
                          rfunc=rfunc, recharge=recharge, precip_source=precip_source)
        win, mean, sig = R.seeded_forecast(rec, train, prec, evap,
                                           origin=cutoff, horizon=window)
    except Exception:                                          # pragma: no cover
        return None
    obs = h.reindex(win).to_numpy(float)
    valid = np.isfinite(obs)
    n = int(valid.sum())
    if n < MIN_EVAL_OBS:
        return None
    o = obs[valid]
    p50 = mean[valid]
    sigv = sig[valid]
    p10 = p50 - _Z90 * sigv
    p90 = p50 + _Z90 * sigv
    cov = 100.0 * float(((o >= p10) & (o <= p90)).sum()) / n
    mae = float(np.abs(o - p50).mean())
    # Persistence baseline: hold the last training observation flat over the
    # window. A fan worth publishing must beat it (SKILL); a flat borehole ties.
    mae_persist = float(np.abs(o - float(train.iloc[-1])).mean())
    band = float(np.mean(p90 - p10))                       # mean P10–P90 width (m)
    # |standardised residuals| — the raw material for the σ-inflation calibration
    # (pooled across origins upstream). Uses the BASE per-lead σ (the calibrated
    # rec carries no sigma_inflation here → simulate_path used 1.0).
    absz = np.abs(o - p50) / np.clip(sigv, _EPS, None)
    return {"back": int(back), "n_train": int(len(train)), "n_eval": n,
            "cov": round(cov, 1), "mae": round(mae, 4),
            "mae_persist": round(mae_persist, 4), "band": round(band, 4),
            "absz": absz}


def leakage_safe_hindcast(station_id: str, head: pd.Series, prec: pd.Series,
                          evap: pd.Series, *, rfunc: str, recharge: str,
                          precip_source: str,
                          origins_back: tuple[int, ...] = ORIGINS_BACK,
                          window: int = WINDOW, min_origins: int = MIN_ORIGINS,
                          target_coverage_pct: float = TARGET_COVERAGE_PCT,
                          min_coverage_pct: float = MIN_COVERAGE_PCT,
                          max_skill_ratio: float = MAX_SKILL_RATIO,
                          max_band_frac: float = MAX_BAND_FRAC) -> dict:
    """Backtest ``window``-day forecasts at several origins, calibrate a σ-inflation
    so the band honestly covers ``target_coverage_pct`` of held-out obs, then gate
    the WIDENED fan on skill (vs persistence), coverage and sharpness.

    Returns a JSON-serialisable dict recording the verdict + metrics (stored on
    the ModelRec as ``rec["hindcast"]`` for provenance):
      gate_pass, sigma_inflation, cov14 (post-inflation), base_cov14, mae14,
      skill_ratio, band_frac (post-inflation), range_m, n_origins, origins, reason.
    Never raises — a gate failure must not abort a calibration batch.
    """
    out = {"gate_pass": False, "sigma_inflation": 1.0, "cov14": None,
           "base_cov14": None, "mae14": None, "skill_ratio": None,
           "band_frac": None, "range_m": None, "n_origins": 0, "origins": [],
           "window": int(window), "reason": ""}
    h = head.copy()
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    h = h[~h.index.duplicated(keep="last")].dropna().sort_index()
    if h.empty:
        out["reason"] = "no_obs"; return out
    rng = float(h.max() - h.min())
    out["range_m"] = rng

    per = [s for b in origins_back
           if (s := _score_origin(station_id, h, prec, evap, rfunc=rfunc,
                                   recharge=recharge, precip_source=precip_source,
                                   back=b, window=window)) is not None]
    out["n_origins"] = len(per)
    if len(per) < min_origins:
        for p in per: p.pop("absz", None)          # keep stored origins JSON-clean
        out["origins"] = per
        out["reason"] = f"origins<{min_origins}"; return out

    # Pool |standardised residuals| across origins → the σ-inflation that lifts
    # coverage to target; then strip absz so the stored per-origin dicts stay small.
    allz = np.concatenate([np.abs(np.asarray(p["absz"], float)) for p in per])
    for p in per: p.pop("absz", None)
    out["origins"] = per
    k = _inflation_factor(allz, target_coverage_pct / 100.0)

    base_cov = float(np.mean([p["cov"] for p in per]))
    cov = 100.0 * float((allz <= k * _Z90).mean())          # coverage of the WIDENED band
    mae = float(np.mean([p["mae"] for p in per]))
    mae_persist = float(np.mean([p["mae_persist"] for p in per]))
    band = k * float(np.mean([p["band"] for p in per]))     # WIDENED mean band width
    skill_ratio = mae / max(mae_persist, _EPS)
    band_frac = band / rng if rng > 0 else float("inf")
    out["sigma_inflation"] = round(k, 3)
    out["base_cov14"] = round(base_cov, 1)
    out["cov14"] = round(cov, 1)
    out["mae14"] = round(mae, 4)
    out["skill_ratio"] = round(skill_ratio, 3)
    out["band_frac"] = round(band_frac, 3)
    out["gate_pass"] = gate_pass(cov, skill_ratio, band_frac,
                                 min_coverage_pct=min_coverage_pct,
                                 max_skill_ratio=max_skill_ratio,
                                 max_band_frac=max_band_frac)
    out["reason"] = ("pass" if out["gate_pass"]
                     else f"skill={skill_ratio:.2f} cov={cov:.0f}% "
                          f"band/rng={band_frac:.0%} (σ×{k:.1f})")
    return out

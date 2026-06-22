"""Trend screen — per-borehole non-stationarity diagnostic (Tier 1, report-only).

Flags boreholes whose GW level carries a strong multi-year trend. Such a trend
breaks the stationary, rainfall-driven Pastas forecast (it mean-reverts) and
skews the normals/threshold — and usually signals either a data artefact
(sensor/datum drift) or a real-but-model-incompatible signal (e.g. groundwater
rebound). Trend SHAPE alone cannot tell those apart (both are near-linear), so
this module reports three discriminating signals and an action, but acts on
nothing:

  1. rainfall coherence — a real recharge-driven rise tracks the cumulative
     rainfall-anomaly state; a datum/transducer drift is monotonic and
     rainfall-independent (low coherence).
  2. neighbour isolation — radius-based (note: the catalogue ``aquifer_*``
     fields are 3-value productivity CLASSES, not formations, so distance is
     the primary geological proxy). Regional+rising => likely real; isolated
     => ambiguous (artefact OR hyper-local real).
  3. metadata — datum survey / abstraction licence, the human final arbiter.

Pure numpy/pandas (never imports pastas; importable in either env). I/O lives
in ``scripts/build_trend_screen.py``. See ``docs/trend_screen.md``.

Calibration anchors (locked by tests/test_trend_screen.py):
  Moor Hall      +0.70 m/yr, R^2 0.99, isolated, rainfall-incoherent -> HIGH /
                 artifact_like / review_exclude
  Liverpool Nth  +0.013 m/yr                                         -> not flagged
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_YEAR_DAYS = 365.25


# ---------------------------------------------------------------------------
# Slope estimators
# ---------------------------------------------------------------------------

def theil_sen_slope(x, y) -> float:
    """Median of pairwise slopes — robust to a single datum step / outliers.

    Used as the PRIMARY magnitude so one big jump can't inflate the trend the
    way it inflates OLS. Cheap on a monthly series (~100 points)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    m = np.isfinite(x) & np.isfinite(y)
    x, y = x[m], y[m]
    if len(x) < 2:
        return float("nan")
    i, j = np.triu_indices(len(x), k=1)
    dx = x[j] - x[i]
    ok = dx != 0
    if not ok.any():
        return float("nan")
    return float(np.median((y[j] - y[i])[ok] / dx[ok]))


def _years_axis(idx: pd.DatetimeIndex) -> np.ndarray:
    return (idx - idx[0]).days.to_numpy(dtype=float) / _YEAR_DAYS


def fit_trend(monthly: pd.Series) -> dict:
    """OLS + Theil-Sen slope (m/yr) and linear R^2 on a monthly-mean GW series."""
    s = monthly.dropna()
    if len(s) < 3:
        return dict(slope_ols=np.nan, slope_sen=np.nan, r2=np.nan, intercept=np.nan)
    x = _years_axis(s.index)
    y = s.to_numpy(dtype=float)
    b1, b0 = np.polyfit(x, y, 1)
    yhat = b1 * x + b0
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return dict(slope_ols=float(b1), slope_sen=theil_sen_slope(x, y),
                r2=float(r2), intercept=float(b0))


# ---------------------------------------------------------------------------
# Scale + shape metrics
# ---------------------------------------------------------------------------

def seasonal_amplitude(monthly: pd.Series, slope: float, intercept: float) -> float:
    """Median within-water-year (Oct-Sep) range of the trend-removed monthly series.

    The borehole's own natural seasonal swing — an aquifer-agnostic scale to
    normalise the trend against (see ``drift_ratio``)."""
    s = monthly.dropna()
    if len(s) < 12 or not np.isfinite(slope):
        return float("nan")
    x = _years_axis(s.index)
    detr = pd.Series(s.to_numpy(float) - (slope * x + intercept), index=s.index)
    wy = detr.index.year + (detr.index.month >= 10).astype(int)
    grp = detr.groupby(wy)
    rng = (grp.max() - grp.min())[grp.count() >= 6]
    return float(rng.median()) if len(rng) else float("nan")


def step_metrics(daily: pd.Series, step_threshold: float = 0.30,
                 rel_k: float = 4.0) -> dict:
    """Same-day |diff| stats — a discrete datum jump vs a gradual move.

    Resample to a regular daily grid BEFORE differencing so ``.diff()`` only
    ever compares consecutive calendar days. On sparse (dipped, ~monthly) or
    gappy (telemetry-outage) series, differencing the raw observations would
    treat a normal multi-week move as a single "step". Gap days are NaN, so any
    diff spanning a gap is NaN and never counts; a series too sparse to have
    consecutive days reports no steps.

    A jump counts as a step only if it is both ABSOLUTELY large (> step_threshold)
    AND an extreme OUTLIER vs the borehole's own daily movement (> rel_k x its
    95th-percentile daily |diff|). This stops a responsive aquifer's routine
    recharge — big day-to-day moves that are normal *for it* — from reading as a
    datum jump, while a true discontinuity in an otherwise-quiet record (a tiny
    95th-percentile scale) still stands out."""
    s = daily.dropna()
    if s.empty:
        return dict(max_daily_step=np.nan, n_steps=0)
    d = s.resample("D").mean().diff().abs().dropna()
    if d.empty:
        return dict(max_daily_step=np.nan, n_steps=0)
    scale = float(d.quantile(0.95))
    is_step = (d > step_threshold) & (d > rel_k * scale)
    return dict(max_daily_step=float(d.max()), n_steps=int(is_step.sum()))


def _deseasonalise(monthly: pd.Series) -> pd.Series:
    s = monthly.dropna()
    return s - s.groupby(s.index.month).transform("mean")


def rain_coherence(monthly_gw: pd.Series, monthly_rain: pd.Series | None) -> dict:
    """Pearson corr of de-seasonalised GW anomaly vs cumulative rainfall anomaly.

    High => the rise tracks recharge (real-signal-like); low => monotonic drift
    unrelated to rainfall (artefact-like)."""
    gw = _deseasonalise(monthly_gw)
    if monthly_rain is None or monthly_rain.dropna().empty:
        return dict(rain_corr=np.nan, rain_months=0)
    rain = monthly_rain.dropna()
    cum = (rain - rain.groupby(rain.index.month).transform("mean")).cumsum()
    j = pd.concat([gw.rename("gw"), cum.rename("cum")], axis=1).dropna()
    if len(j) < 12:
        return dict(rain_corr=np.nan, rain_months=int(len(j)))
    c = j["gw"].corr(j["cum"])
    return dict(rain_corr=float(c) if pd.notna(c) else np.nan, rain_months=int(len(j)))


# ---------------------------------------------------------------------------
# Neighbour isolation + classification
# ---------------------------------------------------------------------------

def neighbour_isolation(subject_slope: float, neighbour_slopes, cfg: dict) -> dict:
    """Isolated (neighbours flat) vs regional (neighbours trend with it).

    ``isolated`` does NOT mean artefact on its own — it is combined with
    rainfall coherence in :func:`classify`."""
    nb = cfg.get("neighbour", {})
    slope_min = float(cfg["slope_min_m_per_yr"])
    ratio = float(nb.get("isolation_slope_ratio", 3.0))
    min_n = int(nb.get("min_neighbours", 2))
    ns = [float(s) for s in neighbour_slopes if s is not None and np.isfinite(s)]
    if len(ns) < min_n or not np.isfinite(subject_slope):
        return dict(isolation_class="no_neighbours", neighbour_count=len(ns),
                    neighbour_median_slope=np.nan)
    med = float(np.median(ns))
    eps = 1e-6
    if abs(med) < slope_min and abs(subject_slope) >= ratio * max(abs(med), eps):
        cls = "isolated"
    elif np.sign(med) == np.sign(subject_slope) and abs(med) >= slope_min:
        cls = "regional"
    else:
        # has neighbours but neither clearly flat nor clearly co-trending
        cls = "no_neighbours"
    return dict(isolation_class=cls, neighbour_count=len(ns),
                neighbour_median_slope=med)


def classify(m: dict, cfg: dict) -> dict:
    """Severity + provenance_class + recommended_action from the metric dict.

    is_trend = strong + linear: r2>=r2_min AND (|slope_sen|>=slope_min OR
    drift_ratio>=drift_ratio_min). Provenance combines isolation with rainfall
    coherence so a coherent (real-looking) trend is NEVER tagged for exclusion."""
    r2 = m.get("r2", np.nan)
    slope = abs(m.get("slope_sen", np.nan))
    drift = m.get("drift_ratio", np.nan)
    change = m.get("trend_change_m", np.nan)
    change_min = float(cfg.get("change_min_m", 0.15))
    # The drift-ratio path also requires a minimum ABSOLUTE change: against a
    # tiny seasonal swing a few-cm drift clears the ratio, but a borehole that
    # barely moves shouldn't be called a "trend" — that's noise, not signal.
    is_trend = (
        np.isfinite(r2) and r2 >= cfg["r2_min"]
        and ((np.isfinite(slope) and slope >= cfg["slope_min_m_per_yr"])
             or (np.isfinite(drift) and drift >= cfg["drift_ratio_min"]
                 and np.isfinite(change) and change >= change_min))
    )
    if not is_trend:
        # Step-shift detection (datum re-survey / transducer recalibration): a
        # single large daily jump barely moves the Theil-Sen slope (median of
        # pairwise slopes), so it slips the linear is_trend test above — yet it
        # corrupts the normals ladder and the below/near/above-normal status.
        # Surface it for review off the already-computed step metrics (which
        # classify() previously ignored). Report-only, like the rest of the
        # screen — a human checks the datum survey (recommended_action).
        # A genuine datum/sensor event is rainfall-INDEPENDENT; a borehole whose
        # jumps track rainfall (high rain_corr) is a flashy recharge response,
        # not an artefact — so gate on rainfall incoherence (mirrors the
        # artifact_like test) to stop responsive aquifers reading as datum jumps.
        n_steps = int(m.get("n_steps", 0) or 0)
        max_step = m.get("max_daily_step", np.nan)
        rc = m.get("rain_corr", np.nan)
        rain_coherent = np.isfinite(rc) and rc >= cfg["rain_corr_min"]
        if (np.isfinite(max_step) and n_steps >= int(cfg.get("step_min_count", 1))
                and not rain_coherent):
            if max_step >= cfg["change_high_m"]:
                step_sev = "high"
            elif max_step >= cfg["change_med_m"]:
                step_sev = "medium"
            else:
                step_sev = "low"
            return dict(is_trend=False, severity=step_sev,
                        provenance_class="step_shift",
                        recommended_action="metadata_check")
        return dict(is_trend=False, severity="none",
                    provenance_class="stationary", recommended_action="none")
    if np.isfinite(change) and change >= cfg["change_high_m"]:
        sev = "high"
    elif np.isfinite(change) and change >= cfg["change_med_m"]:
        sev = "medium"
    else:
        sev = "low"
    iso = m.get("isolation_class", "no_neighbours")
    rc = m.get("rain_corr", np.nan)
    rc_min = cfg["rain_corr_min"]
    if iso == "isolated":
        if not np.isfinite(rc):
            prov, act = "indeterminate", "metadata_check"
        elif rc < rc_min:
            prov, act = "artifact_like", "review_exclude"
        else:
            prov, act = "local_real_candidate", "metadata_check"
    elif iso == "regional":
        prov, act = "regional_real", "review_detrend_or_keep"
    else:
        prov, act = "indeterminate", "metadata_check"
    return dict(is_trend=True, severity=sev,
                provenance_class=prov, recommended_action=act)


# ---------------------------------------------------------------------------
# Per-borehole driver-facing helper
# ---------------------------------------------------------------------------

def screen_series(daily_gw: pd.Series, daily_rain: pd.Series | None, cfg: dict) -> dict:
    """All per-borehole metrics (no neighbour cross-check; that needs the fleet).

    ``daily_gw`` / ``daily_rain`` are datetime-indexed daily series. Rainfall is
    aggregated to monthly TOTALS for the coherence test."""
    s = daily_gw.dropna()
    out = dict(n_obs=int(len(s)))
    if len(s) < 2:
        return out
    out["first_date"] = s.index.min()
    out["last_date"] = s.index.max()
    out["record_years"] = (s.index.max() - s.index.min()).days / _YEAR_DAYS
    monthly = s.resample("MS").mean()
    out["monthly_gw"] = monthly  # kept for the neighbour pass; dropped before CSV
    ft = fit_trend(monthly)
    out.update(ft)
    out["seasonal_amp_m"] = seasonal_amplitude(monthly, ft["slope_ols"], ft["intercept"])
    out["trend_change_m"] = (abs(ft["slope_sen"]) * out["record_years"]
                             if np.isfinite(ft["slope_sen"]) else np.nan)
    amp = out["seasonal_amp_m"]
    out["drift_ratio"] = (out["trend_change_m"] / amp
                          if (np.isfinite(amp) and amp > 1e-6) else np.nan)
    out.update(step_metrics(s, float(cfg.get("step_threshold_m", 0.30)),
                            float(cfg.get("step_rel_k", 4.0))))
    monthly_rain = (daily_rain.dropna().resample("MS").sum()
                    if daily_rain is not None and not daily_rain.dropna().empty else None)
    out.update(rain_coherence(monthly, monthly_rain))
    return out

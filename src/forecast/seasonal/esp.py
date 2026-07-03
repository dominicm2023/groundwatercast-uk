"""ESP (Ensemble Streamflow Prediction) trace mechanics + tercile math.

The seasonal outlook's spread comes from ~34 historic-year forcing traces:
"what would groundwater do from today's state if the next 6 months rained
like 1995? like 2012? ..." — the classic ESP method. The GW system's memory
(today's level + recession dynamics) carries most of the signal; the trace
library supplies an honest climatological spread; SEAS5 tercile weighting
(seas5.py) tilts that spread with what seasonal-scale skill exists.

All pure numpy/pandas, no I/O.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm

# Forecast-calendar span each ESP run must cover: the run-in to the first full
# outlook month (0-30 days — the origin is the fan terminal, so its day-of-month
# is arbitrary) plus six calendar months (181-184 days). 183 was too short: month
# 6 was published from 0-30 days of trace (a month-start-biased partial mean, and
# an all-NaN row for a month-1st origin).
TRACE_DAYS = 215


def trace_windows(origin: pd.Timestamp, years: list[int], *,
                  days: int = TRACE_DAYS) -> dict[int, pd.DatetimeIndex]:
    """Per historic year: the `days`-long daily window starting at the
    origin's month-day in that year.

    Leap handling: an origin of 29 Feb maps to 28 Feb in non-leap years.
    Windows may cross a year boundary (origin in autumn) — that's fine,
    the window simply runs into year+1 of the archive.
    """
    origin = pd.Timestamp(origin).normalize()
    out: dict[int, pd.DatetimeIndex] = {}
    for y in years:
        month, day = origin.month, origin.day
        if month == 2 and day == 29:
            day = 28
        start = pd.Timestamp(year=y, month=month, day=day)
        out[y] = pd.date_range(start, periods=days, freq="D")
    return out


def monthly_anchors(origin: pd.Timestamp, months: int = 6) -> list[pd.Period]:
    """The `months` calendar months immediately after the origin's month.

    An origin of 12 Jun → Jul, Aug, ... Dec (month_ahead 1..6): the first
    full calendar month is the first the outlook can speak about.
    """
    origin = pd.Timestamp(origin)
    first = (origin + pd.offsets.MonthBegin(1)).to_period("M")
    return [first + i for i in range(months)]


def monthly_means(values: pd.Series, periods: list[pd.Period]) -> np.ndarray:
    """Mean of a daily series within each calendar-month period (NaN when a
    month has no data)."""
    s = values.copy()
    s.index = pd.PeriodIndex(pd.to_datetime(s.index), freq="M")
    by_month = s.groupby(level=0).mean()
    return np.array([float(by_month.get(p, np.nan)) for p in periods])


def tercile_of(value: float, t1: float, t2: float) -> int:
    """0 = below (< t1), 1 = near, 2 = above (> t2)."""
    if value < t1:
        return 0
    if value > t2:
        return 2
    return 1


def trace_weights(trace_monthly_precip: dict[int, np.ndarray],
                  seas5_tercile_probs: np.ndarray | None,
                  clim_bounds: np.ndarray, *,
                  weight_months: int = 3) -> dict[int, float]:
    """Per-trace weights from SEAS5 tercile probabilities.

    trace_monthly_precip : {year: monthly precip totals over the outlook
                            months} (only the first ``weight_months`` are used)
    seas5_tercile_probs  : M×3 array — P(below/near/above) per month from the
                           SEAS5 members (seas5.tercile_probs), or None.
    clim_bounds          : M×2 array — (t1, t2) climatology tercile bounds
                           per outlook month, on the same precip scale.

    w_trace ∝ Π over the first ``weight_months``: P_SEAS5(tercile that the
    trace's month-m precip falls in). With no SEAS5 (None) → uniform.
    Degenerate weighting (all traces in one tercile and SEAS5 flat, or a
    zero product everywhere) safely renormalises / falls back to uniform.
    """
    years = sorted(trace_monthly_precip)
    n = len(years)
    if n == 0:
        return {}
    if seas5_tercile_probs is None:
        return {y: 1.0 / n for y in years}

    m_use = min(weight_months, len(clim_bounds), len(seas5_tercile_probs))
    w = {}
    for y in years:
        p = 1.0
        for m in range(m_use):
            v = trace_monthly_precip[y][m]
            if not np.isfinite(v):
                continue
            terc = tercile_of(float(v), float(clim_bounds[m][0]),
                              float(clim_bounds[m][1]))
            p *= float(seas5_tercile_probs[m][terc])
        w[y] = p
    total = sum(w.values())
    if total <= 0:
        return {y: 1.0 / n for y in years}
    return {y: v / total for y, v in w.items()}


def weighted_tercile_probs(mu: np.ndarray, sigma: np.ndarray,
                           weights: np.ndarray,
                           t1: float, t2: float) -> tuple[float, float, float]:
    """P(below / near / above) for one month via a weighted normal mixture.

    mu, sigma : per-trace monthly-mean GW and its predictive sd
    weights   : per-trace weights (will be renormalised over finite traces)
    t1, t2    : the borehole's climatological tercile bounds for the month
    """
    mu = np.asarray(mu, float)
    sigma = np.clip(np.asarray(sigma, float), 1e-6, None)
    w = np.asarray(weights, float)
    ok = np.isfinite(mu) & np.isfinite(w)
    if not ok.any():
        return (np.nan, np.nan, np.nan)
    mu, sigma, w = mu[ok], sigma[ok], w[ok]
    w = w / w.sum()
    p_below = float(np.sum(w * norm.cdf((t1 - mu) / sigma)))
    p_above = float(np.sum(w * (1.0 - norm.cdf((t2 - mu) / sigma))))
    return (p_below, max(0.0, 1.0 - p_below - p_above), p_above)


def weighted_quantiles(values: np.ndarray, weights: np.ndarray,
                       qs: tuple[float, ...] = (0.10, 0.50, 0.90)
                       ) -> list[float]:
    """Weighted quantiles of per-trace values (NaNs dropped)."""
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    ok = np.isfinite(v) & np.isfinite(w)
    if not ok.any():
        return [np.nan] * len(qs)
    v, w = v[ok], w[ok]
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w) - 0.5 * w
    cw = cw / w.sum()
    return [float(np.interp(q, cw, v)) for q in qs]


def weighted_var(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted variance of per-trace monthly means — the climatological
    (between-analog-year) spread V_esp (NaNs dropped; 0 if <2 finite traces)."""
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    ok = np.isfinite(v) & np.isfinite(w)
    if ok.sum() < 2:
        return 0.0
    v, w = v[ok], w[ok]
    w = w / w.sum()
    mean = float(np.sum(w * v))
    return float(np.sum(w * (v - mean) ** 2))


def state_memory_timescale(rec: dict, *, q: float = 0.95,
                           min_days: float = 30.0,
                           max_days: float = 2000.0) -> float:
    """State-memory timescale τ_state (days): how long a head anomaly persists,
    governing the decay of the inherited fan-terminal uncertainty.

    Either of two model clocks can be the slower (dominant) one, so we take the
    MAX of both — the conservative "anomaly persists at least this long" choice:
      - the Gamma response's q-time, t_q = a·γ⁻¹(n, q) (regularised inverse lower
        incomplete gamma) — the deterministic recession memory; and
      - the AR1 residual-noise decorrelation ``alpha`` — the timescale on which
        simulate_path actually decays the seed residual (recharge.py r0·e^(−Δt/α)).
    The debate assumed alpha is always fast (days); in practice a chalk borehole
    can have a heavy-tailed Gamma (n<1) whose 95%-time understates its memory AND
    an alpha of weeks-months — so neither alone is safe, hence the max. Clamped
    to [min_days, max_days]; degenerate fits fall back to alpha (then the clamp)."""
    from scipy.special import gammaincinv
    pmap = dict(zip(rec.get("param_names", []), rec.get("params", [])))
    # Gamma rfunc params on the recharge stress: "<name>_n" (shape), "<name>_a"
    # (scale, lower-case a — distinct from "_A" gain). Case-sensitive match.
    n = next((v for k, v in pmap.items() if k.endswith("_n")), None)
    a = next((v for k, v in pmap.items() if k.endswith("_a")), None)
    tau_gamma = 0.0
    if (n is not None and a is not None and np.isfinite(n) and np.isfinite(a)
            and n > 0 and a > 0):
        try:
            tau_gamma = float(a * gammaincinv(float(n), q))
        except Exception:
            tau_gamma = 0.0
    alpha = float(rec.get("alpha") or 0.0)
    tau = max(tau_gamma, alpha if np.isfinite(alpha) else 0.0)
    if not np.isfinite(tau) or tau <= 0:
        tau = min_days
    return float(np.clip(tau, min_days, max_days))


def additive_band(mu: np.ndarray, weights: np.ndarray, *,
                  sigma: float, alpha: float, tau_state: float,
                  sd46: float, dt46: float, dt_month: float, lead_gap: float,
                  z: float = 1.2815515594) -> tuple[float, float, float]:
    """Corrected-additive seasonal monthly band (the uncertainty-inheritance
    fallback). band = p50 ± z·√(V_ar1 + V_esp + V_inherit), where

      p50       = weighted median of the analog monthly means (unchanged)
      V_esp     = weighted_var(mu)                         — climatological spread
      V_ar1     = σ²(1 − e^(−2·dt_month/α))                — model AR1, last-obs-
                                                             clocked, counted ONCE
      V_inherit = s_state²·e^(−2·lead_gap/τ_state)         — the fan's terminal
                  forecast-weather/state uncertainty, DECAYING on the slow aquifer
                  memory τ_state; s_state² = sd46² − σ²(1 − e^(−2·dt46/α))
                  (the fan terminal variance with its AR1 part removed).

    Continuous with the fan at month-1 (V_ar1 carries the floor even when the
    member spread — hence s_state² — is ~0), converging to (AR1 ⊕ climatology)
    at long lead. dt_month / lead_gap are days to the month's mid-point from the
    last real obs / the fan terminal respectively."""
    a = max(float(alpha), 1e-6)
    p50 = weighted_quantiles(mu, weights, qs=(0.5,))[0]
    v_esp = weighted_var(mu, weights)
    v_ar1 = sigma ** 2 * (1.0 - np.exp(-2.0 * max(dt_month, 0.0) / a))
    v_ar1_46 = sigma ** 2 * (1.0 - np.exp(-2.0 * max(dt46, 0.0) / a))
    s_state2 = max(sd46 ** 2 - v_ar1_46, 0.0)
    v_inherit = s_state2 * np.exp(-2.0 * max(lead_gap, 0.0) / max(tau_state, 1e-6))
    sd = float(np.sqrt(max(v_ar1 + v_esp + v_inherit, 0.0)))
    return p50 - z * sd, p50, p50 + z * sd

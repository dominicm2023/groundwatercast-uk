"""Aggregate Pastas member trajectories into the probabilistic summary (module 3).

Unlike the roll's aggregator (ensemble/aggregate.py), the Pastas fan and breach
probability must combine BOTH uncertainty sources:
  - member (rainfall) spread — small over a 14-day chalk window, and
  - the calibrated AR1 noise band (gw_sigma) — the dominant, persistent term.

We therefore Monte-Carlo full trajectories: each draw picks a member point path
and adds a correlated AR1 noise path (phi = exp(-1/alpha), marginal sd growing to
the model residual sigma), then we read the fan / breach / first-crossing off the
sampled trajectories. Output schema mirrors the roll summary so the dashboard/PDF
(module 4) can render either, plus a roll-vs-Pastas **model-spread** band — the
"keep both" uncertainty signal.

Pure numpy/pandas — no pastas import (runs in either environment).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.forecast.ensemble.thresholds import resolve_threshold
from src.forecast.ensemble.aggregate import headline_sentence

SUMMARY_COLS = ["station_id", "run", "scope", "origin_date", "stale_days", "horizon_days",
                "threshold", "threshold_source",
                "p_breach", "p_breach_14d",
                "first_cross_median", "first_cross_p25", "first_cross_p75",
                "first_cross_median_lead", "censored_frac", "gw_p50_end",
                "p_above_p90_14d", "model_spread_mean", "n_members", "n_samples",
                "headline"]

# Tiering window: breach probability grows with window length, and the
# dashboard tiers were calibrated on 14-day windows — so p_breach_14d and
# p_above_p90_14d are computed over this window while p_breach /
# first-crossing cover the full (possibly extended, 46-day) horizon.
OPERATIONAL_WINDOW_DAYS = 14


def above_p90_prob(gw_samples: np.ndarray, dates: pd.DatetimeIndex,
                   p90_by_month: dict[int, float]) -> float:
    """P(any day's level exceeds that calendar month's P90 normal) over the
    MC trajectories — "unusually high for the season", the tier's secondary
    signal (replaces the retired composite-risk p_risk_high; same ~10%
    baseline rarity, so tier thresholds keep their calibration).

    gw_samples   : n×H trajectories (already truncated to the window)
    dates        : the H forecast dates (calendar months drive the bound)
    p90_by_month : {calendar month → p90 normal} from gw_monthly_normals;
                   months without a normal are skipped (can't exceed an
                   unknown bound). NaN when no month has a normal.
    """
    H = gw_samples.shape[1]
    bounds = np.array([p90_by_month.get(pd.Timestamp(d).month, np.nan)
                       for d in dates[:H]], dtype=float)
    ok = np.isfinite(bounds)
    if not ok.any():
        return float("nan")
    exceed = gw_samples[:, ok] > bounds[ok]
    return float(exceed.any(axis=1).mean())


def _ar1_noise(n: int, sd: np.ndarray, phi: float,
               rng: np.random.Generator) -> np.ndarray:
    """n×H AR1 noise paths matching the per-date marginal sd vector ``sd``.

    ``sd`` are the calendar-correct predictive sds (the ``gw_sigma`` column from
    module 2 — sd_k = sigma·sqrt(1−exp(−2·Δt_k/alpha)) with Δt measured from the
    last observation, so the obs→window gap is already baked in). We impose 1-day
    AR1 correlation phi and the exact marginals: z_0 ~ N(0, sd_0); for k≥1,
    z_k = phi·z_{k-1} + u_k with Var(u_k) = sd_k² − phi²·sd_{k-1}² (≥0 since sd
    grows). This fixes the earlier window-position bug that ignored the gap.
    """
    H = len(sd)
    z = np.empty((n, H))
    z[:, 0] = rng.normal(0.0, sd[0], size=n)
    for k in range(1, H):
        var_u = max(sd[k] ** 2 - (phi ** 2) * sd[k - 1] ** 2, 0.0)
        z[:, k] = phi * z[:, k - 1] + rng.normal(0.0, np.sqrt(var_u), size=n)
    return z


def mc_trajectories(W: np.ndarray, sigma_vec: np.ndarray, alpha: float, n: int,
                    rng: np.random.Generator) -> np.ndarray:
    """Sample n trajectories (n×H): a random member point path + AR1 noise.

    W is H×M (dates × members) of member gw_pred; ``sigma_vec`` is the per-date
    predictive sd (gw_sigma). With near-identical members (chalk, 14 d) the
    spread is carried by the noise; with real member spread both contribute.
    """
    H, M = W.shape
    phi = float(np.exp(-1.0 / alpha))
    members = rng.integers(0, M, size=n)
    base = W.T[members]                      # n×H member point paths
    return base + _ar1_noise(n, np.asarray(sigma_vec, float), phi, rng)


def _breach_from_samples(samples: np.ndarray, dates: pd.DatetimeIndex,
                         threshold: float | None, *,
                         operational_window_days: int = OPERATIONAL_WINDOW_DAYS
                         ) -> dict:
    H = samples.shape[1]
    out = {"horizon_days": H, "n_samples": samples.shape[0],
           "p_breach": np.nan, "p_breach_14d": np.nan,
           "censored_frac": np.nan,
           "first_cross_median": pd.NaT, "first_cross_p25": pd.NaT,
           "first_cross_p75": pd.NaT, "first_cross_median_lead": np.nan,
           "gw_p50_end": float(np.median(samples[:, -1]))}
    if threshold is None:
        return out
    ge = samples >= threshold                       # n×H
    crossed = ge.any(axis=1)
    p = float(crossed.mean())
    out["p_breach"] = p
    out["p_breach_14d"] = float(
        ge[:, :int(operational_window_days)].any(axis=1).mean())
    out["censored_frac"] = 1.0 - p
    if crossed.any():
        first_lead = ge[crossed].argmax(axis=1) + 1  # 1-based lead of first cross
        med, p25, p75 = (float(np.quantile(first_lead, q)) for q in (0.5, 0.25, 0.75))
        out["first_cross_median_lead"] = med
        dl = list(dates)
        # round half UP — mirrors aggregate.breach_stats (banker's round(2.5)==2
        # would date the crossing a day before the reported fractional lead)
        _half_up = lambda x: int(np.floor(x + 0.5))  # noqa: E731
        out["first_cross_median"] = dl[_half_up(med) - 1]
        out["first_cross_p25"] = dl[_half_up(p25) - 1]
        out["first_cross_p75"] = dl[_half_up(p75) - 1]
    return out


def aggregate_pastas(members_df: pd.DataFrame, models: dict, *,
                     run: pd.Timestamp,
                     gw_p90_by_station: dict[str, float] | None = None,
                     roll_p50_by_station: dict[str, pd.Series] | None = None,
                     monthly_p90_by_station: dict[str, dict[int, float]] | None = None,
                     n_samples: int = 4000, seed: int = 12345
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Aggregate Pastas member trajectories → (summary_df, fan_df).

    ``monthly_p90_by_station``: {station → {calendar month → p90 normal}}
    from gw_monthly_normals.csv — drives the p_above_p90_14d tier signal.
    """
    gw_p90_by_station = gw_p90_by_station or {}
    roll_p50_by_station = roll_p50_by_station or {}
    monthly_p90_by_station = monthly_p90_by_station or {}
    rng = np.random.default_rng(seed)
    summaries, fans = [], []

    has_segment = "segment" in members_df.columns
    for sid, g_all in members_df.groupby("station_id"):
        rec = models.get(sid)
        if rec is None:
            continue
        # Forecast members only drive the fan/breach/tier stats; the nowcast
        # (segment == "nowcast", the last-obs -> today gap on observed rainfall)
        # is aggregated separately below so it can't shift the 14-day windows.
        g = g_all[g_all["segment"] == "forecast"] if has_segment else g_all
        if g.empty:
            continue
        W = (g.pivot_table(index="date", columns="member", values="gw_pred")
             .sort_index())
        dates = pd.DatetimeIndex(W.index)
        # per-date predictive sd (calendar-correct, from module 2)
        sigma_vec = (g.groupby("date")["gw_sigma"].first().reindex(dates)
                     .to_numpy(float))
        samples = mc_trajectories(W.to_numpy(float), sigma_vec,
                                  float(rec["alpha"]), n_samples, rng)
        q10, q50, q90 = np.quantile(samples, [0.10, 0.50, 0.90], axis=0)

        roll = roll_p50_by_station.get(sid)
        roll_p50 = (roll.reindex(dates).to_numpy(float)
                    if roll is not None else np.full(len(dates), np.nan))
        model_spread = q50 - roll_p50
        spread_mean = (float(np.nanmean(np.abs(model_spread)))
                       if np.isfinite(model_spread).any() else np.nan)

        origin = (pd.Timestamp(g["origin_date"].iloc[0])
                  if "origin_date" in g.columns else pd.NaT)
        stale_days = (int((pd.Timestamp(run).tz_localize(None) - origin).days)
                      if pd.notna(origin) else np.nan)

        thr, src = resolve_threshold(sid, gw_p90=gw_p90_by_station.get(sid))
        stats = _breach_from_samples(samples, dates, thr)
        # Secondary tier signal, windowed at the operational 14 days like
        # p_breach_14d: P(unusually high for the season).
        op = samples[:, :OPERATIONAL_WINDOW_DAYS]
        p90s = monthly_p90_by_station.get(sid)
        p_above = (above_p90_prob(op, dates[:OPERATIONAL_WINDOW_DAYS], p90s)
                   if p90s else np.nan)
        # Provenance: members parquets pre-dating the scope column → "unknown".
        scope = str(g["scope"].iloc[0]) if "scope" in g.columns else "unknown"
        row = {"station_id": sid, "run": run, "scope": scope,
               "origin_date": (origin.date().isoformat() if pd.notna(origin) else None),
               "stale_days": stale_days, "threshold": thr,
               "threshold_source": src, "p_above_p90_14d": p_above,
               "model_spread_mean": spread_mean,
               "n_members": int(W.shape[1]), **stats}
        row["headline"] = headline_sentence(row)
        summaries.append(row)

        fans.append(pd.DataFrame({
            "station_id": sid, "run": run,
            "lead": range(1, len(dates) + 1), "date": dates,
            "gw_p10": q10, "gw_p50": q50, "gw_p90": q90,
            "roll_p50": roll_p50, "model_spread": model_spread,
            "segment": "forecast",
        }))

        # Nowcast fan over the observed-rainfall gap. Members are identical here,
        # so the band is the model sd alone (gw_sigma, grown from the last obs):
        # parametric P10/P90 = P50 ∓ 1.2816·sigma matches the forecast's Gaussian
        # quantiles, so the two segments join continuously at the window. Leads
        # are negative (days before the forecast start).
        if has_segment:
            nc = g_all[g_all["segment"] == "nowcast"].sort_values("date")
            if not nc.empty:
                z = 1.2815515594
                mu = nc["gw_pred"].to_numpy(float)
                sd = nc["gw_sigma"].to_numpy(float)
                fans.append(pd.DataFrame({
                    "station_id": sid, "run": run,
                    "lead": range(-len(nc), 0), "date": pd.DatetimeIndex(nc["date"]),
                    "gw_p10": mu - z * sd, "gw_p50": mu, "gw_p90": mu + z * sd,
                    "roll_p50": np.nan, "model_spread": np.nan, "segment": "nowcast",
                }))

    summary_df = (pd.DataFrame(summaries)[SUMMARY_COLS] if summaries
                  else pd.DataFrame(columns=SUMMARY_COLS))
    fan_df = pd.concat(fans, ignore_index=True) if fans else pd.DataFrame()
    return summary_df, fan_df

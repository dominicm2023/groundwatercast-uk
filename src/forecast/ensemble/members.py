"""Per-member forecast chain (design §5): bias-correct → bridge → recharge →
reduced-form GW roll → member GW trajectories.

This is the Phase 2 "done" artefact: for each pilot borehole, N member GW
trajectories over the forecast horizon, written to
`data/model/forecast_ensemble_members.parquet`.

Temporal-alignment note (MVP): we seed the roll at the borehole's last observed
GW level and roll over the provider's forecast dates, bridging the v19-extended
raw gauge rainfall (which reaches ~today) to each member's forecast. Precise
live-GW seeding / origin alignment is a Phase 4 refinement; on chalk the small
gap between the last GW obs and the forecast origin moves GW negligibly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.build import (
    load_timeseries, resample_to_daily, average_rainfall,
    compute_weibull_kernel, apply_weibull_recharge,
)
from src.forecast.ensemble import gw_roll
from src.forecast.ensemble.seeding import freshest_gw

_MEMBER_COLS = ["member", "date", "precip_mm", "recharge_weibull", "gw_pred"]


def gauge_rainfall_for(sid: str, links: pd.DataFrame | None,
                       raw_root: str) -> pd.Series:
    """Top-3-gauge daily rainfall for ONE borehole via its station_links row —
    the raw v19-extended series that reaches ~today. THE observed-rain source
    for Pastas calibration, the fan drivers AND the seasonal bridge (they must
    all use the same forcing, or the model is fitted on one rainfall reality
    and simulated over another). Empty series when the station has no link row
    or no gauge data — callers fall back to the joined column and record it.

    links : station_links.csv deduped + indexed on GWStationID (or None).
    """
    if links is None or sid not in links.index:
        return pd.Series(dtype="float64")
    rain_ids = [links.loc[sid].get(f"RainMeasureID_{i}") for i in (1, 2, 3)]
    return observed_daily_rainfall(rain_ids, raw_root)


# Long-run mean daily rainfall above this is not rain (England's wettest gauges
# run ~9.7 mm/day long-term): a broken/cumulative EA measure slips through the
# catalogue occasionally (seen live: a "gauge" averaging 128 mm/day) and, once
# averaged in, poisons the recharge forcing ~5x for every consumer.
_MAX_PLAUSIBLE_MEAN_MM_DAY = 12.0


def observed_daily_rainfall(rain_ids: list[str], raw_root: str) -> pd.Series:
    """Top-3-gauge averaged observed daily Rainfall (tz-naive), from raw files.
    Gauges with an implausible long-run mean are excluded (broken series)."""
    series = []
    for mid in rain_ids:
        if mid is None or (isinstance(mid, float) and pd.isna(mid)):
            series.append(None)
            continue
        raw = load_timeseries(str(mid), "rainfall", str(raw_root))
        daily = resample_to_daily(raw, agg="sum") if raw is not None else None
        if daily is not None and len(daily):
            m = float(daily["value"].mean())
            if m > _MAX_PLAUSIBLE_MEAN_MM_DAY:
                print(f"  ! rainfall gauge {str(mid)[:8]}…: implausible long-run "
                      f"mean {m:.1f} mm/day — excluded (broken/cumulative series)")
                daily = None
        series.append(daily)
    avg = average_rainfall(series)
    if avg is None or avg.empty:
        return pd.Series(dtype="float64")
    s = avg["value"].copy()
    s.index = pd.to_datetime(s.index)
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    return s.sort_index()


def forecast_recharge(observed_rain: pd.Series, member_forecast: pd.Series,
                      kernel: np.ndarray, forecast_dates: pd.DatetimeIndex) -> pd.Series:
    """Convolve the bridged (observed + member-forecast) daily rainfall with the
    Weibull kernel and return recharge at `forecast_dates`.

    Bridges on a contiguous daily index (missing days → 0 mm) so forecast
    recharge lands on the same scale as the feature pipeline's
    Recharge_Weibull.
    """
    obs = observed_rain.copy()
    obs.index = pd.to_datetime(obs.index).tz_localize(None) \
        if obs.index.tz is not None else pd.to_datetime(obs.index)
    mf = member_forecast.copy()
    mf.index = pd.to_datetime(mf.index)
    # Forecast wins on any overlapping date.
    full = pd.concat([obs[~obs.index.isin(mf.index)], mf]).sort_index()
    if full.empty:
        return pd.Series(index=forecast_dates, dtype="float64")
    idx = pd.date_range(full.index.min(), forecast_dates.max(), freq="D")
    full = full.reindex(idx).fillna(0.0)
    recharge = apply_weibull_recharge(full, kernel)
    return recharge.reindex(forecast_dates)


# Beyond this gap (days) between the two freshest shard observations, the
# implied recent momentum is too stale to trust as a one-day increment, so we
# keep the joined-feature daily delta rather than inject an unsupported guess.
_FRESH_SEED_DGW_MAX_GAP_DAYS = 14


def _seed_gw_dgw(hist: pd.DataFrame, fresh: pd.Series) -> tuple[float, float]:
    """Seed level + one-day momentum (dgw_prev) for the GW roll.

    Default: the joined-feature last level and its true one-day delta
    (GW_Level − GW_Lag1, daily by construction). When the per-station shard
    (archive + live flood-monitoring tail) is strictly more recent, reseed the
    LEVEL to the freshest observation — this fixes a ~5 m stale-seed error on
    flood-monitoring-matched boreholes (see ensemble/seeding.py).

    The shard is daily-normalised but NOT gap-free, so its last two observations
    can span several days. seed_dgw feeds the roll's *one-day* momentum term, so
    the inter-observation change is converted to a per-DAY rate (÷ the day gap);
    if the gap exceeds ``_FRESH_SEED_DGW_MAX_GAP_DAYS`` the momentum is stale and
    we keep the joined-feature daily delta. (Previously the raw inter-obs change
    was passed as a one-day increment — an N-day rise read as N× the true daily
    momentum, biasing the early fan in the breach-critical rising regime.)
    """
    seed_gw = float(hist.iloc[-1]["GW_Level"])
    seed_dgw = float(hist.iloc[-1]["GW_Level"] - hist.iloc[-1]["GW_Lag1"])
    if len(fresh) >= 2:
        hist_last = pd.Timestamp(hist.index.max())
        hist_last = hist_last.tz_localize(None) if hist_last.tz else hist_last
        if fresh.index.max() > hist_last.normalize():
            seed_gw = float(fresh.iloc[-1])
            gap_days = int((fresh.index[-1] - fresh.index[-2]).days)
            if 0 < gap_days <= _FRESH_SEED_DGW_MAX_GAP_DAYS:
                seed_dgw = float(fresh.iloc[-1] - fresh.iloc[-2]) / gap_days
    return seed_gw, seed_dgw


def member_trajectories(station_id: str, members_df: pd.DataFrame,
                        history: pd.DataFrame, kernel: np.ndarray, *,
                        f_bh: float = 1.0,
                        observed_rain: pd.Series,
                        method: str = "reduced_form_ar") -> pd.DataFrame:
    """Roll every member forward for one borehole.

    members_df : provider output [member, date, precip_mm] for this borehole.
    history    : the borehole's feature rows (GW_Level, GW_Lag1,
                 Recharge_Weibull, Sin_DOY, Cos_DOY), sorted ascending.
    method     : GW-roll method (default the hindcast-chosen reduced_form_ar).
    """
    if members_df.empty or history.empty:
        return pd.DataFrame(columns=_MEMBER_COLS)

    hist = history.sort_index()
    dgw_clip, gw_clip = gw_roll.station_guardrails(hist)
    seed_gw, seed_dgw = _seed_gw_dgw(hist, freshest_gw(station_id))
    params = gw_roll.fit(method, hist)

    forecast_dates = pd.DatetimeIndex(sorted(members_df["date"].unique()))
    doy = forecast_dates.day_of_year.values
    seasonal = pd.DataFrame({
        "Sin_DOY": np.sin(2 * np.pi * doy / 365.25),
        "Cos_DOY": np.cos(2 * np.pi * doy / 365.25),
    }, index=forecast_dates)

    out = []
    for m, grp in members_df.groupby("member"):
        mf = grp.set_index("date")["precip_mm"].astype(float) * float(f_bh)
        mf.index = pd.to_datetime(mf.index)
        rech = forecast_recharge(observed_rain, mf, kernel, forecast_dates)
        # A NaN recharge (gauge record missing / shorter than the Weibull lag)
        # would roll into an all-NaN trajectory: the roll's min/max clip passes
        # NaN through, aggregate's pivot then drops every row and breach_stats
        # crashes the whole summary stage on one bad station. Skip it loudly.
        if rech.isna().any():
            print(f"  ! {station_id[:8]}: NaN forecast recharge "
                  f"({int(rech.isna().sum())}/{len(rech)} days — gauge record "
                  f"missing or shorter than the Weibull lag) — station skipped")
            return pd.DataFrame(columns=_MEMBER_COLS)
        exog = seasonal.copy()
        exog["Recharge_Weibull"] = rech.values
        gw = gw_roll.roll(method, seed_gw=seed_gw, seed_dgw=seed_dgw,
                          exog_future=exog, params=params,
                          dgw_clip=dgw_clip, gw_clip=gw_clip)
        for i, dt in enumerate(forecast_dates):
            out.append({
                "member": int(m), "date": dt,
                "precip_mm": float(mf.reindex([dt]).iloc[0]) if dt in mf.index else 0.0,
                "recharge_weibull": float(rech.iloc[i]) if not np.isnan(rech.iloc[i]) else np.nan,
                "gw_pred": float(gw[i]),
            })
    df = pd.DataFrame(out, columns=_MEMBER_COLS)
    df.insert(0, "station_id", station_id)
    return df

"""Monthly climatologies: GW normals per borehole + precip tercile bounds.

The tercile vocabulary ("below / near / above normal") needs a per-borehole,
per-calendar-month definition of normal. GW normals come from the joined
observation history; precip climatology bounds come from the same ERA5
series the ESP traces are cut from (so SEAS5 members and traces are
classified against an identical yardstick).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NORMALS_COLS = ["station_id", "month", "p10", "t1", "median", "t2", "p90",
                "n_years"]
MIN_YEARS = 5          # fewer than this and "normal" is not defensible
# The stored quantile ladder — used by status percentile interpolation.
QUANTILE_LEVELS = (0.10, 1 / 3, 0.50, 2 / 3, 0.90)
QUANTILE_COLS = ("p10", "t1", "median", "t2", "p90")


def gw_monthly_normals(joined: pd.DataFrame) -> pd.DataFrame:
    """Per (station_id, calendar month): quantile ladder of monthly-mean GW
    (p10 / t1=33% / median / t2=67% / p90).

    The terciles define below/near/above normal; p10/p90 add the
    "unusually low/high" bounds used by the status percentile and the
    forecast tier's p_above_p90 signal.

    joined : rows with dateTime, station_id, GW_Level (the joined
             timeseries). Monthly means are computed per station-year-month
             first so wet winters with dense data don't out-vote sparse ones.
    """
    df = joined.dropna(subset=["GW_Level"]).copy()
    if df.empty:
        return pd.DataFrame(columns=NORMALS_COLS)
    dt = pd.to_datetime(df["dateTime"])
    try:
        dt = dt.dt.tz_localize(None)
    except TypeError:
        pass
    df["ym"] = dt.dt.to_period("M")
    df["month"] = dt.dt.month

    monthly = (df.groupby(["station_id", "ym", "month"])["GW_Level"]
               .mean().reset_index())
    rows = []
    for (sid, month), grp in monthly.groupby(["station_id", "month"]):
        vals = grp["GW_Level"].to_numpy(float)
        if len(vals) < MIN_YEARS:
            continue
        qs = np.quantile(vals, QUANTILE_LEVELS)
        rows.append({"station_id": sid, "month": int(month),
                     **{c: float(q) for c, q in zip(QUANTILE_COLS, qs)},
                     "n_years": int(len(vals))})
    return pd.DataFrame(rows, columns=NORMALS_COLS)


def precip_monthly_clim_bounds(precip: pd.Series,
                               periods: list[pd.Period]) -> np.ndarray:
    """M×2 (t1, t2) tercile bounds of historical monthly precip totals for
    each outlook month's calendar month, from a long daily series."""
    s = precip.dropna().copy()
    s.index = pd.PeriodIndex(pd.to_datetime(s.index), freq="M")
    monthly_totals = s.groupby(level=0).sum()
    cal_month = monthly_totals.index.month
    out = np.full((len(periods), 2), np.nan)
    for i, p in enumerate(periods):
        vals = monthly_totals[cal_month == p.month].to_numpy(float)
        if len(vals) >= MIN_YEARS:
            out[i] = np.quantile(vals, [1 / 3, 2 / 3])
    return out

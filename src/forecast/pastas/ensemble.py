"""Drive calibrated Pastas models with the ECMWF ensemble members (module 2).

Consumes the bias-corrected per-member forecast rainfall the main-env stage
already produces (`forecast_ensemble_members.parquet`, column ``precip_mm``),
bridges it onto each borehole's observed rainfall/PET, and rolls every member
forward with the module-1 calibrated model via ``recharge.simulate_path`` — the
exact analogue of ``ensemble/members.py`` but with a calibrated TFN in place of
the reduced-form roll.

Pure transform: pandas in, pandas out. ``pastas`` is imported lazily (inside
``recharge``), so importing this module does not require pastas.

Temporal-alignment note: like ``ensemble/members.py`` (its MVP caveat), the seed
is the borehole's last observed GW. We forecast at the provider's real
``forecast_dates`` with calendar-lead AR1 decay, so a stale observation is
honestly down-weighted rather than assumed to sit at the window start. Precise
live-GW origin alignment is the shared Phase-4 refinement.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import recharge as R
from src.forecast.ensemble.seeding import freshest_gw  # shared with the roll

MEMBER_COLS = ["station_id", "member", "date", "precip_mm", "gw_pred",
               "gw_sigma", "origin_date", "segment"]


def _clim_by_doy(s: pd.Series) -> pd.Series:
    s = R._norm(s)
    return s.groupby(s.index.day_of_year).mean()


def drive_borehole(station_id: str, rec: R.ModelRec, head: pd.Series,
                   observed_rain: pd.Series, observed_pet: pd.Series,
                   members_df: pd.DataFrame) -> pd.DataFrame:
    """Per-member Pastas GW trajectories for one borehole.

    members_df : [member, date, precip_mm] for this borehole (already
                 bias-corrected — precip_mm is written post-f_bh upstream).
    Returns rows [station_id, member, date, precip_mm, gw_pred, gw_sigma].
    """
    h = R._norm(head).dropna()
    if members_df.empty or h.empty:
        return pd.DataFrame(columns=MEMBER_COLS)

    origin = h.index.max()
    forecast_dates = pd.DatetimeIndex(sorted(pd.to_datetime(members_df["date"].unique()))) \
        .tz_localize(None).normalize()
    win_start = forecast_dates.min()

    obs_rain = R._norm(observed_rain)
    obs_rain = obs_rain[obs_rain.index < win_start]      # history through the gap
    obs_pet = R._norm(observed_pet)
    obs_pet = obs_pet[obs_pet.index < win_start]
    clim = _clim_by_doy(observed_pet)
    evap_win = pd.Series(clim.reindex(forecast_dates.day_of_year).to_numpy(float),
                         index=forecast_dates)
    bridged_evap = pd.concat([obs_pet, evap_win]).sort_index()
    bridged_evap = bridged_evap[~bridged_evap.index.duplicated(keep="last")]

    # ONE continuous trajectory per member over [last-obs+1 -> +horizon]: the gap
    # (last obs -> today) on observed rainfall, then the member forecast. Slicing
    # one trajectory (rather than two separate simulate_path calls) guarantees the
    # nowcast and forecast lie on the same curve — no baseline step at "today".
    gap_dates = pd.date_range(origin + pd.Timedelta(days=1),
                              win_start - pd.Timedelta(days=1), freq="D")
    targets = forecast_dates.union(gap_dates)            # continuous, sorted

    rows = []
    nowcast_done = False
    for m, grp in members_df.groupby("member"):
        mf = grp.set_index("date")["precip_mm"].astype(float)
        mf.index = pd.to_datetime(mf.index).tz_localize(None).normalize()
        # forecast wins on any overlap; observed fills the history + the gap
        bridged_prec = pd.concat([obs_rain[~obs_rain.index.isin(mf.index)], mf]).sort_index()
        mean, sig = R.simulate_path(rec, head, bridged_prec, bridged_evap, origin, targets)
        for i, d in enumerate(targets):
            if d >= win_start:                            # forecast: per member
                rows.append({"station_id": station_id, "member": int(m), "date": d,
                             "precip_mm": float(mf.get(d, 0.0)),
                             "gw_pred": float(mean[i]), "gw_sigma": float(sig[i]),
                             "origin_date": origin, "segment": "forecast"})
            elif not nowcast_done:                        # nowcast: once (members share
                rows.append({"station_id": station_id, "member": -1, "date": d,  # the observed gap)
                             "precip_mm": float(bridged_prec.get(d, 0.0)),
                             "gw_pred": float(mean[i]), "gw_sigma": float(sig[i]),
                             "origin_date": origin, "segment": "nowcast"})
        nowcast_done = True
    return pd.DataFrame(rows, columns=MEMBER_COLS)

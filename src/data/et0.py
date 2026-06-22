"""Self-computed reference evapotranspiration (ET0) — W5 of the free-data
migration (docs/free_data_migration.md).

Replaces the Open-Meteo archive's ``et0_fao_evapotranspiration`` variable
with our own FAO-56 Penman–Monteith computation (``pyet``, by the Pastas
authors) from daily met fields, so any free ERA5 met source (CDS) can feed
the PET cache. Hargreaves (temperatures only) is the degraded fallback.

The formula implementation is validated against the cached Open-Meteo ET0
series (same FAO-56 upstream) by ``scripts/validate_et0.py`` — report in
``outputs/et0_validation.md``.

Input contract — ``met``: a daily-indexed DataFrame with columns
    tmean_c, tmax_c, tmin_c    (°C)
    dewpoint_c                 (°C; FAO-56 actual vapour pressure source)
    wind10_ms                  (m/s at 10 m; converted to 2 m internally)
    srad_mj                    (MJ m⁻² day⁻¹ incoming shortwave)
Output: tz-naive daily Series ``et0_mm`` (mm/day), NaN where inputs are.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MET_COLUMNS = ("tmean_c", "tmax_c", "tmin_c", "dewpoint_c",
               "wind10_ms", "srad_mj")

# FAO-56 eq. 47: logarithmic wind-profile conversion 10 m -> 2 m.
_WIND_10_TO_2 = 4.87 / np.log(67.8 * 10.0 - 5.42)


def ea_from_dewpoint(td_c) -> pd.Series:
    """Actual vapour pressure (kPa) from dewpoint (°C) — FAO-56 eq. 14:
    ea = e0(Tdew)."""
    td = pd.Series(td_c, dtype="float64") if not isinstance(td_c, pd.Series) else td_c
    return 0.6108 * np.exp(17.27 * td / (td + 237.3))


def wind2_from_wind10(u10) -> pd.Series:
    """FAO-56 eq. 47: wind speed at 2 m from 10 m."""
    u = pd.Series(u10, dtype="float64") if not isinstance(u10, pd.Series) else u10
    return u * _WIND_10_TO_2


def et0_fao56_daily(met: pd.DataFrame, lat_deg: float,
                    elevation_m: float = 0.0) -> pd.Series:
    """Daily FAO-56 Penman–Monteith ET0 (mm/day) via pyet.

    ``met`` per the module contract; rows with any missing input give NaN.
    """
    import pyet

    missing = [c for c in MET_COLUMNS if c not in met.columns]
    if missing:
        raise ValueError(f"met frame missing columns: {missing}")
    idx = pd.DatetimeIndex(met.index)
    out = pyet.pm_fao56(
        tmean=pd.Series(met["tmean_c"].to_numpy(float), index=idx),
        wind=wind2_from_wind10(
            pd.Series(met["wind10_ms"].to_numpy(float), index=idx)),
        rs=pd.Series(met["srad_mj"].to_numpy(float), index=idx),
        tmax=pd.Series(met["tmax_c"].to_numpy(float), index=idx),
        tmin=pd.Series(met["tmin_c"].to_numpy(float), index=idx),
        ea=ea_from_dewpoint(
            pd.Series(met["dewpoint_c"].to_numpy(float), index=idx)),
        elevation=float(elevation_m),
        lat=float(np.deg2rad(lat_deg)),
    )
    return pd.Series(out.to_numpy(float), index=idx, name="et0_mm")


def et0_hargreaves_daily(met: pd.DataFrame, lat_deg: float) -> pd.Series:
    """Hargreaves ET0 (mm/day) — the temperatures-only fallback when wind /
    radiation / humidity are unavailable. Systematically cruder than FAO-56
    (no aerodynamic term); use only when the full met set can't be had."""
    import pyet

    for c in ("tmean_c", "tmax_c", "tmin_c"):
        if c not in met.columns:
            raise ValueError(f"met frame missing column: {c}")
    idx = pd.DatetimeIndex(met.index)
    out = pyet.hargreaves(
        tmean=pd.Series(met["tmean_c"].to_numpy(float), index=idx),
        tmax=pd.Series(met["tmax_c"].to_numpy(float), index=idx),
        tmin=pd.Series(met["tmin_c"].to_numpy(float), index=idx),
        lat=float(np.deg2rad(lat_deg)),
    )
    return pd.Series(out.to_numpy(float), index=idx, name="et0_mm")

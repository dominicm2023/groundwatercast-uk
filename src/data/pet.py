"""Potential evapotranspiration (PET) ingestion — Open-Meteo archive (ERA5).

The forecasting recharge term (Weibull convolution) currently consumes *raw*
rainfall. The literature (Collenteur et al. 2021, HESS; AquiMod's FAO soil
module, Mackay et al. 2014) shows a PET-driven *effective rainfall* /
soil-moisture balance improves recharge and groundwater-level simulation,
especially in dry periods when ET is moisture-limited. This module fetches daily
FAO-56 reference evapotranspiration (ET0) at a borehole point and caches it for
audit, mirroring the Open-Meteo archive pattern in
`src/forecast/ensemble/bias.py`.

ET0 source: Open-Meteo Historical (ERA5-Land), variable
`et0_fao_evapotranspiration` (mm/day). Cached per station to
`data/raw/pet/<station_id>.csv` (raw-data-for-audit non-negotiable). Idempotent:
a cached file is merged with any freshly fetched dates (union, fresh wins), so
repeated calls extend coverage rather than re-downloading the whole span.

This is the Step-1 artefact for the "does PET help the recharge kernel?" pilot
(see docs/ensemble_forecast_design.md amendments / the lit-review follow-up).
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parents[2]
_FREE_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_CUSTOMER_ARCHIVE = "https://customer-archive-api.open-meteo.com/v1/archive"
_TIMEOUT_S = 60
# Multi-year daily archive pulls are "expensive" calls in Open-Meteo's
# free-tier quota accounting — a fleet refresh WILL hit 429s without
# backoff. Waits chosen to ride out the per-minute window.
_RATE_LIMIT_WAITS_S = (30, 60, 120)
# ERA5 archive lags real time by a few days; cap requested end dates so we don't
# ask for an all-NaN tail.
_ARCHIVE_LAG_DAYS = 5
PET_CACHE_ROOT = ROOT / "data" / "raw" / "pet"
_VAR = "et0_fao_evapotranspiration"


def et0_archive_daily(lat: float, lon: float,
                      start: date, end: date) -> pd.Series:
    """Daily FAO-56 reference ET0 (mm) at a point from the Open-Meteo archive
    (ERA5). Returns a tz-naive daily Series named ``et0_mm`` (may be empty).

    Honours the GWC_OPEN_METEO_API_KEY commercial key (same tiering as the
    ensemble provider); retries with backoff on free-tier 429s."""
    from src.forecast.ensemble.open_meteo import api_key
    key = api_key()
    params = {
        "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": _VAR, "timezone": "GMT",
    }
    if key:
        params["apikey"] = key
    url = _CUSTOMER_ARCHIVE if key else _FREE_ARCHIVE

    r = None
    last_exc: Exception | None = None
    for attempt, wait_s in enumerate((0,) + _RATE_LIMIT_WAITS_S):
        if wait_s:
            print(f"    retrying in {wait_s}s "
                  f"({attempt}/{len(_RATE_LIMIT_WAITS_S)})")
            time.sleep(wait_s)
        try:
            r = requests.get(url, params=params, timeout=_TIMEOUT_S)
        except requests.exceptions.RequestException as exc:
            # Transient network failure (read timeout, handshake timeout,
            # connection reset) — the 2026-07-17 outage class. Same backoff
            # ladder as 429s.
            last_exc = exc
            r = None
            print(f"    transient Open-Meteo error: {type(exc).__name__}")
            continue
        if r.status_code != 429:
            break
        print("    rate-limited (429)")
    if r is None:
        raise last_exc if last_exc is not None else RuntimeError(
            "et0_archive_daily: no response and no exception")
    r.raise_for_status()
    d = r.json().get("daily", {})
    idx = pd.to_datetime(d.get("time", []))
    return pd.Series(d.get(_VAR, []), index=idx, dtype="float64", name="et0_mm")


def _cache_path(station_id: str, cache_root: Path) -> Path:
    return cache_root / f"{station_id}.csv"


def _read_cache(path: Path) -> pd.Series:
    """Read a cached PET CSV (date,et0_mm) → tz-naive daily Series (empty if
    absent)."""
    if not path.exists():
        return pd.Series(dtype="float64", name="et0_mm")
    df = pd.read_csv(path, parse_dates=["date"])
    return pd.Series(df["et0_mm"].to_numpy(dtype="float64"),
                     index=pd.to_datetime(df["date"]).dt.tz_localize(None),
                     name="et0_mm")


def fetch_station_pet(station_id: str, lat: float, lon: float,
                      start: date, end: date, *,
                      cache_root: Path | None = None,
                      refresh: bool = False) -> pd.Series:
    """Daily ET0 (mm) for one borehole over [start, end], cache-aware.

    Reuses cached dates and only fetches what's missing (or the whole range when
    ``refresh``). Freshly fetched values are merged into the cache CSV (union by
    date, fresh wins) for the audit trail. Returns the requested window as a
    tz-naive daily Series; dates the archive cannot yet supply are simply absent.
    """
    cache_root = cache_root or PET_CACHE_ROOT
    end = min(end, date.today() - timedelta(days=_ARCHIVE_LAG_DAYS))
    if end < start:
        return pd.Series(dtype="float64", name="et0_mm")

    path = _cache_path(station_id, cache_root)
    cached = pd.Series(dtype="float64", name="et0_mm") if refresh else _read_cache(path)

    want = pd.date_range(start, end, freq="D")
    have = cached.index
    missing = want.difference(have)

    fetched = pd.Series(dtype="float64", name="et0_mm")
    if len(missing):
        # One contiguous request spanning the missing range is cheaper than
        # day-by-day; the archive returns the whole [min,max] window.
        try:
            fetched = et0_archive_daily(lat, lon, missing.min().date(),
                                        missing.max().date())
        except requests.exceptions.RequestException as exc:
            # Archive unreachable after retries. A cache-backed caller gets the
            # cached tail (a day-or-two-short PET series is harmless for the
            # slow recharge kernels — the 2026-07-17 alternative was the whole
            # daily publish dying on this exact exception). With NO cache there
            # is nothing safe to serve — re-raise.
            if cached.empty:
                raise
            print(f"  ! PET fetch failed for {station_id[:8]} "
                  f"({type(exc).__name__}); serving cached tail "
                  f"(ends {cached.index.max().date()}, {len(missing)} day(s) "
                  f"short) — degraded, not fatal")
        else:
            fetched.index = pd.to_datetime(fetched.index).tz_localize(None)
            # Courtesy spacing between archive hits on the free tier — a fleet
            # of back-to-back multi-year pulls trips the per-minute quota.
            from src.forecast.ensemble.open_meteo import api_key
            if not api_key():
                time.sleep(1.0)

    merged = pd.concat([cached, fetched])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    if len(fetched):
        cache_root.mkdir(parents=True, exist_ok=True)
        merged.rename_axis("date").reset_index().rename(
            columns={"index": "date"}).to_csv(path, index=False)

    return merged.loc[(merged.index >= pd.Timestamp(start))
                      & (merged.index <= pd.Timestamp(end))]


def load_station_pet(station_id: str, *,
                     cache_root: Path | None = None) -> pd.Series:
    """Read a station's cached ET0 series (empty Series if not yet fetched)."""
    return _read_cache(_cache_path(station_id, cache_root or PET_CACHE_ROOT))


def effective_rainfall(rainfall: pd.Series, pet: pd.Series, *,
                       floor: float = 0.0) -> pd.Series:
    """Naive PET-effective rainfall: max(rain − ET0, floor) on aligned dates.

    A deliberately simple first variant for the pilot (no soil-moisture store);
    the alternative FAO soil-water-balance variant is fitted separately. Dates
    present in ``rainfall`` but missing ET0 are treated as ET0=0 (no reduction)
    so the series is never shortened by ET0 gaps.
    """
    rain = rainfall.copy()
    rain.index = pd.to_datetime(rain.index).tz_localize(None).normalize()
    et0 = pet.copy()
    et0.index = pd.to_datetime(et0.index).tz_localize(None).normalize()
    et0 = et0.reindex(rain.index).fillna(0.0)
    return (rain - et0).clip(lower=floor).rename("Rainfall_eff")

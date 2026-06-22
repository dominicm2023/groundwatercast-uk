"""ERA5 daily precipitation cache — Open-Meteo archive, per borehole point.

The seasonal ESP outlook (src/forecast/seasonal/) builds its historic-year
trace library from ~35 years of ERA5 daily precipitation at each borehole's
grid point. Gauge records in the raw cache only reach back to the download
window (2018+), so ERA5 is the long-history source; the existing per-BH
bias factor ``f_bh`` is *defined* as mean(gauge)/mean(ERA5), so multiplying
an ERA5 trace by ``f_bh`` puts it on exactly the gauge scale the recharge
models were calibrated on.

Mirrors ``src/data/pet.py``: per-station CSV cache (raw-data-for-audit),
incremental date-union merge so repeated calls extend coverage, commercial
key tiering (``GWC_OPEN_METEO_API_KEY``), free-tier 429 backoff + courtesy
pacing.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# NOTE: `requests` is imported lazily inside the fetch path — the pastas
# venv (which only READS this cache via load_station_precip) doesn't ship it.

ROOT = Path(__file__).parents[2]
_FREE_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_CUSTOMER_ARCHIVE = "https://customer-archive-api.open-meteo.com/v1/archive"
_TIMEOUT_S = 120                       # multi-decade payloads are chunky
_RATE_LIMIT_WAITS_S = (30, 60, 120)
_ARCHIVE_LAG_DAYS = 5
PRECIP_CACHE_ROOT = ROOT / "data" / "raw" / "era5_precip"
_VAR = "precipitation_sum"


def precip_archive_daily(lat: float, lon: float,
                         start: date, end: date) -> pd.Series:
    """Daily ERA5 precipitation (mm) at a point. tz-naive Series ``precip_mm``.

    Honours the GWC_OPEN_METEO_API_KEY commercial key; retries with backoff
    on free-tier 429s (multi-year pulls are expensive quota calls)."""
    import requests
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

    for attempt, wait_s in enumerate((0,) + _RATE_LIMIT_WAITS_S):
        if wait_s:
            print(f"    rate-limited (429) — waiting {wait_s}s before retry "
                  f"{attempt}/{len(_RATE_LIMIT_WAITS_S)}")
            time.sleep(wait_s)
        r = requests.get(url, params=params, timeout=_TIMEOUT_S)
        if r.status_code != 429:
            break
    r.raise_for_status()
    d = r.json().get("daily", {})
    idx = pd.to_datetime(d.get("time", []))
    return pd.Series(d.get(_VAR, []), index=idx, dtype="float64",
                     name="precip_mm")


def _cache_path(station_id: str, cache_root: Path) -> Path:
    return cache_root / f"{station_id}.csv"


def _read_cache(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype="float64", name="precip_mm")
    df = pd.read_csv(path, parse_dates=["date"])
    return pd.Series(df["precip_mm"].to_numpy(dtype="float64"),
                     index=pd.to_datetime(df["date"]).dt.tz_localize(None),
                     name="precip_mm")


def fetch_station_precip(station_id: str, lat: float, lon: float,
                         start: date, end: date, *,
                         cache_root: Path | None = None,
                         refresh: bool = False) -> pd.Series:
    """Daily ERA5 precip for one borehole over [start, end], cache-aware.

    Only missing dates are fetched (one contiguous request spanning the
    missing range); fresh values merge into the cache CSV (union by date,
    fresh wins). Returns the requested window."""
    cache_root = cache_root or PRECIP_CACHE_ROOT
    end = min(end, date.today() - timedelta(days=_ARCHIVE_LAG_DAYS))
    if end < start:
        return pd.Series(dtype="float64", name="precip_mm")

    path = _cache_path(station_id, cache_root)
    cached = (pd.Series(dtype="float64", name="precip_mm") if refresh
              else _read_cache(path))

    want = pd.date_range(start, end, freq="D")
    missing = want.difference(cached.index)

    fetched = pd.Series(dtype="float64", name="precip_mm")
    if len(missing):
        fetched = precip_archive_daily(lat, lon, missing.min().date(),
                                       missing.max().date())
        fetched.index = pd.to_datetime(fetched.index).tz_localize(None)
        from src.forecast.ensemble.open_meteo import api_key
        if not api_key():
            time.sleep(1.0)            # courtesy spacing on the free tier

    merged = pd.concat([cached, fetched])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    if len(fetched):
        cache_root.mkdir(parents=True, exist_ok=True)
        merged.rename_axis("date").reset_index().rename(
            columns={"index": "date"}).to_csv(path, index=False)

    return merged.loc[(merged.index >= pd.Timestamp(start))
                      & (merged.index <= pd.Timestamp(end))]


def load_station_precip(station_id: str, *,
                        cache_root: Path | None = None) -> pd.Series:
    """Read a station's cached ERA5 precip series (empty if not yet fetched)."""
    return _read_cache(_cache_path(station_id, cache_root or PRECIP_CACHE_ROOT))

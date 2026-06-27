"""ERA5 daily met via the CDS ARCO point-time-series dataset — the FAST path.

`reanalysis-era5-single-levels-timeseries` is a cloud-optimised (ARCO) ERA5
subset built for fast long time-series at a SINGLE POINT with **no request
queue** (returned as CSV). One multi-variable request per borehole yields
precipitation + the FAO-56 met fields; we aggregate hourly→daily here and reuse
`src/data/et0.py` for ET0. Same CDS key + Copernicus licence as `cds_era5`
(free, commercial-OK with attribution).

Drop-in for the `cds_era5` box path: writes the identical `era5_precip` / `pet`
cache schemas (union-by-date, fresh wins). Use when the daily-statistics box
path is too slow — the box requests are queue-bound at fleet scale (~12-15 min
processing + a growing free-tier queue wait), whereas these point pulls are
direct ARCO reads. The dataset is flagged EXPERIMENTAL by ECMWF, so keep
`cds_era5` as the fallback and validate day-alignment with
`scripts/validate_cds_timeseries.py` before trusting it (the W4 zero-shift test).

Docs: docs/free_data_migration.md (W4); CDS dataset
`reanalysis-era5-single-levels-timeseries`.
"""
from __future__ import annotations

import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.et0 import et0_fao56_daily

TS_DATASET = "reanalysis-era5-single-levels-timeseries"

# Everything FAO-56 ET0 needs + total precip, in ONE request per point.
TS_VARS = [
    "total_precipitation",
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_solar_radiation_downwards",
]

# CSV header → canonical name (ECMWF short names AND full variable names, since
# the timeseries CSV column naming isn't pinned by ECMWF docs — map both).
_COLMAP = {
    "tp": "tp", "total_precipitation": "tp",
    "t2m": "t2m", "2m_temperature": "t2m",
    "d2m": "d2m", "2m_dewpoint_temperature": "d2m",
    "u10": "u10", "10m_u_component_of_wind": "u10",
    "v10": "v10", "10m_v_component_of_wind": "v10",
    "ssrd": "ssrd", "surface_solar_radiation_downwards": "ssrd",
}
_NEEDED = {"tp", "t2m", "d2m", "u10", "v10", "ssrd"}


def _client():
    try:
        import cdsapi
    except ImportError as exc:                       # pragma: no cover
        raise ImportError("CDS fetch needs the 'cdsapi' package + a CDS API key "
                          "(~/.cdsapirc) — https://cds.climate.copernicus.eu") from exc
    # quiet=True mutes the per-poll INFO chatter the CDS/datastores client emits
    # ("Request ID is…", "status updated to running/successful", the ARCO
    # boilerplate) that otherwise floods cron_forecast.log; warnings/errors still
    # surface. Guarded so a future cdsapi without the kwarg can't break the
    # unattended pipeline.
    try:
        return cdsapi.Client(quiet=True)
    except TypeError:
        return cdsapi.Client()


def _read_any_csv(target: Path, into: Path) -> pd.DataFrame:
    """The response is a CSV, or a zip containing one."""
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise ValueError(f"no CSV in CDS response zip: {z.namelist()}")
            z.extract(names[0], into)
            return pd.read_csv(into / names[0])
    return pd.read_csv(target)


def fetch_point_raw(lat: float, lon: float, start: date, end: date,
                    *, client=None, retries: int = 3) -> pd.DataFrame:
    """Raw CSV (verbatim columns) for one point — for schema inspection."""
    client = client or _client()
    req = {
        "variable": TS_VARS,
        "location": {"latitude": round(float(lat), 3),
                     "longitude": round(float(lon), 3)},
        "date": [f"{start.isoformat()}/{end.isoformat()}"],
        "data_format": "csv",
    }
    last = None
    for attempt in range(retries):
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                target = Path(td) / "ts.csv"
                client.retrieve(TS_DATASET, req, str(target))
                return _read_any_csv(target, Path(td))
        except Exception as exc:                      # transient ARCO/API flake
            last = exc
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise last


def canonicalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Verbatim CSV → tz-naive hourly DataFrame with canonical columns."""
    tcol = next((c for c in raw.columns
                 if str(c).strip().lower() in ("valid_time", "time", "date", "datetime")),
                raw.columns[0])
    idx = pd.to_datetime(raw[tcol], utc=True, errors="coerce")
    out = pd.DataFrame(index=pd.DatetimeIndex(idx).tz_localize(None))
    for c in raw.columns:
        key = _COLMAP.get(str(c).strip().lower())
        if key:
            out[key] = pd.to_numeric(pd.Series(raw[c].values), errors="coerce").values
    out = out[~out.index.isna()].sort_index()
    missing = _NEEDED - set(out.columns)
    if missing:
        raise KeyError(f"timeseries CSV missing {sorted(missing)}; "
                       f"raw columns were {list(raw.columns)}")
    return out


def fetch_point_hourly(lat: float, lon: float, start: date, end: date,
                       *, client=None) -> pd.DataFrame:
    return canonicalise(fetch_point_raw(lat, lon, start, end, client=client))


def daily_aggregate(hourly: pd.DataFrame, *, accum_shift_h: int = 0):
    """Hourly → (precip_mm daily Series, met daily DataFrame for FAO-56).

    ERA5 accumulated fields (tp, ssrd) are summed to daily totals; instantaneous
    fields are mean/max/min. `accum_shift_h` shifts the index before resampling
    to correct the accumulation day-label if the validation alignment check
    demands it (ERA5 hour T covers [T-1h, T])."""
    h = hourly if accum_shift_h == 0 else hourly.shift(freq=f"{accum_shift_h}h")
    g = h.resample("D")
    precip_mm = (g["tp"].sum() * 1000.0).clip(lower=0.0).rename("precip_mm")
    wind = np.sqrt(h["u10"] ** 2 + h["v10"] ** 2)
    met = pd.DataFrame({
        "tmean_c": g["t2m"].mean() - 273.15,
        "tmax_c": g["t2m"].max() - 273.15,
        "tmin_c": g["t2m"].min() - 273.15,
        "dewpoint_c": g["d2m"].mean() - 273.15,
        "wind10_ms": wind.resample("D").mean(),
        "srad_mj": (g["ssrd"].sum() / 1e6).clip(lower=0.0),
    })
    return precip_mm, met


def update_caches_timeseries(points: dict, start: date, end: date, *,
                             precip_root: Path | None = None,
                             pet_root: Path | None = None,
                             elevations: dict | None = None,
                             max_workers: int | None = None,
                             accum_shift_h: int = 0) -> tuple[int, list]:
    """Per point: one ARCO fetch → era5_precip + pet caches (same schemas as
    cds_era5), run CONCURRENTLY across points. Returns (n_ok, [(sid, error), ...]);
    per-point failures are collected, not fatal — re-runs are idempotent.

    The ARCO point endpoint has no request queue, so I/O-bound concurrency gives a
    near-linear speedup (subject to whatever the CDS free tier caps concurrent
    jobs at). Each worker thread holds its own cdsapi client (not shared), and
    every point writes its own per-sid CSV, so there's no client or file
    contention. `max_workers` defaults to env GWC_CDS_TS_WORKERS or 6."""
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from src.data.cds_era5 import _merge_into_cache
    from src.data.era5_precip import PRECIP_CACHE_ROOT
    from src.data.pet import PET_CACHE_ROOT
    precip_root = precip_root or PRECIP_CACHE_ROOT
    pet_root = pet_root or PET_CACHE_ROOT
    elevations = elevations or {}
    if max_workers is None:
        max_workers = int(os.environ.get("GWC_CDS_TS_WORKERS", "6"))

    _tl = threading.local()

    def _client_for_thread():
        c = getattr(_tl, "client", None)
        if c is None:
            c = _tl.client = _client()
        return c

    def _one(item):
        sid, (lat, lon) = item
        hourly = fetch_point_hourly(lat, lon, start, end, client=_client_for_thread())
        precip_mm, met = daily_aggregate(hourly, accum_shift_h=accum_shift_h)
        et0 = et0_fao56_daily(met, lat_deg=float(lat),
                              elevation_m=float(elevations.get(sid, 0.0)))
        _merge_into_cache(precip_root / f"{sid}.csv", precip_mm.dropna(), "precip_mm")
        _merge_into_cache(pet_root / f"{sid}.csv", et0.dropna(), "et0_mm")

    ok, failed = 0, []
    total = len(points)
    print(f"  fetching {total} points with {max_workers} concurrent workers …")
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, it): it[0] for it in points.items()}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                fut.result()
                ok += 1
                if ok % 25 == 0:
                    print(f"  …{ok}/{total} points cached", flush=True)
            except Exception as exc:
                failed.append((sid, f"{type(exc).__name__}: {exc}"))
    return ok, failed

"""ERA5 daily met via the Copernicus Climate Data Store — W4 of the
free-data migration (docs/free_data_migration.md).

Why CDS and not ARCO: every public ERA5 Zarr (ARCO ``ar``/``co``,
WeatherBench) is **map-chunked** — one chunk = one hour × the full globe
(~4.2 MB compressed per variable-hour), so point time-series extraction
transfers the whole planet per hour (verified 2026-06-13; a 1-year point
pull was projected at ~30 min and a 35-year backfill at ~17 h per
variable). CDS does the subsetting server-side: a UK bounding box over
35 years of *daily statistics* is a few MB. CDS is free (registration),
and the Copernicus licence permits commercial use with attribution.

Requirements: a CDS account + API key (``~/.cdsapirc`` or the
``CDSAPI_URL``/``CDSAPI_KEY`` env vars) and the ``cdsapi`` package. The
fetch uses the ``derived-era5-single-levels-daily-statistics`` dataset —
daily aggregation happens at Copernicus, not here.

Status: code complete with offline tests (request shape + parsing);
**live validation pending a CDS key** — see the W4 section of the
migration doc. The cache writers emit the exact schemas
``era5_precip``/``pet`` already use, so downstream code is untouched.
"""
from __future__ import annotations

import tempfile
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.et0 import et0_fao56_daily

ROOT = Path(__file__).parents[2]
DATASET = "derived-era5-single-levels-daily-statistics"

# England (+ Wales) bounding box [N, W, S, E] with margin — the forecast domain.
# Tighter than the old whole-UK box (dropped Scotland + open-sea cells): CDS caps
# cost per request (area × days × variables), so fewer cells means larger safe
# time-chunks and a faster backfill. Every forecast borehole falls inside this.
UK_AREA = [56.8, -6.8, 49.5, 2.2]

# (CDS variable, daily_statistic) pairs needed for precip + FAO-56 ET0.
PRECIP_VAR = ("total_precipitation", "daily_mean")     # mean of hourly acc
MET_VARS = (
    ("2m_temperature", "daily_mean"),
    ("2m_temperature", "daily_maximum"),
    ("2m_temperature", "daily_minimum"),
    ("2m_dewpoint_temperature", "daily_mean"),
    ("10m_wind_speed", "daily_mean"),                  # derived speed, not |mean vector|
    ("surface_solar_radiation_downwards", "daily_mean"),
)


def build_request(variable: str, statistic: str, years: list[int],
                  area: list[float] | None = None) -> dict:
    """The CDS request body for one (variable, statistic) over whole years.
    Pure — pinned by tests so the request shape can't drift silently."""
    return {
        "product_type": "reanalysis",
        "variable": [variable],
        "year": [str(y) for y in years],
        "month": [f"{m:02d}" for m in range(1, 13)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "daily_statistic": statistic,
        "time_zone": "utc+00:00",
        "frequency": "1-hourly",
        "area": area or UK_AREA,
    }


def _client():
    try:
        import cdsapi
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise ImportError("CDS fetch needs the 'cdsapi' package "
                          "(pip install cdsapi) and a CDS API key — "
                          "https://cds.climate.copernicus.eu") from exc
    # quiet=True mutes the per-poll INFO chatter the CDS/datastores client emits
    # ("Request ID is…", "status updated to running/successful", the ARCO
    # boilerplate) that otherwise floods cron_forecast.log; warnings/errors still
    # surface. Guarded so a future cdsapi without the kwarg can't break the
    # unattended pipeline.
    try:
        return cdsapi.Client(quiet=True)
    except TypeError:
        return cdsapi.Client()


_COST_MARKERS = ("cost limit", "too large", "request is too large",
                 "exceeds the limit")


def _is_cost_error(exc: Exception) -> bool:
    """A CDS 'request too large / cost limits exceeded' rejection (vs a genuine
    failure). These are size errors we can recover from by splitting the block."""
    return any(m in str(exc).lower() for m in _COST_MARKERS)


def _retrieve_block(client, variable: str, statistic: str, years: list[int],
                    area: list[float] | None) -> list:
    """Retrieve one whole-year block, AUTO-SPLITTING in half on a CDS cost-limit
    rejection (recurses down to a single year). Returns loaded xr.Datasets.

    CDS caps cost per request (area × days × variables); the exact cap isn't
    published and depends on the box, so rather than guess a fixed chunk size we
    start big and halve only the blocks that get refused — self-tuning, and
    robust if the cap or the area changes."""
    import xarray as xr

    req = build_request(variable, statistic, years, area)
    try:
        # ignore_cleanup_errors: on Windows a NetCDF backend can hold the file
        # handle a beat past close; the OS reaps the temp dir later.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            target = Path(td) / "cds.zip"
            client.retrieve(DATASET, req, str(target))
            out = []
            for nc in _extract_netcdfs(target, Path(td)):
                with xr.open_dataset(nc) as raw:
                    out.append(raw.load())
            return out
    except Exception as exc:
        if _is_cost_error(exc) and len(years) > 1:
            mid = len(years) // 2
            print(f"    CDS cost limit on {variable} {years[0]}–{years[-1]}; "
                  f"splitting {years[0]}–{years[mid - 1]} + {years[mid]}–{years[-1]}")
            return (_retrieve_block(client, variable, statistic, years[:mid], area)
                    + _retrieve_block(client, variable, statistic, years[mid:], area))
        raise


def fetch_daily_box(variable: str, statistic: str, start: date, end: date,
                    *, area: list[float] | None = None, client=None,
                    chunk_years: int = 5):
    """Daily-statistic field over the box for [start, end] → xr.Dataset.

    Requests in ``chunk_years`` blocks (CDS caps request volume); any block the
    server refuses as too large is split in half automatically (_retrieve_block),
    so the default never has to be conservative. Responses are NetCDF(s)
    concatenated along time."""
    import xarray as xr

    client = client or _client()
    pieces = []
    years = list(range(start.year, end.year + 1))
    for i in range(0, len(years), chunk_years):
        block = years[i:i + chunk_years]
        pieces.extend(_retrieve_block(client, variable, statistic, block, area))
    ds = xr.concat(pieces, dim="valid_time") if len(pieces) > 1 else pieces[0]
    ds = ds.sortby("valid_time")
    return ds.sel(valid_time=slice(pd.Timestamp(start), pd.Timestamp(end)))


def _extract_netcdfs(archive: Path, into: Path) -> list[Path]:
    """CDS responses arrive as a zip of .nc files (or a bare .nc)."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
        return sorted(into.glob("*.nc"))
    return [archive]


def point_series(ds, lat: float, lon: float) -> pd.Series:
    """Nearest-cell daily series from a fetched box (first data var)."""
    var = next(iter(ds.data_vars))
    pt = ds[var].sel(latitude=lat, longitude=lon, method="nearest")
    idx = pd.DatetimeIndex(pd.to_datetime(pt["valid_time"].values))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return pd.Series(pt.values.astype(float), index=idx.normalize())


def precip_mm_from_daily_mean(s: pd.Series) -> pd.Series:
    """ERA5 ``tp`` is an hourly accumulation in metres; the daily MEAN of
    those hourly accumulations × 24 × 1000 = the daily total in mm."""
    return (s * 24.0 * 1000.0).clip(lower=0.0).rename("precip_mm")


def srad_mj_from_daily_mean(s: pd.Series) -> pd.Series:
    """``ssrd`` hourly accumulations are J m⁻²; daily mean × 24 / 1e6 =
    MJ m⁻² day⁻¹."""
    return (s * 24.0 / 1e6).clip(lower=0.0).rename("srad_mj")


def met_frame_for_point(fields: dict[str, "object"], lat: float,
                        lon: float) -> pd.DataFrame:
    """Assemble the ``src.data.et0`` met contract from fetched box datasets.

    ``fields`` keys: t2m_mean, t2m_max, t2m_min, d2m_mean, wind10_mean,
    ssrd_mean — each an xr.Dataset from :func:`fetch_daily_box`.
    """
    k = 273.15
    return pd.DataFrame({
        "tmean_c": point_series(fields["t2m_mean"], lat, lon) - k,
        "tmax_c": point_series(fields["t2m_max"], lat, lon) - k,
        "tmin_c": point_series(fields["t2m_min"], lat, lon) - k,
        "dewpoint_c": point_series(fields["d2m_mean"], lat, lon) - k,
        "wind10_ms": point_series(fields["wind10_mean"], lat, lon),
        "srad_mj": srad_mj_from_daily_mean(
            point_series(fields["ssrd_mean"], lat, lon)),
    })


# ---------------------------------------------------------------------------
# Cache writers — emit the EXACT schemas era5_precip.py / pet.py maintain
# ---------------------------------------------------------------------------

def _merge_into_cache(path: Path, fresh: pd.Series, value_col: str) -> None:
    """Union-by-date merge, fresh wins — the same idempotent contract as
    ``era5_precip.fetch_station_precip``."""
    cached = pd.Series(dtype="float64", name=value_col)
    if path.exists():
        df = pd.read_csv(path, parse_dates=["date"])
        cached = pd.Series(df[value_col].to_numpy(float),
                           index=pd.DatetimeIndex(df["date"]).tz_localize(None),
                           name=value_col)
    fresh = fresh.rename(value_col).dropna()
    merged = (fresh if cached.empty
              else pd.concat([cached, fresh]))
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    (merged.rename_axis("date").reset_index()
     .to_csv(path, index=False))


def update_precip_caches(points: dict[str, tuple[float, float]],
                         start: date, end: date, *,
                         cache_root: Path | None = None,
                         client=None) -> int:
    """One box fetch → every station's ``era5_precip`` cache updated.
    Returns the station count."""
    from src.data.era5_precip import PRECIP_CACHE_ROOT
    cache_root = cache_root or PRECIP_CACHE_ROOT
    ds = fetch_daily_box(*PRECIP_VAR, start, end, client=client)
    for sid, (lat, lon) in points.items():
        mm = precip_mm_from_daily_mean(point_series(ds, lat, lon))
        _merge_into_cache(cache_root / f"{sid}.csv", mm, "precip_mm")
    return len(points)


def update_pet_caches(points: dict[str, tuple[float, float]],
                      start: date, end: date, *,
                      cache_root: Path | None = None,
                      elevations: dict[str, float] | None = None,
                      client=None) -> int:
    """One box fetch per met field → self-computed FAO-56 ET0 → every
    station's ``pet`` cache updated. Returns the station count."""
    from src.data.pet import PET_CACHE_ROOT
    cache_root = cache_root or PET_CACHE_ROOT
    client = client or _client()
    keys = ("t2m_mean", "t2m_max", "t2m_min", "d2m_mean",
            "wind10_mean", "ssrd_mean")
    fields = {key: fetch_daily_box(var, stat, start, end, client=client)
              for key, (var, stat) in zip(keys, MET_VARS)}
    for sid, (lat, lon) in points.items():
        met = met_frame_for_point(fields, lat, lon)
        elev = (elevations or {}).get(sid, 0.0)
        et0 = et0_fao56_daily(met, lat_deg=lat, elevation_m=elev)
        _merge_into_cache(cache_root / f"{sid}.csv", et0, "et0_mm")
    return len(points)

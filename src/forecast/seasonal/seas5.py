"""SEAS5 seasonal rainfall members — fetch + monthly tercile probabilities.

ECMWF SEAS5 (51 members, 7 months, updated monthly on the 5th). We never
use daily values as forcing — seasonal daily rainfall has no skill — only
the distribution of MONTHLY member totals, expressed as tercile
probabilities against the local ERA5 climatology, which then weight the
ESP traces (esp.trace_weights).

Two sources behind the same ``monthly_member_totals`` contract:
  - **Open-Meteo seasonal API** (dev / non-commercial): daily member
    precip → summed to monthly totals.
  - **Copernicus CDS** ``seasonal-monthly-single-levels`` (free,
    commercial-OK; W3 of docs/free_data_migration.md): monthly-mean
    precip *rate* per member, converted to monthly totals. The main-env
    fetch caches a tidy per-point CSV; the pastas-env builder reads it
    with plain pandas (no cdsapi/xarray in that venv).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# NOTE: `requests`/`cdsapi`/`xarray` are imported lazily inside the fetch
# paths — the pastas venv only PARSES cached payloads / CSVs.

_FREE_BASE = "https://seasonal-api.open-meteo.com/v1/seasonal"
_CUSTOMER_BASE = "https://customer-seasonal-api.open-meteo.com/v1/seasonal"
_MODEL = "ecmwf_seas5"
_FIELD = "precipitation_sum"
_TIMEOUT_S = 120
ROOT = Path(__file__).parents[3]
RAW_CACHE_ROOT = ROOT / "data" / "raw" / "ensemble" / "open_meteo_seas5"

# --- CDS seasonal-monthly source (W3) --------------------------------------
CDS_SEAS5_DATASET = "seasonal-monthly-single-levels"
CDS_SEAS5_SYSTEM = "51"               # SEAS5
CDS_CACHE_ROOT = ROOT / "data" / "raw" / "ensemble" / "cds_seas5"
# UK bounding box [N, W, S, E] — matches src/data/cds_era5.UK_AREA.
_UK_AREA = [61.0, -8.5, 49.5, 2.5]
_SECONDS_PER_DAY = 86400.0


def fetch_seas5_daily(lat: float, lon: float, *, forecast_days: int = 183,
                      cache_root: Path | None = None) -> dict:
    """Raw SEAS5 daily-member payload for a point (and cache it for audit)."""
    import requests
    from src.forecast.ensemble.open_meteo import api_key
    params = {
        "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
        "daily": _FIELD, "models": _MODEL,
        "forecast_days": int(forecast_days), "timezone": "GMT",
    }
    key = api_key()
    if key:
        params["apikey"] = key
    url = _CUSTOMER_BASE if key else _FREE_BASE
    r = requests.get(url, params=params, timeout=_TIMEOUT_S)
    r.raise_for_status()
    payload = r.json()

    run = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d")
    d = (cache_root or RAW_CACHE_ROOT) / run
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{params['latitude']}_{params['longitude']}.json").write_text(
        json.dumps(payload), encoding="utf-8")
    return payload


def load_cached_payload(lat: float, lon: float, *,
                        cache_root: Path | None = None) -> dict | None:
    """Most recent cached SEAS5 payload for a point (None if never fetched).

    The pastas-env outlook builder reads these caches; the main-env input
    refresh writes them — same handoff pattern as the PET cache."""
    root = cache_root or RAW_CACHE_ROOT
    if not root.exists():
        return None
    name = f"{round(float(lat), 4)}_{round(float(lon), 4)}.json"
    runs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    for run in runs:
        fp = run / name
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    return None


def member_daily_frame(payload: dict) -> pd.DataFrame:
    """Payload → tidy [member, date, precip_mm] (same convention as EC46)."""
    daily = payload.get("daily", {})
    dates = pd.to_datetime(daily.get("time", []))
    if len(dates) == 0:
        return pd.DataFrame(columns=["member", "date", "precip_mm"])
    cols = [c for c in daily
            if c == _FIELD or c.startswith(f"{_FIELD}_member")]
    wide = pd.DataFrame({c: daily[c] for c in cols}, index=dates)
    long = (wide.reset_index(names="date")
            .melt(id_vars="date", var_name="field", value_name="precip_mm"))
    long["member"] = long["field"].map(
        lambda f: 0 if f == _FIELD else int(f.replace(f"{_FIELD}_member", "")))
    return long[["member", "date", "precip_mm"]].dropna(subset=["precip_mm"])


def monthly_member_totals(members: pd.DataFrame,
                          periods: list[pd.Period]) -> pd.DataFrame:
    """Per (member, outlook month): total precip. Months a member doesn't
    fully cover are NaN (the last partial month of the window)."""
    df = members.copy()
    df["period"] = pd.PeriodIndex(pd.to_datetime(df["date"]), freq="M")
    days_in = df.groupby(["member", "period"])["date"].nunique()
    totals = df.groupby(["member", "period"])["precip_mm"].sum()
    full = days_in >= [p.days_in_month for p in days_in.index.get_level_values(1)]
    totals = totals.where(full)
    out = totals.unstack("period")
    return out.reindex(columns=periods)


def tprate_to_monthly_totals(rate_ms, period: pd.Period) -> float:
    """SEAS5 ``tprate`` (m/s, monthly-mean precip rate) → that calendar
    month's total precipitation in mm."""
    return float(rate_ms) * period.days_in_month * _SECONDS_PER_DAY * 1000.0


def cds_member_period_totals(ds, lat: float, lon: float) -> pd.DataFrame:
    """A fetched CDS SEAS5 dataset → member-indexed monthly-totals frame
    (columns = calendar ``pd.Period`` freq M, values = mm), the same shape
    ``monthly_member_totals`` returns. ``forecastMonth`` 1 is the init
    month; the calendar month = reference month + (forecastMonth − 1)."""
    da = ds["tprate"].sel(latitude=lat, longitude=lon, method="nearest")
    ref = pd.Timestamp(np.atleast_1d(ds["forecast_reference_time"].values)[0])
    ref_p = pd.Period(ref, freq="M")
    fmonths = [int(v) for v in np.atleast_1d(ds["forecastMonth"].values)]
    members = [int(v) for v in np.atleast_1d(ds["number"].values)]
    da = da.transpose("number", "forecastMonth", ...)
    vals = np.asarray(da.values, dtype=float).reshape(len(members), len(fmonths))
    cols = [ref_p + (f - 1) for f in fmonths]
    totals = pd.DataFrame(
        [[tprate_to_monthly_totals(vals[mi, fi], cols[fi])
          for fi in range(len(fmonths))] for mi in range(len(members))],
        index=pd.Index(members, name="member"), columns=cols)
    return totals


def fetch_seas5_cds(ref_month: date, points: dict[str, tuple[float, float]], *,
                    months: int = 6, system: str = CDS_SEAS5_SYSTEM,
                    area: list[float] | None = None,
                    cache_root: Path | None = None, client=None) -> int:
    """Main-env: ONE CDS box fetch for the SEAS5 run initialised in
    ``ref_month`` → a tidy per-point cache CSV (``member, period,
    total_mm``) for every borehole. Returns the station count.

    The pastas-env builder reads these via :func:`load_cds_totals` (plain
    pandas), so cdsapi/xarray stay out of that venv."""
    import tempfile
    import xarray as xr

    if client is None:
        # Hardened client (socket-timeout backstop + bounded retries) — the
        # seasonal CDS fetch is exactly the stage that wedged overnight on
        # 2026-07-07. cdsapi is only needed when no client is injected (tests
        # pass a fake), so the import lives inside the branch.
        try:
            from src.data.cds_client import hardened_client
            client = hardened_client()
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError("SEAS5 CDS fetch needs 'cdsapi' + a CDS key "
                              "(docs/free_data_migration.md W3)") from exc
    ref = pd.Period(pd.Timestamp(ref_month), freq="M")
    req = {
        "originating_centre": "ecmwf", "system": str(system),
        "variable": ["total_precipitation"], "product_type": ["monthly_mean"],
        "year": [f"{ref.year}"], "month": [f"{ref.month:02d}"],
        "leadtime_month": [str(m) for m in range(1, int(months) + 1)],
        "data_format": "netcdf", "area": area or _UK_AREA,
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        target = Path(td) / "seas5.nc"
        client.retrieve(CDS_SEAS5_DATASET, req, str(target))
        with xr.open_dataset(target) as raw:
            ds = raw.load()

    out_dir = (cache_root or CDS_CACHE_ROOT) / ref.strftime("%Y%m")
    out_dir.mkdir(parents=True, exist_ok=True)
    for sid, (lat, lon) in points.items():
        totals = cds_member_period_totals(ds, lat, lon)
        tidy = (totals.stack().rename("total_mm").reset_index()
                .rename(columns={"level_1": "period"}))
        tidy["period"] = tidy["period"].astype(str)
        tidy.to_csv(out_dir / f"{sid}.csv", index=False)
    return len(points)


def load_cds_totals(station_id: str, periods: list[pd.Period], *,
                    cache_root: Path | None = None) -> pd.DataFrame | None:
    """Pastas-env: most recent cached CDS SEAS5 totals for a borehole →
    member-indexed monthly-totals frame reindexed to ``periods`` (None if
    never fetched). Plain pandas — no cdsapi/xarray."""
    root = cache_root or CDS_CACHE_ROOT
    if not root.exists():
        return None
    runs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    for run in runs:
        fp = run / f"{station_id}.csv"
        if not fp.exists():
            continue
        tidy = pd.read_csv(fp)
        tidy["period"] = tidy["period"].apply(lambda s: pd.Period(s, freq="M"))
        wide = tidy.pivot_table(index="member", columns="period",
                                values="total_mm")
        return wide.reindex(columns=periods)
    return None


def tercile_probs(member_totals: pd.DataFrame,
                  clim_bounds: np.ndarray) -> np.ndarray:
    """M×3 array of P(below/near/above) per outlook month from the member
    totals vs the climatology bounds (M×2). Months with no usable members
    → uniform (1/3, 1/3, 1/3) so they never tilt the trace weights."""
    months = member_totals.columns
    out = np.full((len(months), 3), 1.0 / 3.0)
    for i, p in enumerate(months):
        vals = member_totals[p].dropna().to_numpy(float)
        if len(vals) < 10:
            continue
        t1, t2 = float(clim_bounds[i][0]), float(clim_bounds[i][1])
        below = float((vals < t1).mean())
        above = float((vals > t2).mean())
        out[i] = (below, max(0.0, 1.0 - below - above), above)
    return out

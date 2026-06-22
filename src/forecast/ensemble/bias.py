"""Grid→gauge bias-correction (design §4, D4).

The model's `Rainfall` feature is a top-3 *gauge* average; ensemble precip is a
~20 km grid-cell areal mean with a different climatology. We correct each
member's precip by a per-borehole multiplicative factor

    f_bh = mean(observed gauge Rainfall) / mean(reference precip)   over an overlap window

fit against a reanalysis reference (Open-Meteo archive / ERA5) at the borehole
point. This is a deliberate first-order (mean-ratio) correction; seasonal
quantile mapping is deferred to Phase C. Factors are persisted to
`data/model/ensemble_bias_factors.csv` so they are auditable and frozen between
refits.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parents[3]
_FREE_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
_CUSTOMER_ARCHIVE = "https://customer-archive-api.open-meteo.com/v1/archive"
_TIMEOUT_S = 60
_DEFAULT_OVERLAP_YEARS = 2
BIAS_PATH = ROOT / "data" / "model" / "ensemble_bias_factors.csv"

# Sane multiplicative range for the grid→gauge factor. A ratio far outside this
# means a degenerate reference (e.g. a near-zero arid/edge-cell mean that still
# clears the 1e-6 guard, or a coordinate/units mismatch): applied raw it would
# scale every member's recharge into a confident garbage fan, and because f_bh
# is persisted and frozen the corruption is sticky across refits.
F_BH_MIN: float = 0.2
F_BH_MAX: float = 5.0


def reference_archive_daily(lat: float, lon: float,
                            start: date, end: date) -> pd.Series:
    """Daily reference precipitation (mm) at a point from the Open-Meteo
    archive (ERA5). Returns a tz-naive daily Series.

    Honours the GWC_OPEN_METEO_API_KEY commercial key (same tiering as the
    ensemble provider — see open_meteo.py)."""
    from .open_meteo import api_key
    key = api_key()
    params = {
        "latitude": round(float(lat), 4), "longitude": round(float(lon), 4),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum", "timezone": "GMT",
    }
    if key:
        params["apikey"] = key
    r = requests.get(_CUSTOMER_ARCHIVE if key else _FREE_ARCHIVE,
                     params=params, timeout=_TIMEOUT_S)
    r.raise_for_status()
    d = r.json().get("daily", {})
    idx = pd.to_datetime(d.get("time", []))
    return pd.Series(d.get("precipitation_sum", []), index=idx,
                     dtype="float64", name="ref_precip")


def fit_bias_factor(gauge_rainfall: pd.Series, reference_precip: pd.Series) -> float:
    """f_bh = mean(gauge) / mean(reference) on the common (non-NaN) dates.

    Returns 1.0 when the reference mean is ~0 or there is no overlap (no
    correction rather than a divide-by-zero blow-up)."""
    g = gauge_rainfall.dropna()
    g.index = pd.to_datetime(g.index).tz_localize(None).normalize()
    rf = reference_precip.dropna()
    rf.index = pd.to_datetime(rf.index).tz_localize(None).normalize()
    common = g.index.intersection(rf.index)
    if len(common) < 30:
        return 1.0
    ref_mean = float(rf.loc[common].mean())
    if ref_mean <= 1e-6:
        return 1.0
    f = float(g.loc[common].mean()) / ref_mean
    return float(min(max(f, F_BH_MIN), F_BH_MAX))


def fit_bias_factors(stations: list[dict], gauge_rainfall_by_station: dict[str, pd.Series],
                     *, overlap_years: int = _DEFAULT_OVERLAP_YEARS,
                     write: bool = True) -> pd.DataFrame:
    """Fit f_bh for each station.

    `stations`: list of {"station_id", "lat", "lon"}.
    `gauge_rainfall_by_station`: station_id -> observed daily Rainfall Series.
    """
    end = date.today() - timedelta(days=30)         # avoid the not-yet-final tail
    start = end - timedelta(days=365 * overlap_years)
    rows = []
    for s in stations:
        sid = s["station_id"]
        gauge = gauge_rainfall_by_station.get(sid)
        if gauge is None or gauge.dropna().empty:
            rows.append({"station_id": sid, "f_bh": 1.0,
                         "overlap_start": start, "overlap_end": end,
                         "fitted_on": date.today(), "note": "no gauge data → 1.0"})
            continue
        try:
            ref = reference_archive_daily(s["lat"], s["lon"], start, end)
            f = fit_bias_factor(gauge, ref)
            note = (f"f_bh clamped to [{F_BH_MIN}, {F_BH_MAX}]"
                    if f <= F_BH_MIN or f >= F_BH_MAX else "")
        except Exception as exc:                      # network / API issue
            f, note = 1.0, f"reference fetch failed: {exc}"
        rows.append({"station_id": sid, "f_bh": round(f, 4),
                     "overlap_start": start, "overlap_end": end,
                     "fitted_on": date.today(), "note": note})

    df = pd.DataFrame(rows)
    if write:
        BIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(BIAS_PATH, index=False)
    return df


def upsert_bias_factors(rows: pd.DataFrame) -> pd.DataFrame:
    """Merge-write `rows` into the persisted factors CSV (keyed on station_id).

    Updates/inserts only the given stations and preserves every other row
    untouched — so a narrow-scope run (e.g. --scope live) cannot truncate a
    wider fleet file, and previously fitted stations keep their original
    `fitted_on` / overlap provenance. Returns the full merged frame."""
    rows = pd.DataFrame(rows)
    if BIAS_PATH.exists() and not rows.empty:
        existing = pd.read_csv(BIAS_PATH)
        keep = existing[~existing["station_id"].astype(str)
                        .isin(rows["station_id"].astype(str))]
        merged = pd.concat([keep, rows], ignore_index=True)
    elif BIAS_PATH.exists():
        merged = pd.read_csv(BIAS_PATH)
    else:
        merged = rows
    BIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(BIAS_PATH, index=False)
    return merged


def load_bias_factors() -> dict[str, float]:
    """station_id -> f_bh (empty dict when the artefact is absent → callers
    default to 1.0, i.e. no correction)."""
    if not BIAS_PATH.exists():
        return {}
    df = pd.read_csv(BIAS_PATH)
    return dict(zip(df["station_id"].astype(str), df["f_bh"].astype(float)))

"""Open-Meteo ensemble provider — the supported default source.

Open-Meteo serves the ECMWF IFS ENS members (and other models) as plain JSON,
which makes it the fastest way to iterate and the easiest to smoke-test on any
OS (no GRIB / eccodes). It returns the *same underlying ECMWF ENS data* as the
``ecmwf_opendata`` GRIB provider, just over a different transport.

Licence: the free Open-Meteo API tier is for **non-commercial** use only.
Commercial deployments set ``GWC_OPEN_METEO_API_KEY`` (an Open-Meteo API
subscription key), which switches requests to the customer endpoint. The
``ecmwf_opendata`` GRIB provider (CC-BY-4.0) remains the zero-cost commercial
fallback. See docs/ensemble_forecast_design.md §2.

Response shape (model=ecmwf_ifs025): ``hourly`` carries ``precipitation``
(control = member 0) plus ``precipitation_member01..50`` (perturbed), in mm.
We sum the hourly series to daily totals per member.
"""
from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd
import requests

from .provider import EnsembleRainfallProvider

_FREE_BASE = "https://ensemble-api.open-meteo.com/v1/ensemble"
_CUSTOMER_BASE = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
_API_KEY_ENV = "GWC_OPEN_METEO_API_KEY"
_DEFAULT_MODEL = "ecmwf_ifs025"   # ECMWF IFS ENS 0.25° — 51 members
_TIMEOUT_S = 60


def api_key() -> str | None:
    """Commercial Open-Meteo API key, or None (free, non-commercial tier)."""
    return os.environ.get(_API_KEY_ENV) or None


def base_url(key: str | None = None) -> str:
    """Endpoint for the given key — customer host when a key is present."""
    return _CUSTOMER_BASE if (key if key is not None else api_key()) else _FREE_BASE


class OpenMeteoEnsemble(EnsembleRainfallProvider):
    name = "open_meteo"

    def __init__(self, cache_root="data/raw/ensemble", *,
                 model: str = _DEFAULT_MODEL):
        super().__init__(cache_root)
        self.model = model

    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        params = {
            "latitude":     round(float(lat), 4),
            "longitude":    round(float(lon), 4),
            "hourly":       "precipitation",
            "models":       self.model,
            "forecast_days": int(horizon_days),
            "timezone":     "GMT",
        }
        key = api_key()
        if key:
            params["apikey"] = key
        r = requests.get(base_url(key), params=params, timeout=_TIMEOUT_S)
        r.raise_for_status()
        payload = r.json()

        # Cache the raw JSON for audit before parsing.
        run = pd.Timestamp.now(tz="UTC").strftime("%Y%m%d%H")
        cache = self._cache_dir(run) / f"{params['latitude']}_{params['longitude']}.json"
        cache.write_text(json.dumps(payload), encoding="utf-8")

        df = self._parse(payload)
        # Caller filters to >= start as needed; Open-Meteo begins at "today".
        df = df[df["date"] >= pd.Timestamp(start).normalize()]
        return self._validate(df)

    @staticmethod
    def _parse(payload: dict) -> pd.DataFrame:
        hourly = payload.get("hourly", {})
        times = pd.to_datetime(hourly.get("time", []), utc=True)
        if len(times) == 0:
            return pd.DataFrame(columns=["member", "date", "precip_mm"])

        member_cols = [c for c in hourly
                       if c == "precipitation" or c.startswith("precipitation_member")]
        wide = pd.DataFrame({c: hourly[c] for c in member_cols}, index=times)

        # Hourly mm -> daily totals (UTC calendar day).
        daily = wide.resample("1D").sum(min_count=1)
        long = (daily.reset_index(names="date")
                .melt(id_vars="date", var_name="field", value_name="precip_mm"))
        long["member"] = long["field"].map(_member_index)
        long["date"] = long["date"].dt.tz_convert("UTC").dt.tz_localize(None)
        return long[["member", "date", "precip_mm"]]


def _member_index(field: str) -> int:
    """precipitation -> 0 (control); precipitation_memberNN -> NN."""
    if field == "precipitation":
        return 0
    return int(field.replace("precipitation_member", ""))

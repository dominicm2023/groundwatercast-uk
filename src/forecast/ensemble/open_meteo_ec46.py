"""Open-Meteo seasonal API provider — ECMWF EC46 extended-range members.

EC46 is ECMWF's sub-seasonal system: 51 members to 46 days, updated daily
(~20:30 UTC), served by Open-Meteo's seasonal API as **daily precipitation
sums per member** — the same member-column convention as the ensemble API
(``precipitation_sum`` = control, ``precipitation_sum_member01..50``).

Used as the *extension* segment of the spliced 46-day forecast (see
``splice.SplicedEnsemble``): daily ENS drives days 1–15, EC46 drives the
extended tail. Daily rainfall skill at these leads is weak — the envelope
across members is the signal, and the dashboard labels the extended range
accordingly.

Licence tiering is identical to the ensemble API: the free endpoint is
non-commercial; ``GWC_OPEN_METEO_API_KEY`` switches to the customer host.

Payload quirks (probed 2026-06-12): dates are daily and span exactly
``forecast_days``; some members carry a null on the final day (ragged member
tails) — ``_validate``'s NaN drop handles this, so a member may end one day
short of the horizon.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import requests

from .open_meteo import api_key
from .provider import EnsembleRainfallProvider

_FREE_BASE = "https://seasonal-api.open-meteo.com/v1/seasonal"
_CUSTOMER_BASE = "https://customer-seasonal-api.open-meteo.com/v1/seasonal"
_DEFAULT_MODEL = "ecmwf_ec46"     # ECMWF extended-range — 51 members, 46 days
MAX_HORIZON_DAYS = 46
_TIMEOUT_S = 60
_FIELD = "precipitation_sum"


def base_url(key: str | None = None) -> str:
    """Endpoint for the given key — customer host when a key is present."""
    return _CUSTOMER_BASE if (key if key is not None else api_key()) else _FREE_BASE


class OpenMeteoEC46(EnsembleRainfallProvider):
    name = "open_meteo_ec46"

    def __init__(self, cache_root="data/raw/ensemble", *,
                 model: str = _DEFAULT_MODEL):
        super().__init__(cache_root)
        self.model = model

    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        params = {
            "latitude":     round(float(lat), 4),
            "longitude":    round(float(lon), 4),
            "daily":        _FIELD,
            "models":       self.model,
            "forecast_days": min(int(horizon_days), MAX_HORIZON_DAYS),
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
        df = df[df["date"] >= pd.Timestamp(start).normalize()]
        return self._validate(df)

    @staticmethod
    def _parse(payload: dict) -> pd.DataFrame:
        daily = payload.get("daily", {})
        dates = pd.to_datetime(daily.get("time", []))
        if len(dates) == 0:
            return pd.DataFrame(columns=["member", "date", "precip_mm"])

        member_cols = [c for c in daily
                       if c == _FIELD or c.startswith(f"{_FIELD}_member")]
        wide = pd.DataFrame({c: daily[c] for c in member_cols}, index=dates)
        long = (wide.reset_index(names="date")
                .melt(id_vars="date", var_name="field", value_name="precip_mm"))
        long["member"] = long["field"].map(_member_index)
        return long[["member", "date", "precip_mm"]]


def _member_index(field: str) -> int:
    """precipitation_sum -> 0 (control); precipitation_sum_memberNN -> NN."""
    if field == _FIELD:
        return 0
    return int(field.replace(f"{_FIELD}_member", ""))

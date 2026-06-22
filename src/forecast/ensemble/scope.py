"""Forecast borehole-scope selection — shared by the ensemble rainfall fetch
(`build_ensemble_members`) and the Pastas calibration (`build_pastas_models`) so
both target the same set.

Scopes:
  user  — boreholes with a user-supplied breach threshold only (the smallest
          operationally-meaningful set; see thresholds.py).
  live  — boreholes with a live EA flood-monitoring feed AND enough history to
          calibrate, UNION the user-threshold set (so user-declared boreholes
          are never dropped). The default: forecasts only where the seed GW
          is fresh, plus the user's declared stations.
  fleet — every calibratable borehole (the full fleet; mostly stale-seeded —
          needs the rainfall-fetch-at-scale work before it's practical).

Pure pandas — importable in either environment.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .thresholds import user_threshold_station_ids
# Known-bad station register (datum/scaling shifts). Lives under
# src/dashboard/ but is streamlit-free (yaml + lru_cache) — shared here so
# the forecast scope honours the same exclusions as the dashboard pages:
# a datum-shifted sensor can neither seed a forecast nor be compared
# against thresholds derived from its pre-shift history.
from src.dashboard.exclusions import excluded_station_ids

_ROOT = Path(__file__).resolve().parents[3]
_CATALOGUE = _ROOT / "data" / "processed" / "catalogue.csv"
_XREF = _ROOT / "data" / "processed" / "flood_monitoring_xref.csv"
_JOINED = _ROOT / "data" / "features" / "joined_timeseries.csv"
MIN_ROWS = 2000                       # min GW observations to calibrate a TFN


def _gw_with_coords() -> set[str]:
    c = pd.read_csv(_CATALOGUE)
    return set(c[c["measure_type"] == "groundwater"]
               .dropna(subset=["lat", "lon"])["station_id"].astype(str))


def calibratable_ids(min_rows: int = MIN_ROWS) -> set[str]:
    """Boreholes with >= min_rows non-null GW observations in the joined series."""
    n = (pd.read_csv(_JOINED, usecols=["GW_Level", "station_id"])
         .dropna(subset=["GW_Level"]).groupby("station_id").size())
    return set(n[n >= min_rows].index.astype(str))


def live_capable_ids() -> set[str]:
    """Boreholes with an EA flood-monitoring match (a live GW feed) + coords."""
    if not _XREF.exists():
        return set()
    x = pd.read_csv(_XREF)
    matched = set(x[x["fm_notation"].notna()]["station_id"].astype(str))
    return matched & _gw_with_coords()


def select_scope(scope: str, *, min_rows: int = MIN_ROWS) -> set[str]:
    """Resolve a scope name to its borehole station-id set.

    Stations in the known-bad register are dropped from EVERY scope
    (including user-declared): their live readings aren't comparable with
    the history the models and thresholds were built on."""
    user = set(user_threshold_station_ids())
    if scope == "user":
        ids = user
    elif scope == "live":
        ids = (live_capable_ids() & calibratable_ids(min_rows)) | user
    elif scope == "fleet":
        ids = calibratable_ids(min_rows) | user
    else:
        raise ValueError(f"unknown scope: {scope!r} (expected user | live | fleet)")
    return ids - excluded_station_ids()

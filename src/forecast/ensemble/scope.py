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
MIN_ROWS = 2000                       # min GW obs for a FULL-record TFN (fan + seasonal)
# Short-record floor: a borehole with [MIN_ROWS_FAN, MIN_ROWS) obs (~2–5.5 yr) can
# still yield a useful 14-day fan, but ONLY behind the gauge-rainfall + leakage-safe
# hindcast gate in build_pastas_models (src.forecast.pastas.screen). Seasonal stays
# at MIN_ROWS — short records fail the long horizon (national test 2026-07). Below
# MIN_ROWS_FAN there is too little record to identify even a short-lead response.
MIN_ROWS_FAN = 730


def _gw_with_coords() -> set[str]:
    c = pd.read_csv(_CATALOGUE)
    return set(c[c["measure_type"] == "groundwater"]
               .dropna(subset=["lat", "lon"])["station_id"].astype(str))


def _gw_row_counts() -> pd.Series:
    return (pd.read_csv(_JOINED, usecols=["GW_Level", "station_id"])
            .dropna(subset=["GW_Level"]).groupby("station_id").size())


def calibratable_ids(min_rows: int = MIN_ROWS) -> set[str]:
    """Boreholes with >= min_rows non-null GW observations in the joined series."""
    n = _gw_row_counts()
    return set(n[n >= min_rows].index.astype(str))


def short_record_ids(min_fan: int | None = None,
                     min_full: int | None = None) -> set[str]:
    """Short-record fan CANDIDATES: [min_fan, min_full) GW obs. Not yet admitted —
    each must still pass the gauge-rainfall + hindcast gate (screen.py) before a
    fan is published; a candidate that fails is dropped to status-only.

    Thresholds read at call time (None → module defaults) so they stay overridable."""
    min_fan = MIN_ROWS_FAN if min_fan is None else min_fan
    min_full = MIN_ROWS if min_full is None else min_full
    n = _gw_row_counts()
    return set(n[(n >= min_fan) & (n < min_full)].index.astype(str))


def live_capable_ids() -> set[str]:
    """Boreholes with an EA flood-monitoring match (a live GW feed) + coords."""
    if not _XREF.exists():
        return set()
    x = pd.read_csv(_XREF)
    matched = set(x[x["fm_notation"].notna()]["station_id"].astype(str))
    return matched & _gw_with_coords()


def select_scope(scope: str, *, min_rows: int = MIN_ROWS,
                 include_short: bool = False) -> set[str]:
    """Resolve a scope name to its borehole station-id set.

    ``include_short`` also admits the short-record fan candidates
    ([MIN_ROWS_FAN, min_rows) obs) — the gauge-rainfall + hindcast gate that
    decides which of them actually get a fan runs downstream in
    build_pastas_models, so the ensemble-member fetch and the calibration cover
    the same wider candidate set (they must stay aligned).

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
    if include_short:
        ids = ids | short_record_ids(min_full=min_rows)
    return ids - excluded_station_ids()

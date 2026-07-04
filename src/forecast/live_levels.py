"""Live readings from the EA flood-monitoring API — fetchers + QC.

The hydrology API the pipeline uses is the *audited historical archive* —
it lags by weeks. The flood-monitoring API is the EA's near-real-time feed
(15-minute cadence) for the same telemetry. For stations matched in
``flood_monitoring_xref.csv`` these fetchers extend the per-station shards
(and the raw rainfall tail) right up to "now" — which is what keeps the
forecasts freshly seeded.

Consumers: ``scripts/v16_refresh_live_gw.py`` (GW levels → shards) and
``scripts/v19_refresh_live_rainfall.py`` (rainfall tail → raw cache).

Live readings are sensor-grade, not quality-checked upstream — ``apply_qc``
applies the minimal defensible rules (NaN/duplicate drop, |z| outlier cap
against the station's own history, stuck-sensor flag).
"""
from __future__ import annotations

import pandas as pd
ROOT_FM_BASE = "https://environment.data.gov.uk/flood-monitoring"

# Tunables — exposed at module level for testability
LIVE_WINDOW_DAYS:    int   = 7
Z_SCORE_OUTLIER_CAP: float = 10.0   # drop readings beyond this many σ
STUCK_THRESHOLD_H:   float = 24.0   # value unchanged > this → stuck flag
REQUEST_TIMEOUT_S:   int   = 30

# data_source marker the live refresher writes on a frozen-telemetry daily row
# (apply_qc flagged the window stuck). Single source of truth — readers that
# date "the latest real reading" (status, freshness) must treat a row carrying
# this marker as NOT a trustworthy fresh reading.
LIVE_STUCK_SOURCE: str = "logged_live_stuck"


# ---------------------------------------------------------------------------
# QC
# ---------------------------------------------------------------------------

def apply_qc(
    readings: pd.DataFrame,
    historical_mean: float,
    historical_std: float,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Apply minimal QC to live readings.  Returns (cleaned_df, qc_flags).

    Rules
    -----
    * Drop NaN / null values.
    * Drop duplicates (keep last).
    * Drop |z| > Z_SCORE_OUTLIER_CAP against the station's hydrology mean.
    * Flag (do not drop) stuck readings — value unchanged for > 24 h.
    """
    flags: list[str] = []
    if readings.empty:
        return readings, flags

    out = readings.dropna(subset=["value"]).copy()
    out = out.drop_duplicates(subset="dateTime", keep="last")

    # Outlier filter against the station's own historical distribution
    if historical_std and historical_std > 0:
        z = (out["value"] - historical_mean) / historical_std
        n_before = len(out)
        out = out[z.abs() <= Z_SCORE_OUTLIER_CAP]
        if len(out) < n_before:
            flags.append(f"dropped_{n_before - len(out)}_outliers")

    # Stuck-sensor flag (informational; rows retained). Two checks:
    #  (a) the WHOLE window is one constant value (the original check), and
    #  (b) the TRAILING readings — the ones that actually seed the forecast
    #      origin — are one constant value spanning > STUCK_THRESHOLD_H. A
    #      sensor that varied last week but froze two days ago must still
    #      flag; the whole-window check alone missed exactly that case.
    if len(out) >= 2:
        out = out.sort_values("dateTime")
        time_gap_h = (
            (out["dateTime"].iloc[-1] - out["dateTime"].iloc[0]).total_seconds() / 3600
        )
        if time_gap_h > STUCK_THRESHOLD_H and out["value"].nunique() == 1:
            flags.append("stuck_sensor")
        else:
            # trailing run of identical values, scanned from the newest back
            vals = out["value"].to_numpy()
            i = len(vals) - 1
            while i > 0 and vals[i - 1] == vals[-1]:
                i -= 1
            run_h = (
                (out["dateTime"].iloc[-1] - out["dateTime"].iloc[i]).total_seconds() / 3600
            )
            if len(vals) - i >= 2 and run_h > STUCK_THRESHOLD_H:
                flags.append("stuck_sensor")

    return out, flags


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_live_readings(
    fm_notation: str,
    since: pd.Timestamp,
    *,
    timeout: int = REQUEST_TIMEOUT_S,
    return_raw: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, dict | None]:
    """
    Fetch raw readings from the flood-monitoring API for a single station.

    Returns a DataFrame with columns ``dateTime`` (tz-aware UTC) and
    ``value`` (mAOD).  Returns an empty DataFrame on any error.

    With ``return_raw=True`` returns ``(df, payload)`` where ``payload`` is
    the JSON dict exactly as the API returned it (``None`` on fetch error) —
    used by the v16 refresher to persist the raw audit copy.
    """
    url = f"{ROOT_FM_BASE}/id/stations/{fm_notation}/readings"
    params = {
        "since":   since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_limit":  5000,
        "_sorted": "",
    }
    payload: dict | None = None
    try:
        import requests  # lazy: keeps the network dep out of pure-read importers
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("items", [])
    except Exception:
        empty = pd.DataFrame(columns=["dateTime", "value"])
        return (empty, payload) if return_raw else empty

    rows: list[dict] = []
    for it in items:
        # The flood-monitoring API can return multiple measures per
        # station; we only want the groundwater level one.
        measure = it.get("measure", "")
        if isinstance(measure, list):
            measure = measure[0] if measure else ""
        if "groundwater" not in str(measure).lower():
            # Some stations don't tag the measure in the reading; fall back
            # to keeping the row when no measure info is present.
            if measure:
                continue
        rows.append({
            "dateTime": pd.Timestamp(it["dateTime"]).tz_convert("UTC"),
            "value":    float(it["value"]) if it.get("value") is not None else float("nan"),
        })
    df = (pd.DataFrame(rows).sort_values("dateTime").reset_index(drop=True)
          if rows else pd.DataFrame(columns=["dateTime", "value"]))
    return (df, payload) if return_raw else df


def fetch_live_rainfall(
    fm_notation: str,
    since: pd.Timestamp,
    *,
    timeout: int = REQUEST_TIMEOUT_S,
    return_raw: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, dict | None]:
    """
    Fetch raw rainfall readings from the flood-monitoring API for a single
    gauge.

    Rainfall gauges report tipping-bucket totals per measurement interval
    (typically 15 min, in mm).  Returns a DataFrame with columns
    ``dateTime`` (tz-aware UTC) and ``value`` (mm in that interval) — the
    caller is responsible for summing to a daily total (matching the
    hydrology archive's ``-86400-mm`` daily-total convention).  Returns an
    empty DataFrame on any error.

    With ``return_raw=True`` returns ``(df, payload)`` where ``payload`` is
    the JSON dict exactly as the API returned it (``None`` on fetch error) —
    used by the v19 refresher to persist the raw audit copy.
    """
    url = f"{ROOT_FM_BASE}/id/stations/{fm_notation}/readings"
    params = {
        "since":   since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_limit":  5000,
        "_sorted": "",
    }
    payload: dict | None = None
    try:
        import requests  # lazy: keeps the network dep out of pure-read importers
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("items", [])
    except Exception:
        empty = pd.DataFrame(columns=["dateTime", "value"])
        return (empty, payload) if return_raw else empty

    rows: list[dict] = []
    for it in items:
        # A station can expose multiple measures; keep only the rainfall one.
        measure = it.get("measure", "")
        if isinstance(measure, list):
            measure = measure[0] if measure else ""
        if "rainfall" not in str(measure).lower():
            # Some readings don't tag the measure; keep them only when no
            # measure info is present at all (single-measure rain gauge).
            if measure:
                continue
        val = it.get("value")
        # Rainfall totals are non-negative; the feed occasionally emits small
        # negative artefacts on sensor resets — clamp those to zero.
        v = float(val) if val is not None else float("nan")
        if v < 0:
            v = 0.0
        rows.append({
            "dateTime": pd.Timestamp(it["dateTime"]).tz_convert("UTC"),
            "value":    v,
        })
    df = (pd.DataFrame(rows).sort_values("dateTime").reset_index(drop=True)
          if rows else pd.DataFrame(columns=["dateTime", "value"]))
    return (df, payload) if return_raw else df

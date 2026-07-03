"""The artifact-pack contract — schema constants the builder, the tests and
``docs/artifact_contract.md`` all pin against.

This module is the single source of truth for what the published pack looks
like. It deliberately contains **no I/O and no config**: the schema version
and the rounding/key conventions are part of the public contract and must
move only when the code that emits them moves (a config knob could silently
skew the published schema).

Change policy (mirrored in docs/artifact_contract.md):
  - additive change (new key/file)            -> changelog entry, no bump
  - rename / remove / retype / semantic change -> bump SCHEMA_VERSION + entry
"""
from __future__ import annotations

# Pack-internal schema version — independent of the repo's SemVer.
SCHEMA_VERSION = "1.0"

# Rounding (decimal places). Levels & thresholds are metres AOD; mm precision
# is the most the sensors justify. Probabilities at 4 dp keep small breach
# masses (~0.0001) visible without false precision.
LEVEL_DP = 3      # GW levels, thresholds, fan quantiles, model spread
PROB_DP = 4       # probabilities (p_breach*, p_above_p90*, terciles)
PCT_DP = 1        # status percentile

# stations.geojson — flat feature properties (MapLibre data-driven styling).
# ``slug`` is the canonical /b/<slug>/ page path segment (collision-suffixed at
# pack build; additive, 2026-07) — link generators must use it, never re-derive
# it from the name, or duplicate-named stations link to the wrong page.
GEOJSON_IDENTITY_PROPS = ("station_id", "slug", "name", "aquifer", "aquifer_designation")
GEOJSON_STATUS_PROPS = ("status", "percentile", "trend", "level",
                        "obs_date", "obs_age_days", "sgi")
GEOJSON_FRESHNESS_PROPS = ("freshness", "days_since", "data_source")
GEOJSON_FORECAST_PROPS = ("tier", "p_breach_14d", "p_above_p90_14d",
                          "first_cross_median", "headline",
                          "threshold", "threshold_source", "is_pinned")
GEOJSON_FLAG_PROPS = ("has_forecast", "has_seasonal")
# Trend-screen stability flag (roadmap 1.1) — present on every feature so the
# explorer can filter/style; ``has_trend_flag`` false + null severity when the
# borehole is not in outputs/trend_flags.csv.
GEOJSON_TREND_PROPS = ("has_trend_flag", "trend_severity")
# Forecast-timeline scrubber: compact per-frame status + opacity arrays (frame
# labels live in meta.forecast_frames). Length == number of frames on every
# feature; the explorer recolours the map by indexing these as the slider moves.
GEOJSON_TIMELINE_PROPS = ("st_seq", "op_seq")

# stations/<id>.json — keys of the ``forecast`` block, each mapped to the
# SUMMARY_COLS column it is lifted from (the schema pin: a test asserts every
# source column still exists in src.forecast.pastas.summary.SUMMARY_COLS).
# ``tier`` and ``is_pinned`` are derived at pack time (forecast_outlook
# triage), not summary columns, so they are not in this mapping.
SUMMARY_COL_SOURCES: dict[str, str] = {
    "run": "run",
    "origin_date": "origin_date",
    "stale_days": "stale_days",
    "horizon_days": "horizon_days",
    "threshold": "threshold",
    "threshold_source": "threshold_source",
    "p_breach": "p_breach",
    "p_breach_14d": "p_breach_14d",
    "p_above_p90_14d": "p_above_p90_14d",
    "first_cross_median": "first_cross_median",
    "first_cross_p25": "first_cross_p25",
    "first_cross_p75": "first_cross_p75",
    "first_cross_median_lead": "first_cross_median_lead",
    "censored_frac": "censored_frac",
    "gw_p50_end": "gw_p50_end",
    "model_spread_mean": "model_spread_mean",
    "n_members": "n_members",
    "n_samples": "n_samples",
    "headline": "headline",
}
DETAIL_FORECAST_KEYS = (*SUMMARY_COL_SOURCES, "tier", "is_pinned", "fan")

# Fan rows inside the (already-namespaced) ``forecast.fan`` array drop the
# ``gw_`` prefix; the cross-check roll median and spread keep their names.
FAN_KEY_MAP: dict[str, str] = {
    "gw_p10": "p10",
    "gw_p50": "p50",
    "gw_p90": "p90",
    "roll_p50": "roll_p50",
    "model_spread": "model_spread",
}
# Non-numeric fan-row keys set directly by the pack builder (outside the
# numeric FAN_KEY_MAP rounding loop). ``segment`` tags each row "nowcast"
# (the modelled last-obs -> today gap on observed rainfall) or "forecast".
FAN_EXTRA_KEYS: tuple[str, ...] = ("segment",)

# stations/<id>.json — keys of each entry in ``seasonal.months``.
SEASONAL_MONTH_KEYS = ("month_ahead", "month_start",
                       "p_below", "p_near", "p_above",
                       "gw_p10", "gw_p50", "gw_p90")

# stations/<id>.json — keys of each entry in ``normals``.
NORMALS_ROW_KEYS = ("month", "p10", "t1", "median", "t2", "p90", "n_years")

# stations/<id>.json — keys of the ``trend_flag`` block (null when unflagged).
# A report-only non-stationarity flag from outputs/trend_flags.csv (the trend
# screen): the verdict (severity / provenance / action) plus the signals behind
# it (slope, rainfall coherence, neighbour isolation). Roadmap 1.1.
TREND_FLAG_KEYS = ("severity", "provenance_class", "recommended_action",
                   "slope_sen_m_yr", "trend_change_m", "rain_corr",
                   "isolation_class", "neighbour_count", "already_in_register")

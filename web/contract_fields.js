// The subset of the artifact-pack schema the explorer consumes.
//
// tests/test_explorer_contract.py asserts each name below is a documented
// field in src/publish/contract.py — so a pack-schema change that would
// break the explorer fails a Python test, with no JS test runner needed.
// Keep these in sync with the fields app.js / detail.js / charts.js read.
window.GWC_CONTRACT = {
  // stations.geojson feature.properties
  GEOJSON_FIELDS: [
    "station_id", "slug", "name", "aquifer", "aquifer_designation",
    "status", "percentile", "trend", "level", "obs_date", "obs_age_days",
    "freshness", "days_since", "data_source",
    "tier", "p_breach_14d", "p_above_p90_14d", "first_cross_median",
    "headline", "threshold", "threshold_source", "is_pinned",
    "has_forecast", "has_seasonal",
    "st_seq", "op_seq",
    // RiverCast — station_type is absent on every GW feature;
    // river_name/rain_dependent/winterbourne are flow-only (winterbourne
    // here = the seasonal dry_months-based read, stricter than the detail's).
    "station_type", "river_name", "rain_dependent", "winterbourne",
  ],

  // stations/<id>.json — dotted paths (nested groups flattened for the test)
  DETAIL_FIELDS: [
    "station.station_id", "station.slug", "station.name", "station.lat",
    "station.lon", "station.aquifer",
    // RiverCast (Stage 7) — flow-only station fields.
    "station.station_type", "station.river_name", "station.linked_boreholes",
    "station.winterbourne", "station.dry_months",
    "status.status", "status.percentile", "status.trend", "status.level",
    "status.obs_date", "status.obs_age_days", "status.month",
    "freshness.label", "freshness.days_since", "freshness.last_real_reading",
    "freshness.data_source",
    "normals.month", "normals.p10", "normals.t1", "normals.median",
    "normals.t2", "normals.p90",
    "observed.unit", "observed.series",
    "forecast.run", "forecast.origin_date", "forecast.horizon_days",
    "forecast.stale_days",
    "forecast.threshold", "forecast.threshold_source", "forecast.p_breach",
    "forecast.p_breach_14d", "forecast.p_above_p90_14d",
    "forecast.first_cross_median", "forecast.first_cross_median_lead",
    "forecast.model_spread_mean", "forecast.censored_frac",
    "forecast.gw_p50_end", "forecast.tier", "forecast.headline",
    "forecast.fan",
    // RiverCast (Stage 7) — flow-only forecast fields (never overload
    // p_breach_14d: opposite direction/semantics from the GW field above).
    "forecast.p_below_q95", "forecast.p_below_q95_14d",
    "forecast.rain_dependent",
    "fan.lead", "fan.date", "fan.p10", "fan.p50", "fan.p90", "fan.segment",
    "seasonal.run", "seasonal.seas5_weighted", "seasonal.n_traces",
    "seasonal.months",
    "months.month_ahead", "months.month_start", "months.p_below",
    "months.p_near", "months.p_above", "months.gw_p50",
  ],
};

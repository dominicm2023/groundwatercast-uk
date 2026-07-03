# Artifact contract — the published daily pack

The pack (`outputs/pack/`, built by `python -m scripts.build_artifact_pack`,
orchestrated as `run_chain --publish`) is the product's **public API**: a set
of static JSON/GeoJSON files a web front-end or any third party fetches over
HTTP. This document is the contract for those files. The schema constants
live in [`src/publish/contract.py`](../src/publish/contract.py); a test
(`tests/test_artifact_pack.py::test_contract_doc_in_sync`) keeps the three in
lock-step.

## 1. Scope & consumers

- **Target consumer**: the MapLibre static explorer (in development) —
  `stations.geojson` paints the map in one fetch; `stations/<id>.json` is
  lazy-loaded per click.
- **Other consumers**: anyone. The pack is self-describing (`meta.json`) and
  carries no secrets — it is safe to host publicly.
- The **Streamlit app does not consume the pack** — it reads the pipeline
  artefacts directly. The pack is a downstream export, never an input.

## 2. Versioning & change policy

`schema_version` is currently `"1.0"` (`SCHEMA_VERSION` in
`src/publish/contract.py`). It is **pack-internal** — independent of the
repository's own version.

| Change | Policy |
|---|---|
| Additive (new key, new file) | No bump; changelog entry below |
| Rename / remove / retype a key, or change its semantics | Bump `schema_version`; changelog entry |
| New rounding / date convention | Treated as semantic — bump |

Consumers should read `meta.json` first and check `schema_version` before
parsing anything else. (The published pack schemas are one of the
repo-versioned surfaces — see `docs/product/product_definition.md` §4.2.)

## 3. File inventory

| File | Purpose |
|---|---|
| `meta.json` | Pack self-description: version, timestamps, counts, input provenance, attribution, disclaimer. Fetch first. |
| `manifest.json` | Integrity: `{file → {sha256, bytes}}` for every other file (including `meta.json`). |
| `stations.geojson` | FeatureCollection — one Feature per published station. The map index. |
| `stations/<station_id>.json` | Per-station detail (status, normals, observed history, forecast, seasonal). |

**Which stations are published**: catalogued groundwater stations that have
observation data (a per-station shard) or a forecast, minus the curated
known-bad register (`data/external/known_bad_stations.yaml`) — excluded
stations get **no feature and no detail file**. Catalogued-but-never-observed
stations are skipped and counted in `meta.counts.no_data`.

## 4. Conventions

| Convention | Rule |
|---|---|
| Keys | `snake_case`, matching the pipeline CSV column names where one exists |
| Dates | `YYYY-MM-DD` (ISO 8601 date) |
| Timestamps | ISO 8601 UTC with `Z` suffix, e.g. `2026-06-12T17:00:00Z` |
| Missing values | JSON `null` — never `NaN`/`Infinity` (the builder hard-fails on a leaked NaN) |
| Levels (mAOD) | rounded to **3 dp** (`level`, `threshold`, fan quantiles, `gw_p50_end`, `model_spread_mean`, normals) |
| Probabilities | rounded to **4 dp** (`p_breach`, `p_breach_14d`, `p_above_p90_14d`, `censored_frac`, `p_below`/`p_near`/`p_above`) |
| Status percentile | rounded to **1 dp** |
| SGI | rounded to **2 dp**; clamped ~±2.05 |
| Coordinates | WGS84; GeoJSON order `[lon, lat]`, 6 dp |
| Units | groundwater levels are metres Above Ordnance Datum (`mAOD`) |

## 5. File schemas

### 5.1 `meta.json`

| Key | Type | Nullable | Semantics |
|---|---|---|---|
| `schema_version` | string | no | This contract's version (`"1.0"`) |
| `generated_at` | timestamp | no | When the pack was built (UTC) |
| `region` | string | no | `config.region.name` |
| `counts` | object | no | `stations` (published), `with_forecast`, `with_seasonal`, `excluded`, `no_data` |
| `coverage` | object | no | Network-coverage audit: `catalogued`, `observed`, `with_forecast`, `no_data`, `excluded`, `live_capable` (network count with an EA live feed; `null` if unknown). For honest "how much of the network is this?" disclosure. |
| `runs.forecast` | timestamp | yes | The forecast run this pack carries; `null` if the forecast input was missing |
| `runs.seasonal` | object | yes | `{run, origin_date}` of the seasonal outlook; `null` if missing |
| `inputs.<name>` | object | no | Per-input provenance: `{path, mtime_utc, status}` with `status` ∈ `ok` \| `missing` |
| `attribution` | string | no | Data licence attribution (EA OGL v3, ECMWF CC-BY-4.0) |
| `disclaimer` | string | no | "Not flood warnings" wording — display wherever pack data is shown |
| `history_days` | int | no | Observed-history depth in the detail files |

### 5.2 `manifest.json`

`{"schema_version": "1.0", "files": {"<relative path>": {"sha256": "...", "bytes": N}}}` —
covers every file in the pack except itself. Use for integrity checks and
cache busting (a changed hash = refetch).

### 5.3 `stations.geojson`

A `FeatureCollection`; each Feature is `{"type": "Feature", "geometry":
{"type": "Point", "coordinates": [lon, lat]}, "properties": {…}}`. Properties
are **flat** (MapLibre data-driven styling cannot reach into nested objects).
For MapLibre feature-state, load with `promoteId: "station_id"`.

| Property | Type | Nullable | Semantics |
|---|---|---|---|
| `station_id` | string | no | EA hydrology station GUID — the join key everywhere |
| `slug` | string | no | Canonical `/b/<slug>/` page path segment, assigned ONCE at pack build (name-slug; duplicate-named stations get a `-<sid[:6]>` suffix). Link generators must use this, never re-derive it from `name`. Additive, 2026-07. |
| `name` | string | yes | Station name |
| `aquifer` | string | yes | Aquifer name (from EA designation data) |
| `aquifer_designation` | string | yes | e.g. Principal / Secondary A |
| `status` | string | yes | Current level vs the month's normals: `below` \| `near` \| `above`; `null` when the observation is > 45 days old or no normals exist |
| `percentile` | number | yes | Approximate percentile of the current level for the calendar month (clamped 2–98) |
| `sgi` | number | yes | Standardised Groundwater Index — ladder-based normal-scores approximation `Phi^-1(percentile/100)` (Bloomfield & Marchant 2013); negative = below normal, positive = above. Saturates at ~±2.05 because the percentile is clamped 2–98 (the ladder can't resolve the tails). `null` when `percentile` is `null`. |
| `trend` | string | yes | 7-day trend: `rising` \| `falling` \| `stable` |
| `level` | number | yes | Latest observed level (mAOD) |
| `obs_date` | date | yes | Date of the latest observation |
| `obs_age_days` | int | yes | Age of that observation at pack build time |
| `freshness` | string | yes | Freshness label from the pipeline audit (`fresh`/`recent`/`stale`/`very_stale`) |
| `days_since` | int | yes | Days since the last real reading |
| `data_source` | string | yes | e.g. `logged` / `dipped` / live feed marker |
| `tier` | string | yes | Forecast tier: `BREACH_LIKELY` \| `BREACH_POSSIBLE` \| `WATCH` \| `STABLE`; `null` out of forecast scope |
| `p_breach_14d` | number | yes | P(breach within the operational 14-day window) |
| `p_above_p90_14d` | number | yes | P(any day in the window above the month's P90 normal) |
| `first_cross_median` | date | yes | Median first threshold-crossing date |
| `headline` | string | yes | One-sentence forecast summary |
| `threshold` | number | yes | Breach level (mAOD) |
| `threshold_source` | string | yes | `user` \| `gw_p90_proxy` \| `none` — proxy values are NOT operational thresholds |
| `is_pinned` | bool | no | Station has a user-supplied threshold (`false` out of scope) |
| `has_forecast` | bool | no | Convenience filter flag |
| `has_seasonal` | bool | no | Convenience filter flag |
| `has_trend_flag` | bool | no | Station is in the trend screen's review queue (roadmap 1.1) |
| `trend_severity` | string | yes | Flag severity `high` \| `medium` \| `low`; `null` when unflagged |
| `st_seq` | string[] | no | Forecast-timeline category per frame (`below` \| `near` \| `above` \| `none`); frame order is `meta.forecast_frames`. Frame 0 is the current status, or a faint nowcast estimate when the latest reading is stale |
| `op_seq` | number[] | no | Forecast-timeline opacity per frame (0–1): confidence × lead-time fade, for the map scrubber |

### 5.4 `stations/<station_id>.json`

Top level: `schema_version`, `station`, `status`, `freshness`, `normals`,
`observed`, `forecast` (nullable), `seasonal` (nullable), `trend_flag` (nullable).

- **`station`**: `station_id`, `slug` (same semantics as the geojson `slug`),
  `name`, `lat`, `lon`, `aquifer`, `aquifer_designation`.
- **`status`**: same keys/semantics as the geojson status block, plus
  `month` (int, the calendar month the status is judged against).
- **`freshness`**: `label`, `days_since`, `last_real_reading` (date),
  `data_source`.
- **`normals`**: up to 12 rows of the station's monthly quantile ladder —
  `month`, `p10`, `t1`, `median`, `t2`, `p90`, `n_years` (`t1`/`t2` are the
  terciles; built from ≥ 5 years of observations per month). Empty list when
  the station has no normals.
- **`observed`**: `{"unit": "mAOD", "series": [[date, level], …]}` — the
  last `meta.history_days` days of daily observations (live tail included).
  May be `[]` when `publish.include_history_for = "scope"` and the station
  has no forecast.
- **`forecast`** (`null` out of scope): lifted from the forecast summary —
  `run` (timestamp), `origin_date` (seed date), `stale_days` (seed age at
  run), `horizon_days`, `threshold` + `threshold_source`, `p_breach` (full
  horizon), `p_breach_14d`, `p_above_p90_14d`, `first_cross_median`,
  `first_cross_p25`, `first_cross_p75` (dates), `first_cross_median_lead`
  (int days), `censored_frac` (fraction of trajectories never crossing),
  `gw_p50_end` (median level at horizon), `model_spread_mean` (mean |Pastas −
  roll| cross-check), `n_members`, `n_samples`, `headline`, plus the derived
  `tier` and `is_pinned`, and:
  - **`fan`**: per-lead rows with keys `lead`, `date`, `p10`, `p50`, `p90`,
    `roll_p50`, `model_spread`, `segment` — note the `gw_` prefix of the source
    columns is dropped inside the already-namespaced array (`gw_p10` → `p10`
    etc.); `roll_p50` is the reduced-form cross-check median, `model_spread` =
    `p50` − `roll_p50`. `segment` is `"nowcast"` for the modelled last-obs → today
    gap (observed rainfall; negative `lead`; `roll_p50`/`model_spread` null) or
    `"forecast"` for the 46-day horizon from today. Leads beyond 15 are ECMWF
    extended-range (EC46) — daily skill is weak there; the envelope is the signal.
- **`seasonal`** (`null` when absent): `run`, `origin_date`,
  `seas5_weighted` (bool), `n_traces`, and `months` — up to 6 rows with keys
  `month_ahead`, `month_start`, `p_below`, `p_near`, `p_above`, `gw_p10`,
  `gw_p50`, `gw_p90` (tercile probabilities vs the station's own monthly
  climatology; the `gw_*` quantiles are weighted monthly means, not daily
  levels).
- **`trend_flag`** (`null` unless the station is in `outputs/trend_flags.csv`):
  the trend screen's report-only non-stationarity flag (roadmap 1.1) — the
  verdict (`severity`, `provenance_class`, `recommended_action`) plus the signals
  behind it: `slope_sen_m_yr` (robust Theil-Sen slope), `trend_change_m` (metres
  of drift over the record), `rain_corr` (de-seasonalised head vs cumulative
  rainfall anomaly — low ⇒ artefact-like), `isolation_class`
  (`isolated` \| `regional` \| `no_neighbours`), `neighbour_count`, and
  `already_in_register`. Surfaced as a "flagged for review" badge, not "broken".

## 6. Degradation semantics

The pack always builds if the catalogue and shards exist. Optional inputs
degrade per-field, and `meta.inputs.<name>.status = "missing"` records why:

| Missing input | Effect |
|---|---|
| `pastas_summary` / `pastas_fan` | every `forecast` is `null`; `has_forecast` all `false`; `runs.forecast` `null` |
| `seasonal` | every `seasonal` is `null`; `runs.seasonal` `null` |
| `normals` | `normals` empty; `status.status`/`percentile` `null` (level/trend still present) |
| `freshness` | freshness fields `null` |
| catalogue or shards | **build fails** (a pack without stations is meaningless) |

Consumers must treat every nullable field as genuinely null-able — a missing
forecast is normal (most stations are status-only), not an error.

## 7. Examples

`stations.geojson` feature (abridged):

```json
{"type": "Feature",
 "geometry": {"type": "Point", "coordinates": [-1.0, 51.0]},
 "properties": {"station_id": "abc-123", "name": "Alpha BH",
   "aquifer": "Chalk", "aquifer_designation": "Principal",
   "status": "near", "percentile": 56.0, "sgi": 0.15, "trend": "falling",
   "level": 50.123, "obs_date": "2026-06-10", "obs_age_days": 2,
   "freshness": "fresh", "days_since": 2, "data_source": "logged",
   "tier": "WATCH", "p_breach_14d": 0.1235, "p_above_p90_14d": 0.0568,
   "first_cross_median": "2026-07-01", "headline": "…",
   "threshold": 51.234, "threshold_source": "user", "is_pinned": true,
   "has_forecast": true, "has_seasonal": true,
   "st_seq": ["near", "near", "below", "..."], "op_seq": [0.9, 0.83, 0.4, "..."]}}
```

Status-only station detail (abridged):

```json
{"schema_version": "1.0",
 "station": {"station_id": "def-456", "name": "Beta BH", "lat": 51.1,
             "lon": -1.1, "aquifer": "Greensand", "aquifer_designation": "Secondary A"},
 "status": {"status": "near", "percentile": 50.0, "sgi": 0.0, "trend": "stable",
            "level": 50.25, "obs_date": "2026-06-08", "obs_age_days": 4, "month": 6},
 "freshness": {"label": "fresh", "days_since": 4,
               "last_real_reading": "2026-06-08", "data_source": "logged"},
 "normals": [{"month": 1, "p10": 48.0, "t1": 49.0, "median": 50.0,
              "t2": 51.0, "p90": 52.0, "n_years": 10}],
 "observed": {"unit": "mAOD", "series": [["2025-05-06", 50.001]]},
 "forecast": null,
 "seasonal": null}
```

## 8. Changelog

| Version | Date | Change |
|---|---|---|
| `1.0` | 2026-06-12 | Initial contract: `meta.json`, `manifest.json`, `stations.geojson`, `stations/<id>.json` |
| `1.0` | 2026-06-16 | Additive (no bump): `has_trend_flag` + `trend_severity` geojson props; `trend_flag` detail block (roadmap 1.1 trend-screen stability badge) |
| `1.0` | 2026-06-16 | Additive (no bump): `sgi` (ladder-based Standardised Groundwater Index) on the status block — geojson prop + `detail.status` |
| `1.0` | 2026-06-17 | Additive (no bump): `meta.coverage` block (catalogued / observed / with_forecast / no_data / excluded / live_capable) — network-coverage audit disclosure |
| `1.0` | 2026-06-20 | Additive (no bump): `st_seq` / `op_seq` geojson props + `meta.forecast_frames` / `meta.forecast_frame_days` — forecast-timeline scrubber (recolour the map through Today → +2 wk → Months 1–6; slider spaced by real elapsed time) |
| `1.0` | 2026-07-01 | Additive (no bump): `slug` on geojson props + `detail.station` — the canonical `/b/<slug>/` page path, assigned once at pack build so duplicate-named stations can never link to the wrong page |

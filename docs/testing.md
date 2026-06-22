# Testing and validation

The system ships with **412 tests** covering every stage from raw EA
parsing to dashboard chart rendering, the data-freshness layer, and
the live-tail integration with the EA flood-monitoring API.
This document explains what is validated, why it matters, and how to
run the suite.

Tests live in `tests/`. Each file targets a single concern. Pure
functions (no Streamlit, no I/O) are unit-tested directly; the small
amount of I/O (region GeoJSON, aquifer GeoJSON, catalogue CSV) is
tested with the real artefacts as fixtures — no mocks for filesystem.

---

## Running the tests

```cmd
:: Full suite
python -m pytest tests/ -q

:: A single concern
python -m pytest tests/test_geology.py -v

:: A single test
python -m pytest tests/test_region.py::TestPointInRegion::test_brighton_is_inside -v
```

Typical runtime on the full suite: **~60 seconds**.

---

## Coverage by concern

| File | Tests | What it validates |
|---|---|---|
| `test_catalogue.py` | parse, classify, period, expand | EA CSV parsing; measure classification; period inference from measure_id; expand pipe-delimited measures |
| `test_download.py` | fetch, retries | Download retry / fallback behaviour |
| `test_linking.py` | preference logic, nearest | Predictor selection, haversine, fallback |
| `test_features.py` | feature engineering | Lag/roll/Weibull features |
| `test_region.py` | region polygon (England+Wales) | GeoJSON validity, point-in-region, buffer |
| `test_geology.py` | aquifer integration | GeoJSON validity, lookup, enrichment, clipping |
| `test_status.py` | current status vs normal | Quantile-ladder percentile interpolation + clamping, below/near/above classification, trend, stale-observation guard, `attach_current_status` join contract (tolerant, tie-break only) |
| `test_live_levels.py` | flood-monitoring live levels | QC rules (outlier, duplicate, stuck), xref helpers (haversine, name normalisation, fuzzy ratio), xref match-method paths (reference / coords / none) |
| `test_ensemble.py` | ensemble provider layer | Output-schema validation, Open-Meteo hourly→daily parsing, member indexing, raw caching, provider factory, ECMWF GRIB-stack guard |
| `test_ensemble_roll.py` | GW-roll methods | OLS coefficient recovery on synthetic systems, recharge response, AR momentum, conditional-recession behaviour, guardrail clipping, dispatcher |
| `test_ensemble_members.py` | per-member chain | Bias factor (mean-ratio), recharge bridge/convolution, member-trajectory shape + wetter-member-rises-higher |
| `test_ensemble_aggregate.py` | aggregation + thresholds | Breach probability + censoring, first-crossing lead, fan ordering, headline formatting, threshold priority (user→proxy), user-threshold scope |
| `test_ensemble_view.py` | forward-outlook UI | Fan-figure traces/threshold line, roll overlay, no-data paths |

---

## What each category validates and why

### Catalogue tests (`test_catalogue.py`)

**Spatial filtering** — `filter_to_region()` keeps only stations whose
coordinates fall inside the SW polygon (with the +0.001° membership
buffer). The tests use a mix of known-in / known-out coordinates
(Brighton, Southampton, Manchester, Edinburgh) so the boundary
behaviour is regression-locked.

**Period parsing from `measure_id`** — `parse_period_from_measure_id()`
extracts the sampling period from the EA measure_id slug rather than
the unreliable `measures.period` column. Ten parameterised cases cover:

- Numeric period tokens (`-900-`, `-86400-`, `-3600-`).
- `subdaily` keyword → 900.
- `gw-dipped-i` → `None`.
- Composite IDs with embedded reference numbers — must **not** false-
  match arbitrary digits as periods.
- Empty / `None` input → `None`.

This is the test that prevented the system from silently dropping ~330
continuous-logger boreholes due to a CSV-parsing edge case.

### Analysis tests (`test_dashboard.py::TestDetectHighEvents`, `TestComputeLeadTimes`, `TestDetectAlertMarkers`)

**Event detection** — `detect_high_events()` groups consecutive HIGH
days into events with `(start_date, end_date, duration_days)`. Tested
with: single-event series, multi-event series (gaps in HIGH must
produce separate events), no events (empty list), HIGH at start, HIGH
at end.

**Lead time** — `compute_lead_times()` finds the most recent qualifying
MEDIUM and RISING precursors before each HIGH event start, within the
30-day look-back window. Tests cover:

- MEDIUM-only precursor present → MEDIUM lead populated, RISING lead `—`.
- RISING-only precursor present → reverse.
- Both precursors → both columns populated.
- No precursor within window → both `—`.
- Empty `station_df` → empty DataFrame with the correct columns
  (schema stability for downstream consumers).
- "Closest" semantics — when multiple MEDIUM rows precede a HIGH event,
  the **most recent** one is reported, not the earliest.

### Dashboard snapshot tests (`test_dashboard.py`)

**`build_snapshot()`** — Live mode latest-row-per-station selector.
Tests verify:

- The latest row per station is selected (not the most-recent across
  the dataset).
- Filters compose correctly (`risk_classes`, `trends`,
  `min_persistence`).
- `top_n` caps the result after sorting.
- "Action priority" vs "Risk score" sort orders.

**`build_historical_snapshot()`** — Historical mode date-specific
selector. Tests cover the robust date normalisation
(`datetime.date` / `pd.Timestamp` tz-aware and naive / `datetime` /
`str` all map to the same `datetime.date`); empty-result-empty-frame
schema preservation; out-of-window dates returning empty.

**`classify_action()`** — Maps `(risk_raw, trend, persistence_days)` to
one of `IMMEDIATE_ACTION / SUSTAINED_HIGH / EMERGING_RISK / STABLE_LOW`.
Every priority path has a dedicated test.

### Chart tests (`test_charts.py`)

For each of the 10 chart-builder functions: tests confirm

1. **Valid input** returns a `go.Figure` with ≥1 data trace.
2. **Empty input** returns a `go.Figure` (no exception) — usually with
   zero traces and a placeholder annotation.
3. **Edge cases** specific to that chart (e.g. histogram receives only
   `"—"` strings for lead times → empty-safe).

Plus shared sanity tests on the colour constants (`RISK_COLORS`,
`TREND_COLORS`, canonical orderings).

These tests are deliberately lightweight — they catch regressions
("we accidentally broke `plot_lead_time_scatter` for empty input")
without trying to verify pixel-level layout.

### Region tests (`test_region.py`)

**GeoJSON validity** — `england_wales.geojson` loads, is a
FeatureCollection, has at least one valid Polygon/MultiPolygon, the
provenance sidecar exists, and the provenance contains every required
key (source URL, dataset name, licence, fetch date).

**CRS sanity** — vertex bounds fall in the WGS84 / England+Wales range
(roughly `lon ∈ [-7, +3]`, `lat ∈ [49, 56]`). Catches a class of error
where projected metres get accidentally treated as lat/lon.

**Point-in-region** — known-in (Brighton, Southampton, Isle of Wight)
and known-out (Manchester, Edinburgh) coordinates. Plus the three
real EA stations (Kingstanding, Climping Ryebank, Keepers Wood) used
as a regression test for the simplification / buffering combination.

**Buffer sanity** — `REGION_CONTAINS_BUFFER_DEG` is positive but small
(< 0.005°). Stops a future maintainer accidentally setting an
implausible value.

### Freshness + forward-roll tests (`test_freshness.py`) *(new in 1.1)*

**`add_freshness`** — verifies per-station `data_age_days` is computed
from the *max* of each station's `dateTime` (so every row for a station
shares the same age), that the column appears, and that the confidence
mapping is applied correctly.

**Confidence band boundaries** — parameterised tests at the exact
thresholds 0 / 7 / 8 / 15 / 30 / 31 / 90 / 365 days, ensuring a value
of 7 maps to HIGH and 8 to MEDIUM (no off-by-one at the band edges).

**Damped-persistence dGW path** — confirms the projection length
matches the requested horizon, that the seed value decays monotonically
toward zero, and that empty / all-NaN history produces a flat-zero
path (no NaN propagation into projections).

**`project_station`** — verifies:
- Horizon matches `data_age_days` exactly.
- Every projected row carries `is_extrapolated = True`.
- Fresh stations (age ≤ 0) return `None` — never projected.
- Too-stale stations (age > 14) return `None` — never projected.
- Empty history returns `None` cleanly.
- Resulting `risk_raw` values are always in `{LOW, MEDIUM, HIGH}`.

**Cause-attribution heuristic** — three branches of the diagnostic's
`attribute_cause()` (`pipeline_stale`, `predictors_missing`,
`ea_unavailable`) plus a None-input safety case.

### Geology tests (`test_geology.py`)

**GeoJSON validity** — `aquifer_bedrock.geojson` loads, is a
FeatureCollection with ≥1 valid Polygon/MultiPolygon, the provenance
sidecar exists, and `clipping_applied` is `true`, `crs` is `EPSG:4326`,
`simplification_tolerance_deg` matches the spec (0.001).

**Aquifer lookup** — `lookup_aquifer(lat, lon, tree, metadata)`:

- Keepers Wood (chalk borehole) → `Principal`.
- Manchester → `None` (out of region, so out of the clipped layer).
- Returns a dict with exactly `aquifer_name` and `aquifer_designation`
  keys.
- Designation is always one of the four canonical values.

**Catalogue enrichment integrity** — `enrich_with_aquifer()`:

- Adds the two new columns.
- Does **not** drop or mutate any existing columns.
- Classifies an inside-region station, returns `None` for an outside-
  region station.
- Empty DataFrame in → empty DataFrame out with the new columns
  present (schema stability).
- Missing GeoJSON file → columns added but filled with `None`,
  no exception.

**Clipping check** — every aquifer feature must lie inside the SW
region polygon (with a 0.0015° tolerance to absorb post-clip
simplification drift). Stops the layer from inadvertently leaking
outside the region.

**Dashboard loader** — `src.dashboard.geology.load_aquifer_layer()`
returns a FeatureCollection on the real file, returns `None` for a
missing path, exposes `AQUIFER_STYLE` / `AQUIFER_ORDER` with complete
entries for all four designations.

---

## Why these specific tests matter

Each test category exists because of a real failure mode that could
otherwise reach production silently. The "Real-world failure prevented"
column is the elevator pitch for the audit reviewer.

| Test category | What it validates | Real-world failure prevented |
|---|---|---|
| Catalogue period parsing | Period inferred from `measure_id` slug | Silently dropping 330 / 333 GW stations again because the EA CSV's `measures.period` column is unreliable. |
| Catalogue spatial filtering | Region membership test | Catalogue spans the wrong geography; ops teams act on stations outside their remit. |
| Event detection | Consecutive HIGH-day grouping | Lead-time calculations attribute precursors to the wrong event. |
| Lead-time "closest" logic | Most-recent precursor selected, not earliest | Overstating warning time — telling operators they had 20 days notice when they really had 3. |
| Dashboard snapshot filtering | Filter composition + sort order | Risk class / trend / persistence filters quietly fail; ops users see the wrong subset. |
| Action classification | `IMMEDIATE_ACTION` etc. priority logic | Top-priority stations don't surface to the top of the table. |
| Region GeoJSON validity | File loads, schema correct | Dashboard crashes on startup; map missing. |
| Region buffer | Edge-of-boundary inclusion | Real boreholes 100 m from the simplified boundary silently dropped from forecasts. |
| Aquifer lookup | STRtree + contains test correctness | Aquifer column quietly nulls out without anyone noticing — audit claims aquifer enrichment but data shows otherwise. |
| Aquifer clipping | All polygons inside SW region | Layer leaks outside the region; the audit-visible polygon no longer matches what's stored in `provenance` files. |
| Provenance keys | Source URL, licence, fetch date present | An auditor asks "where did this come from?" and the answer is missing. |
| Live-data QC | Outlier / duplicate / stuck-sensor rules | Sensor-grade telemetry leaks bad values into the shards that seed the forecast and status. |
| Xref matching paths | Reference / coords / none each fire correctly | Cross-references silently fail to populate, the live tail produces zero rows, dashboard reverts to all-stale despite the API being available. |
| Status stale guard | Observations > 45 days old carry no status | A months-dark borehole keeps asserting "near normal" off ancient data. |
| Status join contract | `attach_current_status` adds columns, never reorders tiers | The current status silently overrides the forecast triage instead of tie-breaking it. |

---

## Suite-level invariants

Some properties are enforced across the whole suite rather than in a
single file:

- **Operational constants are pinned by tests** — the tier thresholds
  (`p_breach_14d` / `p_above_p90_14d` cut-points in
  `forecast_outlook._tier`), the status guards
  (`MAX_STATUS_AGE_DAYS = 45`, `TREND_EPS_M = 0.02`), and the
  run_chain/pipeline stage lists. Changing any of these triggers test
  failures by design — they are product decisions, not implementation
  details, and must be changed deliberately.

- **No network calls in tests**. All EA / ArcGIS interactions are
  exercised in production code only; tests use inline fixtures and
  the real on-disk artefacts.

- **Schema stability**. Several tests assert that columns are added,
  not replaced, and that empty inputs return empty outputs with the
  full column schema. This is the contract every dashboard consumer
  depends on.

---

## Adding new tests

When adding a new feature:

1. **Pure logic** → unit test in the relevant file
   (`test_catalogue.py`, `test_dashboard.py`, etc.).
2. **A new data artefact** (e.g. a new clipped layer) → add a class to
   `test_geology.py` or `test_region.py` with: file-exists,
   provenance-exists, geometry-validity, point-lookup-known-in /
   known-out, clipping-containment.
3. **A new chart** → smoke-test in `test_charts.py`: returns a Figure
   for valid + empty input; appears in the colour-consistency tests
   if it adds new colours.
4. **A new pipeline stage** → add a top-level test class that runs the
   stage on a tiny synthetic fixture and asserts the output schema.
   Subprocess-based integration tests are out of scope; we cover the
   stages individually.

The general principle: **assert behaviour at the boundary, not the
internals**. A test that asserts `df.shape[0] == 3` is brittle; a test
that asserts "the three known-inside stations are present and the two
known-outside stations are absent" survives refactors.

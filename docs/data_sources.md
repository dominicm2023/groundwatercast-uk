# Data sources — operational runbook

Every external data source GroundwaterCast depends on, with refresh
instructions, schema notes, and where to look when something breaks.

---

## 1. EA Hydrology API

**What it is**: audited historical archive of EA-operated boreholes,
rain-gauges, and river-level/flow stations (England; the platform's
Welsh hydrology coverage is effectively nil for groundwater — see
`docs/uk_data_coverage.md`).

**Endpoints**:
- Station list: `https://environment.data.gov.uk/hydrology/id/stations.csv`
- Per-measure readings: `https://environment.data.gov.uk/hydrology/id/measures/{measure_id}/readings.csv`

**Pipeline scripts**:
- `src.catalogue.build` → `data/processed/catalogue.csv`
- `src.linking.build` → `data/processed/station_links.csv`
- `src.download.build` → `data/raw/groundwater/<measure_id>.csv`
- `src.features.build` → `data/features/joined_timeseries.csv`

**Upstream cadence**:
- Logged (telemetry) stations: typically published within 7-14 days.
- Dipped (manual) stations: typically published 4-6 weeks after collection.

**Our refresh cadence**: manual — invoke `python -m src.pipeline.run`.

**Raw cache**: `data/raw/groundwater/`. Idempotent — re-running
`download.build` skips already-downloaded measure files. Re-fetch by
deleting the file you want refreshed.

**Auth / quota**: none. Be polite (sleep on retries).

**Known schema quirks**:
- `measure_id` is the canonical key; dipped variants end in
  `gw-dipped-i-mAOD-qualified`, logged in `gw-logged-i-subdaily-mAOD-qualified`.
- Quality codes (`quality` column) are `Good` / `Unchecked` / `Estimated` /
  `Suspect` / blank. The pipeline filters to `Good` and `Unchecked` only.

**Known failure modes**:
- Hard 200,000-row truncation on single-call fetches → `download.build`
  auto-falls-back to year-chunked download (handled).
- Occasional 502/504 transient errors → handled by retry-with-backoff.

**Gotcha**: `src.features.build` rebuilds `joined_timeseries.csv` from
scratch and **wipes the dipped-station ingestion**. After any
features-stage rebuild you MUST re-run
`scripts/v15_build_dipped_daily_series.py` (`run_chain --core` step 1
does this automatically).

---

## 2. EA Flood-Monitoring API (live)

**What it is**: near-real-time telemetry from a subset of EA stations.
Same underlying instrumentation as the hydrology archive, but exposed
on a 15-minute cadence (no quality control).

**Endpoints**:
- Station list: `https://environment.data.gov.uk/flood-monitoring/id/stations`
- Readings: `https://environment.data.gov.uk/flood-monitoring/id/stations/{notation}/readings`

**Pipeline scripts**:
- `src.diagnostics.flood_monitoring_xref` → builds the GW matching table
  (`data/processed/flood_monitoring_xref.csv`).
- `src.diagnostics.rainfall_monitoring_xref` → the rainfall-gauge
  matching table (feeds the live rainfall tail).
- `scripts/v16_refresh_live_gw.py` → pulls last 7 days of readings for
  every matched station and write-throughs to the per-station Parquet
  shards.
- `scripts/v19_refresh_live_rainfall.py` → extends the raw rainfall tail
  (keeps Weibull recharge fresh).

**Upstream cadence**: every 15 minutes.

**Our refresh cadence**: hourly (`run_chain --live` via cron, or the
in-app auto-refresh).

**Coverage**: a minority of catalogued GW stations have a
flood-monitoring counterpart (~11 % in the pilot region; ~10–20 %
nationally). Rebuild the xrefs after any `catalogue.build` run
(`run_chain --xref`).

**Auth / quota**: none, fair-use API.

**Known schema quirks**: The `measure` field can be a single string or a
list — `fetch_live_readings` handles both. No quality codes; we accept
all returned non-NaN values.

**Known failure modes**: occasional API gaps for individual stations
during EA maintenance windows — handled silently (no readings written
for that station that hour; next refresh picks up).

---

## 3. BGS Geology 625k (Bedrock) — indicative aquifer layer

**What it is**: BGS Geology 625k (DiGMapGB-625) bedrock geology, classified
to an *indicative* aquifer potential (Principal / Secondary / Low) by
`scripts/build_bedrock_geology.py` from rock composition (`RCS_D`). Used to
enrich the catalogue (`aquifer_name` / `aquifer_designation` columns, the
latter now holding the indicative class) and as the explorer's lazy geology
overlay. **NOT** the official EA/BGS Aquifer Designation — that dataset is
BGS-licensed (not OGL/commercial-clean) and was retired from this product;
see the launch / commercial-clean notes.

**Cache**: `data/geology/bedrock_625k.geojson` (committed, UK-wide, WGS84,
property `aquifer_class`, with provenance sidecar `bedrock_625k.source.json`).
Derived from the BGS 625k GeoPackage (`data/geology_src/`, gitignored) — re-run
`python -m scripts.build_bedrock_geology` when the source or classification
changes.

**Upstream cadence**: rarely updated (geological data). Treat as static.

**Licence**: Open Government Licence v3 (free, commercial use permitted).

---

## 4. Region boundary (ONS)

**What it is**: the catalogue's spatial filter and the dashboard map
overlay, configured via `config.region.geojson_path`.

**Cache**: `data/regions/england_wales.geojson` — ONS *Countries
(December 2024) Boundaries UK BUC* (ultra-generalised 500 m), England
and Wales as separate features. Provenance sidecar alongside.

**Note**: the catalogue applies a 0.15° (~15 km) membership buffer, so
near-coast and near-border stations aren't dropped by the generalised
coastline. Bring your own polygon for a regional deployment — any
WGS84 Polygon/MultiPolygon FeatureCollection works.

**Licence**: OGL v3 (contains OS data © Crown copyright and database
right 2024).

---

## 5. ECMWF ENS (51-member ensemble rainfall)

**What it is**: the ECMWF 51-member ensemble (1 control + 50 perturbed),
15-day horizon, 0.25° (~20 km), the rainfall driver for the
probabilistic groundwater forecast. Two transports:

### 5a. Open-Meteo Ensemble API — the supported default (days 1–15)

- `src/forecast/ensemble/open_meteo.py`; plain JSON, no GRIB stack,
  works on any OS.
- **Licensing tiers**: the free endpoint is **non-commercial only**.
  Commercial deployments set `GWC_OPEN_METEO_API_KEY` (an Open-Meteo
  API subscription), which switches requests to the
  `customer-ensemble-api.open-meteo.com` host.

### 5a-ext. Open-Meteo Seasonal API — ECMWF EC46 extended range (days 16–46)

- `src/forecast/ensemble/open_meteo_ec46.py`, spliced onto the daily ENS
  members by `splice.SplicedEnsemble` (config
  `forecast.ensemble.extended`). Endpoint
  `seasonal-api.open-meteo.com/v1/seasonal`, `models=ecmwf_ec46`,
  `daily=precipitation_sum` per member (51 members, daily values,
  updated daily ~20:30 UTC; some members carry a null on the final day —
  tolerated as a ragged tail).
- Same licensing tiers / API key as §5a (customer host
  `customer-seasonal-api.open-meteo.com`).
- Failure posture: if the EC46 fetch fails the build degrades loudly to
  the 15-day ENS forecast — the extension never blocks the daily build.

### 5b. ECMWF Open Data (GRIB) — zero-cost commercial fallback

- `src/forecast/ensemble/ecmwf_opendata.py`, via the `ecmwf-opendata`
  package (requires the cfgrib/eccodes stack — commented out in
  `requirements.txt`, installed in the Docker image).
- **Licence: CC-BY-4.0 — commercial use permitted with attribution.**
- Quirk: total precipitation `tp` is accumulated from forecast start —
  differenced to daily totals before use.

Raw members are cached under `data/raw/ensemble/<provider>/<run>/` for
audit. Refresh: daily (`scripts/build_ensemble_members.py`).

---

### 5a-seasonal. Open-Meteo Seasonal API — ECMWF SEAS5 (months 1–6 weighting)

- `src/forecast/seasonal/seas5.py`: one call per borehole per month
  (`models=ecmwf_seas5`, `daily=precipitation_sum`, 51 members, 183 days;
  updated monthly on the 5th). Raw payloads cached under
  `data/raw/ensemble/open_meteo_seas5/<run>/`. Daily values are NEVER used
  as forcing — only monthly member totals, as tercile probabilities that
  weight the ESP traces. Same key tiering as §5a.

## 6. Open-Meteo Archive API (ERA5 reference rainfall)

**What it is**: ERA5 reanalysis precipitation at each borehole's grid
point — the reference series for fitting the per-borehole grid→gauge
bias factor `f_bh` (`src/forecast/ensemble/bias.py`, 2-year overlap,
mean-ratio).

**Licensing**: same tiering as §5a — the `GWC_OPEN_METEO_API_KEY`
commercial key switches to the customer archive host. Underlying ERA5
data © ECMWF / Copernicus Climate Change Service.

**Refresh**: bias factors are fitted once per borehole and cached
(`data/model/ensemble_bias_factors.csv`); refitted on retrain or when
the scope changes.

**Long-history precip cache** (`src/data/era5_precip.py`,
`data/raw/era5_precip/`): the seasonal ESP traces need ~35 years of daily
ERA5 precipitation per borehole point (gauge records only reach the
download window). One heavy backfill per borehole on the first
`refresh_seasonal_inputs` run, thin monthly top-ups after; same key
tiering and 429 backoff as the PET cache.

---

## Derived artefacts (built from the above)

| Artefact | Built by | Refreshes when |
|---|---|---|
| `data/processed/catalogue.csv` | `src.catalogue.build` | Catalogue rebuild |
| `data/processed/station_links.csv` | `src.linking.build` | Catalogue rebuild |
| `data/features/joined_timeseries.csv` | `src.features.build` | After download + dipped re-ingestion |
| `data/features/gw_by_station/*.parquet` | `scripts/v15_build_per_station_parquet.py` | After joined_timeseries rebuild |
| `data/processed/gw_freshness.csv` | `scripts/v15_build_gw_freshness.py` | After Parquet rebuild OR live refresh |
| `data/processed/{flood,rainfall}_monitoring_xref.csv` | `src.diagnostics.*_xref` | After catalogue rebuild |
| `data/model/gw_monthly_normals.csv` | `scripts/build_gw_normals.py` | After joined_timeseries rebuild (`run_chain --core`) |
| `data/model/forecast_ensemble_members.parquet` | `scripts/build_ensemble_members.py` | Daily ensemble build |
| `data/model/ensemble_bias_factors.csv` | `scripts/build_ensemble_members.py` | Retrain / scope change |
| `data/model/forecast_ensemble_{summary,fan}.csv` | `scripts/build_ensemble_summary.py` | Daily, after members build |
| `data/model/pastas_models.json` | `scripts/build_pastas_models.py` | Retrain (dedicated venv) |
| `data/model/forecast_pastas_{summary,fan}.csv` | `scripts/build_pastas_{members,summary}.py` | Daily, after members build |
| `*_archive.parquet` | summary builders | Appended each run (calibration trail) |

All derived artefacts are **recoverable from upstream sources** — none
contain primary data. Safe to delete and rebuild at any time.

---

## See also

- `README.md` — quick start and bring-your-own-region setup
- `docs/architecture.md` — pipeline structure + page→artefact map
- `docs/deploy.md` — container/cron deployment
- `docs/uk_data_coverage.md` — nation-by-nation coverage study

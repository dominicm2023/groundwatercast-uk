# App architecture

High-level structural view of GroundwaterCast UK. Three lenses:

1. Navigation tree (what the user sees in the sidebar)
2. Data pipeline (build order from raw to artefact)
3. Page → artefact dependency map (which page reads what)

Cross-references: [`docs/model.md`](model.md) for the current-status
layer and recharge features,
[`docs/ensemble_forecast_design.md`](ensemble_forecast_design.md)
for the forecast methodology, [`docs/deploy.md`](deploy.md) for hosting.

---

## 1. Navigation tree

```
app.py  (entry point: st.set_page_config + st.navigation)
│
├─ Home                                        pages_app/home.py
│
├─ Groundwater
│  └─ Forecast outlook        pages_app/gw_outlook.py
│
└─ Info
   └─ About                   pages_app/about.py
```

The page list is a pure data structure in `src/dashboard/nav.py`
(testable without Streamlit); `app.py` maps it onto `st.Page` /
`st.navigation`. Regional packs can register extra flag-gated pages via
`config.modules`.

**App-start hook**: `src/dashboard/auto_refresh.py` keeps the data fresh
while the app is in use (live chain hourly, forecast build daily,
staleness-gated). Set `GWC_APP_START_REFRESH=0` when hosting with real
cron.

---

## 2. Data pipeline

```
                                EXTERNAL APIs
                ┌──────────────────────────────────────────────┐
                │ EA Hydrology  │ EA Flood-Mon  │ ECMWF ENS /  │
                │ (stations +   │ (live GW)     │ Open-Meteo   │
                │  readings)    │               │ (+ ERA5 ref) │
                └──────┬────────┴───────┬───────┴──────┬───────┘
                       ▼                ▼              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  STAGE 1 — Catalogue, linking, raw download                  │
   │  src/catalogue/build.py    → data/processed/catalogue.csv    │
   │    (region polygon from config.region.geojson_path + buffer, │
   │     aquifer enrichment)                                      │
   │  src/linking/build.py      → data/processed/station_links.csv│
   │    (top-3 rainfall + nearest river per borehole)             │
   │  src/download/build.py     → data/raw/<station_id>/*.csv     │
   └──────────────────────────────┬───────────────────────────────┘
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  STAGE 2 — Features (resample, lag/roll, Weibull recharge)   │
   │  src/features/build.py     → data/features/joined_*.csv      │
   └──────────────────────────────┬───────────────────────────────┘
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  STAGE 3 — Derived artefacts (scripts/run_chain --core)      │
   │  scripts/v15_build_dipped_daily_series   ← re-merge dipped   │
   │     stations after any features rebuild (documented gotcha)  │
   │  scripts/v15_build_per_station_parquet  → data/features/     │
   │     gw_by_station/*.parquet  (per-BH fast path, ~20× faster) │
   │  scripts/v15_build_gw_freshness  → data/processed/           │
   │     gw_freshness.csv                                         │
   │  scripts/build_gw_normals  → data/model/gw_monthly_normals   │
   │     .csv  (per-station monthly quantile ladder — the "vs     │
   │     normal" yardstick for status, tiers and seasonal)        │
   │  src/diagnostics/{flood,rainfall}_monitoring_xref  (--xref)  │
   │  scripts/v16_refresh_live_gw + v19_refresh_live_rainfall     │
   │     (--live, hourly — extends the shards to "now")           │
   └──────────────────────────────┬───────────────────────────────┘
                                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  STAGE 4 — Probabilistic ensemble forecast (daily)           │
   │  scripts/build_ensemble_members  (ENS members → bias →      │
   │     bridge → recharge → reduced_form_ar roll)                │
   │                          → data/model/forecast_ensemble_*.parquet
   │  scripts/build_ensemble_summary  → forecast_ensemble_{summary,│
   │     fan}.csv + append-only _archive.parquet (calibration trail)
   │  scripts/refresh_pet + build_pastas_models (retrain, dedicated│
   │     .venv-pastas) → pastas_models.json                       │
   │  scripts/build_pastas_members + build_pastas_summary (daily) │
   │     → forecast_pastas_{summary,fan}.csv (+ _archive.parquet) │
   │  → Forecast outlook page (Pastas primary, roll cross-check)  │
   └──────────────────────────────────────────────────────────────┘
```

Re-run any stage that downstream stages depend on. The orchestrator
`python -m scripts.run_chain --list` prints the executable DAG; the full
chain is fast once raw data is in place. Gotcha: the features stage
overwrites the dipped-station merge, so `run_chain --core` step 1 always
re-runs it.

**Publish (step 10, `run_chain --publish`)**: after a forecast refresh,
`scripts/build_artifact_pack.py` assembles the **published artifact pack**
(`outputs/pack/` — `stations.geojson` map index + per-station JSON detail
files + `meta.json`/`manifest.json`) from the artefacts above. The pack is
the product's public API for static front-ends and third parties; its
schema is the contract in [`docs/artifact_contract.md`](artifact_contract.md).
The Streamlit app does not consume it.

---

## 3. Page → artefact dependency map

```
                     │ catalogue │ gw_monthly_ │ gw_by_   │ pastas/   │
                     │ .csv      │ normals.csv │ station/ │ ensemble  │
─────────────────────┼───────────┼─────────────┼──────────┼───────────┤
home                 │     ·     │      ·      │    ✓ †   │    ✓ †    │
gw_outlook           │     ✓     │      ✓      │    ✓     │    ✓      │
about                │     ·     │      ·      │    ·     │    ✓ ‡    │
```

`†` — home reads only file mtimes (the data-freshness widget), not
contents.

`‡` — about shows the forecast build timestamp.

`gw_outlook.py` joins the current vs-normal status onto the forecast
triage (`status.attach_current_status`) — the combined product surface:
status-now → trend → 14-day fan → breach probability → seasonal
terciles, all in the below/near/above-normal vocabulary.

### Shared dashboard modules (`src/dashboard/`)

| Module | Purpose |
|---|---|
| `nav.py` | Pure page-spec builder consumed by `app.py` |
| `loaders.py` | `load_catalogue`, `load_gw_for_bh` (Parquet fast path), `load_freshness` / `freshness_for` |
| `status.py` | Current vs-normal status (quantile-ladder percentile, trend, `attach_current_status` join) |
| `forecast_outlook.py` | Triage tiers (worst-first, staleness-demoting) |
| `ensemble_view.py` | Fan figures, stitched observed→forecast trajectory, forecast detail panel |
| `seasonal_view.py` / `season_view.py` | Months 1–6 tercile bars / season-aligned history view |
| `map_builder.py` | Folium map factory (region polygon from config) |
| `geology.py` | Aquifer polygon helpers |
| `exclusions.py` | Known-bad station register (shared with the forecast scope) |
| `auto_refresh.py` | Staleness-gated in-app refresh jobs |

---

## 4. Refresh cadences

| Cadence | Action | Output |
|---|---|---|
| Per pipeline rebuild | `src.catalogue.build` → `src.pipeline.run` → `run_chain --core` | catalogue, features, shards, freshness, monthly normals |
| After catalogue rebuild | `run_chain --xref` | flood-monitoring + rainfall cross-references |
| Hourly | `run_chain --live` | live GW tail in the per-station shards + live rainfall tail |
| Daily | `run_chain --forecast` (or `--ensemble` without the pastas venv) | ensemble + Pastas forecast artefacts |
| On retrain | `scripts.refresh_pet` + `run_chain --pastas` | recalibrated Pastas models |

See [`docs/deploy.md`](deploy.md) for the container/cron commands, or
rely on the in-app auto-refresh when running locally.

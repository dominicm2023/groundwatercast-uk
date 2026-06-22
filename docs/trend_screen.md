# Trend screen

A per-borehole **non-stationarity diagnostic**. It flags boreholes whose
groundwater level carries a strong multi-year trend — the kind that **breaks the
forecast** (the rainfall-driven, stationary Pastas model mean-reverts away from
the trend, producing a misleading fan) and **skews the normals/threshold** — and
helps decide whether that trend is a **data artefact** or a **real signal**.

Implemented as **Tier 1 (report-only)**: it computes, ranks, and explains; it
changes nothing in the forecast. Confirmed artefacts are added to the known-bad
register *by hand*. Tiers 2–3 (below) are planned layers on the same module.

## Why

The motivating case is **Moor Hall** (NW England, Permo-Triassic Sherwood
Sandstone): GW rose **+0.70 m/yr, R² 0.99** (near-perfectly linear), 29.4 → 35.0
mAOD over 2018–2026. Two boreholes on the *same* aquifer within ~9 km were
**flat** over the identical period (Liverpool North +0.013 m/yr; Yew Tree Farm),
so the rise is **isolated, not regional** — and a clean monotonic ramp does **not**
track the non-monotonic 2018–2026 rainfall (2022 drought → 2023–24 wet). That
combination reads **artefact-like**. The Pastas forecast for Moor Hall
mean-reverted (a model artefact, not a prediction); it has been excluded.

**The hard part:** trend *shape* alone cannot separate a sensor/datum drift from
a real recharge-driven rise — both are near-linear, high-R². So the screen
reports **three signals of increasing cost/confidence**, and acts on none
silently:

1. **Rainfall coherence** *(cheap, automatable)* — does the de-seasonalised GW
   anomaly track the cumulative rainfall-anomaly state? A real recharge rise
   does; a datum/transducer drift is monotonic and rainfall-independent.
2. **Neighbour isolation** *(medium, data-quality-bound)* — regional + rising ⇒
   likely real; isolated ⇒ ambiguous (artefact *or* hyper-local real). Radius-
   based: the catalogue `aquifer_*` fields are 3-value productivity classes
   (Principal/Secondary/Low), **not formations**, so distance is the primary
   geological proxy.
3. **Metadata** *(expensive, human)* — datum-survey / abstraction-licence — the
   final arbiter, surfaced as `recommended_action = metadata_check`.

Crucially, **isolated does not map to "exclude"**: an isolated + rainfall-
*coherent* rise is a possible hyper-local rebound (`metadata_check`, never
auto-dropped); only isolated + *incoherent* is `artifact_like`.

## Metrics (per borehole, on monthly-mean GW)

| metric | meaning |
|---|---|
| `slope_sen_m_yr` | Theil-Sen slope (PRIMARY magnitude — resists a single datum step) |
| `slope_ols_m_yr`, `r2` | OLS slope + linear R² (the linearity signature) |
| `trend_change_m` | `|slope_sen| × record_years` — metres of unexplained drift |
| `seasonal_amp_m` | median within-water-year range of the de-trended series (own swing) |
| `drift_ratio` | `trend_change_m / seasonal_amp_m` — trend vs natural seasonal swing |
| `max_daily_step_m`, `n_steps_gt_thr` | smooth ramp vs discrete datum jump |
| `rain_corr` | corr(de-seasonalised GW anomaly, cumulative rainfall anomaly) |
| `neighbour_*`, `isolation_class` | radius-based isolated / regional / no_neighbours |

**Flag rule** (all thresholds in `config.diagnostics.trend_screen`):

```
is_trend = (r2 >= r2_min) AND (|slope_sen| >= slope_min_m_per_yr OR drift_ratio >= drift_ratio_min)
severity = high  if trend_change_m >= change_high_m
           medium if trend_change_m >= change_med_m
           low    otherwise          (none if not is_trend)
```

**Provenance / action** (the artefact-vs-real resolver):

| isolation | rainfall | `provenance_class` | `recommended_action` |
|---|---|---|---|
| isolated | incoherent (`rain_corr < rain_corr_min`) | `artifact_like` | `review_exclude` |
| isolated | coherent | `local_real_candidate` | `metadata_check` |
| isolated | unknown | `indeterminate` | `metadata_check` |
| regional | — | `regional_real` | `review_detrend_or_keep` |
| no_neighbours | — | `indeterminate` | `metadata_check` |

Calibration is **locked by `tests/test_trend_screen.py`**: the synthetic Moor
Hall ramp → `high / artifact_like / review_exclude`; the Liverpool North control
(slope 0.013) is **not flagged** (the `slope_min` gate sits ~7× above it).

## How to run

```
python -m scripts.build_trend_screen          # standalone
python -m scripts.run_chain --diagnostics      # via the chain
python -m scripts.run_chain --all              # includes it (after --core)
```

It is in the new `diagnostics` group — part of `--all` but **not** `--core`.
**Re-run after any `joined_timeseries` rebuild / retrain** (a `--core`-only
routine would let the review queue go stale).

**Output** `outputs/trend_flags.csv` — one row per flagged borehole (severity
≥ `emit_min_severity`), sorted severity desc then `trend_change_m` desc, with
`already_in_register` annotated from `exclusions.excluded_station_ids()` so
actioned cases drop off the queue. An append-only `outputs/trend_flags_history.parquet`
gives a per-run diff (new-this-run flags).

**Review workflow:** triage `review_exclude` rows; confirm with a datum-survey /
abstraction check; add confirmed artefacts to
`data/external/known_bad_stations.yaml` (which `scope.py` already subtracts).
Leave `regional_real` / `local_real_candidate` in place.

## Roadmap (tiers)

- **Tier 1 — report-only** *(this)*. Zero forecast-code risk; generates the
  labels the later tiers need before acting on an unmeasured false-positive rate.
- **Tier 2 — auto-exclude** *(deferred)*. After a national run shows the
  `artifact_like` false-positive rate is low: a machine-managed
  `auto_excluded_stations.yaml` + human `trend_allowlist.yaml`, one merge edit in
  `exclusions._load_register()` (hand-curated wins), a `provenance_class == artifact_like`
  **necessary gate** (so a regional/coherent rebound can never auto-drop), and a
  per-run circuit-breaker. Resolve the in-process `lru_cache` staleness first.
- **Tier 3 — detrend-and-forecast** *(later, regional cases only)*. Detrend →
  Pastas on the residual → re-add a **damped** trend extrapolation, behind a
  `confirmed_by_metadata` gate. **Prerequisite:** replace the register's boolean
  exclusion with a `treatment: exclude | detrend` field so `scope.py` subtracts
  only `exclude` and detrend boreholes survive `select_scope`; and re-derive the
  P90 proxy + normals on the detrended basis to avoid a stuck-"breached" state.

## Limitations

- **Linear/monotonic only.** A rise-then-fall or sub-gate slow drift has low net
  slope/R² and is missed; **step-changes** (datum shifts, the Graylingwell
  pattern) have low linear R² — the `*_step` columns flag them for a reviewer,
  but a dedicated change-point screen is a separate future item.
- **Two-anchor calibration** (Moor Hall, Liverpool North) is thin — treat the
  first national run as a tuning pass (all thresholds are config-driven).
- **Neighbour quality** depends on borehole density and on distance standing in
  for geology; sparse areas weaken the isolated/regional call (hence it is never
  the sole arbiter — rainfall coherence carries the provenance).
- A clean `trend_flags.csv` does **not** prove stationarity — only that no
  *linear-monotonic* trend exceeded the gates.

*Design note: the tier comparison and this method (esp. the rainfall-coherence
discriminator and the radius-not-formation neighbour gate) came out of a
multi-agent plan-and-interrogate pass; see the project memory.*

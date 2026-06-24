# Model & methodology overview

What the product computes per borehole and where each piece lives. The
detailed forecast methodology is in
[`ensemble_forecast_design.md`](ensemble_forecast_design.md); the
end-to-end build order is in [`architecture.md`](architecture.md).

> **History note**: earlier versions shipped a composite LOW/MED/HIGH
> "risk index" (weighted GW/dGW/recharge/river z-scores on top of a
> pooled random-forest delta model). It was retired pre-launch in favour
> of the single below/near/above-normal vocabulary described here — see
> the amendments log in `ensemble_forecast_design.md`.

---

## Features (`src/features/build.py`)

Engineered from the EA hydrology archive on a daily grid:

- **Lag features**: `GW_Lag1`, `GW_Lag7`, `GW_Lag30`.
- **Rainfall rollups**: `Rain_1d_sum`, `Rain_3d_sum`, `Rain_7d_sum` from
  the 3 nearest rainfall stations (Top-3 averaging).
- **River state**: nearest river level and flow.
- **Weibull recharge**: convolution of rainfall with a Weibull kernel
  (shape `k=1.8`, scale `λ=10.0`, lag 45 days) — a parametric proxy for
  soil-zone delay. The same kernel converts forecast member rainfall to
  forecast recharge in the ensemble chain.

House rule throughout: strict **time-based splits** wherever anything is
fitted or validated — train and evaluation windows never overlap in time.

---

## Current status vs normal (`src/dashboard/status.py`)

"Where is this borehole right now?", answered against the station's own
history — no composite weights, no thresholds to defend.

1. `scripts/build_gw_normals.py` builds a per-(station, calendar-month)
   **quantile ladder** — P10, tercile-1 (33%), median, tercile-2 (67%),
   P90 — from the full joined history (`data/model/gw_monthly_normals.csv`;
   months with under 5 distinct years are dropped, `n_years` is carried).
2. The freshest observed level (per-station Parquet shard, live EA tail
   included) is placed on that month's ladder:
   - **below normal** — under tercile-1
   - **near normal** — between the terciles
   - **above normal** — over tercile-2
   plus an interpolated percentile (clamped to [2, 98] — the ladder can't
   resolve extreme tails) and a 7-day trend
   (rising / falling / stable, ±2 cm dead band).
3. Observations older than **45 days** carry no status claim (grey "no
   status" chip); the level and its age are still shown.

The same below/near/above palette and wording runs through the 14-day
fan's secondary tier signal (`p_above_p90_14d` — P(any day in the
operational window exceeds that month's P90)) and the seasonal tercile
bars — one vocabulary across all three horizons.

---

## Live levels (`src/forecast/live_levels.py`)

For stations matched to the EA flood-monitoring API
(`src/diagnostics/flood_monitoring_xref.py`), hourly refreshes pull the
last 7 days of near-real-time readings and append them to the
per-station shards, so the status, forecast seeding, and charts all see
data to within the hour. QC on the live window:

- Drop NaN values and duplicates (keep last).
- Drop |z-score| > 10 against the station's archive mean (sensor spike).
- Flag (not drop) readings unchanged for > 24 h (stuck sensor).

Live data is **sensor-grade**, not quality-checked — the dashboard
badges it accordingly.

---

## Probabilistic forecast (14 days)

In-scope boreholes carry a probabilistic groundwater forecast driven by
the 51-member ECMWF ENS (daily, to day 15). Each member's rainfall is
bias-corrected, bridged to the observed tail, convolved into Weibull
recharge, and run through two response models:

- **Primary: calibrated Pastas transfer-function model** per borehole
  (FlexModel rain+PET recharge) with a calibrated AR1 noise band,
  Monte-Carlo sampled.
- **Cross-check: reduced-form autoregressive roll** (chosen by a
  regime-stratified hindcast), overlaid so the two methods keep each
  other honest.

Outputs per borehole: P10/P50/P90 fan, breach probability against the
resolved threshold (user-supplied YAML, else the station's own P90 as a
badged proxy), and a censored first-crossing-date distribution. Urgency
tiers key on the first 14 days (`p_breach_14d`, `p_above_p90_14d`).
**Indicative / uncalibrated** pending a verified archived winter. Full
methodology:
[`ensemble_forecast_design.md`](ensemble_forecast_design.md).

---

## Seasonal outlook (months 1–6, experimental)

Monthly tercile probabilities — P(below / near / above-normal
groundwater) against the same monthly normals — from ~34 historic-year
ESP forcing traces (ERA5 rainfall + ET0, bias-matched) run through the
calibrated Pastas models, with the trace spread weighted by ECMWF SEAS5
monthly rainfall terciles for months 1–3. Rebuilt monthly
(`run_chain --seasonal`). Unverified — climatological context, not a
prediction.

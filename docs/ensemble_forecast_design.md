# Probabilistic Ensemble Forecast — Design Note

How GroundwaterCast turns a free weather ensemble into a per-borehole
probabilistic groundwater-level forecast. This is the as-built methodology
record for the daily forecast chain.

Everything runs on **free, commercially-licensed** data, so the hosted service
carries no recurring data cost and is reproducible by anyone with a free
Copernicus account. Outputs are labelled **indicative / uncalibrated** until
verified against a full archived winter (see §12).

## 1. Headline output

Per borehole, each run produces:

1. **P10 / P50 / P90 level fan** — per-day quantiles of the member trajectories
   over the 14-day horizon.
2. **Breach probability** — `P(level crosses threshold T within the horizon)` =
   the fraction of members whose trajectory crosses `T`. The urgency tier keys
   on the first 14 days (`p_breach_14d`).
3. **First-crossing distribution** — among crossing members, the median
   first-crossing date and the P25–P75 range; non-crossing members are reported
   as a **censored fraction**, never silently dropped.

Canonical sentence:

> "32% chance of breaching 14.0 mAOD within 14 days; median first crossing
> ~24 Jun (P25–P75 18–30 Jun). 68% of members do not cross. *Indicative —
> uncalibrated.*"

`T₀` = the borehole's latest observed/live level (or a nowcast estimate from
recent rainfall where the live reading is stale). Thresholds resolve
**user-supplied → GW-P90 proxy** (§8).

## 2. Data sources

| Dataset | Source | Detail | Licence |
|---|---|---|---|
| Ensemble rainfall (forecast) | **ECMWF Open Data — IFS ENS** | 51 members (1 control + 50 perturbed), 15-day, 0.25° (~20 km) | CC-BY-4.0 |
| Observed + live GW levels & gauge rainfall | **EA Hydrology / flood-monitoring** | daily gauge totals + GW levels (the live tail that seeds each forecast) | OGL v3 |
| Reanalysis rainfall + met fields | **Copernicus CDS — ERA5** | 35-yr daily precip for the bias fit + the seasonal ESP traces; FAO-56 ET0 self-computed (`pyet`) | Copernicus |
| Seasonal precipitation terciles | **Copernicus CDS — SEAS5** | monthly tercile probabilities (the seasonal weighting) | Copernicus |

Every input is free **and** redistributable for commercial use with
attribution. An optional paid feed (e.g. a Met Office partner ensemble, or an
extended-range model) can slot behind the same provider interface (§3) — it is
**disabled by default** on the open-data path.

**Accumulation gotcha (implementer note):** ENS total precipitation (`tp`) is
accumulated from forecast start — difference consecutive accumulations at day
boundaries to get daily totals, and clamp tiny numerical negatives to 0.

**Audit:** raw retrieved members are cached under
`data/raw/ensemble/<provider>/<run>/` (raw-data-for-audit); derived artefacts
are regenerable.

## 3. Provider abstraction (the swappable hinge)

The whole downstream chain treats the ensemble as *"N member daily-rainfall
series for a location"* — the only contract:

```python
# src/forecast/ensemble/provider.py
class EnsembleRainfallProvider(Protocol):
    name: str
    def fetch(self, lat: float, lon: float, start: date,
              horizon_days: int) -> pd.DataFrame:
        """columns = [member (int), date (UTC, daily), precip_mm (float)];
        raw payload cached to data/raw/ensemble/<name>/<run>/ for audit."""
```

Implementations: `ECMWFOpenDataENS` (production, free) and `OpenMeteoEnsemble`
(prototyping / self-host). Adding another feed is one class + one config value
(`forecast.ensemble.provider`), with no downstream change.

## 4. Grid → gauge bias correction

The response models are trained on **gauge** rainfall (point series); ENS precip
is a **~20 km areal mean**. Feeding raw grid precip into a gauge-trained kernel
would bias recharge. A per-borehole **multiplicative** factor corrects the
scale:

```
f_bh = mean(gauge rainfall over overlap window) / mean(reanalysis precip over same window)
```

applied to every member's daily precip. `f_bh` is fitted against **CDS-ERA5** at
the borehole point (so it is gauge/ERA5 by construction) and stored in
`data/model/ensemble_bias_factors.csv` — auditable and frozen between refits.
Full **quantile mapping** (correcting wet-day frequency and intensity, not just
the mean) is a planned refinement.

## 5. Per-member forward chain

For each member *m* and borehole:

1. **Bias-correct:** `precip ← f_bh · precip`.
2. **Bridge:** concatenate the recent **observed + live** gauge tail (≥ 45 days,
   for the kernel) with the bias-corrected member forecast → one continuous
   daily series. The historical tail is identical across members; only the
   forecast segment varies.
3. **Recharge:** convolve with the Weibull recharge kernel (k ≈ 1.8, λ ≈ 10 d,
   lag 45 d) → daily recharge.
4. **GW response:** roll the level forward through the calibrated response model
   (§6) → a member level trajectory.

## 6. GW response model

- **Primary — calibrated Pastas TFN.** A per-borehole transfer-function–noise
  model (FlexModel rain + PET recharge) with a calibrated AR1 noise band,
  Monte-Carlo sampled across the ensemble → the member level fan.
- **Cross-check — reduced-form AR roll** (`reduced_form_ar`): a per-borehole
  recharge→level response with a momentum term, selected by a pre-specified,
  leakage-safe, **regime-stratified perfect-forecast hindcast** (it beat
  persistence and simpler reduced-form variants on lead-day MAE with ~zero bias
  drift). Overlaid as a dotted line so the two methods keep each other honest.

The active roll is set by `forecast.ensemble.gw_roll_method`; rejected variants
are retained as baselines.

## 7. Aggregation → probabilistic outputs

- **Member weighting:** equal weight across the 51 members.
- **Breach probability:** `(# members crossing T within the horizon) / N`; the
  urgency tier keys on the operational 14-day window (`p_breach_14d`).
- **First-crossing:** for crossing members, the median first-crossing date +
  P25–P75; `1 − crossers/N` reported as the censored fraction.
- **Fan:** per-day P10 / P50 / P90 of member level.

## 8. Thresholds

Resolution order per borehole:

1. **User-supplied** (`data/thresholds/user_thresholds.yaml`) — operational
   levels in mAOD (flood-onset, asset, licence levels).
2. **GW-P90 proxy** — the station's own 90th-percentile level, clearly badged as
   a proxy.

`threshold_source` records which was used; proxy values are never presented as
operational thresholds.

## 9. Scope of uncertainty — honest framing

**Propagated:** rainfall-member spread + the Pastas AR1 model-noise band.
**Excluded** (so the raw fan is **under-dispersed**): GW-model structural error,
Weibull-parameter uncertainty, bias-correction error. Outputs are therefore
**indicative / uncalibrated** — the true breach probability has wider tails than
the raw member fraction; calibration (§12) partially corrects this.

**Chalk expectation:** in a ~2-week window most recharge comes from
*already-observed* rain (the kernel peaks ~7–10 days back); member spread in
level is largest exactly when a borehole sits near a threshold — which is when
the product is most useful. Far from any threshold, expect a tight fan and
~0% / ~100% breach probability. That is correct behaviour, not a bug.

### 9.1 Seasonal handoff — additive uncertainty band

The 14-day fan and the months-1–6 seasonal outlook are two estimators of the
same head, and drawn naively they disagree *in width* at the seam: the fan
terminal carries accumulated member-rainfall spread + AR1, while a raw ESP
monthly band is only between-analog-year climatological spread — so the band
would **collapse** at the handoff, reading as spurious tightening of confidence
exactly where confidence should be lowest.

`band_mode="additive"` (default) makes the seasonal band *inherit* the fan's
terminal uncertainty and decay it:

```
band = p50 ± z·√(V_ar1 + V_esp + V_inherit)
  V_ar1     model AR1, last-obs-clocked, counted ONCE
  V_esp     between-analog-year climatological spread
  V_inherit fan-terminal state/weather variance (AR1 removed), decaying on the
            state-memory timescale τ_state = max(Gamma-response 95%-time, AR1 α)
```

Two design points: **AR1 is counted once** (subtracted out of the inherited
fan-terminal width, then re-added as a single lead-clocked term, so model noise
never double-loads); and the inherited anomaly **decays on the slow state clock**
`τ_state`, not the fast residual α. The band is continuous with the fan at
month 1 and converges to (AR1 ⊕ climatology) at long lead. A full
distribution-propagation successor (mixture quantiles) is noted as follow-on.

## 10. Seasonal outlook (months 1–6)

~34 historic-year **ESP** traces per borehole — CDS-ERA5 daily precip × `f_bh`
+ ERA5 ET0, re-stamped onto the forecast calendar, bridged onto observed
history, and run through the calibrated model from today's state. Trace weights
come from **SEAS5** monthly precipitation terciles (months 1–3; later months
unweighted; a missing payload degrades loudly to equal weights). Output is
**monthly terciles** — weighted P(below / near / above-normal monthly-mean GW)
vs the borehole's own monthly climatology — plus weighted P10/50/90. Seasonal
*daily* rainfall values are never used as forcing; only the SEAS5 monthly
distribution tilts the historic spread. Rebuilt monthly; hard-labelled
**experimental**.

## 11. Output artefacts

| File | Grain | Key columns |
|---|---|---|
| `data/model/ensemble_bias_factors.csv` | per BH | `station_id, f_bh, overlap_start, overlap_end, fitted_on` |
| `data/model/forecast_*_members.parquet` | member × BH × day | `station_id, run, member, date, precip_mm, recharge_weibull, gw_pred` |
| `data/model/forecast_*_summary.csv` | BH × run (+ per-day fan) | `station_id, run, horizon_days, threshold, threshold_source, p_breach, p_breach_14d, first_cross_*`, plus per-day `gw_p10/p50/p90` |
| `data/model/forecast_*_archive.parquet` | append-only | every run's summary + fan, for verification (§12) |

## 12. Calibration & verification (ongoing)

Reliability diagrams, rainfall quantile mapping, isotonic recalibration of
breach probabilities, and Brier/CRPS skill vs damped-persistence all need
**≥ 1 winter of archived forecasts paired with eventual actuals**. Every run's
fan + summary is archived append-only so verification can begin as soon as a
winter accrues. Until then, every surface is labelled **indicative /
uncalibrated**.

## 13. Config

```jsonc
"forecast": {
  "ensemble": {
    "provider": "ecmwf_opendata",   // ecmwf_opendata | open_meteo
    "horizon_days": 15,
    "members": null,                 // null = all the provider offers
    "raw_cache_root": "data/raw/ensemble",
    "gw_roll_method": "reduced_form_ar",
    "bias_correction": "mean_ratio", // mean_ratio | quantile_map (planned)
    "scope": "fleet"                 // user | live | fleet
  }
}
```

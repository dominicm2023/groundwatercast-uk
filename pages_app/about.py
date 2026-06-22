"""About / methodology page."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

st.markdown("## ℹ️ About — methodology, data sources & roadmap")

st.markdown(
    """
**GroundwaterCast UK** produces daily probabilistic groundwater
forecasts — current status vs normal, a 15-day fan, and months 1–6
seasonal terciles — for boreholes monitored through the Environment
Agency's open hydrology APIs, built entirely on open data.

> Independent open-source project — not affiliated with or endorsed by the
> Environment Agency, the Met Office, ECMWF, or any water company.

---

### Data sources

| Source | Use | Licence |
|---|---|---|
| **EA Hydrology API** (`environment.data.gov.uk/hydrology`) | Daily groundwater levels and gauge rainfall — the station catalogue and full observation history | Open Government Licence v3 |
| **EA flood-monitoring API** | Near-real-time GW readings (the live tail that seeds the forecast and the current status) | OGL v3 |
| **ECMWF Open Data** (51-member ENS) | Daily forecast rainfall members, days 1–15 | CC-BY-4.0 |
| **Copernicus CDS — ERA5** | Reanalysis rainfall + met fields for per-borehole bias correction and self-computed FAO-56 ET0 | Copernicus licence (free, commercial use with attribution) |
| **Copernicus CDS — SEAS5** | Seasonal monthly rainfall tercile probabilities (the seasonal weighting) | Copernicus licence |
| **BGS 625k bedrock geology** | Aquifer classification per borehole (Principal / Secondary / Low) | Open Government Licence v3 |

### Current status vs normal

"Where is this borehole right now?" — the latest observed level (live EA
readings included, to within the hour where a station has a
flood-monitoring feed) is placed against the station's **own monthly
climatology**: a per-(station, month) quantile ladder (P10 / tercile-1 /
median / tercile-2 / P90) built from the full observation history.

- **below normal** (🟡) — under the month's lower tercile
- **near normal** (⚪) — between the terciles
- **above normal** (🔵) — over the month's upper tercile

An approximate percentile ("84th percentile for June") and a 7-day trend
arrow accompany the status. Observations older than 45 days carry no
status claim. The same below/near/above vocabulary (and palette) runs
through the whole product — current status, the 15-day fan's tier signal
(P(above the month's P90) within 14 days), and the seasonal terciles —
so there is one language, not a separate composite "risk" score.

### Forecast outlook (15 days)

For each in-scope borehole, each of the 51 ensemble rainfall members is
bias-corrected (per-borehole mean ratio against a 2-year reanalysis
overlap), convolved through a Weibull recharge kernel, and rolled forward
through a groundwater response model. The forcing is the **daily ECMWF
ENS to day 15** (all 51 members). Daily rainfall skill fades across that
window — the cross-member envelope is the signal — so the dashboard keys
its urgency tiers on the first 14 days only (`p_breach_14d`). The response models:

- **Primary: calibrated Pastas transfer-function model** (FlexModel
  rain+PET recharge, one model per borehole) with a calibrated AR1 noise
  band, Monte-Carlo sampled.
- **Cross-check: reduced-form autoregressive roll** (the incumbent model,
  overlaid as a dotted line so the two methods keep each other honest).

The fan (P10/P50/P90), breach probability, and first-crossing distribution
are read off the member trajectories. **All probabilities are indicative —
the spread is rainfall-member + model noise, not yet verified against a
held-out winter.** Forecasts are only seeded fresh where a live feed
exists; stale-seeded boreholes are demoted and flagged.

### Seasonal outlook (experimental, months 1–6)

Beyond the 15-day fan, each in-scope borehole gets monthly **tercile
probabilities** — P(below / near / above-normal groundwater) against its
own monthly climatology. Method: ~34 historic-year forcing traces (ERA5
rainfall + ET0 at the borehole point, bias-matched to the local gauges)
run through the calibrated model from today's state (the classic ESP
approach — groundwater memory carries the signal), with the trace spread
tilted by ECMWF SEAS5's monthly rainfall tercile probabilities. Seasonal
daily rainfall values are never used as forcing. Rebuilt monthly;
**unverified — treat as climatological context, not a prediction**.

### Breach thresholds

A breach level per borehole resolves in priority order:

1. **User-supplied threshold** — `data/thresholds/user_thresholds.yaml`
   (your operational levels: flood-onset, asset, licence levels)
2. **GW P90 proxy** — the station's own 90th-percentile level, clearly
   badged as a proxy

The source is always displayed, so proxy-based numbers are never mistaken
for operational ones.

### Known limitations

- **Uncalibrated uncertainty** — the fan is under-dispersed until a full
  winter of archived forecasts can be verified against observations.
- **Chalk-validated** — the method was developed and validated on chalk
  boreholes; other hydrogeologies run in an experimental tier.
- **England-first** — Wales has no open GW feed (NRW is not on the Defra
  hydrology platform), Scotland needs a separate SEPA client, and Northern
  Ireland has no open GW data. See `docs/uk_data_coverage.md`.
- **Known-bad stations** — sensors with datum/scaling shifts are excluded
  via a curated register (`data/external/known_bad_stations.yaml`);
  automated shift detection is roadmap work.

### Roadmap

- Verification + fan calibration after the first archived winter
- England-wide scale-up (national catalogue → readings-at-scale →
  national retrain with per-aquifer tiers)
- Static national map page consuming the published artifact contract
- SEPA (Scotland) adapter

See `docs/uk_data_coverage.md` for the national coverage study behind these.
"""
)

_summary = Path("data/model/forecast_pastas_summary.csv")
if _summary.exists():
    st.caption("Forecast last built: "
               + datetime.fromtimestamp(_summary.stat().st_mtime).strftime("%d %b %Y %H:%M"))

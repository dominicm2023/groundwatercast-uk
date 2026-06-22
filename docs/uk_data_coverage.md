# UK data coverage — how much of the country can be forecast

**Status:** research note (reproducible API counts).
**Date of all API counts:** 2026-06-11 (live read-only GETs; queries shown
verbatim so they can be re-run).
**Question:** how far across the UK does the open-data pipeline — a 15-day
probabilistic groundwater forecast plus a 6-month seasonal outlook — reach?
England is the default build; Wales, Scotland and Northern Ireland are assessed
for whether the same machinery extends to them.

---

## TL;DR

| Finding | Number |
|---|---|
| National GW-level stations on the Defra hydrology API | **3,610** (3,376 active) |
| …of which have a *logged* (telemetered) measure — the forecastable set | **1,073** (965 active) |
| …dipped-only (manual readings, weekly–monthly cadence) | **2,537 (70%)** |
| National rainfall stations on the hydrology API | **995** (973 active; all carry daily + 15-min measures) |
| National river level / flow stations | **2,638 / 1,102** |
| GW stations with a plausible *live* feed (flood-monitoring API, `qualifier=Groundwater`) | **635** |
| **Wales on the Defra hydrology API** | **effectively zero** (see §1.3) |
| Scotland (SEPA KiWIS): GW stations / with hourly series / fresh within ~2 months | **58 / 58 / ~40** |
| Northern Ireland open GW feed | **none found** |

**Headline:** an **England** build is strongly supported — the national hydrology
API is the *same API the pipeline already uses*
(`api.stations_url` in [`config/config.json`](../config/config.json)), and it
offers **965 active-logged GW stations** (258 of them on chalk, the
hydrogeology the method is validated for). **Wales cannot ride along**: NRW
hydrology stations are *not* on the Defra hydrology API, and NRW's own open API
has no groundwater endpoint. Scotland is a small but well-engineered API away
(58 hourly GW stations); NI has no open feed at all.

---

## 1. England + Wales — the Defra hydrology API

### 1.1 The API the pipeline uses

The station catalogue is built by
[`src/catalogue/build.py`](../src/catalogue/build.py) from the endpoint in
[`config/config.json`](../config/config.json) →
`api.stations_url = https://environment.data.gov.uk/hydrology/id/stations.csv`,
fetched with `api.stations_limit = 10000` and clipped to the configured region
polygon (`config.region.geojson_path`, default
[`data/regions/england_wales.geojson`](../data/regions/england_wales.geojson)).
Swapping the region polygon is the only change needed to re-scope the build.

### 1.2 National counts (England)

Queried 2026-06-11 against the API reference at
<https://environment.data.gov.uk/hydrology/doc/reference> (filter params:
`observedProperty`, `status.label`, `lat/long/dist`, `_limit`):

| Query | Count |
|---|---|
| `…/id/stations.json?observedProperty=groundwaterLevel&_limit=20000` | **3,610** |
| `…?observedProperty=rainfall&_limit=20000` | **995** |
| `…?observedProperty=waterLevel&_limit=20000` | **2,638** |
| `…?observedProperty=waterFlow&_limit=20000` | **1,102** |

All counts are well under the configured `stations_limit` of 10,000 per
property, so the existing fetch code works nationally without pagination changes
(a combined all-property list would need the limit raised or per-property
fetches).

GW station detail (CSV form of the same query):

| Slice | Count |
|---|---|
| Status: Active / Closed / Suspended | 3,376 / 187 / 47 |
| Has a `logged` measure (sub-daily telemetry) | **1,073** |
| Dipped-only (manual, typically weekly–monthly) | **2,537 (70%)** |
| Both dipped + logged measures | 558 |
| **Active AND logged — the realistically forecastable set** | **965** |
| Aquifer field contains "chalk" (case-insensitive) | 918 |
| Chalk AND active AND logged | **258** |

Implications:

- **The forecast core covers ~965 active-logged GW stations with zero API
  changes** — same endpoint, same measure-id grammar
  (`-gw-logged-i-subdaily-mAOD-qualified`, `-gw-dipped-i-mAOD-qualified`), same
  readings endpoint (`api.readings_url_template` in config).
- **70% of national GW stations are dipped-only.** These can be displayed (we
  ingest dipped series via `scripts/v15_build_dipped_daily_series.py`) but cannot
  seed a daily 15-day forecast with a fresh head — they are catalogue/context
  content, not forecast targets.
- **The method is chalk-calibrated** (Weibull recharge kernel; Pastas FlexModel
  on chalk boreholes — see
  [`docs/ensemble_forecast_design.md`](ensemble_forecast_design.md)). 258 of the
  965 active-logged stations are chalk — the highest-confidence first tranche.
  The remaining ~700 (oolites, Lincolnshire Limestone, sandstones, sands &
  gravels) run in a clearly-labelled experimental tier pending per-aquifer
  validation.

### 1.3 Wales — the premise is wrong: NRW is NOT on the hydrology API

A common assumption is that NRW data sits on the same Defra platform. **Direct
spatial queries falsify this.** Using the API's own `lat/long/dist` filter at
four points deep inside Wales (all observed properties):

| Query (all `…/id/stations.json?...&_limit=2000`) | Stations |
|---|---|
| `lat=51.86&long=-4.31&dist=30` (Carmarthen) | 1 (a water-quality sonde) |
| `lat=52.41&long=-4.08&dist=30` (Aberystwyth) | 0 |
| `lat=53.23&long=-4.13&dist=30` (Bangor) | 0 |
| `lat=51.48&long=-3.18&dist=30` (Cardiff) | 136 — all but one across the Bristol Channel in England |

A coarse-polygon sweep of the 3,610 GW stations initially flagged 45 as "Wales",
but inspection shows every one is in an English border county (Shropshire,
Cheshire/Wirral, Herefordshire). **Welsh GW stations on the Defra hydrology
API: ~0.**

What Wales actually offers:

- The Defra platform's NRW area, <https://environment.data.gov.uk/wales/>,
  currently lists **only bathing-water quality data** (checked 2026-06-11).
- NRW's own open API (<https://api-portal.naturalresources.wales/>, the "River
  Levels API") serves **river level, rainfall and sea level** at ~15-min cadence
  under OGL, with a free subscription key. **It has no groundwater endpoint.**
- Welsh GW level data exists (NRW WISKI; NRW supplies BGS-hosted GW data to the
  [UK Water Resources Portal](https://ukwrp.ceh.ac.uk/about/)) but there is **no
  open machine-readable feed** — access is by data request.

**Verdict on Wales:** an England-only build, with Welsh rainfall/river context
optional. We *could* show Welsh rainfall and river levels via the NRW API, but
with no GW observations there is nothing to forecast. Revisit if/when NRW
publishes GW on its open API.

---

## 2. Live-feed coverage (the live tail)

The live tail ([`src/forecast/live_levels.py`](../src/forecast/live_levels.py))
depends on the **EA flood-monitoring API** for real-time GW readings,
cross-referenced to hydrology stations by
[`src/diagnostics/flood_monitoring_xref.py`](../src/diagnostics/flood_monitoring_xref.py)
(priority: `stationReference`/`wiskiID` reference match → coordinate proximity →
exact/fuzzy name).

**National live pool** (queried 2026-06-11):

| Query | Count |
|---|---|
| `…/flood-monitoring/id/stations?qualifier=Groundwater&_limit=10000` | **635** |
| `…?parameter=level&type=Groundwater&_limit=10000` | 48 — *do not use; the `type` filter misses most GW stations. Use `qualifier=Groundwater`.* |

635 national flood-monitoring GW stations against 965 active-logged hydrology
stations is an upper bound of ~66% live coverage of the forecastable set;
reference/coordinate matching realistically lands **~40–65% of logged stations,
i.e. roughly 380–630 GW stations with an hourly-refreshable live tail**. The
rest of the forecastable set still gets a daily forecast (hydrology API data
lags ~1 day) — only the *intra-day* tail is unavailable.

Caveat: the flood-monitoring station list returns coordinates for only ~49 of
the 635 GW stations, so national matching leans on the reference/wiskiID path
(also the highest-confidence method).

---

## 3. Scotland — SEPA time-series API (desk assessment + live counts)

SEPA exposes a **KISTERS KiWIS** instance at
`https://timeseries.sepa.org.uk/KiWIS/KiWIS`, documented at
<https://timeseriesdoc.sepa.org.uk/>. Open data under OGL; basic access needs no
key. Live counts, 2026-06-11:

| Query | Result |
|---|---|
| `getStationList&parametertype_name=GWLVL` | **58 GW stations** |
| `getTimeseriesList&parametertype_name=GWLVL` | 928 series — every station has `Hour.Cmd` (hourly) plus daily/monthly/annual aggregates |
| Latest-reading month histogram (`Hour.Cmd`) | Jun 2026: 26 · May: 14 · Apr: 8 · older: 10 |
| `getStationList&parametertype_name=Precip` | 380 rainfall stations |

So the claim that Scottish GW is "mostly manually dipped" is out of date for
*this* network: all 58 stations publish an hourly series, and **~40 of 58 are
fresh within the last two months (~26 near-real-time)**. The long tail means a
freshness gate — which we already have
([`scripts/v15_build_gw_freshness.py`](../scripts/v15_build_gw_freshness.py) and
the `live` scope in config) — is essential.

**What a client takes:** KiWIS is a clean, well-documented query API; a
read-only client is ~1–2 days of work, plus mapping SEPA's parameter codes onto
our measure grammar and units (SEPA GW levels are typically depth/local datum,
not mAOD — a normalisation step is needed).

**Verdict:** 58 stations (~26 truly live) covering a hydrogeology —
fractured/superficial aquifers, not chalk — our recharge kernels have never been
validated on. It is a *credible "covers Great Britain" checkbox* and a nice
open-source story (one extra client class), but adds little analytical value
versus the ~965-station England build. **Do after England ships, as a
community-contribution-sized item.**

---

## 4. Northern Ireland — desk assessment

No open machine-readable GW level feed was found (searched 2026-06-11):

- **GSNI** (Geological Survey of NI, hosted by BGS) publishes aquifer
  classification and vulnerability *maps* via GeoIndex and static datasets —
  **no time-series API**.
- **DfI Rivers** runs ~130 active hydrometric stations — **surface water levels
  only**, no public API comparable to EA/SEPA.
- The polished GW-level viewer at <https://gwlevel.ie/> is **GSI Ireland (RoI)**,
  not NI.

**Verdict:** NI is out of scope for any data-driven build. State it honestly in
the docs ("no open groundwater feed exists for Northern Ireland — if you know
otherwise, open an issue") — that fits the open-source positioning better than a
quietly grey map.

---

## 5. Rainfall driver coverage

The forecast needs two rainfall inputs (see
[`docs/ensemble_forecast_design.md`](ensemble_forecast_design.md)):

1. **ECMWF ensemble grid rainfall** (forecast horizon) — global coverage, **no
   issue anywhere in the UK**. The existing provider chain
   (`forecast.ensemble.provider = ecmwf_opendata`) works unchanged.
2. **Gauge rainfall history** (top-3 stations per borehole, recharge kernel
   input):
   - England: **995 hydrology-API gauges (973 active)**, every one carrying both
     daily and 15-min measures — so the linker's daily-preference logic holds
     nationally.
   - Live rainfall tail: the flood-monitoring API lists **1,039** telemetered
     rain gauges; the xref
     ([`src/diagnostics/rainfall_monitoring_xref.py`](../src/diagnostics/rainfall_monitoring_xref.py))
     achieves ~90% on the gauges in use, and the national gauge population is the
     same platform.

**Rainfall is not a blocker anywhere we have groundwater.**

---

## 6. Verdict table and recommendation

| Nation | GW stations (forecastable) | Live GW feed | Rainfall gauges | API effort | Value |
|---|---|---|---|---|---|
| **England** | 3,610 total; **965 active+logged** (258 chalk) | **~380–630** est. | 995 + 1,039 live | **None** — same API, set the region polygon | **High** — same code path |
| **Wales** | **~0 open** | none | NRW open API (15-min, free key) | Small client, **but nothing to forecast** | **Nil until NRW publishes GW** |
| **Scotland** | **58** (all hourly; ~40 fresh) | built-in (hourly KiWIS) | 380 (SEPA Precip) | **Moderate** — new KiWIS client + unit/datum mapping + non-chalk validation | **Low–medium** — small n, unvalidated hydrogeology |
| **N. Ireland** | none open | none | none open | N/A — data does not exist openly | **Nil** — document honestly |

### Recommendation

1. **England as the default build**, scoped to the ~965 active-logged GW stations
   (forecast), with the ~2,537 dipped stations shown as catalogue/context. Tier
   the forecast: full confidence on the 258 chalk active-logged stations the
   method is validated for; a clearly-labelled experimental tier elsewhere
   pending per-aquifer checks.
2. **Drop "England + Wales" framing** — the shared-platform premise is false for
   hydrology stations. Track <https://environment.data.gov.uk/wales/> for NRW
   additions.
3. **Scotland as a fast-follow / community contribution**: a self-contained SEPA
   KiWIS adapter behind the existing catalogue interface.
4. **NI: state plainly that no open GW feed exists.** Saying so is more credible
   than a quietly grey map.

### Re-run the counts

All numbers above are reproducible with read-only GETs; the key queries are
listed verbatim. Counts will drift as networks change — re-run before relying on
any figure.

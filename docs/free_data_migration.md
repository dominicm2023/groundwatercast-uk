# Free-data migration plan (off the Open-Meteo paid tier)

**Status: planned, not started.** Decision (2026-06-12): the Open-Meteo
Professional plan (required for the Ensemble / Seasonal / Historical APIs
we use commercially; ~€99/mo) is too much for this project. Every dataset
we consume is available free-with-commercial-rights elsewhere; Open-Meteo
is buying us transport convenience, not data. This doc is the executable
plan for cutting over before the hosted (= commercial-use) deployment
goes live.

**What forces the timing:** the Open-Meteo *free* tier is non-commercial
only. Local development can stay on it indefinitely; the hosted public
demo cannot, at any traffic volume. Deadline = VM go-live.

## Target architecture

| Need | Today (Open-Meteo) | Free replacement | Licence |
|---|---|---|---|
| ENS daily members, days 1–15 | ensemble API | **ECMWF Open Data** GRIB (`src/forecast/ensemble/ecmwf_opendata.py` — **validated, W1 PASS**) | CC-BY-4.0 |
| EC46 extended, days 16–46 | seasonal API | ~~Open Data extended stream~~ **not on the free dissemination** (W2 finding) — extension disabled on the free path; S2S (48 h delay) or the 2026 dissemination expansion later | CC-BY-4.0 (licence open; channel isn't) |
| SEAS5 monthly member totals (trace weighting) | seasonal API | **CDS** `seasonal-monthly-single-levels` (**live, W3 PASS**; one UK-box fetch → `monthly_member_totals` contract) — else equal-weight ESP | Copernicus (commercial OK, attribution) |
| ERA5 daily precip (ESP traces + bias reference) | archive API | ~~ARCO-ERA5~~ (map-chunked — unusable for point series, W4 finding) → **CDS** `derived-era5-single-levels-daily-statistics` (`src/data/cds_era5.py` — **live-validated, PASS**; raw ERA5, so `f_bh` refits at cutover) | Copernicus (commercial OK, attribution) |
| ET0 / PET (Pastas recharge + traces) | archive API `et0_fao_evapotranspiration` | **Self-computed** — `src/data/et0.py`, pyet FAO-56 PM from CDS met fields (**validated, W5 PASS**: r ≥ 0.992 vs the OM series); Hargreaves fallback | as above |
| Live GW / rainfall / current status | — (EA APIs) | unchanged | OGL v3 |

**Single user-side step**: one free CDS account
(https://cds.climate.copernicus.eu → API key into `~/.cdsapirc` or
`CDSAPI_URL`/`CDSAPI_KEY`) unblocks W3 + W4 live runs and the cutover.

Open-Meteo remains a supported *non-commercial* provider behind the same
abstractions (it's the nicest dev experience); the migration adds free
commercial paths, it does not delete anything.

## Workstreams

### W1 — Validate the ENS GRIB provider: **DONE, PASS** (2026-06-13)

Validated locally on Windows (no VM needed): pip `eccodes` ships binary
wheels since 2.37.0, but they track specific CPythons (cp313 newest as of
Jun 2026) — on py3.14 the universal wheel installs and then fails at
runtime, so the GRIB stack lives in a small **`.venv-grib`** side-venv on
py3.13 (mirrors `.venv-pastas`; setup in `requirements-grib.txt`). The
provider hardens that failure mode (`RuntimeError` from the universal
wheel → the same actionable ImportError).

**Four pre-existing bugs found & fixed** in `ecmwf_opendata.py` (each now
pinned by `tests/test_ecmwf_grib.py`): (1) daily increments labelled one
day late (window END instead of window START); (2) step boundaries
anchored to the run hour instead of UTC midnight (12Z runs produced
12Z→12Z "days"); (3) cache keyed on wall-clock `now()` with unconditional
re-download; (4) `lon % 360` assumed without checking the decoded grid's
convention.

**Dissemination findings** (verified against the live index):
- Since IFS Cycle 50r1 (Oct 2025) the in-stream ENS control (`enfo`
  `type=cf`) no longer exists — the former HRES *is* the ENS control,
  published as `oper` `type=fc` for **all** cycles (the `scda` stream is
  gone too). Member 0 now comes from `oper`; members 1–50 from
  `enfo`/`pf`. The pre-50r1-written provider could never have worked.
- 06/18Z cycles disseminate to 144 h only (provider caps steps per cycle).
- **Open-Meteo serves a per-day MOSAIC of cycles** (e.g. yesterday-18Z /
  00Z / 06Z / 12Z across one 14-day horizon, advancing as each run
  ingests) — so single-run member-matched comparison is impossible
  without per-day run attribution, which the harness does (tiny 8-member
  probe downloads per candidate cycle).

**Parity protocol & result** (`scripts/validate_ens_provider.py`, run in
`.venv-grib`; report `outputs/ens_provider_parity.md`): 5 geographically
extreme boreholes (incl. 2 deliberate coastal stress cases), per-day run
attribution, gates over interior single-cycle days only (the start day
and mosaic splice-boundary days can mix two cycles within a calendar day
on Open-Meteo's side). Two-tier verdict:
- *Same field* (every borehole): Open-Meteo's value inside a 5×5
  grid-cell envelope ±0.2 mm for ≥97% of (member, day) cells →
  **97.3–99.0% across all 5**.
- *Point-value fidelity* (majority): member-matched corr ≥0.90,
  ensemble-mean r ≥0.98 → **inland 0.992–0.996 corr, member totals
  within 2.6%**; the 2 coastal cases diverge member-by-member (their
  land-corrected point blend vs our nearest cell) while staying inside
  the envelope — expected, accepted, documented.

Note: GRIB caches written before this fix-set are not comparable (the
day-label change shifts every series by one day) — delete
`data/raw/ensemble/ecmwf_opendata/` from before 2026-06-13. The
once-per-run grid download is in place (per-(run, step-range) cache;
fetches 2..N per run are cache hits), which is the national-scale fetch
strategy.

### W2 — EC46 extension: **BLOCKED upstream** (finding, 2026-06-12)
The premise was wrong. ECMWF's Oct 2025 "fully open" milestone opened the
*licence* (CC-BY-4.0, no data cost) on the whole Real-time Catalogue, but
the free **dissemination channel** (data.ecmwf.int + AWS/Azure/GCS
mirrors) still carries only the medium-range streams — verified
empirically against the dissemination roots (only `enfo`/`oper`/`waef`/
`wave` + AIFS exist; no `eefo` on any date/cycle), and the latest
`ecmwf-opendata` client (0.3.29) has no extended-range support. Delivery
of the full catalogue "may involve service charges to cover distribution
costs" (ECMWF announcement).

Free-ish alternatives, both needing registration and engineering:
- **S2S database** (apps.ecmwf.int): ECMWF sub-seasonal real-time with a
  ~48 h delay, via the MARS web API (free account, queued). A 2-day-old
  EC46 run still extends the fan usefully (skill at those leads is
  envelope-level anyway) but shifts the splice seam.
- Wait for ECMWF's announced 2026 dissemination expansion.

**Decision for cutover**: on the free path, disable the extension
(`forecast.ensemble.extended.enabled = false`) — the fan degrades to the
15-day ENS (the splice already degrades loudly by design) and the
months-1–6 seasonal outlook still covers the long view. Revisit when
`eefo` reaches the open dissemination. The Open-Meteo EC46 provider stays
for non-commercial/dev use.

### W3 — SEAS5 weighting via CDS: **DONE, live** (2026-06-13)

The open dissemination's seasonal stream (`mmsa`) is absent (verified —
404 everywhere), so SEAS5 routes through the **Copernicus CDS**
`seasonal-monthly-single-levels` (SEAS5 = system 51; free, commercial-OK;
licence accepted). The product is monthly-mean precip **rate** (`tprate`,
m/s) per member × `forecastMonth` 1–7 — converted to monthly totals
(rate × that calendar month's seconds × 1000) so it lands on the existing
`monthly_member_totals` contract; `tercile_probs` and the trace weighting
are unchanged.

Built in `src/forecast/seasonal/seas5.py`: `fetch_seas5_cds` (main-env —
ONE UK-box fetch for all in-scope points → tidy per-point CSV cache),
`cds_member_period_totals` (units + `forecastMonth`→calendar-period
mapping; pure), `load_cds_totals` (pastas-env reader, plain pandas — no
cdsapi/xarray in that venv). Wired into `refresh_seasonal_inputs` (CDS box
first, Open-Meteo per-point fallback; requests leadtime 1…months+1 so a
current-month init covers all 6 outlook months) and
`build_seasonal_outlook` (CDS cache → OM payload → equal weights).

Live-verified end-to-end (2026-06-13): one CDS box served the live-scope
points; member-mean monthly totals show the textbook UK cycle (summer
~42–50 mm → autumn ~72–91 mm). Offline tests: `tests/test_seasonal.py`
`TestSeas5Cds`. `_seas5_ref` picks this month's init once the ~5th-of-month
run is out, else the previous month.

### W4 — ERA5 point series: **ARCO unsuitable; CDS fetcher built** (2026-06-13)

The ARCO premise was wrong too. Every public ERA5 Zarr (ARCO `ar` and
`co` families, WeatherBench) is **map-chunked** — one chunk = one hour ×
the full globe (verified: `total_precipitation` chunks `[1, 721, 1440]`,
~4.2 MB compressed per variable-hour). Point time-series extraction
therefore transfers the whole planet per hour: a 1-year point pull
measured ~30 min; a 35-year backfill projects to ~17 h *per variable*,
and even a 1-month tail is ~3 GB/variable. No access pattern makes these
stores viable for our needs (connectivity itself was fine — store opens
anonymously in ~3 s).

The practical free commercial channel is the **Copernicus CDS**
(`derived-era5-single-levels-daily-statistics`): daily statistics are
aggregated server-side and a UK-bounding-box × 35-year request is a few
MB. Free registration; Copernicus licence permits commercial use with
attribution. **One CDS account is now the single user-side requirement
for the whole migration** (it also unblocks W3's SEAS5).

Built: `src/data/cds_era5.py` — pinned request shapes, box→point
extraction, unit conversions (tp daily-mean → mm/day, ssrd → MJ m⁻²
day⁻¹), and cache writers emitting the **exact** `era5_precip`/`pet`
schemas (union-by-date merge, fresh wins) so downstream code is
untouched. Offline tests: `tests/test_cds_era5.py`.

**Live-validated, PASS** (2026-06-13, `scripts/validate_cds_era5.py`,
report `outputs/cds_era5_validation.md`): extraction is correct — the
zero-shift daily correlation (0.745–0.779 over 2022 at 3 chalk boreholes)
**decisively beats the ±1-day neighbours** (0.23–0.32), which is the
signature that proves the de-accumulation + day labelling are right; mean
bias ≤ 0.26 mm/day; annual totals within 6–14%.

**Key correction to the original plan**: CDS-ERA5 is *not* the same series
as Open-Meteo's archive. CDS serves **raw ERA5 (0.25°)**; Open-Meteo
serves a **downscaled ERA5/ERA5-Land blend (~9 km)** that runs higher on
heavy orographic days over the chalk (e.g. 7.3 vs 11.2 mm). So daily
r ≈ 0.75 is a *real source difference*, not an extraction error — and the
per-borehole bias factor `f_bh` (= mean-gauge / mean-ERA5) **must be refit
against CDS-ERA5 at cutover** (a cheap per-borehole mean ratio, folded
into `run_chain --pastas`) rather than carried over. The existing 35-year
Open-Meteo-fetched caches stay usable for now; CDS feeds the incremental
tail and any England-scale-up backfill, with `f_bh` refit when the source
is switched.

CDS operational notes (observed): requests queue server-side (~12 min for
a 1-year UK box, then cached on repeat); responses are bare NetCDF on
Windows the temp-cleanup needs `ignore_cleanup_errors` + an explicit
dataset close (handled). The `derived-era5-single-levels-daily-statistics`
**Known Issue** warning about `maximum/minimum_*` and
`*_total_precipitation_rate` parameters does **not** affect us — we use
`total_precipitation` with `daily_mean`.

### W5 — Self-computed ET0: **formula validated, PASS** (2026-06-13)

`src/data/et0.py`: FAO-56 Penman–Monteith via `pyet` from daily met
fields (tmean/tmax/tmin, dewpoint→actual vapour pressure, 10 m→2 m wind
via FAO eq. 47, shortwave radiation), Hargreaves as the temperatures-only
fallback. Validated against the cached Open-Meteo
`et0_fao_evapotranspiration` series at 3 chalk boreholes × 3 years
(ERA5 met via the Open-Meteo archive as the validation transport):
**r = 0.992–0.994, bias −0.013…−0.032 mm/day, MAE ≤ 0.14 mm/day**
(Hargreaves r ≈ 0.95 — the documented cruder fallback). Report:
`outputs/et0_validation.md`; unit tests `tests/test_et0.py`.

Because the Pastas models were *calibrated* on the Open-Meteo ET0, the
one-off recalibration (`run_chain --pastas`) still happens **at
cutover**, once the PET cache tail is CDS-fed — the recharge models and
their forcing must stay self-consistent.

### W6 — Cutover + parity + docs (~0.5 day; needs the CDS key)

**Rehearsed 2026-06-13**: the full ensemble chain ran end-to-end on the
GRIB provider (`build_ensemble_members --provider ecmwf_opendata` →
summary): 63 boreholes × 51 members × 45 days, sensible trajectories,
provider provenance recorded in the members parquet
(`ecmwf_opendata+open_meteo_ec46`). The production rainfall path works
today, no key needed.

Remaining at cutover (after the CDS key exists):
1. Live-validate `src/data/cds_era5.py` (overlap-window near-equality vs
   the Open-Meteo-cached series) and wire the SEAS5 CDS fetch (W3).
2. Config flips: `provider: ecmwf_opendata`; `extended.enabled: false`
   on the free path (no eefo dissemination — fan becomes 15-day ENS);
   PET tail via CDS.
3. One-off `run_chain --pastas` recalibration (models stay
   self-consistent with the ET0 forcing).
4. End-to-end parity (old vs new providers: member stats, p_breach
   within sampling tolerance, seasonal terciles within a few %); update
   `data_sources.md`, README licensing notes, the design-doc amendments
   log. Keep the `GWC_OPEN_METEO_API_KEY` machinery — the dev providers
   still honour it.
Linux deploys install `requirements-grib.txt` straight into the main env
(manylinux wheels cover current CPythons — the `.venv-grib` side-venv is
a Windows-dev artefact only).

## Order & effort — state (2026-06-13)

| WS | State |
|---|---|
| W1 ENS GRIB provider | ✅ **PASS** (validated live; 4 bugs fixed; post-50r1 layout) |
| W2 EC46 extension | ❌ blocked upstream (no `eefo` on the free dissemination) — extension off on the free path |
| W3 SEAS5 weighting | ✅ **PASS** (live CDS box fetch → tercile weighting; degrades to equal-weight ESP without a key) |
| W4 ERA5 met/precip | ✅ **PASS** (live CDS validation; extraction correct, `f_bh` refits at cutover — raw ERA5 ≠ OM's downscaled blend) |
| W5 self-computed ET0 | ✅ **PASS** (r 0.992–0.994 vs the OM series) |
| W6 cutover | 🔶 GRIB chain rehearsed end-to-end; remaining items need the CDS key |

**Status 2026-06-13: W1+W3+W4+W5 all PASS and live; W2 blocked upstream
(extension off on the free path). The only remaining work is the W6
cutover** — config flips + the `--pastas` recalibration (which also refits
`f_bh` against CDS-ERA5). No further data-source engineering is needed.

**Update 2026-06-19 — historical-data half of the cutover WIRED (live deploy).**
The national England scale-up (73 → 655 forecast boreholes) exhausted the
Open-Meteo *free* archive quota on the 35-year per-point ERA5/PET backfill (HTTP
429 after ~130 boreholes), which forced the cutover early. Done:
- `scripts/refresh_seasonal_inputs.py` + `scripts/refresh_pet.py` now default to
  the **CDS box fetch** (`cds_era5.update_precip_caches`/`update_pet_caches`) —
  one UK-box request for the whole fleet instead of N throttled per-point calls
  — with `--full` (one-off backfill) vs incremental top-up and an automatic
  Open-Meteo per-point fallback (`--source open_meteo`) for dev / no-key.
- `scripts/build_seasonal_outlook.py` fits `f_bh` **inline from the CDS-ERA5
  cache** (`mean gauge / mean CDS-ERA5`) so the traces are self-consistent with
  the new source; the forecast side's persisted Open-Meteo `f_bh` is untouched
  (clean historical-only boundary).
- `netcdf4` pinned in `requirements.txt` (xarray engine for the CDS NetCDF).
Still pending (the *forecast* half): flip `provider: ecmwf_opendata` +
`extended.enabled: false` (fan 46→15 d) to take the live ensemble off Open-Meteo
too — the remaining non-commercial-licence exposure for the hosted deployment.

**Update 2026-06-19 — forecast half STAGED (15-day decision).** A 5-lens multi-agent
deliberation (unanimous, high-confidence) concluded the 16–46 d EC46 tail should be
**dropped, not replaced**: it is the product's lowest-skill, least-calibrated segment
(the code itself says "only the cross-member envelope carries signal" at those leads),
and that uncertainty is already carried — better-calibrated — by the SEAS5-weighted
6-month seasonal outlook, which re-anchors at the fan terminal (so a day-15 terminal just
slides the handoff left; no gap). NOAA CFSv2 was rejected (a full second GRIB workstream
for a *lower*-skill copy; ECMWF `eefo` may obsolete it). Escape hatch if a customer ever
needs a continuous daily 16–46 d fan: **pay Open-Meteo** (instant, keeps validated EC46),
never CFSv2. Config flipped to `provider: ecmwf_opendata` + `extended.enabled: false`.
Deploy (after the CDS cutover lands): install `requirements-grib.txt` into `.venv`
(`cfgrib selfcheck`), run `scripts.validate_ens_provider`, then `run_chain --forecast
--publish`, then verify the day-15 seasonal seam (re-anchoring only fleet-checked at the
day-46 anchor so far). Residual Open-Meteo dependency to retire later for full purity:
`bias.reference_archive_daily` (forecast `f_bh` fitting) still calls the OM archive when a
new station needs a factor — dormant for the cached fleet; point it at the CDS cache next.

## Risks / open questions

- **GRIB wrangling friction** (the reason Open-Meteo was nice): eccodes
  quirks, stream/parameter naming for the extended + seasonal streams.
  Mitigation: W1 first — it derisks the toolchain for W2/W3.
- **ARCO-ERA5 lag** (~1 week for ERA5T): irrelevant for traces/climatology;
  the bias-fit overlap window already avoids the recent tail.
- **ET0 differences** shift Pastas parameters slightly — handled by the
  W5 recalibration; verify hindcast skill doesn't regress.
- **ECMWF open-data rolling window** (~12 runs): no history — backfills
  are ERA5's job, which is exactly how the system already divides labour.
- **Egress/timeouts on GCS Zarr** from a small VM: chunked lazy reads keep
  it modest (per-borehole point series); cache-once design limits repeats.

## What this unblocks

Hosted demo with **zero recurring data costs** (VPS only), and the
once-per-run grid extraction (W1/W3) is the same machinery the
England-wide scale-up needs — this migration is half of the
rainfall-fetch-at-scale work.

# Abstraction-influenced-site screening (roadmap H7) — design note

> **Update 2026-07-09:** the §3 ingest now EXISTS —
> `scripts/build_abstraction_points.py` → `data/processed/abstraction_points.csv`
> (EA Water Rights Trading NALD extract, OGL v3, Jan 2025: 11,597 licences,
> point E/N + lat/lon, source, purpose, licence-level max quantities;
> holder identities stripped). Built for the valley-3D abstraction layer; the
> capture-zone join to boreholes (§3's screen) is now unblocked and only
> awaits the §5 decision. Caveats to honour: >100 m³/d returns-submitting
> licences only; security-sensitive supplies excluded; capacity ≠ pumping.

> **Status:** scoped 2026-06-17; prototyped 2026-06-17. **Shipped:** the
> register-reason path (`abstraction_influenced`). **Experimental & disabled:** the
> advisory amplitude detector — it over-flags natural Chalk on the real fleet (see
> §Findings). The EA-licence ingest (§3) remains the recommended operational path.

## Findings from the prototype (2026-06-17)

Built the advisory detector (`src/diagnostics/abstraction_screen.py` +
`scripts/build_abstraction_screen.py`) and ran it over the fleet. The metric —
seasonal-amplitude excess vs same-aquifer-class neighbours — **discriminates the
intended signal on synthetic series** (a pumped summer-drawdown borehole reads a
much larger swing than natural neighbours; pinned in `tests/test_abstraction_screen.py`).
**But on the real fleet it over-flags:** 575 boreholes evaluated → **125 flagged
(61 HIGH) at a 25 km neighbour radius, still 51 at 6 km.** The HIGH list is dominated
by classic downland Chalk (Woldingham ~28 m, Jevington, Brightstone…) whose *natural*
seasonal water-table swing genuinely is 10–30 m.

Root cause: the only geological covariate available — `aquifer_designation` — is a
**3-value productivity class** (Principal/Secondary/Low), which lumps high-amplitude
downland Chalk with low-amplitude valley/confined Chalk. "Same aquifer class" therefore
doesn't control for the natural-amplitude regime, so a genuinely-natural 28 m Chalk
site looks like a 22× outlier against a neighbour median dragged down by valley sites.
Tightening the radius reduces but does not remove the confounder.

**Conclusion:** the daily-data amplitude heuristic is **not operationally trustworthy
without a depth-to-water / hydrogeological-domain covariate** (or the EA-licence ingest,
§3). The detector is therefore **kept disabled** (`enabled: false`) as a validated
primitive + documented negative result. What ships and works today is the
register-reason path — a human confirms a pumped site and adds it to
`known_bad_stations.yaml` with `reason: abstraction_influenced`.

## The gap (from the reference-system sweep)

The BGS Hydrological Outlook (HOUK) deliberately **excludes heavily-pumped
boreholes** from its index sites: at a site dominated by local abstraction, the
level reflects a pump schedule, not the aquifer's response to recharge, so a
recharge-driven forecast is unreliable there. GroundwaterCast currently applies
no abstraction screen, so it will happily publish a confident fan at exactly the
most-modified sites — the same "present an unreliable forecast as trustworthy"
failure mode the rest of the engine-hardening pass has been closing.

## What already exists (and already covers part of this)

The repo has a mature, **human-in-the-loop** screening stack — an abstraction
screen should extend it, not reinvent it:

- **Hand-curated exclusion register** — `data/external/known_bad_stations.yaml`
  (`reason` ∈ scaling_change | datum_shift | sensor_fault | decommissioned |
  other), loaded by `src/dashboard/exclusions.py` (`excluded_station_ids()`),
  subtracted from every forecast scope in `src/forecast/ensemble/scope.py` and
  from the pack in `src/publish/pack.py`. **This is the only thing that actually
  removes a site** — by design, a human adds it.
- **Trend screen** — `src/diagnostics/trend_screen.py` + `scripts/build_trend_screen.py`
  → `outputs/trend_flags.csv`. Auto-classifies each borehole and **recommends**
  an action; it never auto-excludes. Its `classify()` already routes an
  **isolated** decline whose de-seasonalised anomaly is **uncorrelated with
  rainfall** (`rain_corr < rain_corr_min`) to `provenance_class="artifact_like",
  recommended_action="review_exclude"`. A heavily-pumped site that shows a
  sustained, neighbour-isolated, non-recharge drawdown is therefore **already
  flagged today** for human review.
- **Per-borehole signals available** for any detector: the daily shards
  `data/features/gw_by_station/<id>.parquet` (`date, GW_Level, is_interpolated,
  data_source`), the `neighbour_isolation()` + `rain_coherence()` helpers
  (already factored and tested), aquifer class (`aquifer_designation` ∈
  Principal/Secondary/Low), and the live QC `stuck_sensor` mark.

**Implication:** the *monotonic-decline* form of abstraction influence is largely
handled. The genuine gap is **cyclic / seasonal pumping** (summer abstraction
sawtooth) that isn't a long-run trend and so slips past a trend detector.

## The hard part (why this isn't an autonomous build)

Detecting a pumping signature from **daily-mean** groundwater is error-prone:

- Daily means smooth out the sub-daily and weekly pump cycles that are the
  cleanest abstraction fingerprint; what survives is a seasonal drawdown that is
  **hard to separate from genuine seasonal recharge depletion** — especially in
  the Chalk/limestone aquifers that dominate the network, where natural summer
  recession is large.
- A naive "summer drawdown ⇒ abstraction" heuristic would **false-positive on
  exactly the normal seasonal behaviour the product is built to forecast.**
- The authoritative signal is external: **EA abstraction-licence / NALD data**
  (is there a licensed abstraction within the borehole's capture zone, and at
  what volume). That is an ingest, not a heuristic, and it is not in the repo.

Shipping a fragile daily-data pumping detector unsupervised is the "doing
something silly" this hardening pass is meant to avoid. Hence: decision first.

## Recommended approach (the safe, in-pattern version)

Mirror the trend-screen contract exactly — **flag + recommend, human confirms,
register excludes** — and lean on what's already there:

1. **Register reason (cheap, ship-anytime):** add `abstraction_influenced` as a
   valid `reason` in `known_bad_stations.yaml` (+ a one-line schema note in
   `exclusions.py`). Lets a human exclude a confirmed pumped site **today**,
   with provenance, through the existing path. No new pipeline.
2. **Advisory detector (only if the heuristic proves sound):** a
   `scripts/build_abstraction_screen.py` following `build_trend_screen.py`,
   reusing `neighbour_isolation` + `rain_coherence`, targeting the **cyclic**
   case the trend screen misses — e.g. an isolated borehole whose **seasonal
   drawdown amplitude** greatly exceeds its neighbours' on the same aquifer and
   is uncorrelated with rainfall. Output advisory `outputs/abstraction_flags.csv`
   with `recommended_action`, **never auto-exclude**. Validate against a few
   known abstraction sites before trusting it.
3. **External truth (highest value, an ingest project):** join EA
   abstraction-licence locations/volumes to boreholes by capture-zone proximity;
   this is the HOUK-equivalent screen and the only non-heuristic answer.

## Decision needed (§5)

1. **Scope for now:** (1) only — add the register reason and lean on the
   trend-screen `review_exclude` path? Or commit to (2) the advisory detector,
   or (3) the EA-licence ingest?
2. **If (2):** is a daily-data seasonal-amplitude-vs-neighbours heuristic
   acceptable as *advisory-only*, given the false-positive risk on natural Chalk
   recession? What ground-truth sites can we validate against?
3. **If (3):** which EA dataset (NALD / EA abstraction licences open data), and
   is capture-zone proximity an acceptable proxy without a hydrogeological model?

## Build sketch (once decided)

- Register reason: `data/external/known_bad_stations.yaml` + `exclusions.py` note.
- Detector (opt): `scripts/build_abstraction_screen.py` → `outputs/abstraction_flags.csv`;
  reuse `trend_screen.neighbour_isolation` / `rain_coherence`.
- Publish (opt): extend `GEOJSON_*`/detail block in `src/publish/contract.py` +
  `pack.py` with an abstraction flag (additive — changelog entry, no
  `SCHEMA_VERSION` bump), so the explorer can label "abstraction-influenced —
  forecast indicative only".
- Tests: classify() unit cases + a register-union test in `exclusions.py`.

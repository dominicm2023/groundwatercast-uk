# Abstraction-influenced-site screening (roadmap H7) — design note

> **Update 2026-07-18: the capture-zone screen is BUILT** (§Capture-zone screen
> below). `scripts/build_abstraction_influence.py` joins the NALD licence
> points to every catalogued groundwater borehole via volume-banded influence
> radii → `data/processed/abstraction_influence.csv` (per-borehole nearest
> licence, in-radius count + deduped licensed capacity, tier
> none/possible/likely). The amplitude detector is **re-enabled**, gated on
> licence proximity (proximity prior × amplitude evidence): the 575-station
> re-run flags **11 (4 HIGH)** vs the ungated 125 — the gate suppressed exactly
> the 114 excess-amplitude boreholes with no licence in range. Still
> report-only; the register remains the only exclusion path.

> **Update 2026-07-09:** the §3 ingest now EXISTS —
> `scripts/build_abstraction_points.py` → `data/processed/abstraction_points.csv`
> (EA Water Rights Trading NALD extract, OGL v3, Jan 2025: 11,597 licences,
> point E/N + lat/lon, source, purpose, licence-level max quantities;
> holder identities stripped). Built for the valley-3D abstraction layer; the
> capture-zone join to boreholes (§3's screen) is now unblocked and only
> awaits the §5 decision. Caveats to honour: >100 m³/d returns-submitting
> licences only; security-sensitive supplies excluded; capacity ≠ pumping.

> **Status:** scoped 2026-06-17; prototyped 2026-06-17; **capture-zone screen +
> licence-gated detector shipped 2026-07-18.** Shipped earlier: the
> register-reason path (`abstraction_influenced`) — still the only thing that
> excludes a site.

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
primitive + documented negative result. *(Superseded 2026-07-18: the EA-licence
ingest landed and the detector is re-enabled behind the licence-proximity gate —
see §Capture-zone screen above.)* What ships and works today is the
register-reason path — a human confirms a pumped site and adds it to
`known_bad_stations.yaml` with `reason: abstraction_influenced`.

## Capture-zone screen (shipped 2026-07-18)

`src/diagnostics/abstraction_influence.py` + `scripts/build_abstraction_influence.py`
→ `data/processed/abstraction_influence.csv`, config block
`diagnostics.abstraction_influence`. A **screen, not a drawdown model**: each
Groundwater-source licence gets an influence radius banded by its licensed
daily volume, and a borehole inside any licence's radius is a candidate.

### Radius bands and why (chalk-typical transmissivity reasoning)

| licensed daily volume | radius | reasoning |
|---|---|---|
| < 500 m³/d | 500 m | Steady-state capture-zone width in a regional gradient is `W ≈ Q/(T·i)`. With chalk-typical `T` ≈ 500–2,000 m²/d and gradient `i` ≈ 0.002–0.01 (`T·i` ~ 5 m²/d per m), Q = 500 m³/d supports `W` ≈ 100 m — 500 m is a ×5-generous screen radius. |
| 500–5,000 m³/d | 1,500 m | Same formula at Q = 5,000 gives `W` ≈ 1,000 m. 1,500 m also sits inside the seasonal radius-of-influence `r ≈ 1.5·√(T·t/S)` (T = 1,000 m²/d, t = 180 d pumping season, unconfined chalk S ≈ 0.02 → r ≈ 4.5 km), where a Theis estimate puts seasonal drawdown for a full-rate 5,000 m³/d abstraction at ~1 m — detectable against chalk seasonal swings. |
| ≥ 5,000 m³/d | 3,000 m | Public-supply scale. Capture width `W` reaches multiple km; 3 km is deliberately *conservative* vs the radius-of-influence so the screen doesn't blanket whole valleys — the point is a review queue, not coverage maximisation. |

Bands are config-driven (`radius_bands_m`) and err toward catching candidates:
the output is report-only, so a false candidate costs one human glance, while a
missed pumped site costs a confidently-wrong public forecast. An unquantified
licence falls into the smallest band (the extract's floor is >100 m³/d), never
out of the screen.

### Tiers

- **likely** — a licence point within `likely_inner_fraction` (0.5×) of its
  radius, or deduped in-radius licensed capacity ≥ `likely_capacity_m3d`
  (5,000 m³/d).
- **possible** — ≥1 licence within its banded radius, outer ring only.
- **none** — no Groundwater licence in range. NB: the extract covers >100 m³/d
  *returns-submitting* licences only (security-sensitive supplies excluded), so
  `none` means "no large returns-submitting licence nearby", **not** "no
  abstraction".

Fleet result (2026-07-18, 3,317 catalogued GW boreholes, Jan-2025 vintage):
**587 likely / 335 possible / 2,395 none.**

Invariants honoured from the ingest: capacity figures are **licensed maxima,
not actual pumping** (every CSV row carries `capacity_basis=
licensed_max_not_actual_pumping`); multi-point licences repeat licence-level
maxima per row, so capacity is deduped per `licence_no` and **never summed
across rows** (pinned in `tests/test_abstraction_influence.py`); only
`licence_no` (the public join key) is carried — no holder data.

**Same-aquifer check: not feasible with this extract** — the NALD rows carry
no aquifer attribute, so the join is source-filter (Groundwater) + distance
banding only. A future refinement could spatially join licences to BGS aquifer
polygons and require concordance with the borehole's `aquifer_designation`;
noted, not built.

### Detector re-enabled behind the licence gate

`classify()` now takes the borehole's `influence_tier` as a **proximity
prior**: excess amplitude with tier `none` → `excess_amplitude_no_licence`,
severity none (fails closed on a missing tier); tier `possible` → severity
downgraded one notch; tier `likely` → full ratio-based severity. Config:
`abstraction_screen.licence_gate` (`enabled`, `min_tier`,
`downgrade_possible`); `enabled: true` restored on the screen itself.

**575-station re-run (same population as the 2026-06-17 negative result):**

| | ungated (2026-06-17) | licence-gated (2026-07-18) |
|---|---|---|
| flagged | 125 (61 HIGH) | **11 (4 HIGH)** |
| suppressed as no-licence | — | 114 |

The licence prior is precisely the external covariate the negative result
asked for. The 4 HIGH: College Wood (×6.49, licence 2.7 km), Norton Grange
Farm (×6.0, licence 8 m), Chilgrove House (×5.72, licence 227 m), Hoaden
Court (×5.32, licence 20 m). **Caution that keeps this report-only:** the gate
reduces but cannot eliminate natural-Chalk false positives — Chilgrove House
is a BGS index borehole whose huge seasonal swing is substantially natural,
and it still flags because a real licence sits 227 m away. Licence proximity
is a prior, not evidence of influence; the human `metadata_check` stays the
arbiter, and only the register excludes.

### Pack badge decision: **no badge (for now)**

Considered surfacing a quiet per-borehole "abstraction-screened" badge in the
published pack; decided **against**, on the same honesty logic as the rest of
the screen: licence proximity is licensed *capacity* near the borehole, not
observed pumping, and an unreviewed screen tier published as a per-site label
would over-claim (28% of the fleet would carry it). Confirmed sites already
leave the pack entirely via the register. Revisit once a human-reviewed cohort
exists — a *reviewed-and-confirmed-but-still-published* site is the right
badge candidate (artifact-contract addition would be additive, no
`SCHEMA_VERSION` bump).

### Issue tracking

This work resolves issue **#6** ("Abstraction-influenced borehole screening:
depth-to-water covariate or EA licence ingest") via the second of its two
candidate unlocks — the EA-licence ingest: capture-zone screen + licence-gated
detector, 125 → 11 flags on the 575-station re-run.

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

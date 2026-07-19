// Detail panel — render stations/<id>.json as the three-horizon narrative:
// current status vs normal → 14-day fan → seasonal terciles.
(function () {
  "use strict";

  const STATUS_LABEL = { below: "below normal", near: "near normal", above: "above normal" };
  const TREND_ARROW = { rising: "↑", falling: "↓", stable: "→" };
  const TIER_LABEL = {
    BREACH_LIKELY: "🔴 Breach likely", BREACH_POSSIBLE: "🟠 Breach possible",
    WATCH: "🟡 Watch", STABLE: "🟢 Stable",
  };
  const FRESH_LABEL = {
    fresh: "fresh", recent: "recent", stale: "stale", very_stale: "very stale",
  };
  // Model-disagreement thresholds (metres). A coarse, human-legible half-metre
  // split flagging where the two engines diverge by more than a typical
  // normal-band half-width. Structural cross-check, NOT a calibrated band.
  const SPREAD_HI = 0.5;
  const SPREAD_MED = 0.25;

  // -- "Show data" disclosures (1.3) --
  // Module-scoped refs set at render() start; the post-render binder bindData()
  // reads them. Safe because exactly one detail panel renders at a time — the
  // same single-panel pattern charts.js relies on for _fanCtx.
  let _meta = null;
  let _fanRedraw = null;        // set by bindFan; lets the trigger-levels editor redraw the chart
  let _curStationName = "";
  let _datasets = {};        // kind -> {caption, cols, rows}; reset each render()
  let _sortState = {};       // kind -> {idx, dir}; reset each render()
  function _stateOf(kind) { return _sortState[kind] || { idx: 0, dir: "asc" }; }
  function slug(s) {
    return String(s || "station").toLowerCase().replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "station";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  const fmt1 = (v) => (v == null || isNaN(v) ? "–" : (+v).toFixed(1));
  const fmt3 = (v) => (v == null || isNaN(v) ? "–" : (+v).toFixed(3));
  // Probability formatter. Floor/ceiling at <1% / >99% so a near-zero or
  // near-one breach probability never renders as a flat "0%"/"100%" that reads
  // as certainty — a 0 here means "no member crossed", not "impossible" (mirrors
  // the server headline's "<floor%" wording in aggregate.py).
  const pct = (v) => {
    if (v == null || isNaN(v)) return "–";
    if (v < 0.01) return "<1%";
    if (v > 0.99) return ">99%";
    return Math.round(v * 100) + "%";
  };
  function ordinal(n) {
    n = Math.round(n);
    if (n % 100 >= 11 && n % 100 <= 13) return n + "th";
    return n + ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
  }
  function prettyDate(s) {
    if (!s) return "–";
    return new Date(s + "T00:00:00Z").toLocaleDateString(
      "en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }
  // forecast.run is a full ISO timestamp (e.g. "2026-06-15T07:00:00Z"); the
  // plain prettyDate appends T00:00:00Z and would misparse it. Date-only is
  // enough for the trust card.
  function prettyDateTime(s) {
    if (!s) return "–";
    const d = new Date(s);
    if (isNaN(d.getTime())) return "–";
    return d.toLocaleDateString(
      "en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  }

  function statusChip(st) {
    const s = st && st.status;
    if (!s) return `<span class="chip none">no current status</span>`;
    const arrow = TREND_ARROW[st.trend] ? " " + TREND_ARROW[st.trend] : "";
    const p = st.percentile != null && isFinite(st.percentile)
      ? ` <span class="chip-pct">${ordinal(st.percentile)} pct</span>` : "";
    return `<span class="chip ${s}">${STATUS_LABEL[s]}${arrow}${p}</span>`;
  }

  function row(k, v) { return `<div class="d-row"><span class="k">${k}</span><span class="v">${v}</span></div>`; }

  // Winterbourne chip (RiverCast) — "bourne: flowing/dry, typically dry
  // <months>". Pure presentation of station.dry_months + the current level;
  // "flowing" is a display convention (level above a tiny epsilon), not a
  // new model claim.
  function winterbourneChip(stn, st) {
    const dry = Array.isArray(stn.dry_months) ? stn.dry_months : [];
    const monthsTxt = dry.map((m) => monthName(m).slice(0, 3)).join(", ");
    const level = st && st.level;
    const flowing = level != null && isFinite(level) && level > 0.001;
    return `<p class="winterbourne-chip"><span class="chip bourne">` +
      `🏞 bourne: ${flowing ? "flowing" : "dry"}</span>` +
      (monthsTxt ? ` <span class="caption">typically dry ${esc(monthsTxt)}</span>` : "") +
      `</p>`;
  }

  // Collapsed-by-default disclosure section (mirrors the data-drawer / trust-card
  // pattern). Used to demote the secondary panel sections below the fan chart so
  // the summary stays scannable on first click. Returns "" for empty content.
  function fold(title, innerHTML) {
    if (!innerHTML) return "";
    return `<details class="d-fold"><summary class="d-fold-sum">${esc(title)}</summary>` +
      `<div class="d-fold-body">${innerHTML}</div></details>`;
  }

  // On a standalone /b/ page the secondary sections become open cards in the
  // dashboard grid (same content, different affordance from the panel's folds).
  function pageCard(title, innerHTML) {
    if (!innerHTML) return "";
    return `<section class="d-section d-card"><h3>${esc(title)}</h3>${innerHTML}</section>`;
  }

  // Quick-watch ☆ shown next to the borehole name (the full rule editor lives in
  // the "Set a watch / alerts" fold). Toggling either keeps both in sync via
  // GWC_WATCH.refreshPinControl. Reflects the current watched state at render.
  function starBtn(stn, detail) {
    const id = stn && stn.station_id;
    if (!id) return "";
    // Watchlist/alerts are built around GW breach semantics (p_breach_14d,
    // mAOD thresholds) — not wired for RiverCast's Q95 read yet, so don't
    // offer a control that would silently evaluate against the wrong field.
    if (stn.station_type === "flow") return "";
    const W = window.GWC_WATCH;
    const watched = !!(W && W.has && W.has(id));
    const hasFc = !!(detail && detail.forecast);
    const lbl = watched ? "Watching this borehole — click to remove" : "Watch this borehole";
    return `<button type="button" class="d-star${watched ? " on" : ""}" ` +
      `data-star-id="${esc(id)}" data-star-name="${esc(stn.name || id)}" ` +
      `data-star-fc="${hasFc ? "1" : "0"}" aria-pressed="${watched ? "true" : "false"}" ` +
      `title="${esc(lbl)}" aria-label="${esc(lbl)}">${watched ? "★" : "☆"}</button>`;
  }

  // The borehole's saved trigger levels (ladder rungs) — passed to the fan chart
  // so each draws as a line. Empty when none set or the ladder module is absent.
  function triggerLevels(detail) {
    const id = detail && detail.station && detail.station.station_id;
    const L = window.GWC_LADDER;
    return (id && L && L.rungsFor) ? L.rungsFor(id) : [];
  }

  // Trend-screen stability flag (roadmap 1.1) — a "review", not "broken", badge.
  const PROV_LABEL = {
    artifact_like: "looks like a data artefact (datum / sensor drift)",
    step_shift: "a sudden datum step in the record",
    local_real_candidate: "possibly a real local trend",
    regional_real: "a regional trend, shared with nearby boreholes",
    indeterminate: "an unexplained multi-year trend",
  };
  const ACTION_LABEL = {
    review_exclude: "under review for exclusion",
    metadata_check: "pending a datum / abstraction-licence check",
    review_detrend_or_keep: "under review (detrend or keep)",
  };
  const ISO_LABEL = {
    isolated: "isolated from its neighbours",
    regional: "shared with nearby boreholes",
    no_neighbours: "too few neighbours to compare",
  };
  function trendFlagBlock(tf) {
    if (!tf) return "";
    const slope = tf.slope_sen_m_yr;
    const slopeTxt = (slope != null && isFinite(slope))
      ? `${slope > 0 ? "+" : ""}${(+slope).toFixed(2)} m/yr` : "–";
    const coh = (tf.rain_corr != null && isFinite(tf.rain_corr))
      ? (tf.rain_corr < 0.35 ? "doesn't track rainfall" : "tracks rainfall")
      : "rainfall link unknown";
    const prov = PROV_LABEL[tf.provenance_class] || "a multi-year trend";
    const act = ACTION_LABEL[tf.recommended_action] || "under review";
    const iso = ISO_LABEL[tf.isolation_class];
    const signals = [slopeTxt, coh].concat(iso ? [iso] : []).join(" · ");
    return `<div class="trend-note">` +
      `<span class="chip review">⚠ flagged for review</span>` +
      `<span class="trend-sev">${esc(tf.severity || "")} priority</span>` +
      `<p>This borehole shows ${esc(prov)} (${esc(signals)}) — ${esc(act)}. ` +
      `The forecast assumes a stationary, rainfall-driven response, so treat it ` +
      `with extra caution here.</p></div>`;
  }

  // Unified "How this number was made" trust card (roadmap 1.2). One
  // collapsed-by-default disclosure merging data confidence, methodology
  // lineage, and model disagreement. Self-contained native <details> — no JS
  // wiring, no .fan-host/.svg-fan selectors, so bindFan + deep-links are
  // untouched. Appended LAST in render().
  function trustCard(detail, meta) {
    if (!detail) return "";
    const st = detail.status || {};
    const fr = detail.freshness || {};
    const fc = detail.forecast;            // may be null (status-only stations)
    const mrow = (detail.normals || []).find((r) => r.month === st.month);
    const isFlow = detail.station && detail.station.station_type === "flow";
    const C = window.GWC_CHARTS;
    const unit = C && C.unitOf ? C.unitOf(detail) : "mAOD";
    const uLabel = C && C.unitLabel ? C.unitLabel(unit) : "mAOD";
    const fv = C && C.fmtOf ? C.fmtOf(unit) : fmt1;

    const obsAge = st.obs_age_days;
    const frLabel = fr.label;
    const daysSince = fr.days_since;
    const lastReal = fr.last_real_reading;
    const dataSrc = fr.data_source;
    const nYears = mrow && mrow.n_years;
    const fcRun = fc && fc.run;
    const thSrc = fc && fc.threshold_source;
    const spread = fc && fc.model_spread_mean;
    const nMembers = fc && fc.n_members;

    // -- Tier 1: always-visible one-line summary --
    const ageNum = obsAge != null ? obsAge : daysSince;
    const freshTxt = esc(FRESH_LABEL[frLabel] || frLabel || "—");
    const parts = [];
    parts.push(`based on a ${freshTxt} reading` +
      (ageNum != null ? ` (${ageNum} d old)` : ""));
    if (fc) parts.push(`forecast run ${prettyDateTime(fcRun)}`);
    if (fc && thSrc === "q95_proxy") parts.push("Q95-proxy threshold");
    else if (fc && thSrc === "gw_p90_proxy") parts.push("P90-proxy threshold");
    else if (fc && thSrc === "user") parts.push("against your threshold");
    if (isFlow) parts.push("gauged flow — includes abstraction & discharge effects");
    const oneLine = parts.join(", ") + ".";

    // -- Tier 2a: data confidence --
    let dataRows = "";
    dataRows += row("Latest reading",
      `${prettyDate(lastReal)}` + (obsAge != null ? ` · ${obsAge} d old` : ""));
    dataRows += row("Freshness",
      freshTxt + (daysSince != null ? ` · ${daysSince} d since real reading` : ""));
    if (dataSrc != null) dataRows += row("Source", esc(dataSrc));
    dataRows += row("Normal built from",
      nYears != null ? `${nYears} years of ${monthName(st.month)} data` : "—");
    // GW-only: the pack computes the generic normal-scores value for flow
    // gauges too, but "Standardised Groundwater Index" is a borehole concept —
    // don't present it on a river's discharge reading.
    if (!isFlow && st.sgi != null && isFinite(st.sgi))
      dataRows += row("SGI",
        (+st.sgi).toFixed(1) === "-0.0" ? "0.0" : (+st.sgi).toFixed(1));
    const staleExplain = "A normal cadence is a roughly monthly dipped reading; " +
      "a longer gap is a telemetry outage. ‘Stale’ here means the most " +
      "recent reading is older than the usual dip interval, so the current " +
      "status is an older snapshot.";

    // -- Tier 2b: methodology lineage (only when fc) --
    let lineageRows = "";
    if (fc) {
      lineageRows += row("Forecast run", prettyDateTime(fcRun));
      const thBasis = thSrc === "q95_proxy"
        ? "Q95 proxy — a climatological reference, not a licence Hands-off-Flow value"
        : thSrc === "gw_p90_proxy"
        ? "P90 proxy — not an operational threshold"
        : thSrc === "user" ? "Your threshold" : esc(thSrc || "none");
      lineageRows += row("Threshold basis", thBasis);
      if (fc.threshold != null)
        lineageRows += row("Threshold", `${fv(fc.threshold)} ${uLabel}`);
      if (fc.stale_days != null)
        lineageRows += row("Seed age", `${fc.stale_days} d`);
      if (isFlow) {
        // Full-horizon read (13-14 d for flow, so this rarely differs from the
        // headline 14-day figure above — shown anyway for methodology parity
        // with the GW long-horizon row below).
        if (fc.p_below_q95 != null && fc.horizon_days > 14)
          lineageRows += row(`P(flow &lt; Q95, ${fc.horizon_days} d)`, pct(fc.p_below_q95));
      } else if (fc.p_breach != null && fc.horizon_days > 14) {
        // Only when the horizon genuinely exceeds 14 d (else p_breach == the
        // 14-day breach already shown in the forecast section above).
        lineageRows += row(`Breach prob (${fc.horizon_days} d)`, pct(fc.p_breach));
      }
    }
    const forcingNote = "Forcing: ECMWF ensemble (see About for attribution).";
    // River-specific caveats (RiverCast) — always visible when fc exists, not
    // buried behind a spread check like the GW model-disagreement group.
    const riverCaveat = isFlow
      ? "Gauged flow — includes abstraction &amp; discharge effects. Rating " +
        "curves (the stage-to-flow conversion) are least accurate at low " +
        "flows. Indicative &amp; experimental."
      : "";

    // -- Tier 2c: model disagreement (only when fc && spread present) --
    let spreadRow = "", spreadCaveat = "";
    if (fc && spread != null) {
      const mag = Math.abs(spread);
      spreadRow = row("Model cross-check spread",
        `${fmt1(mag)} m mean` + (nMembers != null ? ` · ${nMembers} members` : ""));
      if (mag >= SPREAD_HI) {
        spreadCaveat = `The two engines (Pastas vs the reduced-form roll) ` +
          `disagree by ${fmt1(mag)} m on average over this horizon — a sign of ` +
          `structural uncertainty in the model, NOT a calibrated forecast ` +
          `spread. Read the fan as indicative.`;
      } else if (mag >= SPREAD_MED) {
        spreadCaveat = `The two engines differ by ~${fmt1(mag)} m on average — ` +
          `modest structural disagreement; not a calibrated uncertainty band.`;
      } else {
        spreadCaveat = `The two engines broadly agree (mean difference ` +
          `${fmt1(mag)} m). This is a model cross-check, not a calibrated ` +
          `uncertainty band.`;
      }
    }

    return `<div class="d-section trust-card">` +
      `<details class="trust-details">` +
        `<summary class="trust-summary"><span class="trust-title">How this number was made</span>` +
          `<span class="trust-oneline">${oneLine}</span></summary>` +
        `<div class="trust-body">` +
          `<div class="trust-grp"><h4>Data confidence</h4>${dataRows}` +
            `<p class="caption">${staleExplain}</p></div>` +
          (fc ? `<div class="trust-grp"><h4>Methodology</h4>${lineageRows}` +
            `<p class="caption">${forcingNote}</p></div>` : "") +
          (fc && spread != null
            ? `<div class="trust-grp"><h4>Model disagreement</h4>${spreadRow}` +
              `<p class="caption">${spreadCaveat}</p></div>`
            : "") +
          (riverCaveat
            ? `<div class="trust-grp river-caveat"><h4>Gauged flow</h4>` +
              `<p class="caption">${riverCaveat}</p></div>`
            : "") +
        `</div>` +
      `</details>` +
    `</div>`;
  }

  function render(detail, meta, opts) {
    const C = window.GWC_CHARTS;
    // Stash provenance + reset the data-disclosure registry (1.3). One panel
    // renders at a time, so module-scoped refs are safe.
    _meta = meta || {};
    _datasets = {};
    _sortState = {};
    _curStationName = (detail && detail.station &&
      (detail.station.name || detail.station.station_id)) || "";
    opts = opts || {};
    const range = ["90", "365", "730", "all"].includes(opts.range) ? opts.range : "90";
    const initDays = range === "all" ? 1e9 : parseInt(range, 10);
    const stn = detail.station || {};
    const st = detail.status || {};
    const fr = detail.freshness || {};
    const out = [];
    // RiverCast (Stage 7): a flow gauge reuses this whole renderer — same
    // envelope, same chart/fan components — with a handful of branches below
    // for the flow-specific vocabulary (Q95 not a breach threshold, gauged-flow
    // caveats, no seasonal/no standalone page yet). isFlow is the ONE
    // discriminator; unit/uLabel/fv drive every level-shaped number.
    const isFlow = stn.station_type === "flow";
    const unit = C.unitOf ? C.unitOf(detail) : "mAOD";
    const uLabel = C.unitLabel ? C.unitLabel(unit) : "mAOD";
    const fv = C.fmtOf ? C.fmtOf(unit) : fmt1;
    // On a standalone page (/b/<slug>/ for boreholes, /r/<slug>/ for flow
    // gauges — the RiverCast expansion's stub pages) the static shell already
    // shows the name, sub-line and a status chip — skip our own to avoid
    // duplication; the ☆ watch control moves into the actions row (still
    // inside #detail-body so watchlist.js can bind it). In the map side panel
    // everything is shown. The name stays "onBoreholePage" — it's the
    // am-I-on-my-own-stub-page discriminator for either station kind.
    const onBoreholePage = location.pathname.indexOf(isFlow ? "/r/" : "/b/") === 0;
    if (!onBoreholePage) {
      out.push(`<div class="d-head"><h2 class="d-name">${esc(stn.name || stn.station_id)}</h2>${starBtn(stn, detail)}</div>`);
      out.push(isFlow
        ? `<p class="d-sub">${esc(stn.river_name || "River gauge")} · RiverCast</p>`
        : `<p class="d-sub">${esc(stn.aquifer || "—")} · ${fmt1(stn.lat)}°N ${fmt1(stn.lon)}°E</p>`);
      if (isFlow && stn.winterbourne) {
        out.push(winterbourneChip(stn, st));
      }
    }

    // Share + verify-on-source actions. station_id IS the EA hydrology GUID, so
    // the official record is a direct client-side URL ("where's the real
    // data?"); Copy link makes the existing #bh deep-link shareable.
    if (stn.station_id) {
      // "Open full page" links to the station's static page (/b/<slug>/ for
      // boreholes, /r/<slug>/ for flow gauges) — shown in the map side panel,
      // hidden when we're already on that page.
      // Prefer the pack's canonical slug: for duplicate-named stations the stub
      // builder suffixes the slug, so re-deriving it from the name here would
      // link the suffixed twin to the WRONG station's page.
      const pageSlug = stn.slug || slug(stn.name || stn.station_id);
      const fullPage = onBoreholePage ? "" :
        `<a class="d-act-btn d-act-primary" href="${isFlow ? "/r/" : "/b/"}${pageSlug}/">Open full page ↗</a>`;
      const pageStar = onBoreholePage ? starBtn(stn, detail) : "";
      out.push(`<div class="d-actions">` +
        pageStar +
        fullPage +
        `<button type="button" class="d-act-btn d-copy-link">🔗 Copy link</button>` +
        `<a class="d-act-btn" target="_blank" rel="noopener" ` +
          `href="https://environment.data.gov.uk/hydrology/station/${encodeURIComponent(stn.station_id)}">` +
          `Verify on the EA record ↗</a>` +
        `<span class="d-act-status caption" role="status" aria-live="polite"></span>` +
        `</div>`);
    }

    // -- current status vs normal (skipped on the standalone page: its masthead
    // shows the chip + observation date already) --
    if (!onBoreholePage) {
      out.push(`<div>${statusChip(st)}</div>`);
      const obs = st.obs_date ? `observed ${prettyDate(st.obs_date)}` : "no recent observation";
      const age = st.obs_age_days != null ? ` (${st.obs_age_days} d old)` : "";
      out.push(`<p class="caption">${obs}${age}</p>`);
    }

    // Stability flag (if the trend screen flagged this borehole for review).
    out.push(trendFlagBlock(detail.trend_flag));

    // Stuck-sensor caution: a frozen telemetry value (flat readings > 24h) is
    // flagged by apply_qc and carried through as data_source "logged_live_stuck".
    // Surface it directly beneath the "data: fresh/stable" caption so the latest
    // reading is no longer presented as confidently fresh. Additive amber note.
    if (fr.data_source && String(fr.data_source).indexOf("stuck") !== -1) {
      out.push(`<p class="stale-note">⚠ Sensor may be stuck (flat readings) — the latest value has not changed and may not reflect the true level. Treat the current reading with caution.</p>`);
    }

    const fc = detail.forecast;
    const hd = fc && fc.horizon_days;
    const hLabel = hd != null ? esc(hd) : "";   // real forecast horizon (days)
    const hasSeasonal = !!(detail.seasonal && detail.seasonal.months && detail.seasonal.months.length);
    const mrow = (detail.normals || []).find((r) => r.month === st.month);
    const se = detail.seasonal;

    // -- forecast outlook FIRST: the fan chart is the lead visual (the main thing
    // on first click). Only the three headline metrics sit under it; threshold /
    // seed-age / long-horizon breach move into the trust card below. --
    let fcDetailCard = "";   // on a /b/ page the forecast metrics split into their own grid card
    if (fc) {
      // When the chart continues into the 6-month seasonal outlook, a bare
      // "14-day forecast" undersells it — use a span-neutral header instead.
      const fcTitle = hasSeasonal
        ? "Forecast outlook"
        : (hLabel ? `${hLabel}-day forecast` : "Forecast");
      out.push(`<div class="d-section${onBoreholePage ? " d-lead" : ""}"><h3>${fcTitle}</h3>`);
      if (fc.headline) out.push(`<p class="headline">${esc(fc.headline)}</p>`);
      // Short-record fan tier: a younger borehole (under ~5½ years of record)
      // that passed a leakage-safe backtest gate. The 14-day fan is real but
      // provisional — wider bands, and no seasonal outlook (it fails the long
      // horizon). Badge it so it's never mistaken for a mature-record forecast.
      if (fc.short_record) {
        out.push(`<p class="short-rec-note">⏳ <b>Short record — provisional.</b> ` +
          `This borehole has a shorter observation history than most, so its ` +
          `14-day forecast passed a backtest but carries wider uncertainty, ` +
          `and no seasonal outlook is shown. It sharpens as the record grows.</p>`);
      }
      // stale-seed note: when the last reading is weeks old, the nowcast
      // estimates the level to today from observed rainfall (the dashed segment).
      // Age comes from the OBSERVED series (the truth on this page), not the
      // forecast's stale_days — the series can be fresher than the seed when
      // the archive tail lands after the morning run (the Via Gellia skew).
      let obsAge = fc.stale_days;
      const obsSer = (detail.observed && detail.observed.series) || [];
      if (obsSer.length) {
        const lastT = new Date(obsSer[obsSer.length - 1][0] + "T00:00:00Z").getTime();
        const age = Math.max(0, Math.round((Date.now() - lastT) / 86400000));
        if (fc.stale_days == null || age < fc.stale_days) obsAge = age;
      }
      if (obsAge != null && obsAge > 14) {
        out.push(`<p class="stale-note">⚠ Last real reading <b>${obsAge} days ago</b> — ` +
          `the dashed segment estimates the level from there to today using recent ` +
          `rainfall, then the ${hLabel}-day forecast continues.</p>`);
      }
      const btns = [["90", "90 d"], ["365", "1y"], ["730", "2y"], ["all", "All"]].map(([d, l]) =>
        `<button class="range-btn${d === range ? " active" : ""}" data-days="${d}">${l}</button>`
      ).join("");
      out.push(`<div class="fan-controls"><span class="range-label">History</span>${btns}</div>`);
      out.push(`<div class="fan-host">${C.fanChart(detail, { historyDays: initDays, levels: triggerLevels(detail), large: pageLarge() })}</div>`);
      out.push(isFlow
        ? `<p class="caption">Observed flow (dark) → ${hLabel}-day P10/P50/P90 forecast fan (blue). ` +
          `Red dashed = the Q95 low-flow proxy — a climatological reference, not a licence trigger. ` +
          `Gauged flow, including any abstraction and discharge effects. Hover for values.</p>`
        : `<p class="caption">Observed history (dark) → ${hLabel}-day P10/P50/P90 forecast fan (blue), continuing as a monthly seasonal outlook — each circle coloured by that month's most-likely tercile (matching the map: amber below / grey near / blue above normal) with P10–P90 whiskers. Red dashed = breach threshold. ${onBoreholePage ? "Hover, or drag the timeline below, for values." : "Hover for values."}</p>`);
      // Standalone page: a draggable timeline scrubber with a live value readout
      // (bindFan wires it to the chart's scrub API). Discoverable + touch-friendly.
      if (onBoreholePage) {
        out.push(`<div class="fan-scrub-wrap">` +
          `<div class="fan-scrub-row"><span class="fan-scrub-label">Timeline</span>` +
          `<input class="fan-scrub" type="range" min="0" max="1000" value="1000" ` +
          `aria-label="Scrub the forecast timeline"></div>` +
          `<div class="fan-readout" role="status" aria-live="polite"></div></div>`);
      }
      let metrics;
      if (isFlow) {
        // No GW-style tier (BREACH_LIKELY etc.) for rivers — the headline IS
        // the probability; rain_dependent surfaces the Stage-4 gate's tier
        // instead (memory-only skill vs leaning on the rain forecast).
        const dep = fc.rain_dependent
          ? `<span class="tier-badge tier-RAIN_DEPENDENT" title="Skilful largely because of the rain forecast — wider bands, extra caution">🌧 rain-dependent</span>`
          : `<span class="tier-badge tier-STABLE" title="Skilful even on climatological rain alone">memory-robust</span>`;
        metrics = row("Gate", dep) +
          row("P(flow &lt; Q95, 14 d)", pct(fc.p_below_q95_14d));
      } else {
        const tier = fc.tier ? `<span class="tier-badge tier-${esc(fc.tier)}">${TIER_LABEL[fc.tier] || fc.tier}</span>` : "–";
        metrics = row("Tier", tier) + row("Breach prob (14 d)", pct(fc.p_breach_14d));
      }
      if (fc.first_cross_median)
        metrics += row(isFlow ? "Median first drop below Q95" : "Median first crossing",
          prettyDate(fc.first_cross_median));
      // Two-method cross-check chip: the mean Pastas-vs-roll disagreement is
      // already published (model_spread_mean); surface it as agree/diverge —
      // structural DISAGREEMENT between independent engines, never an error bar.
      // (Flow has no reduced-form roll model, so fc.model_spread_mean is
      // always absent there and this simply doesn't fire.)
      if (fc.model_spread_mean != null && isFinite(fc.model_spread_mean)) {
        const sp = Math.abs(+fc.model_spread_mean);
        const agree = sp < 0.10 ? ["agree", "two independent methods land within 10 cm"]
          : sp < 0.30 ? ["broadly-agree", `two independent methods differ by ~${Math.round(sp * 100)} cm on average`]
            : ["diverge", `two independent methods differ by ~${fmt1(sp)} m — treat the band with extra caution`];
        metrics += row("Cross-check", `<span class="xchk xchk-${agree[0]}" title="${esc(agree[1])}">` +
          (sp < 0.30 ? "methods agree" : "methods diverge") + ` (±${fmt1(sp)} m)</span>`);
      }
      // Censored-fraction framing: pair the headline probability with how many
      // sampled scenarios NEVER cross the level in the window (honesty framing
      // for small probabilities — "0%" means no member crossed, not impossible).
      let censoredNote = "";
      if (fc.threshold != null && fc.censored_frac != null && isFinite(fc.censored_frac)) {
        censoredNote = isFlow
          ? `<p class="caption">${pct(fc.censored_frac)} of sampled scenarios never fall ` +
            `below ${fv(fc.threshold)} ${uLabel} (the Q95 proxy) inside the forecast window.</p>`
          : `<p class="caption">${pct(fc.censored_frac)} of sampled scenarios ` +
            `never reach ${fmt1(fc.threshold)} mAOD inside the forecast window.</p>`;
      }
      if (onBoreholePage) {
        out.push(`</div>`);                      // close the fan lead card…
        fcDetailCard = pageCard("Forecast detail", metrics + censoredNote);   // …metrics become a grid card
      } else {
        out.push(`<div style="margin-top:10px">${metrics}${censoredNote}</div></div>`);
      }
    }

    // Plain-English summary — below the forecast outlook (the chart leads; this
    // restates it in words). For status-only boreholes there's no forecast
    // section above, so it simply follows the status block.
    out.push(plainSentence(detail));
    if (fcDetailCard) out.push(fcDetailCard);

    // -- current level vs normal: the main visual for status-only boreholes
    // (shown open); a secondary disclosure when the forecast chart leads. --
    if (mrow && st.level != null) {
      const inner = C.ladder(st.level, mrow, st.status || "none") +
        (isFlow
          ? `<p class="caption">Latest flow ${fv(st.level)} ${uLabel} against this ` +
            `gauge's ${monthName(st.month)} flow climatology.</p>`
          : `<p class="caption">Latest level ${fmt1(st.level)} mAOD against this ` +
            `borehole's ${monthName(st.month)} normal range.</p>`);
      out.push(fc
        ? (onBoreholePage ? pageCard("Current level vs normal", inner)
                          : fold("Current level vs normal", inner))
        : `<div class="d-section"><h3>Current level vs normal</h3>${inner}</div>`);
    }

    // -- seasonal outlook (experimental) — collapsed --
    if (se && se.months && se.months.length) {
      // Seasonal threshold-crossing read (Option A): extend the published
      // threshold's crossing past day-14, qualitatively, from the monthly
      // envelopes. Experimental; the published breach is an "above" crossing.
      let thrSeasonal = "";
      if (fc && fc.threshold != null && window.GWC_WATCH && window.GWC_WATCH.evaluateFloorSeasonal) {
        const sres = window.GWC_WATCH.evaluateFloorSeasonal(
          { type: "breach", floor_mAOD: fc.threshold, dir: "above" }, detail);
        if (sres && sres.summary)
          thrSeasonal = `<p class="caption">Threshold (${fmt1(fc.threshold)} mAOD) over the season: ` +
            `<b>${esc(sres.summary)}</b> — indicative, from the experimental outlook (not the 14-day fan).</p>`;
      }
      const inner = C.seasonalBars(se.months) + thrSeasonal +
        `<p class="caption">P(below / near / above normal groundwater). ` +
        `${se.seas5_weighted ? "SEAS5-weighted" : "Equal-weight"} ESP, ` +
        `${se.n_traces || "–"} traces. Experimental.</p>`;
      out.push(onBoreholePage
        ? pageCard("Seasonal outlook (6 months) — experimental", inner)
        : fold("Seasonal outlook (6 months) — experimental", inner));
    }

    // -- "How did the last forecast do?" — the newest ARCHIVED forecast whose
    // window has closed, overlaid with what was then observed. Published as-is,
    // good or bad — the honesty feature the eventual verification page grows
    // from. Null until a window closes with enough observations. --
    const vf = detail.verification;
    if (vf && C.verifyChart) {
      const chart = C.verifyChart(detail, { large: pageLarge() });
      if (chart) {
        const frac = vf.n_obs ? vf.n_in_band / vf.n_obs : 0;
        const verdict = frac >= 0.7
          ? "about as often as an honest 80% band should"
          : frac >= 0.5
            ? "a little below the ≈8-in-10 an honest band aims for"
            : "well below the ≈8-in-10 aim — the band was too narrow over this window";
        const miss = vf.mae_p50 == null ? "–"
          : vf.mae_p50 < 1 ? Math.round(vf.mae_p50 * 100) + " cm" : fmt1(vf.mae_p50) + " m";
        const inner =
          `<p class="verify-line">Forecast issued <b>${esc(prettyDate(vf.run.slice(0, 10)))}</b>: ` +
          `<b>${vf.n_in_band} of ${vf.n_obs}</b> observed days landed inside the published ` +
          `P10–P90 band — ${verdict}. Typical miss on the middle line: <b>${esc(miss)}</b>.</p>` +
          `<div class="verify-host">${chart}</div>` +
          `<p class="caption">Band and dashed middle line are exactly what we published on ` +
          `${esc(prettyDate(vf.run.slice(0, 10)))} — never re-run with today's model. Dark dots ` +
          `are the observed daily levels; red dots fell outside the band. A new window is ` +
          `scored automatically as each day's forecast closes.</p>`;
        out.push(onBoreholePage
          ? pageCard("How did the last forecast do?", inner)
          : fold("How did the last forecast do?", inner));
      }
    }

    // -- set your own trigger levels — the ladder (named mAOD levels). Each level
    // now draws a line on the fan chart above (live as you edit) and is read
    // qualitatively against the fan. Forecast-only; the name-☆ owns watching.
    // GW-only for now — the watchlist/ladder machinery is built around mAOD
    // breach semantics; a flow-native trigger-level editor is future work. --
    if (!isFlow && fc && window.GWC_LADDER && window.GWC_LADDER.ladderHTML)
      out.push(onBoreholePage
        ? pageCard("Set your own trigger levels", window.GWC_LADDER.ladderHTML(stn, detail))
        : fold("Set your own trigger levels", window.GWC_LADDER.ladderHTML(stn, detail)));

    // -- "Groundwater feeding this river" (RiverCast) — the boreholes whose
    // station_links.csv row names this gauge, inverted at pack build. Lazy:
    // each borehole's OWN already-published detail JSON is fetched after
    // render (bindLinkedBoreholes) so this stays a cheap synchronous render
    // here; carries no new model claim, just links + each borehole's
    // existing status chip / seasonal one-liner.
    if (isFlow && Array.isArray(stn.linked_boreholes) && stn.linked_boreholes.length) {
      const rows = stn.linked_boreholes.map((sid) =>
        `<div class="linked-bh" data-linked-id="${esc(sid)}"><span class="caption">Loading…</span></div>`
      ).join("");
      const inner = `<div class="linked-bh-list">${rows}</div>` +
        `<p class="caption">Groundwater feeding this river, from each borehole's own ` +
        `published forecast — no new model here.</p>`;
      out.push(onBoreholePage
        ? pageCard("Groundwater feeding this river", inner)
        : `<div class="d-section"><h3>Groundwater feeding this river</h3>${inner}</div>`);
    }

    // -- consolidated data & downloads (1.3) — every series behind the charts
    // above, in one place rather than a drawer scattered under each chart. Each
    // dataset keeps its sortable table (the screen-reader alternative to the
    // SVGs) + copy/CSV/JSON. dataDisclosure registers _datasets[kind] for the
    // post-render binder. --
    const dataBlocks = [
      dataDisclosure("normals", "Monthly normal ranges", NORM_COLS, detail.normals || []),
      fc ? dataDisclosure("fan", `Forecast fan (${hLabel}-day P10/P50/P90)`,
                          isFlow ? flowFanCols(uLabel) : FAN_COLS, fc.fan || []) : "",
      (detail.observed && detail.observed.series)
        ? dataDisclosure("observed",
            "Observed " + (isFlow ? "flow" : "levels") + " (" + (detail.observed.unit || "mAOD") + ")",
            isFlow ? flowObsCols() : OBS_COLS, detail.observed.series, { lazy: true }) : "",
      (se && se.months)
        ? dataDisclosure("seasonal", "Seasonal outlook (months 1–6)", SEAS_COLS, se.months) : "",
    ].filter(Boolean);
    if (dataBlocks.length) {
      out.push(`<div class="d-section${onBoreholePage ? " d-wide" : ""}"><h3>Data &amp; downloads</h3>`);
      out.push(`<p class="caption">The series behind the charts above, as sortable ` +
        `tables you can copy or download (CSV / JSON). Each export carries the data ` +
        `attribution and the indicative/uncalibrated disclaimer.</p>`);
      out.push(dataBlocks.join(""));
      out.push(`</div>`);
    }

    if (!fc && !se) {
      out.push(`<div class="d-section"><p class="caption">This borehole is ` +
        `outside the forecast scope — current status only. Forecasts cover ` +
        `live-feed boreholes plus any with a user-supplied threshold.</p></div>`);
    }

    // -- "How this number was made" trust card (last, self-contained) --
    out.push(trustCard(detail, meta));

    return out.join("");
  }

  function srcLabel(s) {
    return s === "user" ? "your threshold"
      : s === "gw_p90_proxy" ? "P90 proxy" : (s || "none");
  }
  function monthName(m) {
    return ["", "January", "February", "March", "April", "May", "June", "July",
      "August", "September", "October", "November", "December"][m] || "this month";
  }

  // Plain-English one-liner (deterministic, no LLM) — the lay-audience lead.
  // Built only from values already on the panel; pre-caveated, no advice.
  function plainSentence(detail) {
    const st = detail.status || {};
    const fc = detail.forecast;
    const se = detail.seasonal;
    const isFlow = detail.station && detail.station.station_type === "flow";
    const noun = isFlow ? "this river gauge" : "this borehole";
    const bits = [];
    if (st.status) {
      const pctTxt = (st.percentile != null && isFinite(st.percentile))
        ? ` (around the ${ordinal(st.percentile)} percentile)` : "";
      const trend = { rising: " and rising", falling: " and falling",
        stable: " and holding steady" }[st.trend] || "";
      bits.push(`${noun} is <b>${STATUS_LABEL[st.status]}</b> for ` +
        `${monthName(st.month)}${pctTxt}${trend}`);
    } else {
      bits.push(`there's no recent enough reading to place ${noun} ` +
        "against its normal range");
    }
    if (isFlow && fc && fc.p_below_q95_14d != null && isFinite(fc.p_below_q95_14d)) {
      const p = fc.p_below_q95_14d;
      const phrase = p < 0.01 ? "looks very unlikely"
        : p > 0.5 ? "looks likely"
          : `is around ${pct(p)}`;
      bits.push(`the chance of falling below its Q95 low-flow proxy in the next 14 days ${phrase}`);
    } else if (fc && fc.p_breach_14d != null && isFinite(fc.p_breach_14d)) {
      const p = fc.p_breach_14d;
      const phrase = p < 0.01 ? "looks very unlikely"
        : p > 0.5 ? "looks likely"
          : `is around ${pct(p)}`;
      bits.push(`the chance of crossing its threshold in the next 14 days ${phrase}`);
    }
    if (se && se.months && se.months.length) {
      const m = se.months[0];
      const probs = { below: m.p_below, near: m.p_near, above: m.p_above };
      let lean = null, best = -1;
      for (const k in probs) {
        if (probs[k] != null && isFinite(probs[k]) && probs[k] > best) {
          best = probs[k]; lean = k;
        }
      }
      if (lean) {
        const leanTxt = lean === "near" ? "close to normal"
          : `leaning ${STATUS_LABEL[lean]}`;
        bits.push(`the 6-month outlook is ${leanTxt}`);
      }
    }
    if (!bits.length) return "";
    const joined = bits.length === 1 ? bits[0]
      : bits.slice(0, -1).join("; ") + "; and " + bits[bits.length - 1];
    const sentence = joined.charAt(0).toUpperCase() + joined.slice(1);
    return `<p class="plain-lang"><span class="pl-tag">In plain terms</span> ` +
      `${sentence}. <span class="pl-caveat">Indicative only — not a flood or drought warning.</span></p>`;
  }

  // -- column definitions (1.3). Each col: {key,label,get(row),type,fmt}. The
  //    `get` accessor lets observed rows (arrays) and object rows share one
  //    renderer; display uses fmt, exports use the raw get() value. --
  const FAN_COLS = [
    { key: "lead", label: "Lead (days)", type: "num", get: (r) => r.lead, fmt: (v) => v },
    { key: "date", label: "Date", type: "date", get: (r) => r.date, fmt: prettyDate },
    { key: "segment", label: "Segment", type: "str", get: (r) => r.segment, fmt: (v) => v },
    { key: "p10", label: "P10 (mAOD)", type: "num", get: (r) => r.p10, fmt: fmt3 },
    { key: "p50", label: "P50 (mAOD)", type: "num", get: (r) => r.p50, fmt: fmt3 },
    { key: "p90", label: "P90 (mAOD)", type: "num", get: (r) => r.p90, fmt: fmt3 },
    { key: "roll_p50", label: "Roll P50", type: "num", get: (r) => r.roll_p50, fmt: fmt3 },
    { key: "model_spread", label: "Model spread", type: "num", get: (r) => r.model_spread, fmt: fmt3 },
  ];
  // RiverCast fan/observed columns: unit-aware labels, no roll_p50/model_spread
  // (flow has no reduced-form cross-check model — see FLOW_FAN_KEY_MAP).
  function flowFanCols(uLabel) {
    return [
      { key: "lead", label: "Lead (days)", type: "num", get: (r) => r.lead, fmt: (v) => v },
      { key: "date", label: "Date", type: "date", get: (r) => r.date, fmt: prettyDate },
      { key: "segment", label: "Segment", type: "str", get: (r) => r.segment, fmt: (v) => v },
      { key: "p10", label: `P10 (${uLabel})`, type: "num", get: (r) => r.p10, fmt: fmt3 },
      { key: "p50", label: `P50 (${uLabel})`, type: "num", get: (r) => r.p50, fmt: fmt3 },
      { key: "p90", label: `P90 (${uLabel})`, type: "num", get: (r) => r.p90, fmt: fmt3 },
    ];
  }
  function flowObsCols() {
    return [
      { key: "date", label: "Date", type: "date", get: (r) => r[0], fmt: prettyDate },
      { key: "level", label: "Flow (m³/s)", type: "num", get: (r) => r[1], fmt: fmt3 },
    ];
  }
  const SEAS_COLS = [
    { key: "month_ahead", label: "Month", type: "num", get: (r) => r.month_ahead, fmt: (v) => v },
    { key: "month_start", label: "Start", type: "date", get: (r) => r.month_start, fmt: prettyDate },
    { key: "p_below", label: "P(below)", type: "num", get: (r) => r.p_below, fmt: pct },
    { key: "p_near", label: "P(near)", type: "num", get: (r) => r.p_near, fmt: pct },
    { key: "p_above", label: "P(above)", type: "num", get: (r) => r.p_above, fmt: pct },
    { key: "gw_p10", label: "GW P10", type: "num", get: (r) => r.gw_p10, fmt: fmt3 },
    { key: "gw_p50", label: "GW P50", type: "num", get: (r) => r.gw_p50, fmt: fmt3 },
    { key: "gw_p90", label: "GW P90", type: "num", get: (r) => r.gw_p90, fmt: fmt3 },
  ];
  const NORM_COLS = [
    { key: "month", label: "Month", type: "num", get: (r) => r.month, fmt: monthName },
    { key: "p10", label: "P10", type: "num", get: (r) => r.p10, fmt: fmt3 },
    { key: "t1", label: "Tercile 1", type: "num", get: (r) => r.t1, fmt: fmt3 },
    { key: "median", label: "Median", type: "num", get: (r) => r.median, fmt: fmt3 },
    { key: "t2", label: "Tercile 2", type: "num", get: (r) => r.t2, fmt: fmt3 },
    { key: "p90", label: "P90", type: "num", get: (r) => r.p90, fmt: fmt3 },
    { key: "n_years", label: "Years", type: "num", get: (r) => r.n_years, fmt: (v) => v },
  ];
  const OBS_COLS = [
    { key: "date", label: "Date", type: "date", get: (r) => r[0], fmt: prettyDate },
    { key: "level", label: "Level (mAOD)", type: "num", get: (r) => r[1], fmt: fmt3 },
  ];

  // Stable sort over a copy (preserves _datasets[kind].rows order). Nulls last.
  function sortRows(rows, cols, idx, dir) {
    const c = cols[idx]; const mul = dir === "desc" ? -1 : 1;
    return rows.slice().sort((a, b) => {
      let va = c.get(a), vb = c.get(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (c.type === "num") return (Number(va) - Number(vb)) * mul;
      if (c.type === "date") return (Date.parse(va) - Date.parse(vb)) * mul;
      return String(va).localeCompare(String(vb)) * mul;
    });
  }

  // Build one accessible <table>. First column is a <th scope="row">; headers are
  // <button> sort controls with aria-sort on the <th>. All cells via esc().
  function buildTable(kind, caption, cols, rows, sortIdx, sortDir) {
    const sorted = sortRows(rows, cols, sortIdx, sortDir);
    const ths = cols.map((c, i) => {
      const aria = i === sortIdx ? (sortDir === "desc" ? "descending" : "ascending") : "none";
      const arrow = i === sortIdx ? (sortDir === "desc" ? " ▼" : " ▲") : "";
      return `<th scope="col" aria-sort="${aria}">` +
        `<button type="button" class="dd-sort" data-col="${i}">${esc(c.label)}${esc(arrow)}</button></th>`;
    }).join("");
    const trs = sorted.map((r) => "<tr>" + cols.map((c, ci) => {
      const raw = c.get(r);
      const disp = (raw == null || raw === "") ? "–" : esc(String(c.fmt(raw)));
      return ci === 0 ? `<th scope="row">${disp}</th>` : `<td>${disp}</td>`;
    }).join("") + "</tr>").join("");
    return `<table class="data-table">` +
      `<caption>${esc(caption)} — ${sorted.length} rows. Sortable; column headers act as sort buttons.</caption>` +
      `<thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
  }

  // Emit one disclosure + register its dataset for the post-render binder.
  function dataDisclosure(kind, caption, cols, rows, o) {
    o = o || {};
    if (!rows || !rows.length) return "";
    _datasets[kind] = { caption, cols, rows };
    const lazy = !!o.lazy;
    const body = lazy ? "" : buildTable(kind, caption, cols, rows, 0, "asc");
    return `<details class="data-drawer" data-kind="${esc(kind)}">` +
      `<summary>${esc(caption)}<span class="dd-hint"> — table, copy &amp; download</span></summary>` +
      `<div class="dd-tools" role="group" aria-label="${esc(caption)} export">` +
        `<button type="button" class="dd-btn" data-act="copy">Copy</button>` +
        `<button type="button" class="dd-btn" data-act="csv">CSV</button>` +
        `<button type="button" class="dd-btn" data-act="json">JSON</button>` +
        `<span class="dd-status" role="status" aria-live="polite"></span>` +
      `</div>` +
      `<div class="dd-tablewrap"${lazy ? ` data-lazy="1"` : ``}>${body}</div>` +
      `</details>`;
  }

  // -- export provenance (stamped from meta) --
  function exportHeaderLines(ds) {
    const m = _meta || {};
    const lines = [
      "GroundwaterCast — " + ds.caption,
      "Station: " + (_curStationName || ""),
      "Pack generated: " + (m.generated_at || ""),
      "Forecast run: " + ((m.runs && m.runs.forecast) || "") +
        "  Seasonal run: " + ((m.runs && m.runs.seasonal && m.runs.seasonal.run) || ""),
      "Attribution: " + (m.attribution || ""),
      "Disclaimer: " + (m.disclaimer || ""),
      "Exported: " + new Date().toISOString(),
    ];
    // The fan export carries a raw model_spread column — make sure a downstream
    // reader of the file can't mistake it for a calibrated uncertainty band.
    if (ds.cols && ds.cols.some((c) => c.key === "model_spread")) {
      lines.push("Note: 'Model spread' is a Pastas-vs-roll structural " +
        "cross-check, NOT a calibrated uncertainty band.");
    }
    return lines;
  }

  // RFC-4180 CSV field quoting — distinct from the HTML esc() above. Output goes
  // only into a Blob, never innerHTML.
  function csvField(v) {
    if (v == null) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function toCSV(kind, ds) {
    const { cols, rows } = ds;
    const s = _stateOf(kind);
    const head = cols.map((c) => csvField(c.label)).join(",");
    const body = sortRows(rows, cols, s.idx, s.dir)
      .map((r) => cols.map((c) => csvField(c.get(r))).join(",")).join("\n");
    const comments = exportHeaderLines(ds).map((l) => "# " + String(l).replace(/\n/g, " ")).join("\n");
    return comments + "\n\n" + head + "\n" + body + "\n";
  }
  function toJSON(kind, ds) {
    const { cols, rows } = ds;
    const m = _meta || {};
    const s = _stateOf(kind);
    const data = sortRows(rows, cols, s.idx, s.dir).map((r) => {
      const o = {}; cols.forEach((c) => { o[c.key] = c.get(r); }); return o;
    });
    return JSON.stringify({
      _meta: {
        source: "GroundwaterCast", dataset: ds.caption, station: _curStationName,
        attribution: m.attribution, disclaimer: m.disclaimer,
        generated_at: m.generated_at, runs: m.runs, exported: new Date().toISOString(),
      },
      columns: cols.map((c) => ({ key: c.key, label: c.label })),
      rows: data,
    }, null, 2);
  }
  function download(filename, text, mime) {
    const blob = new Blob([text], { type: mime + ";charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function copyText(text, statusEl) {
    const done = () => {
      if (statusEl) { statusEl.textContent = "Copied"; setTimeout(() => { statusEl.textContent = ""; }, 2000); }
    };
    function fallback() {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { /* ignore */ }
      ta.remove();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else { fallback(); }
  }
  function flashStatus(el, msg) {
    if (el) { el.textContent = msg; setTimeout(() => { el.textContent = ""; }, 2000); }
  }

  // Shared action-button behaviour (Copy link + ☆ watch). Used by the delegated
  // container handler (map panel) AND by a direct binding when the actions are
  // relocated into the page masthead (bindActions). Returns true if handled.
  function actionClick(ev) {
    const linkBtn = ev.target.closest(".d-copy-link");
    if (linkBtn) {
      copyText(location.href, linkBtn.parentElement.querySelector(".d-act-status"));
      return true;
    }
    const star = ev.target.closest(".d-star");
    if (star && window.GWC_WATCH && window.GWC_WATCH.toggle) {
      const def = star.dataset.starFc === "1"
        ? { type: "breach", prob_pct: 25 }
        : { type: "status", crosses: "below" };
      window.GWC_WATCH.toggle(star.dataset.starId, star.dataset.starName, def);
      return true;
    }
    return false;
  }
  function bindActions(el) { if (el) el.addEventListener("click", actionClick); }

  // Post-render: wire lazy build, column sort, and export buttons via event
  // delegation on the outer container (covers all drawers; survives fan-host
  // re-renders, which only touch .fan-host). Reads _datasets/_meta set by render.
  function bindData(container) {
    if (!container || container.dataset.ddBound === "1") return;
    // Bind ONCE: #detail-body persists across selections (only its innerHTML is
    // replaced), so re-binding each render would stack listeners and fire N×.
    // The delegated handlers read the module-scoped _datasets/_sortState/
    // _curStationName that render() refreshes, so a single binding stays correct.
    container.dataset.ddBound = "1";
    // (a) lazy table build on first open — toggle does not bubble, use capture
    container.addEventListener("toggle", (ev) => {
      const d = ev.target;
      if (!d.classList || !d.classList.contains("data-drawer") || !d.open) return;
      const wrap = d.querySelector(".dd-tablewrap");
      if (wrap && wrap.dataset.lazy === "1" && !wrap.firstChild) {
        const kind = d.dataset.kind, ds = _datasets[kind];
        if (!ds) return;
        const s = _stateOf(kind);
        wrap.innerHTML = buildTable(kind, ds.caption, ds.cols, ds.rows, s.idx, s.dir);
      }
    }, true);
    // (b)/(c) sort + export
    container.addEventListener("click", (ev) => {
      // Copy a shareable deep-link to this borehole (location already carries
      // the #bh hash set by app.js on selection).
      // Copy-link + ☆ watch (shared with the page's relocated masthead actions).
      if (actionClick(ev)) return;
      const sortBtn = ev.target.closest(".dd-sort");
      if (sortBtn) {
        const d = sortBtn.closest(".data-drawer"); const kind = d.dataset.kind;
        const ds = _datasets[kind]; if (!ds) return;
        const idx = +sortBtn.dataset.col; const cur = _stateOf(kind);
        const dir = (cur.idx === idx && cur.dir === "asc") ? "desc" : "asc";
        _sortState[kind] = { idx, dir };
        d.querySelector(".dd-tablewrap").innerHTML =
          buildTable(kind, ds.caption, ds.cols, ds.rows, idx, dir);
        return;
      }
      const exBtn = ev.target.closest(".dd-btn");
      if (!exBtn) return;
      const d = exBtn.closest(".data-drawer"); const kind = d.dataset.kind;
      const ds = _datasets[kind]; if (!ds) return;
      const status = d.querySelector(".dd-status"); const act = exBtn.dataset.act;
      if (act === "copy") {
        copyText(toCSV(kind, ds), status);
      } else if (act === "csv") {
        download(`gwc_${kind}_${slug(_curStationName)}.csv`, toCSV(kind, ds), "text/csv");
        flashStatus(status, "CSV downloaded");
      } else if (act === "json") {
        download(`gwc_${kind}_${slug(_curStationName)}.json`, toJSON(kind, ds), "application/json");
        flashStatus(status, "JSON downloaded");
      }
    });
  }

  // Post-render: wire the history-range buttons + attach the fan hover.
  // Re-renders just the fan into .fan-host with a new observed-history window
  // (the forecast + seasonal always stay in view).
  // On a standalone stub page? /b/ = borehole pages, /r/ = RiverCast gauge
  // pages — both use the same full-width .bore-detail layout, so both get
  // page-mode charts and the wired scrubber.
  function onStubPage() {
    return location.pathname.indexOf("/b/") === 0
      || location.pathname.indexOf("/r/") === 0;
  }

  // The page's big chart variant is only legible when it actually gets space:
  // squeezed to a phone width its 760-unit viewBox renders ~4px fonts. Below
  // 640px use the compact variant (designed for ~340px panels).
  function pageLarge() {
    return onStubPage()
      && !(window.matchMedia && window.matchMedia("(max-width: 640px)").matches);
  }

  function bindFan(container, detail) {
    const C = window.GWC_CHARTS;
    const host = container.querySelector(".fan-host");
    if (!host) return;
    const onPage = onStubPage();
    const slider = container.querySelector(".fan-scrub");
    const readout = container.querySelector(".fan-readout");
    const initBtn = container.querySelector(".range-btn.active");
    let activeDays = initBtn
      ? (initBtn.dataset.days === "all" ? 1e9 : parseInt(initBtn.dataset.days, 10))
      : 90;
    let api = null;

    // The scrubber spans the forecast portion only: today (origin / first fan
    // day) → the end of the chart (seasonal). History-window changes don't move
    // it. api.ctx.x1 is the chart's max time.
    function span() {
      const fc = detail.forecast || {};
      const fanAll = fc.fan || [];
      const fcSeg = fanAll.filter((f) => f.segment !== "nowcast");
      const first = fc.origin_date || (fcSeg[0] && fcSeg[0].date) || (fanAll[0] && fanAll[0].date);
      const startT = first ? new Date(first + "T00:00:00Z").getTime() : 0;
      const lastDaily = fcSeg.length
        ? new Date(fcSeg[fcSeg.length - 1].date + "T00:00:00Z").getTime() : startT;
      const endT = api ? api.ctx.x1 : lastDaily;
      return { startT, endT, lastDaily };
    }
    function renderReadout(rows) {
      if (!readout) return;
      if (!rows || !rows.length) { readout.innerHTML = ""; return; }
      const tiles = rows.slice(1).map((r) =>
        `<div class="fr-tile"><span class="fr-k">${esc(r[0])}</span>` +
        `<span class="fr-v">${esc(r[1])}</span></div>`).join("");
      readout.innerHTML = `<span class="fr-date">${esc(rows[0][0])}</span>${tiles}`;
    }
    function wireScrub() {
      if (!(onPage && slider && readout && api)) return;
      const { startT, endT, lastDaily } = span();
      const tAt = (val) => startT + (val / 1000) * (endT - startT);
      slider.oninput = () => renderReadout(api.scrubToTime(tAt(+slider.value)));
      const defFrac = endT > startT
        ? Math.round(((lastDaily - startT) / (endT - startT)) * 1000) : 1000;
      slider.value = defFrac;
      renderReadout(api.scrubToTime(tAt(defFrac)));   // default: the end of the daily forecast
    }
    function draw(days) {
      activeDays = days;
      host.innerHTML = C.fanChart(detail, { historyDays: days, levels: triggerLevels(detail), large: pageLarge() });
      const svg = host.querySelector(".svg-fan");
      api = svg ? C.attachFanHover(svg) : null;
      wireScrub();
    }
    container.querySelectorAll(".range-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        container.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const d = btn.dataset.days;
        draw(d === "all" ? 1e9 : parseInt(d, 10));
        if (window.GWC_onRangeChange) window.GWC_onRangeChange(d);   // sync the deep-link
      });
    });
    // the initial fan is already in the HTML — attach hover + wire the scrubber
    const svg = host.querySelector(".svg-fan");
    api = svg ? C.attachFanHover(svg) : null;
    wireScrub();
    // Expose a levels-aware redraw so the trigger-levels editor (ladders.js) can
    // move the lines on the chart live as rungs change. Keeps the active range.
    _fanRedraw = () => draw(activeDays);
  }

  // -- "Groundwater feeding this river" (RiverCast) — lazy-fetch each linked
  // borehole's OWN already-published detail JSON and show its status chip
  // plus, if it has a seasonal outlook, one qualitative line. No new model:
  // pure reuse of numbers that are already computed and already published
  // for that borehole, carrying that borehole's own existing caveats.
  function seasonalQualLine(seasonal) {
    if (!seasonal || !seasonal.months || !seasonal.months.length) return "";
    const qual = { below: "likely below normal", near: "likely near normal",
      above: "likely above normal" };
    const m = seasonal.months[seasonal.months.length - 1];   // furthest month = "into <season>"
    const probs = { below: m.p_below, near: m.p_near, above: m.p_above };
    let lean = null, best = -1;
    for (const k in probs) {
      if (probs[k] != null && isFinite(probs[k]) && probs[k] > best) { best = probs[k]; lean = k; }
    }
    if (!lean) return "";
    const when = m.month_start
      ? monthName(new Date(m.month_start + "T00:00:00Z").getUTCMonth() + 1) : "the coming months";
    return `Groundwater feeding this river is ${qual[lean]} into ${esc(when)}.`;
  }
  function bindLinkedBoreholes(container) {
    if (!container) return;
    const nodes = container.querySelectorAll(".linked-bh[data-linked-id]");
    if (!nodes.length) return;
    const packBase = (window.GWC_CONFIG && window.GWC_CONFIG.packBase) || "/pack";
    nodes.forEach((el) => {
      const id = el.dataset.linkedId;
      fetch(`${packBase}/stations/${id}.json`)
        .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
        .then((d) => {
          const stn = d.station || {};
          const href = "/b/" + (stn.slug || slug(stn.name || id)) + "/";
          const qual = seasonalQualLine(d.seasonal);
          el.innerHTML = `<a class="linked-bh-link" href="${esc(href)}">${esc(stn.name || id)}</a> ` +
            statusChip(d.status) + (qual ? `<p class="caption">${qual}</p>` : "");
        })
        .catch(() => {
          el.innerHTML = `<span class="caption">Borehole detail unavailable.</span>`;
        });
    });
  }

  window.GWC_DETAIL = {
    render, bindFan, bindData, bindActions, bindLinkedBoreholes,
    refreshFanLevels: () => { if (_fanRedraw) _fanRedraw(); },
  };
})();

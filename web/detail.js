// Detail panel — render stations/<id>.json as the three-horizon narrative:
// current status vs normal → 15-day fan → seasonal terciles.
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
    if (fc && thSrc === "gw_p90_proxy") parts.push("P90-proxy threshold");
    else if (fc && thSrc === "user") parts.push("against your threshold");
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
    const staleExplain = "A normal cadence is a roughly monthly dipped reading; " +
      "a longer gap is a telemetry outage. ‘Stale’ here means the most " +
      "recent reading is older than the usual dip interval, so the current " +
      "status is an older snapshot.";

    // -- Tier 2b: methodology lineage (only when fc) --
    let lineageRows = "";
    if (fc) {
      lineageRows += row("Forecast run", prettyDateTime(fcRun));
      const thBasis = thSrc === "gw_p90_proxy"
        ? "P90 proxy — not an operational threshold"
        : thSrc === "user" ? "Your threshold" : esc(thSrc || "none");
      lineageRows += row("Threshold basis", thBasis);
    }
    const forcingNote = "Forcing: ECMWF ensemble (see About for attribution).";

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
    const range = ["365", "730", "all"].includes(opts.range) ? opts.range : "365";
    const initDays = range === "all" ? 1e9 : parseInt(range, 10);
    const stn = detail.station || {};
    const st = detail.status || {};
    const fr = detail.freshness || {};
    const out = [];

    out.push(`<h2 class="d-name">${esc(stn.name || stn.station_id)}</h2>`);
    out.push(`<p class="d-sub">${esc(stn.aquifer || "—")} · ${fmt1(stn.lat)}°N ${fmt1(stn.lon)}°E</p>`);

    // -- current status vs normal --
    out.push(`<div>${statusChip(st)}</div>`);

    // -- watchlist pin control (2.1). HTML here, interactive wiring post-render
    // via GWC_WATCH.bindDetail (mirrors the bindFan/bindData split). Guarded so a
    // missing module degrades gracefully rather than throwing in render(). --
    if (window.GWC_WATCH && window.GWC_WATCH.pinControlHTML) {
      out.push(window.GWC_WATCH.pinControlHTML(stn, detail));
    }
    const obs = st.obs_date ? `observed ${prettyDate(st.obs_date)}` : "no recent observation";
    const age = st.obs_age_days != null ? ` · ${st.obs_age_days} d old` : "";
    // SGI: ladder-based Standardised Groundwater Index (negative = below normal).
    const sgiTxt = (st.sgi != null && isFinite(st.sgi))
      ? ` · SGI ${((+st.sgi).toFixed(1) === "-0.0" ? "0.0" : (+st.sgi).toFixed(1))}` : "";
    out.push(`<p class="caption">${obs}${age}${sgiTxt} · data: ${esc(FRESH_LABEL[fr.label] || fr.label || "—")}</p>`);

    // Stability flag (if the trend screen flagged this borehole for review).
    out.push(trendFlagBlock(detail.trend_flag));

    // Stuck-sensor caution: a frozen telemetry value (flat readings > 24h) is
    // flagged by apply_qc and carried through as data_source "logged_live_stuck".
    // Surface it directly beneath the "data: fresh/stable" caption so the latest
    // reading is no longer presented as confidently fresh. Additive amber note.
    if (fr.data_source && String(fr.data_source).indexOf("stuck") !== -1) {
      out.push(`<p class="stale-note">⚠ Sensor may be stuck (flat readings) — the latest value has not changed and may not reflect the true level. Treat the current reading with caution.</p>`);
    }

    // ladder (needs the month's normals row)
    const mrow = (detail.normals || []).find((r) => r.month === st.month);
    if (mrow && st.level != null) {
      out.push(`<div class="d-section"><h3>Current level vs normal</h3>`);
      out.push(C.ladder(st.level, mrow, st.status || "none"));
      out.push(`<p class="caption">Latest level ${fmt1(st.level)} mAOD against this borehole's ${monthName(st.month)} normal range.</p>`);
      out.push(`</div>`);
    }

    // -- forecast (short-range daily fan, continued by the seasonal outlook) --
    const fc = detail.forecast;
    const hd = fc && fc.horizon_days;
    const hLabel = hd != null ? esc(hd) : "";   // real forecast horizon (days)
    if (fc) {
      const fcTitle = hLabel ? `${hLabel}-day forecast` : "Forecast";
      out.push(`<div class="d-section"><h3>${fcTitle}</h3>`);
      if (fc.headline) out.push(`<p class="headline">${esc(fc.headline)}</p>`);
      // stale-seed note: when the last reading is weeks old, the nowcast
      // estimates the level to today from observed rainfall (the dashed segment).
      if (fc.stale_days != null && fc.stale_days > 14) {
        out.push(`<p class="stale-note">⚠ Last real reading <b>${fc.stale_days} days ago</b> — ` +
          `the dashed segment estimates the level to today from recent rainfall, ` +
          `then the ${hLabel}-day forecast continues.</p>`);
      }
      const btns = [["365", "1y"], ["730", "2y"], ["all", "All"]].map(([d, l]) =>
        `<button class="range-btn${d === range ? " active" : ""}" data-days="${d}">${l}</button>`
      ).join("");
      out.push(`<div class="fan-controls"><span class="range-label">History</span>${btns}</div>`);
      out.push(`<div class="fan-host">${C.fanChart(detail, { historyDays: initDays })}</div>`);
      out.push(`<p class="caption">Observed history (dark) → ${hLabel}-day P10/P50/P90 forecast fan (blue), continuing as a monthly seasonal outlook (circles, with P10–P90 whiskers). Red dashed = breach threshold. Hover for values.</p>`);
      const tier = fc.tier ? `<span class="tier-badge tier-${esc(fc.tier)}">${TIER_LABEL[fc.tier] || fc.tier}</span>` : "–";
      out.push(`<div style="margin-top:10px">`);
      out.push(row("Tier", tier));
      out.push(row("Breach prob (14 d)", pct(fc.p_breach_14d)));
      if (fc.p_breach != null && fc.horizon_days)
        out.push(row(`Breach prob (${fc.horizon_days} d)`, pct(fc.p_breach)));
      if (fc.first_cross_median)
        out.push(row("Median first crossing", prettyDate(fc.first_cross_median)));
      out.push(row("Threshold", `${fmt1(fc.threshold)} mAOD <span class="caption">(${esc(srcLabel(fc.threshold_source))})</span>`));
      out.push(row("Seed age", fc.stale_days != null ? `${fc.stale_days} d` : "–"));
      out.push(`</div></div>`);

      // -- threshold ladder (2.2) — forecast-only, sits under the fan it reads.
      // HTML here; interactive wiring post-render via GWC_LADDER.bindDetail
      // (mirrors the watchlist pin control). Guarded so a missing module
      // degrades gracefully rather than throwing in render(). --
      if (window.GWC_LADDER && window.GWC_LADDER.ladderHTML) {
        out.push(window.GWC_LADDER.ladderHTML(stn, detail));
      }
    }

    // -- seasonal outlook --
    const se = detail.seasonal;
    if (se && se.months && se.months.length) {
      out.push(`<div class="d-section"><h3>Seasonal outlook (months 1–6)</h3>`);
      out.push(C.seasonalBars(se.months));
      out.push(`<p class="caption">P(below / near / above normal groundwater). ` +
        `${se.seas5_weighted ? "SEAS5-weighted" : "Equal-weight"} ESP, ${se.n_traces || "–"} traces. Experimental.</p>`);
      out.push(`</div>`);
    }

    // -- consolidated data & downloads (1.3) — every series behind the charts
    // above, in one place rather than a drawer scattered under each chart. Each
    // dataset keeps its sortable table (the screen-reader alternative to the
    // SVGs) + copy/CSV/JSON. dataDisclosure registers _datasets[kind] for the
    // post-render binder. --
    const dataBlocks = [
      dataDisclosure("normals", "Monthly normal ranges", NORM_COLS, detail.normals || []),
      fc ? dataDisclosure("fan", `Forecast fan (${hLabel}-day P10/P50/P90)`,
                          FAN_COLS, fc.fan || []) : "",
      (detail.observed && detail.observed.series)
        ? dataDisclosure("observed",
            "Observed levels (" + (detail.observed.unit || "mAOD") + ")",
            OBS_COLS, detail.observed.series, { lazy: true }) : "",
      (se && se.months)
        ? dataDisclosure("seasonal", "Seasonal outlook (months 1–6)", SEAS_COLS, se.months) : "",
    ].filter(Boolean);
    if (dataBlocks.length) {
      out.push(`<div class="d-section"><h3>Data &amp; downloads</h3>`);
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
  function bindFan(container, detail) {
    const C = window.GWC_CHARTS;
    const host = container.querySelector(".fan-host");
    if (!host) return;
    function draw(days) {
      host.innerHTML = C.fanChart(detail, { historyDays: days });
      const svg = host.querySelector(".svg-fan");
      if (svg) C.attachFanHover(svg);
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
    // the 1y view is already in the initial HTML — just attach hover to it
    const svg = host.querySelector(".svg-fan");
    if (svg) C.attachFanHover(svg);
  }

  window.GWC_DETAIL = { render, bindFan, bindData };
})();

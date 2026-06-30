// Watchlist — roadmap 2.1, Stage A (client-only). See docs/product/watchlist_design.md.
//
// One cohesive module exposing window.GWC_WATCH, consumed by both the detail-panel
// pin control (detail.js render() + app.js bindDetail hook) and the controls-column
// panel (mounted from app.js once featById exists). No backend, no pack/contract
// change: every rule reads the PUBLISHED flat geojson props, and the optional
// custom-floor rule reads only the per-borehole detail fan QUALITATIVELY.
//
// Guardrails (non-negotiable, design note §Guardrails):
//   - "Watch", never "Warning", in all copy.
//   - Proxy-threshold honesty: a gw_p90_proxy breach carries "proxy — not your
//     operational level" on EVERY surface (centralised in evaluate()).
//   - Datum-sanity: a user mAOD floor outside the observed range is flagged.
//   - The custom floor is NEVER a fabricated probability and NEVER a product of
//     per-day probabilities (AR1-correlated) — only likely/possible/unlikely,
//     labelled "indicative, from the fan".
//   - Every watch surface repeats the indicative/uncalibrated framing.
(function () {
  "use strict";

  const STORE_KEY = "gwc_watchlist_v1";
  const INDICATIVE = "Indicative / uncalibrated — a Watch, not a Warning.";
  const PROXY_NOTE = "proxy — not your operational level";

  // -- own escape helper (detail.js's esc() is module-private; do not reach for it) --
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  const fmt1 = (v) => (v == null || isNaN(v) ? "–" : (+v).toFixed(1));

  // ---- store (localStorage, defensive; degrades to in-memory) ----------------
  let _mem = null;            // in-memory fallback when localStorage is unavailable
  let _lsOk = true;

  function load() {
    if (_mem) return _mem.slice();
    try {
      const raw = window.localStorage.getItem(STORE_KEY);
      if (!raw) return [];
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr.filter(isValidEntry) : [];
    } catch (e) {
      _lsOk = false;
      _mem = _mem || [];
      return _mem.slice();
    }
  }
  function save(arr) {
    const clean = (Array.isArray(arr) ? arr : []).filter(isValidEntry);
    _mem = clean.slice();      // always keep a mirror so reads survive a quota throw
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(clean));
      _lsOk = true;
      _mem = null;             // localStorage is the source of truth when it works
    } catch (e) {
      _lsOk = false;           // keep the in-memory copy
    }
  }
  function isValidEntry(e) {
    return e && typeof e === "object" && typeof e.id === "string"
      && e.rule && typeof e.rule === "object" && isValidRule(e.rule);
  }
  function isValidRule(r) {
    if (!r || typeof r !== "object") return false;
    if (r.type === "breach") return typeof r.prob_pct === "number";
    if (r.type === "status") return r.crosses === "below" || r.crosses === "above";
    if (r.type === "sgi") {
      return ["le", "ge", "lt", "gt"].includes(r.op) && typeof r.value === "number";
    }
    return false;
  }

  function has(id) { return load().some((e) => e.id === id); }
  function get(id) { return load().find((e) => e.id === id) || null; }
  function add(id, name, rule) {
    if (!isValidRule(rule)) return;
    const arr = load().filter((e) => e.id !== id);
    arr.push({ id: id, name: String(name || id), rule: rule });
    save(arr);
    refreshAll();
  }
  function remove(id) {
    save(load().filter((e) => e.id !== id));
    refreshAll();
  }
  function toggle(id, name, rule) {
    if (has(id)) remove(id); else add(id, name, rule);
  }

  // ---- pure evaluator (published flat geojson props only) --------------------
  // Returns {tripped, label, standing, isProxy}. Shared by the pin control AND
  // the panel so the proxy-honesty wording can never diverge between them.
  function evaluate(rule, p) {
    p = p || {};
    if (!rule || typeof rule !== "object") {
      return { tripped: false, label: "—", standing: "no rule", isProxy: false };
    }
    if (rule.type === "breach") {
      const isProxy = p.threshold_source === "gw_p90_proxy";
      const proxyTag = isProxy ? ` (${PROXY_NOTE})` : "";
      const v = p.p_breach_14d;
      if (v == null || isNaN(v)) {
        return {
          tripped: false, isProxy: isProxy,
          standing: "no published breach probability",
          label: `Watch when 14-day breach probability ≥ ${rule.prob_pct}%${proxyTag} — none published`,
        };
      }
      const pctNow = Math.round(v * 100);
      const tripped = pctNow >= rule.prob_pct;
      return {
        tripped: tripped, isProxy: isProxy,
        standing: `${pctNow}% chance of breaching within 14 days${proxyTag}`,
        label: tripped
          ? `${pctNow}% chance of breaching within 14 days${proxyTag} — at or over your ${rule.prob_pct}% Watch level`
          : `${pctNow}% breach probability (14 d)${proxyTag} — under your ${rule.prob_pct}% Watch level`,
      };
    }
    if (rule.type === "status") {
      const cur = p.status;
      const want = rule.crosses;
      const tripped = cur === want;
      const curTxt = cur ? `${cur} normal` : "no current status";
      return {
        tripped: tripped, isProxy: false,
        standing: `currently ${curTxt}`,
        label: tripped
          ? `currently ${cur} normal — crossed your Watch level`
          : `currently ${curTxt} — Watch is for "${want} normal"`,
      };
    }
    if (rule.type === "sgi") {
      const sgi = p.sgi;
      if (sgi == null || isNaN(sgi)) {
        return {
          tripped: false, isProxy: false, standing: "no SGI",
          label: `Watch when SGI ${opSym(rule.op)} ${rule.value} — no SGI for this borehole`,
        };
      }
      const tripped = cmp(rule.op, sgi, rule.value);
      const sgiTxt = (+sgi).toFixed(1) === "-0.0" ? "0.0" : (+sgi).toFixed(1);
      return {
        tripped: tripped, isProxy: false,
        standing: `SGI ${sgiTxt}`,
        label: tripped
          ? `SGI ${sgiTxt} — past your Watch level (${opSym(rule.op)} ${rule.value})`
          : `SGI ${sgiTxt} — not yet past your Watch level (${opSym(rule.op)} ${rule.value})`,
      };
    }
    return { tripped: false, label: "—", standing: "unknown rule", isProxy: false };
  }
  function cmp(op, a, b) {
    return op === "le" ? a <= b : op === "ge" ? a >= b
      : op === "lt" ? a < b : op === "gt" ? a > b : false;
  }
  function opSym(op) {
    return { le: "≤", ge: "≥", lt: "<", gt: ">" }[op] || op;
  }

  // ---- qualitative custom-floor read (needs detail.forecast.fan) -------------
  // Reads the first 14 FORWARD leads (segment === 'forecast', lead 1..14 — NOT the
  // negative-lead nowcast rows). Compares the user floor against the P10/P50/P90
  // envelope and returns only likely/possible/unlikely. NEVER a number, NEVER a
  // product of per-day probabilities (the days are AR1-correlated).
  function floorLeads(detail) {
    const fan = detail && detail.forecast && detail.forecast.fan;
    if (!Array.isArray(fan)) return [];
    return fan.filter((r) => r && r.segment === "forecast"
      && r.lead != null && r.lead >= 1 && r.lead <= 14);
  }
  function evaluateFloor(rule, detail) {
    if (!rule || rule.type !== "breach" || rule.floor_mAOD == null) return null;
    const leads = floorLeads(detail);
    if (!leads.length) return null;
    const dir = rule.dir === "above" ? "above" : "below";
    const floor = +rule.floor_mAOD;
    const p10s = leads.map((r) => r.p10).filter((v) => v != null && !isNaN(v));
    const p90s = leads.map((r) => r.p90).filter((v) => v != null && !isNaN(v));
    if (!p10s.length || !p90s.length) return null;
    const lo = Math.min.apply(null, p10s);   // worst-case low edge of the fan
    const hi = Math.max.apply(null, p90s);   // worst-case high edge of the fan

    let word;
    if (dir === "below") {
      // "reaching/dropping below the floor" over the next 14 days
      if (floor >= hi) word = "likely";        // floor above the whole fan → fan goes below it
      else if (floor <= lo) word = "unlikely"; // floor below the whole fan
      else word = "possible";                  // floor inside the envelope
    } else {
      // "rising above the floor"
      if (floor <= lo) word = "likely";
      else if (floor >= hi) word = "unlikely";
      else word = "possible";
    }
    const verb = dir === "below" ? "drop to / below" : "rise to / above";
    return {
      word: word,
      note: `Your ${fmt1(floor)} mAOD floor is ${word} to be reached (${verb} it) ` +
        `over the next 14 days — indicative, from the fan envelope (P10–P90), ` +
        `not a calibrated probability.`,
    };
  }

  // ---- seasonal extension of the floor read (months 1–6) ---------------------
  // Per-month likely/possible/unlikely against each seasonal month's published
  // P10–P90 envelope, plus a plain "when it escalates" summary. QUALITATIVE ONLY,
  // and per-month — never a cumulative product (the months are correlated and we
  // only hold quantiles, not the ESP traces). The seasonal outlook is
  // experimental, so callers must badge it as such.
  function evaluateFloorSeasonal(rule, detail) {
    if (!rule || rule.type !== "breach" || rule.floor_mAOD == null) return null;
    const months = detail && detail.seasonal && detail.seasonal.months;
    if (!Array.isArray(months) || !months.length) return null;
    const dir = rule.dir === "above" ? "above" : "below";
    const floor = +rule.floor_mAOD;
    const rows = months.map((m) => {
      const lo = m.gw_p10, hi = m.gw_p90;
      let word = null;
      if (lo != null && hi != null && !isNaN(lo) && !isNaN(hi)) {
        word = dir === "below"
          ? (floor >= hi ? "likely" : floor <= lo ? "unlikely" : "possible")
          : (floor <= lo ? "likely" : floor >= hi ? "unlikely" : "possible");
      }
      return { month_ahead: m.month_ahead, month_start: m.month_start, word: word };
    });
    const valid = rows.filter((r) => r.word);
    if (!valid.length) return { rows: rows, summary: "" };
    const firstLikely = valid.find((r) => r.word === "likely");
    const firstPossible = valid.find((r) => r.word === "possible" || r.word === "likely");
    let summary;
    if (firstLikely) {
      summary = (firstPossible && firstPossible.month_ahead < firstLikely.month_ahead)
        ? `possible from ~month ${firstPossible.month_ahead}, likely from ~month ${firstLikely.month_ahead}`
        : `likely from ~month ${firstLikely.month_ahead}`;
    } else if (firstPossible) {
      summary = `possible from ~month ${firstPossible.month_ahead} (not likely within 6 months)`;
    } else {
      summary = "stays unlikely through the 6-month outlook";
    }
    return { rows: rows, summary: summary };
  }

  // ---- datum-sanity on a user mAOD floor (needs observed.series + normals) ----
  function datumSanity(floor, detail) {
    const f = +floor;
    if (floor == null || floor === "" || isNaN(f)) {
      return { ok: false, msg: "Enter a level in mAOD." };
    }
    const series = detail && detail.observed && detail.observed.series;
    let obsMin = null, obsMax = null;
    if (Array.isArray(series) && series.length) {
      for (const row of series) {
        const lv = row && row[1];
        if (lv == null || isNaN(lv)) continue;
        if (obsMin == null || lv < obsMin) obsMin = lv;
        if (obsMax == null || lv > obsMax) obsMax = lv;
      }
    }
    // widen with the normals P10/P90 spread so a plausible-but-unobserved floor
    // near the climatological edges isn't falsely flagged.
    const norms = (detail && detail.normals) || [];
    for (const n of norms) {
      if (n && n.p10 != null && !isNaN(n.p10) && (obsMin == null || n.p10 < obsMin)) obsMin = n.p10;
      if (n && n.p90 != null && !isNaN(n.p90) && (obsMax == null || n.p90 > obsMax)) obsMax = n.p90;
    }
    if (obsMin == null || obsMax == null) {
      return { ok: true, msg: "" };   // no record to check against — don't block
    }
    const pad = Math.max(2, (obsMax - obsMin) * 0.5);
    if (f < obsMin - pad || f > obsMax + pad) {
      return {
        ok: false,
        msg: `⚠ This looks off-datum (outside the observed range ${fmt1(obsMin)}–${fmt1(obsMax)} mAOD) ` +
          `— check your level is in mAOD on the same datum.`,
      };
    }
    return { ok: true, msg: "" };
  }

  // ---- canonical copyable alert sentence (Stage-A email substitute) ----------
  function alertText(name, ev, floorRead) {
    let s = `GroundwaterCast Watch — ${name}: ${ev.standing}.`;
    if (floorRead) s += ` Custom floor: ${floorRead.word} to be reached (indicative, from the fan).`;
    s += ` ${INDICATIVE}`;
    return s;
  }

  // ---- rule summary (one short line describing a saved rule) -----------------
  function ruleSummary(rule) {
    if (!rule) return "";
    if (rule.type === "breach") {
      let s = `breach probability ≥ ${rule.prob_pct}% (14 d)`;
      if (rule.floor_mAOD != null) {
        s += ` · custom floor ${fmt1(rule.floor_mAOD)} mAOD (${rule.dir === "above" ? "above" : "below"})`;
      }
      return s;
    }
    if (rule.type === "status") return `status crosses ${rule.crosses} normal`;
    if (rule.type === "sgi") return `SGI ${opSym(rule.op)} ${rule.value}`;
    return "";
  }

  // ============================================================================
  // PIN CONTROL (detail panel)
  // ============================================================================
  // pinControlHTML returns an already-esc'd string pushed into render()'s out[].
  // The interactive wiring happens post-render in bindDetail (mirrors bindFan/
  // bindData so a re-render can't leave a dead control).
  function pinControlHTML(stn, detail) {
    const id = stn && stn.station_id;
    if (!id) return "";
    const watched = has(id);
    const name = stn.name || id;
    const hasForecast = !!(detail && detail.forecast);
    return `<div class="wl-pin" data-wl-id="${esc(id)}" data-wl-name="${esc(name)}" ` +
      `data-wl-fc="${hasForecast ? "1" : "0"}">` +
      `<button type="button" class="wl-toggle${watched ? " on" : ""}" data-wl-act="toggle" ` +
        `aria-pressed="${watched ? "true" : "false"}">` +
        `${watched ? "★ Watching" : "☆ Watch this borehole"}</button>` +
      `<button type="button" class="wl-edit-btn" data-wl-act="edit">${watched ? "Edit Watch" : "Set a Watch"}</button>` +
      `<div class="wl-editor" hidden></div>` +
      `</div>`;
  }

  // Build the rule-editor HTML for a borehole, reflecting any saved rule.
  function editorHTML(id, detail) {
    const entry = get(id);
    const r = (entry && entry.rule) || { type: "status", crosses: "below" };
    const sel = (a, b) => (a === b ? " selected" : "");
    const breachPct = r.type === "breach" ? r.prob_pct : 25;
    const floorVal = r.type === "breach" && r.floor_mAOD != null ? r.floor_mAOD : "";
    const floorDir = r.type === "breach" && r.dir === "above" ? "above" : "below";
    const crosses = r.type === "status" ? r.crosses : "below";
    const sgiOp = r.type === "sgi" ? r.op : "le";
    const sgiVal = r.type === "sgi" ? r.value : -1;
    const hasForecast = !!(detail && detail.forecast);

    return `<div class="wl-ed-row">` +
        `<label class="wl-ed-lab">Trigger` +
        `<select class="wl-type">` +
          `<option value="breach"${sel("breach", r.type)}${hasForecast ? "" : " disabled"}>Breach probability</option>` +
          `<option value="status"${sel("status", r.type)}>Status crosses normal</option>` +
          `<option value="sgi"${sel("sgi", r.type)}>SGI threshold</option>` +
        `</select></label>` +
      `</div>` +
      // breach params
      `<div class="wl-params wl-p-breach"${r.type === "breach" ? "" : " hidden"}>` +
        `<label class="wl-ed-lab">Watch when 14-day breach probability ≥ ` +
          `<input type="number" class="wl-breach-pct" min="1" max="100" step="1" value="${esc(breachPct)}"> %</label>` +
        (hasForecast
          ? `<details class="wl-floor"><summary>Optional custom floor (indicative)</summary>` +
            `<label class="wl-ed-lab">Your level ` +
              `<input type="number" class="wl-floor-val" step="0.01" value="${esc(floorVal)}" placeholder="mAOD"> mAOD</label>` +
            `<label class="wl-ed-lab">Concern is the level dropping ` +
              `<select class="wl-floor-dir">` +
                `<option value="below"${sel("below", floorDir)}>below</option>` +
                `<option value="above"${sel("above", floorDir)}>above</option>` +
              `</select> this floor</label>` +
            `<p class="wl-sanity caption" role="status" aria-live="polite"></p>` +
            `<p class="wl-floor-read caption" role="status" aria-live="polite"></p>` +
            `<p class="caption">The floor is read qualitatively against the published fan ` +
              `(P10/P50/P90 over the first 14 leads) — likely / possible / unlikely to be ` +
              `reached. Never a single fabricated probability.</p>` +
            `</details>`
          : "") +
        `<p class="caption">Uses the published, server-computed breach probability against ` +
          `this borehole's resolved threshold. No client-side recompute.</p>` +
      `</div>` +
      // status params
      `<div class="wl-params wl-p-status"${r.type === "status" ? "" : " hidden"}>` +
        `<label class="wl-ed-lab">Watch when status is ` +
          `<select class="wl-status-crosses">` +
            `<option value="below"${sel("below", crosses)}>below normal</option>` +
            `<option value="above"${sel("above", crosses)}>above normal</option>` +
          `</select></label>` +
        `<p class="caption">Needs no datum — works on any borehole with a current status.</p>` +
      `</div>` +
      // sgi params
      `<div class="wl-params wl-p-sgi"${r.type === "sgi" ? "" : " hidden"}>` +
        `<label class="wl-ed-lab">Watch when SGI ` +
          `<select class="wl-sgi-op">` +
            `<option value="le"${sel("le", sgiOp)}>≤</option>` +
            `<option value="ge"${sel("ge", sgiOp)}>≥</option>` +
            `<option value="lt"${sel("lt", sgiOp)}>&lt;</option>` +
            `<option value="gt"${sel("gt", sgiOp)}>&gt;</option>` +
          `</select> ` +
          `<input type="number" class="wl-sgi-val" step="0.1" value="${esc(sgiVal)}"></label>` +
        `<p class="caption">Standardised Groundwater Index. Negative = below normal.</p>` +
      `</div>` +
      `<div class="wl-ed-actions">` +
        `<button type="button" class="wl-save" data-wl-act="save">Save Watch</button>` +
        (get(id) ? `<button type="button" class="wl-remove" data-wl-act="remove">Remove</button>` : "") +
        `<span class="wl-ed-status caption" role="status" aria-live="polite"></span>` +
      `</div>` +
      `<p class="caption">${esc(INDICATIVE)}</p>`;
  }

  // Read the current editor inputs into a rule object (null if invalid).
  function readEditorRule(pinEl) {
    const typeSel = pinEl.querySelector(".wl-type");
    const type = typeSel ? typeSel.value : "status";
    if (type === "breach") {
      const pct = parseFloat(pinEl.querySelector(".wl-breach-pct").value);
      if (isNaN(pct) || pct < 1 || pct > 100) return null;
      const rule = { type: "breach", prob_pct: pct };
      const fv = pinEl.querySelector(".wl-floor-val");
      if (fv && fv.value !== "" && !isNaN(parseFloat(fv.value))) {
        rule.floor_mAOD = parseFloat(fv.value);
        const dirSel = pinEl.querySelector(".wl-floor-dir");
        rule.dir = dirSel && dirSel.value === "above" ? "above" : "below";
      }
      return rule;
    }
    if (type === "status") {
      const c = pinEl.querySelector(".wl-status-crosses").value;
      return { type: "status", crosses: c === "above" ? "above" : "below" };
    }
    if (type === "sgi") {
      const op = pinEl.querySelector(".wl-sgi-op").value;
      const val = parseFloat(pinEl.querySelector(".wl-sgi-val").value);
      if (isNaN(val) || !["le", "ge", "lt", "gt"].includes(op)) return null;
      return { type: "sgi", op: op, value: val };
    }
    return null;
  }

  // Per-render wiring of the pin control (the control lives in the replaced
  // innerHTML, so it needs fresh wiring every render — do NOT bind-once).
  function bindDetail(bodyEl, detail, feature) {
    if (!bodyEl) return;
    const pinEl = bodyEl.querySelector(".wl-pin");
    if (!pinEl) return;
    const id = pinEl.dataset.wlId;
    const name = pinEl.dataset.wlName;
    const editor = pinEl.querySelector(".wl-editor");

    function openEditor() {
      editor.innerHTML = editorHTML(id, detail);
      editor.hidden = false;
      syncTypeVisibility();
      runFloorChecks();
    }
    function syncTypeVisibility() {
      const typeSel = pinEl.querySelector(".wl-type");
      if (!typeSel) return;
      const t = typeSel.value;
      pinEl.querySelectorAll(".wl-params").forEach((el) => {
        el.hidden = !el.classList.contains("wl-p-" + t);
      });
    }
    // Datum-sanity (guardrail) AND the honest qualitative fan read
    // (likely/possible/unlikely) for a custom floor — both shown live in the
    // editor. evaluateFloor was previously computed nowhere visible.
    function runFloorChecks() {
      const fv = pinEl.querySelector(".wl-floor-val");
      if (!fv) return;
      const sanityEl = pinEl.querySelector(".wl-sanity");
      const readEl = pinEl.querySelector(".wl-floor-read");
      if (fv.value === "") {
        if (sanityEl) { sanityEl.textContent = ""; sanityEl.classList.remove("wl-bad"); }
        if (readEl) readEl.textContent = "";
        return;
      }
      if (sanityEl) {
        const r = datumSanity(fv.value, detail);
        sanityEl.textContent = r.ok ? "" : r.msg;
        sanityEl.classList.toggle("wl-bad", !r.ok);
      }
      if (readEl) {
        const dirSel = pinEl.querySelector(".wl-floor-dir");
        const fr = evaluateFloor(
          { type: "breach", prob_pct: 1, floor_mAOD: parseFloat(fv.value),
            dir: dirSel && dirSel.value === "above" ? "above" : "below" }, detail);
        readEl.textContent = fr ? fr.note : "";
      }
    }
    // Delegated change/input on the PERSISTENT pinEl so they survive the editor's
    // innerHTML rebuild after Save (the click handler below is delegated for the
    // same reason). pinEl is fresh per detail render, so no listener accumulation.
    pinEl.addEventListener("change", (ev) => {
      if (ev.target.closest(".wl-type")) syncTypeVisibility();
      else if (ev.target.closest(".wl-floor-dir")) runFloorChecks();
    });
    pinEl.addEventListener("input", (ev) => {
      if (ev.target.closest(".wl-floor-val")) runFloorChecks();
    });

    // delegated click handling, scoped to this pin control
    pinEl.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-wl-act]");
      if (!btn) return;
      const act = btn.dataset.wlAct;
      if (act === "toggle") {
        if (has(id)) {
          remove(id);
        } else {
          // a bare toggle adds a sensible default (status-below, or breach if forecast)
          const def = pinEl.dataset.wlFc === "1"
            ? { type: "breach", prob_pct: 25 }
            : { type: "status", crosses: "below" };
          add(id, name, def);
        }
      } else if (act === "edit") {
        if (editor.hidden) openEditor(); else { editor.hidden = true; }
      } else if (act === "save") {
        const rule = readEditorRule(pinEl);
        const status = pinEl.querySelector(".wl-ed-status");
        if (!rule) { if (status) status.textContent = "Check the values."; return; }
        add(id, name, rule);
        if (status) status.textContent = "Watch saved.";
      } else if (act === "remove") {
        remove(id);
      }
    });
  }

  // Re-render the pin control in place after a store mutation (so ★/☆ + label
  // reflect state without a full panel re-render).
  function refreshPinControl() {
    // Quick-watch ☆ by the name (detail.js) — sync first, independent of the
    // pin control (the ☆ exists even when the watch fold is collapsed).
    const star = document.querySelector("#detail-body .d-star");
    if (star) {
      const sw = has(star.dataset.starId);
      star.textContent = sw ? "★" : "☆";
      star.classList.toggle("on", sw);
      star.setAttribute("aria-pressed", sw ? "true" : "false");
      const lbl = sw ? "Watching this borehole — click to remove" : "Watch this borehole";
      star.title = lbl; star.setAttribute("aria-label", lbl);
    }
    const pinEl = document.querySelector("#detail-body .wl-pin");
    if (!pinEl) return;
    const id = pinEl.dataset.wlId;
    const watched = has(id);
    const toggleBtn = pinEl.querySelector(".wl-toggle");
    const editBtn = pinEl.querySelector(".wl-edit-btn");
    if (toggleBtn) {
      toggleBtn.textContent = watched ? "★ Watching" : "☆ Watch this borehole";
      toggleBtn.classList.toggle("on", watched);
      toggleBtn.setAttribute("aria-pressed", watched ? "true" : "false");
    }
    if (editBtn) editBtn.textContent = watched ? "Edit Watch" : "Set a Watch";
    const editor = pinEl.querySelector(".wl-editor");
    if (editor && !editor.hidden) {
      // keep an open editor in sync (e.g. Remove button appears/disappears).
      editor.innerHTML = editorHTML(id, getCachedDetail(id));
      // re-run the live floor checks on the rebuilt DOM — the delegated
      // change/input handlers on .wl-pin survive this rebuild, so a dispatched
      // input event repopulates datum-sanity + the qualitative floor read.
      const fv = editor.querySelector(".wl-floor-val");
      if (fv) fv.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  // The pin control's editor needs the detail; only the currently-open panel has
  // it. We re-read from app's detail cache via a getter app.js exposes.
  function getCachedDetail(id) {
    return (window.GWC_getDetail && window.GWC_getDetail(id)) || null;
  }

  // Detail JSON (the fan) for a watched borehole — the app cache if it's been
  // opened this session, else a one-shot fetch that re-renders the panel when it
  // lands. Needed so the watchlist can read your custom trigger levels (which
  // need the fan), not just the flat published props. Attempted once per id.
  const _wlDetail = {};
  function detailFor(id) {
    const app = getCachedDetail(id);
    if (app) return app;
    if (id in _wlDetail) return _wlDetail[id] === "pending" ? null : _wlDetail[id];
    _wlDetail[id] = "pending";
    const base = (window.GWC_CONFIG && window.GWC_CONFIG.packBase) || "/pack";
    fetch(base + "/stations/" + encodeURIComponent(id) + ".json")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { _wlDetail[id] = d || null; renderPanel(); })
      .catch(() => { _wlDetail[id] = null; });
    return null;
  }

  // Standing of a watched borehole under the unified model: read against YOUR
  // trigger levels (ladder) if you've set any — the worst of them (likely >
  // possible > unlikely), tripped when 'likely' — otherwise against the published
  // threshold (the stored default rule). A custom level is only ever read
  // qualitatively from the fan, mirroring the detail panel's honesty.
  const _WORD_RANK = { likely: 3, possible: 2, unlikely: 1 };
  function levelStanding(entry, props) {
    const levels = (window.GWC_LADDER && window.GWC_LADDER.rungsFor)
      ? window.GWC_LADDER.rungsFor(entry.id) : [];
    if (!levels.length) {
      const ev = evaluate(entry.rule, props); ev.mode = "published"; return ev;
    }
    const detail = detailFor(entry.id);
    if (!detail) {
      const ev = evaluate(entry.rule, props);
      ev.mode = "loading"; ev.nLevels = levels.length; return ev;
    }
    let worst = null, worstL = null;
    for (const l of levels) {
      const r = evaluateFloor({ type: "breach", floor_mAOD: l.level_mAOD, dir: l.dir }, detail);
      if (r && (!worst || _WORD_RANK[r.word] > _WORD_RANK[worst.word])) { worst = r; worstL = l; }
    }
    if (!worst) { const ev = evaluate(entry.rule, props); ev.mode = "published"; return ev; }
    return {
      tripped: worst.word === "likely",
      isProxy: props.threshold_source === "gw_p90_proxy",
      standing: `${worstL.label} (${fmt1(worstL.level_mAOD)} mAOD): ${worst.word} to be reached in 14 d`,
      label: `Your trigger level “${worstL.label}” (${fmt1(worstL.level_mAOD)} mAOD) is ${worst.word} to be reached within 14 days — indicative, from the fan.`,
      mode: "levels", nLevels: levels.length, word: worst.word,
    };
  }

  // ============================================================================
  // CONTROLS-COLUMN PANEL
  // ============================================================================
  function mountPanel() { renderPanel(); }

  function renderPanel() {
    const host = document.getElementById("watchlist-panel");
    if (!host) return;
    const entries = load();

    // read each LIVE from the current pack via window.GWC_getFeature
    const rows = entries.map((e) => {
      const feat = window.GWC_getFeature ? window.GWC_getFeature(e.id) : null;
      const props = (feat && feat.properties) || {};
      const name = (props.name || e.name || e.id);
      const ev = levelStanding(e, props);
      return { entry: e, name: name, props: props, ev: ev, missing: !feat };
    });
    rows.sort((a, b) => {
      if (a.ev.tripped !== b.ev.tripped) return a.ev.tripped ? -1 : 1;
      return String(a.name).localeCompare(String(b.name));
    });
    const tripped = rows.filter((r) => r.ev.tripped).length;

    let body;
    if (!entries.length) {
      body = `<p class="wl-empty caption">No boreholes watched yet. Open a borehole ` +
        `and use “☆ Watch this borehole” to add one. A Watch is indicative — not a Warning.</p>`;
    } else {
      body = rows.map((r) => rowHTML(r)).join("");
    }

    const lsWarn = _lsOk ? "" :
      `<p class="caption wl-bad">This browser blocked local storage — your Watches ` +
      `last only for this session.</p>`;

    host.innerHTML =
      `<summary class="wl-summary"><span class="wl-title">Watchlist</span>` +
        `<span class="wl-count">${entries.length} watched · ${tripped} tripped</span></summary>` +
      `<div class="wl-body">` +
        body +
        lsWarn +
        `<div class="wl-share">` +
          `<button type="button" class="wl-share-btn" data-wl-share="copy">Copy share code</button>` +
          `<button type="button" class="wl-share-btn" data-wl-share="paste">Paste share code</button>` +
          `<span class="wl-share-status caption" role="status" aria-live="polite"></span>` +
        `</div>` +
        `<p class="caption">${esc(INDICATIVE)}</p>` +
      `</div>`;
  }

  function rowHTML(r) {
    const ev = r.ev;
    const cls = ev.tripped ? "wl-row tripped" : "wl-row";
    const badge = ev.tripped
      ? `<span class="wl-badge trip">TRIPPED</span>`
      : `<span class="wl-badge ok">watching</span>`;
    const missing = r.missing
      ? `<span class="wl-badge gone">not in this pack</span>` : "";
    // What this Watch reads against (unified model): your trigger levels if set,
    // otherwise the published threshold.
    const n = ev.nLevels;
    const modeTxt = ev.mode === "levels"
      ? `Watching your ${n} trigger level${n === 1 ? "" : "s"} (worst shown)`
      : ev.mode === "loading"
        ? `Watching your ${n} trigger level${n === 1 ? "" : "s"} — reading the fan…`
        : `Watching the published threshold (14-day breach)`;
    let alert = "";
    if (ev.tripped) {
      const txt = alertText(r.name, ev, null);
      alert = `<div class="wl-alert">` +
        `<button type="button" class="wl-copy" data-wl-copy="${esc(txt)}">Copy alert text</button>` +
        `<span class="wl-copy-status caption" role="status" aria-live="polite"></span></div>`;
    }
    return `<div class="${cls}" data-wl-row="${esc(r.entry.id)}">` +
      `<div class="wl-row-head">` +
        `<button type="button" class="wl-row-name" data-wl-select="${esc(r.entry.id)}">${esc(r.name)}</button>` +
        `${badge}${missing}` +
      `</div>` +
      `<div class="wl-standing caption">${esc(ev.label)}</div>` +
      `<div class="wl-rule caption">${esc(modeTxt)}</div>` +
      alert +
      `<button type="button" class="wl-row-remove" data-wl-remove="${esc(r.entry.id)}" ` +
        `aria-label="Remove ${esc(r.name)} from watchlist">Remove</button>` +
      `</div>`;
  }

  // Panel events (bound ONCE on the stable #watchlist-panel host via delegation).
  function bindPanelOnce() {
    const host = document.getElementById("watchlist-panel");
    if (!host || host.dataset.wlBound === "1") return;
    host.dataset.wlBound = "1";
    host.addEventListener("click", (ev) => {
      const selBtn = ev.target.closest("[data-wl-select]");
      if (selBtn) {
        const id = selBtn.dataset.wlSelect;
        if (window.GWC_selectById) window.GWC_selectById(id);
        return;
      }
      const rmBtn = ev.target.closest("[data-wl-remove]");
      if (rmBtn) { remove(rmBtn.dataset.wlRemove); return; }
      const copyBtn = ev.target.closest("[data-wl-copy]");
      if (copyBtn) {
        const status = copyBtn.parentElement.querySelector(".wl-copy-status");
        copyToClipboard(copyBtn.dataset.wlCopy, status);
        return;
      }
      const shareBtn = ev.target.closest("[data-wl-share]");
      if (shareBtn) {
        const status = host.querySelector(".wl-share-status");
        if (shareBtn.dataset.wlShare === "copy") {
          const code = exportString();
          if (!code) { if (status) status.textContent = "Nothing to share."; return; }
          copyToClipboard(code, status);
        } else {
          const code = window.prompt("Paste a watchlist share code:");
          if (code == null) return;
          try {
            importString(code.trim());
            if (status) status.textContent = "Imported.";
          } catch (e) {
            if (status) status.textContent = "That code didn't decode.";
          }
        }
      }
    });
  }

  function refreshAll() {
    renderPanel();
    refreshPinControl();
  }

  // ============================================================================
  // SHARING — base64 of the store JSON (NOT the URL hash; the hash is reserved
  // for the 0.2 deep-link). UTF-safe so non-ASCII names round-trip.
  // ============================================================================
  function exportString() {
    const arr = load();
    if (!arr.length) return "";
    const json = JSON.stringify(arr);
    return btoa(unescape(encodeURIComponent(json)));
  }
  function importString(s) {
    if (!s) throw new Error("empty");
    const json = decodeURIComponent(escape(atob(s)));
    const arr = JSON.parse(json);
    if (!Array.isArray(arr)) throw new Error("not an array");
    const clean = arr.filter(isValidEntry);
    if (!clean.length) throw new Error("no valid entries");
    // merge: imported entries replace same-id, others kept
    const byId = new Map();
    for (const e of load()) byId.set(e.id, e);
    for (const e of clean) byId.set(e.id, { id: e.id, name: String(e.name || e.id), rule: e.rule });
    save(Array.from(byId.values()));
    refreshAll();
  }

  // ---- clipboard (mirrors detail.js's copyText pattern) ----------------------
  function copyToClipboard(text, statusEl) {
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

  // ---- public API ------------------------------------------------------------
  window.GWC_WATCH = {
    // store
    load: load, save: save, has: has, get: get,
    add: add, remove: remove, toggle: toggle,
    // pure evaluator + qualitative reads
    evaluate: evaluate, evaluateFloor: evaluateFloor,
    evaluateFloorSeasonal: evaluateFloorSeasonal, datumSanity: datumSanity,
    // pin control
    pinControlHTML: pinControlHTML,
    bindDetail: function (bodyEl, detail, feature) {
      bindPanelOnce();                 // safe to call repeatedly (guarded)
      bindDetail(bodyEl, detail, feature);
    },
    // controls-column panel
    mountPanel: function () { bindPanelOnce(); mountPanel(); },
    renderPanel: renderPanel,
    // sharing
    exportString: exportString, importString: importString,
  };
})();

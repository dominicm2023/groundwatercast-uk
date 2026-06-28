// Threshold ladders — roadmap 2.2 (client-only, qualitative). See
// docs/product/threshold_ladders_design.md.
//
// A per-borehole ordered, operationally-named ladder (drought-permit /
// hands-off-flow / abstraction-cessation / asset-flood …). Each rung shows its
// standing against the PUBLISHED 14-day fan via window.GWC_WATCH.evaluateFloor
// (likely / possible / unlikely over the first 14 forecast leads) — NEVER a
// fabricated or AR1-multiplied probability — and is datum-sanity-checked with
// window.GWC_WATCH.datumSanity. No backend / pack / contract / Python change.
//
// Guardrails (non-negotiable, design note §Guardrails):
//   - "indicative until you supply your own levels" on the section.
//   - Datum-sanity on EVERY rung (readout + editor), live as you type.
//   - Qualitative only — no exact P(cross) / first-crossing (deferred to the
//     pipeline version). Indicative / uncalibrated framing throughout.
//   - "Watch", never "Warning".
//
// Storage: localStorage gwc_ladders_v1, defensive try/catch with in-memory
// fallback. Sharing: base64 of the store JSON (NOT the URL hash — the 0.2
// deep-link owns the hash). Mirrors the 2.1 watchlist machinery.
(function () {
  "use strict";

  const STORE_KEY = "gwc_ladders_v1";
  const INDICATIVE_LADDER =
    "Indicative until you supply your own levels — a Watch, not a Warning.";
  const RUNG_CAVEAT = "indicative, from the fan — not a calibrated probability.";

  // -- own escape helper (detail.js's esc() is module-private; mirror watchlist) --
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
      && Array.isArray(e.rungs) && e.rungs.every(isValidRung);
  }
  function isValidRung(r) {
    return r && typeof r === "object" && typeof r.label === "string"
      && typeof r.level_mAOD === "number" && !isNaN(r.level_mAOD)
      && (r.dir === "below" || r.dir === "above");
  }

  function getLadder(id) { return load().find((e) => e.id === id) || null; }
  function rungsFor(id) {
    const e = getLadder(id);
    return (e && Array.isArray(e.rungs)) ? e.rungs.slice() : [];
  }
  // Write a validated/filtered entry. Stored order is the GIVEN order (never the
  // display sort) so add/remove-by-index stays stable. Drops the entry entirely
  // when no valid rungs remain.
  function setRungs(id, name, rungs) {
    if (typeof id !== "string" || !id) return;
    const clean = (Array.isArray(rungs) ? rungs : []).filter(isValidRung);
    const arr = load().filter((e) => e.id !== id);
    if (clean.length) {
      arr.push({ id: id, name: String(name || id), rungs: clean });
    }
    save(arr);
    refresh();
    // Move the lines on the fan chart live as rungs change (when a panel is open).
    if (window.GWC_DETAIL && window.GWC_DETAIL.refreshFanLevels)
      window.GWC_DETAIL.refreshFanLevels();
  }
  function addRung(id, name, rung) {
    if (!isValidRung(rung)) return;
    const rungs = rungsFor(id);
    rungs.push({ label: String(rung.label), level_mAOD: +rung.level_mAOD,
      dir: rung.dir === "above" ? "above" : "below" });
    setRungs(id, name, rungs);
  }
  function updateRung(id, idx, partial) {
    const rungs = rungsFor(id);
    if (idx < 0 || idx >= rungs.length) return;
    const cur = rungs[idx];
    const next = {
      label: partial.label != null ? String(partial.label) : cur.label,
      level_mAOD: partial.level_mAOD != null ? +partial.level_mAOD : cur.level_mAOD,
      dir: partial.dir != null ? (partial.dir === "above" ? "above" : "below") : cur.dir,
    };
    if (!isValidRung(next)) {
      // tolerate an in-progress edit (e.g. blank/NaN level) by keeping prior valid
      // rungs; only persist when the edited rung itself is valid.
      return;
    }
    rungs[idx] = next;
    setRungs(id, getEntryName(id), rungs);
  }
  function removeRung(id, idx) {
    const rungs = rungsFor(id);
    if (idx < 0 || idx >= rungs.length) return;
    rungs.splice(idx, 1);
    setRungs(id, getEntryName(id), rungs);
  }
  function getEntryName(id) {
    const e = getLadder(id);
    return (e && e.name) || id;
  }

  // ---- per-rung evaluation (reuses GWC_WATCH; never reinvents the fan read) ---
  // Builds the breach-shaped rule the watchlist's evaluateFloor expects and
  // returns the qualitative word + datum-sanity. NEVER fabricates a number.
  function rungStanding(rung, detail) {
    const W = window.GWC_WATCH;
    const sanity = (W && W.datumSanity)
      ? W.datumSanity(rung.level_mAOD, detail) : { ok: true, msg: "" };
    let read = null;
    if (W && W.evaluateFloor) {
      read = W.evaluateFloor(
        { type: "breach", prob_pct: 1, floor_mAOD: rung.level_mAOD, dir: rung.dir },
        detail);
    }
    return { read: read, sanity: sanity };   // read may be null → "no fan read"
  }

  // ---- HTML blocks -----------------------------------------------------------
  // ladderHTML: an already-esc'd string pushed into render()'s out[]. Forecast-
  // only gate at the TOP (belt-and-braces with detail.js). The whole feature
  // lives inside the detail panel on a PERSISTENT .ld-root so the delegated
  // handlers survive the editor/readout innerHTML rebuilds (the 2.1 lesson).
  function ladderHTML(stn, detail) {
    if (!detail || !detail.forecast) return "";
    const id = stn && stn.station_id;
    if (!id) return "";
    const name = (stn && stn.name) || id;
    return `<div class="ladder-sec" data-ld-id="${esc(id)}" data-ld-name="${esc(name)}">` +
      `<p class="caption ld-indicative">${esc(INDICATIVE_LADDER)}</p>` +
      `<div class="ld-root">` +
        `<div class="ld-readout">${readoutHTML(id, detail)}</div>` +
        `<div class="ld-actions">` +
          `<button type="button" class="ld-toggle" data-ld-act="toggle-editor">Add / edit levels</button>` +
        `</div>` +
        `<div class="ld-editor" hidden></div>` +
        (_lsOk ? "" :
          `<p class="caption ld-bad">This browser blocked local storage — your ladders ` +
          `last only for this session.</p>`) +
        `<div class="ld-share">` +
          `<button type="button" class="ld-share-btn" data-ld-act="copy-share">Copy ladder code</button>` +
          `<button type="button" class="ld-share-btn" data-ld-act="paste-share">Paste ladder code</button>` +
          `<span class="ld-share-status caption" role="status" aria-live="polite"></span>` +
        `</div>` +
      `</div>` +
    `</div>`;
  }

  // Readout: rungs sorted by level DESCENDING (highest floor reads top-down
  // naturally) — display sort only, never mutating stored order.
  function readoutHTML(id, detail) {
    const rungs = rungsFor(id);
    if (!rungs.length) {
      return `<p class="caption ld-empty">No levels yet — add operational levels ` +
        `(drought-permit, hands-off-flow, abstraction-cessation, asset-flood…).</p>`;
    }
    const sorted = rungs.slice().sort((a, b) => b.level_mAOD - a.level_mAOD);
    const rows = sorted.map((rung) => {
      const { read, sanity } = rungStanding(rung, detail);
      const dirWord = rung.dir === "above" ? "ceiling" : "floor";
      let word, cls;
      if (!read) { word = "no fan read"; cls = "ld-none"; }
      else { word = read.word; cls = "ld-" + read.word; }
      const sanityFlag = sanity.ok ? "" :
        `<p class="caption ld-bad ld-sanity">${esc(sanity.msg)}</p>`;
      return `<div class="ld-rung">` +
        `<div class="ld-rung-head">` +
          `<span class="ld-rung-label">${esc(rung.label)}</span>` +
          `<span class="ld-badge ${cls}">${esc(word)}</span>` +
        `</div>` +
        `<div class="ld-rung-meta caption">${fmt1(rung.level_mAOD)} mAOD · ${esc(dirWord)}</div>` +
        `<div class="ld-rung-caveat caption">${esc(RUNG_CAVEAT)}</div>` +
        sanityFlag +
      `</div>`;
    }).join("");
    return rows;
  }

  // Editor: each existing rung as an editable row + a blank "add new rung" row.
  // Every row carries a live datum-sanity line. Inputs carry data-ld-idx so the
  // delegated handler (on the persistent .ld-root) knows which rung.
  function editorHTML(id, detail) {
    const rungs = rungsFor(id);   // STORED order — indices match data-ld-idx
    const dirOpts = (d) =>
      `<option value="below"${d === "below" ? " selected" : ""}>below (floor)</option>` +
      `<option value="above"${d === "above" ? " selected" : ""}>above (ceiling)</option>`;

    const rows = rungs.map((rung, i) => {
      const { sanity } = rungStanding(rung, detail);
      const sanMsg = sanity.ok ? "" : esc(sanity.msg);
      const sanCls = sanity.ok ? "" : " ld-bad";
      return `<div class="ld-r-row" data-ld-idx="${i}">` +
        `<input type="text" class="ld-r-label" data-ld-idx="${i}" ` +
          `value="${esc(rung.label)}" placeholder="Rung name" aria-label="Rung name">` +
        `<input type="number" class="ld-r-level" data-ld-idx="${i}" step="0.01" ` +
          `value="${esc(rung.level_mAOD)}" placeholder="mAOD" aria-label="Level (mAOD)"> mAOD` +
        `<select class="ld-r-dir" data-ld-idx="${i}" aria-label="Direction of concern">${dirOpts(rung.dir)}</select>` +
        `<button type="button" class="ld-remove-rung" data-ld-act="remove-rung" ` +
          `data-ld-idx="${i}" aria-label="Remove ${esc(rung.label)}">Remove</button>` +
        `<p class="ld-sanity caption${sanCls}" role="status" aria-live="polite">${sanMsg}</p>` +
      `</div>`;
    }).join("");

    const addRow =
      `<div class="ld-r-row ld-r-new">` +
        `<input type="text" class="ld-new-label" placeholder="Rung name (e.g. Drought permit)" aria-label="New rung name">` +
        `<input type="number" class="ld-new-level" step="0.01" placeholder="mAOD" aria-label="New rung level (mAOD)"> mAOD` +
        `<select class="ld-new-dir" aria-label="New rung direction">${dirOpts("below")}</select>` +
        `<button type="button" class="ld-add" data-ld-act="add-rung">Add level</button>` +
        `<p class="ld-sanity ld-new-sanity caption" role="status" aria-live="polite"></p>` +
      `</div>`;

    return `<div class="ld-ed-list">${rows}</div>` +
      addRow +
      `<p class="caption">Each rung's level is checked against this borehole's ` +
        `observed range (datum-sanity) and read qualitatively against the published ` +
        `fan — likely / possible / unlikely to be reached over the next 14 days. ` +
        `Never a single fabricated probability.</p>` +
      `<p class="caption">${esc(INDICATIVE_LADDER)}</p>`;
  }

  // ---- per-render wiring -----------------------------------------------------
  // Delegate click/change/input on the PERSISTENT .ld-root. .ld-root is fresh
  // per detail render (no listener accumulation) but stable across the editor/
  // readout innerHTML rebuilds within a render — so handlers survive rebuilds
  // (the 2.1 lesson: never direct-attach to the rebuilt rows).
  function bindDetail(bodyEl, detail, feature) {
    if (!bodyEl) return;
    const sec = bodyEl.querySelector(".ladder-sec");
    if (!sec) return;
    const root = sec.querySelector(".ld-root");
    if (!root) return;
    const id = sec.dataset.ldId;
    const name = sec.dataset.ldName;

    const readoutEl = () => root.querySelector(".ld-readout");
    const editorEl = () => root.querySelector(".ld-editor");

    function refreshLocal() {
      const ro = readoutEl();
      if (ro) ro.innerHTML = readoutHTML(id, detail);
      const ed = editorEl();
      if (ed && !ed.hidden) ed.innerHTML = editorHTML(id, detail);
    }

    function liveSanity(levelInput, sanityEl) {
      if (!levelInput || !sanityEl) return;
      const v = levelInput.value;
      const W = window.GWC_WATCH;
      if (v === "") { sanityEl.textContent = ""; sanityEl.classList.remove("ld-bad"); return; }
      const r = (W && W.datumSanity) ? W.datumSanity(v, detail) : { ok: true, msg: "" };
      sanityEl.textContent = r.ok ? "" : r.msg;
      sanityEl.classList.toggle("ld-bad", !r.ok);
    }

    root.addEventListener("input", (ev) => {
      const t = ev.target;
      if (t.classList.contains("ld-new-level")) {
        const row = t.closest(".ld-r-new");
        liveSanity(t, row && row.querySelector(".ld-new-sanity"));
      } else if (t.classList.contains("ld-r-level")) {
        const row = t.closest(".ld-r-row");
        liveSanity(t, row && row.querySelector(".ld-sanity"));
      }
    });

    root.addEventListener("change", (ev) => {
      const t = ev.target;
      const idxAttr = t.dataset && t.dataset.ldIdx;
      if (idxAttr == null) return;
      const idx = parseInt(idxAttr, 10);
      if (isNaN(idx)) return;
      // an existing-rung label/level/dir edit → persist + refresh badges
      if (t.classList.contains("ld-r-label") || t.classList.contains("ld-r-level")
        || t.classList.contains("ld-r-dir")) {
        const row = t.closest(".ld-r-row");
        if (!row) return;
        const labelEl = row.querySelector(".ld-r-label");
        const levelEl = row.querySelector(".ld-r-level");
        const dirEl = row.querySelector(".ld-r-dir");
        const level = parseFloat(levelEl && levelEl.value);
        if (!labelEl || !labelEl.value.trim() || isNaN(level)) {
          // incomplete edit — just re-run the row's live sanity, don't persist
          liveSanity(levelEl, row.querySelector(".ld-sanity"));
          return;
        }
        // updateRung → setRungs → refresh() rebuilds both the readout (so badges
        // track the edit) and the open editor; no manual re-render needed here.
        // The committing input loses focus on the rebuild — acceptable, since this
        // fires on change (blur/commit), not on every keystroke.
        updateRung(id, idx, {
          label: labelEl.value.trim(), level_mAOD: level,
          dir: dirEl && dirEl.value === "above" ? "above" : "below",
        });
      }
    });

    root.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-ld-act]");
      if (!btn) return;
      const act = btn.dataset.ldAct;
      const ed = editorEl();
      if (act === "toggle-editor") {
        if (!ed) return;
        if (ed.hidden) {
          ed.innerHTML = editorHTML(id, detail);
          ed.hidden = false;
          btn.textContent = "Close editor";
        } else {
          ed.hidden = true;
          btn.textContent = "Add / edit levels";
        }
      } else if (act === "add-rung") {
        const row = btn.closest(".ld-r-new");
        if (!row) return;
        const labelEl = row.querySelector(".ld-new-label");
        const levelEl = row.querySelector(".ld-new-level");
        const dirEl = row.querySelector(".ld-new-dir");
        const sanEl = row.querySelector(".ld-new-sanity");
        const label = labelEl ? labelEl.value.trim() : "";
        const level = parseFloat(levelEl && levelEl.value);
        if (!label || isNaN(level)) {
          if (sanEl) { sanEl.textContent = "Enter a rung name and a level in mAOD."; sanEl.classList.add("ld-bad"); }
          return;
        }
        addRung(id, name, {
          label: label, level_mAOD: level,
          dir: dirEl && dirEl.value === "above" ? "above" : "below",
        });
        // rebuild both (new blank add-row + the new rung in the list + readout)
        if (ed) ed.innerHTML = editorHTML(id, detail);
        const ro = readoutEl();
        if (ro) ro.innerHTML = readoutHTML(id, detail);
      } else if (act === "remove-rung") {
        const idx = parseInt(btn.dataset.ldIdx, 10);
        if (isNaN(idx)) return;
        removeRung(id, idx);
        if (ed) ed.innerHTML = editorHTML(id, detail);
        const ro = readoutEl();
        if (ro) ro.innerHTML = readoutHTML(id, detail);
      } else if (act === "copy-share") {
        const status = root.querySelector(".ld-share-status");
        const code = exportString();
        if (!code) { if (status) status.textContent = "Nothing to share."; return; }
        copyToClipboard(code, status);
      } else if (act === "paste-share") {
        const status = root.querySelector(".ld-share-status");
        const code = window.prompt("Paste a ladder share code:");
        if (code == null) return;
        try {
          importString(code.trim());
          if (status) status.textContent = "Imported.";
          // reflect any change to THIS borehole's ladder
          if (ed && !ed.hidden) ed.innerHTML = editorHTML(id, detail);
          const ro = readoutEl();
          if (ro) ro.innerHTML = readoutHTML(id, detail);
        } catch (e) {
          if (status) status.textContent = "That code didn't decode.";
        }
      }
    });
  }

  // ---- refresh (keeps an open detail panel's readout in sync after a store
  // mutation triggered elsewhere — e.g. an import). The delegated handlers on
  // .ld-root survive these innerHTML swaps. ----
  function refresh() {
    const sec = document.querySelector("#detail-body .ladder-sec");
    if (!sec) return;
    const id = sec.dataset.ldId;
    const detail = (window.GWC_getDetail && window.GWC_getDetail(id)) || null;
    if (!detail) return;
    const ro = sec.querySelector(".ld-readout");
    if (ro) ro.innerHTML = readoutHTML(id, detail);
    const ed = sec.querySelector(".ld-editor");
    if (ed && !ed.hidden) ed.innerHTML = editorHTML(id, detail);
  }

  // ---- sharing — base64 of the store JSON (NOT the URL hash). UTF-safe. -------
  function exportString() {
    const arr = load();
    if (!arr.length) return "";
    return btoa(unescape(encodeURIComponent(JSON.stringify(arr))));
  }
  function importString(s) {
    if (!s) throw new Error("empty");
    const json = decodeURIComponent(escape(atob(s)));
    const arr = JSON.parse(json);
    if (!Array.isArray(arr)) throw new Error("not an array");
    const clean = arr.filter(isValidEntry);
    if (!clean.length) throw new Error("no valid entries");
    // merge-by-id: imported entries replace same-id, others kept
    const byId = new Map();
    for (const e of load()) byId.set(e.id, e);
    for (const e of clean) {
      byId.set(e.id, { id: e.id, name: String(e.name || e.id), rungs: e.rungs });
    }
    save(Array.from(byId.values()));
    refresh();
  }

  // ---- clipboard (mirrors watchlist.js / detail.js copyText) -----------------
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
  window.GWC_LADDER = {
    load: load, save: save,
    getLadder: getLadder, setRungs: setRungs,
    addRung: addRung, updateRung: updateRung, removeRung: removeRung,
    ladderHTML: ladderHTML, bindDetail: bindDetail,
    exportString: exportString, importString: importString,
  };
})();

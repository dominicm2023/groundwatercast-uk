// GroundwaterCast explorer — map boot + interactions.
(function () {
  "use strict";
  const CFG = window.GWC_CONFIG;
  const PAL = CFG.palette;
  const PACK = CFG.packBase.replace(/\/$/, "");

  let META = null;
  let detailCache = {};
  let selectedId = null;

  // -- shareable URL-hash state (roadmap 0.2): selected borehole + view + range.
  // #bh=<id-or-name-slug>&view=<active|forecast|all>&range=<90|365|730|all>&rivers=1.
  // Defaults (active view, 90-day range, rivers off, no selection) are omitted
  // to keep links clean; the homepage RiverCast teaser links with rivers=1 so
  // the layer is already on when the explorer opens.
  const VIEWS = ["active", "forecast", "all"];
  const RANGES = ["90", "365", "730", "all"];
  const state = { bh: null, view: "active", range: "90", rivers: false };
  let featById = new Map();
  let idsBySlug = new Map();

  function slugify(s) {
    return String(s || "").toLowerCase().trim()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }
  const Hash = {
    read() {
      const q = new URLSearchParams(location.hash.replace(/^#/, ""));
      return {
        bh: q.get("bh"), view: q.get("view"), range: q.get("range"),
        rivers: q.get("rivers") === "1" || q.get("layer") === "rivers",
      };
    },
    write() {
      const q = new URLSearchParams();
      if (state.bh) q.set("bh", state.bh);
      if (state.view !== "active") q.set("view", state.view);
      if (state.range !== "90") q.set("range", state.range);
      if (state.rivers) q.set("rivers", "1");
      const qs = q.toString();
      const hash = qs ? "#" + qs : "";
      if (hash === location.hash) return;            // no-op guard (avoids churn)
      history.replaceState(null, "", location.pathname + location.search + hash);
    },
  };
  function buildIndexes(geojson) {
    featById = new Map();
    idsBySlug = new Map();
    for (const f of geojson.features) {
      const id = f.properties.station_id;
      featById.set(id, f);
      const slug = slugify(f.properties.name);
      if (!slug) continue;
      if (!idsBySlug.has(slug)) idsBySlug.set(slug, []);
      idsBySlug.get(slug).push(id);
    }
  }
  function resolveBh(key) {
    if (!key) return null;
    if (featById.has(key)) return featById.get(key);   // UUID
    const ids = idsBySlug.get(slugify(key));           // name-slug
    return ids && ids.length ? featById.get(ids[0]) : null;
  }
  function bhKeyFor(feature) {
    // Prefer a readable slug when it's unambiguous; else the stable station_id.
    const slug = slugify(feature.properties.name);
    const ids = idsBySlug.get(slug);
    return slug && ids && ids.length === 1 ? slug : feature.properties.station_id;
  }
  // detail.js calls this when the history-range buttons change.
  window.GWC_onRangeChange = function (days) { state.range = String(days); Hash.write(); };

  // status (below|near|above|null) → fill colour, via a MapLibre match expr
  const COLOR_EXPR = [
    "match", ["coalesce", ["get", "status"], "none"],
    "below", PAL.below, "near", PAL.near, "above", PAL.above,
    /* default */ PAL.none,
  ];

  // -- forecast-timeline scrubber (recolour the map through the forecast) ----
  // Every frame indexes each feature's st_seq/op_seq (built by the pack, length
  // == meta.forecast_frames). Frame 0 ("Today") is the measured status, or a
  // faint "estimated" nowcast where the latest reading is stale; later frames
  // are the fan / seasonal outlook. Colour = category; opacity = confidence ×
  // lead-time fade — so a future (or estimated) dot never looks over-confident.
  let FRAMES = [];
  function colorForFrame(i) {
    return ["match", ["at", i, ["get", "st_seq"]],
      "below", PAL.below, "near", PAL.near, "above", PAL.above, PAL.none];
  }
  function opacityForFrame(i) {
    return ["at", i, ["get", "op_seq"]];
  }
  // Borehole-dot opacity has two writers — the timeline scrubber (per-feature
  // confidence × lead-time fade) and the rivers view (flat dim). Track the
  // timeline's current expression so switching the rivers view OFF restores
  // the fade instead of clobbering it with a constant.
  const RIVERS_DIM = 0.35;
  let dotBaseOpacity = 0.95;             // updated by setFrame once the timeline runs
  function applyDotOpacity() {
    map.setPaintProperty("stations-dot", "circle-opacity",
      state.rivers ? RIVERS_DIM : dotBaseOpacity);
  }
  function setFrame(i) {
    map.setPaintProperty("stations-dot", "circle-color", colorForFrame(i));
    dotBaseOpacity = opacityForFrame(i);
    // rivers view keeps the boreholes dimmed even while the timeline scrubs
    applyDotOpacity();
    const lbl = document.getElementById("tl-frame");
    const note = document.getElementById("tl-note");
    const title = document.getElementById("legend-title");
    if (lbl) lbl.textContent = FRAMES[i] || "";
    if (note) note.hidden = i <= 0;
    if (title) title.textContent = i <= 0 ? "Current level vs normal" : "Forecast vs normal";
  }
  function setupTimeline(meta, geojson) {
    FRAMES = (meta && meta.forecast_frames) || [];
    const DAYS = (meta && meta.forecast_frame_days) || null;
    const sample = geojson.features[0] && geojson.features[0].properties.st_seq;
    // Graceful no-op for older packs without the timeline arrays.
    if (FRAMES.length < 2 || !sample || sample.length !== FRAMES.length) return;
    const box = document.getElementById("timeline");
    const slider = document.getElementById("tl-slider");
    if (!box || !slider) return;

    // Repaint only when the SNAPPED frame actually changes. A drag fires dozens
    // of input events per second; with 9 discrete frames that's at most ~9
    // repaints across the whole bar instead of one per pixel (each repaint
    // re-evaluates every feature, so this is the difference between smooth and
    // janky).
    let curFrame = 0;
    const byDays = Array.isArray(DAYS) && DAYS.length === FRAMES.length;
    if (byDays) {
      // The slider axis is REAL days-ahead, so step spacing reflects elapsed
      // time (weekly steps near the start, monthly steps far apart). A
      // <datalist> draws a tick at each frame; the thumb snaps to the nearest
      // frame on release.
      slider.min = "0";
      slider.max = String(DAYS[DAYS.length - 1]);
      slider.step = "1";
      slider.value = "0";
      const ticks = document.getElementById("tl-ticks");
      if (ticks) {
        ticks.innerHTML = DAYS.map((d) => `<option value="${d}"></option>`).join("");
        slider.setAttribute("list", "tl-ticks");
      }
      const nearest = (v) => {
        let bi = 0, bd = Infinity;
        DAYS.forEach((d, i) => { const dd = Math.abs(d - v); if (dd < bd) { bd = dd; bi = i; } });
        return bi;
      };
      slider.addEventListener("input", () => {
        const i = nearest(parseInt(slider.value, 10) || 0);
        if (i !== curFrame) { curFrame = i; setFrame(i); }
      });
      slider.addEventListener("change", () => { slider.value = String(DAYS[curFrame]); });
    } else {                                        // older pack: even index steps
      slider.max = String(FRAMES.length - 1);
      slider.value = "0";
      slider.addEventListener("input", () => {
        const i = parseInt(slider.value, 10) || 0;
        if (i !== curFrame) { curFrame = i; setFrame(i); }
      });
    }
    // Play / pause: auto-advance through the frames on a timer — the hands-free
    // "watch the forecast evolve" demo. Programmatic slider moves don't fire
    // "input", so a manual drag still cancels playback (listener below).
    const playBtn = document.getElementById("tl-play");
    if (playBtn) {
      let timer = null;
      const setThumb = (i) => { slider.value = String(byDays ? DAYS[i] : i); };
      const stop = () => {
        if (timer) { clearInterval(timer); timer = null; }
        playBtn.textContent = "▶";                 // ▶
        playBtn.setAttribute("aria-pressed", "false");
        playBtn.setAttribute("aria-label", "Play forecast timeline");
      };
      const reduceMotion = window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      playBtn.addEventListener("click", () => {
        if (timer) { stop(); return; }
        playBtn.textContent = "⏸";                 // ⏸
        playBtn.setAttribute("aria-pressed", "true");
        playBtn.setAttribute("aria-label", "Pause forecast timeline");
        if (curFrame >= FRAMES.length - 1) { curFrame = 0; setFrame(0); setThumb(0); }
        // reduced motion: playback stays available (user-initiated) but steps
        // at half speed so the map recolouring isn't a rapid flicker
        timer = setInterval(() => {
          const next = curFrame >= FRAMES.length - 1 ? 0 : curFrame + 1;
          curFrame = next; setFrame(next); setThumb(next);
        }, reduceMotion ? 1700 : 850);
      });
      slider.addEventListener("input", stop);           // manual scrub cancels playback
    }
    box.hidden = false;
    setFrame(0);
  }

  // -- RiverCast rivers VIEW: distinct diamond markers for flow gauges —
  // colour still encodes below/near/above (one vocabulary), the SHAPE is
  // what marks a gauge as a river rather than a borehole. Switching the view
  // on also (a) dims the boreholes — dimmed, never hidden: chalk-stream
  // baseflow IS groundwater draining, and the borehole↔river link is the
  // product's edge — (b) emphasises the diamonds, (c) lazy-loads the river
  // polylines (pack/rivers.geojson, OS Open Rivers, same fetch-on-first-use
  // pattern as the geology layer), and (d) shows the flow-vocabulary legend.
  // Toggled independently of the borehole view-mode/search filters; the
  // whole gated fleet shows together. /rivers/ and shared links
  // pre-activate it via #rivers=1.
  function setupRiverLayer(geojson) {
    const hasFlow = geojson.features.some((f) => f.properties.station_type === "flow");
    const row = document.getElementById("rivers-toggle-row");
    const toggle = document.getElementById("rivers-toggle");
    if (!hasFlow) { if (row) row.hidden = true; return; }

    // A small filled diamond, registered as an SDF image so `icon-color` can
    // paint it with the same status palette the circle dots use.
    const SIZE = 32;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = SIZE;
    const cctx = canvas.getContext("2d");
    cctx.fillStyle = "#000";
    const c = SIZE / 2, r = SIZE / 2 - 3;
    cctx.beginPath();
    cctx.moveTo(c, c - r); cctx.lineTo(c + r, c); cctx.lineTo(c, c + r); cctx.lineTo(c - r, c);
    cctx.closePath(); cctx.fill();
    if (!map.hasImage("river-diamond")) {
      map.addImage("river-diamond", cctx.getImageData(0, 0, SIZE, SIZE), { sdf: true });
    }

    // Collision-managed, not allow-overlap: at 94 gauges the valley clusters
    // (several gauges per chalk stream, plus same-site Main/Total pairs)
    // merged into one blob. Letting MapLibre's collision detection hide the
    // overlapping diamonds at national zoom keeps each visible mark crisp —
    // zooming in reveals the rest. symbol-sort-key gives the DRIEST gauges
    // placement priority, so the low-flow story is never the one hidden.
    map.addLayer({
      id: "stations-flow-dot", type: "symbol", source: "stations",
      filter: ["==", ["get", "station_type"], "flow"],
      layout: {
        "icon-image": "river-diamond",
        "icon-size": ["interpolate", ["linear"], ["zoom"], 5, 0.32, 10, 0.65],
        "icon-padding": 1,
        "symbol-sort-key": ["coalesce", ["get", "percentile"], 100],
        visibility: "none",
      },
      paint: {
        "icon-color": COLOR_EXPR,
        "icon-halo-color": "#ffffff", "icon-halo-width": 2,
        "icon-opacity": 0.95,
      },
    });

    // River polylines — lazy: fetched only the first time the view turns on
    // (mirrors wireGeology), gated on the pack actually shipping the file.
    let riversLoaded = false, riversLoading = false;
    const riversAvailable = () =>
      META && META.inputs && META.inputs.river_polylines &&
      META.inputs.river_polylines.status === "ok";
    function ensureRiverLines(then) {
      if (riversLoaded || riversLoading || !riversAvailable()) { then(); return; }
      riversLoading = true;
      fetch(`${PACK}/rivers.geojson`)
        .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then((gj) => {
          map.addSource("rivers", { type: "geojson", data: gj });
          // beneath the station layers so dots/diamonds stay clickable on top
          map.addLayer({
            id: "rivers-lines", type: "line", source: "rivers",
            layout: { "line-cap": "round", "line-join": "round", visibility: "none" },
            paint: {
              "line-color": "#4a90c4",
              "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.7, 9, 1.8, 12, 3],
              "line-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.5, 8, 0.75],
            },
          }, "stations-ring");
          riversLoaded = true;
        })
        .catch(() => {})                     // polylines are a nicety — degrade silently
        // `then` re-reads state.rivers at completion time: if the user
        // toggled the view off while the fetch was in flight, the lines must
        // NOT appear.
        .finally(() => { riversLoading = false; then(); });
    }

    const legend = document.getElementById("rivers-legend");
    const legendBox = document.getElementById("legend");
    const showLines = () => {
      // reads LIVE state (not a captured `on`) so an in-flight polyline fetch
      // that completes after the user toggled the view off stays hidden
      if (map.getLayer("rivers-lines")) {
        map.setLayoutProperty("rivers-lines", "visibility",
          state.rivers ? "visible" : "none");
      }
    };
    const setOn = (on) => {
      state.rivers = on;
      map.setLayoutProperty("stations-flow-dot", "visibility", on ? "visible" : "none");
      // rivers view: gauges emphasised, boreholes dimmed but PRESENT
      map.setLayoutProperty("stations-flow-dot", "icon-size", on
        ? ["interpolate", ["linear"], ["zoom"], 5, 0.42, 10, 0.85]
        : ["interpolate", ["linear"], ["zoom"], 5, 0.32, 10, 0.65]);
      applyDotOpacity();
      map.setPaintProperty("stations-dot", "circle-stroke-width", on ? 0.5 : 1);
      map.setPaintProperty("stations-ring", "circle-stroke-opacity", on ? 0.25 : 1);
      if (on) ensureRiverLines(showLines); else showLines();
      if (toggle) toggle.checked = on;
      if (legend) legend.hidden = !on;
      if (legendBox) legendBox.classList.toggle("rivers-on", on);
    };
    if (row) row.hidden = false;
    if (toggle) toggle.addEventListener("change", () => { setOn(toggle.checked); Hash.write(); });
    setOn(state.rivers);
    // exposed so the hash-restore / hashchange handlers can flip it without
    // re-deriving the layer/image setup.
    window.GWC_setRiversLayer = setOn;
  }

  const map = new maplibregl.Map({
    container: "map",
    style: CFG.basemapStyle,
    center: CFG.center,
    zoom: CFG.zoom,
    minZoom: CFG.minZoom,
    maxZoom: CFG.maxZoom,
    attributionControl: false,
  });
  window.gwcMap = map; // exposed for embedding / debugging
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");

  Promise.all([
    fetch(`${PACK}/meta.json`).then((r) => r.json()),
    fetch(`${PACK}/stations.geojson`).then((r) => r.json()),
    new Promise((res) => map.on("load", res)),
  ]).then(([meta, geojson]) => {
    META = meta;
    showMeta(meta);

    // Restore view/range from the hash BEFORE wiring filters (which reads
    // state.view for its initial mode); the borehole is restored after.
    buildIndexes(geojson);

    // -- watchlist hooks (2.1). Exposed only after featById is populated so the
    // panel never reads features at script-eval time. The panel reads LIVE props
    // through these getters; GWC_getDetail lets the pin editor reach the cached
    // detail for its qualitative floor read / datum-sanity. --
    window.GWC_getFeature = (id) => featById.get(id);
    window.GWC_getDetail = (id) => detailCache[id] || null;
    window.GWC_selectById = (id) => {
      const f = featById.get(id);
      if (f) {
        if (!visibleUnderView(f.properties, state.view) && wireFilters._setView) {
          wireFilters._setView("all");
        }
        selectFeature(f, { flyTo: true });
      }
    };
    if (window.GWC_WATCH && window.GWC_WATCH.mountPanel) window.GWC_WATCH.mountPanel();

    const boot = Hash.read();
    if (VIEWS.includes(boot.view)) state.view = boot.view;
    if (RANGES.includes(boot.range)) state.range = boot.range;
    if (boot.rivers) state.rivers = true;

    map.addSource("stations", { type: "geojson", data: geojson });

    // GW boreholes only — RiverCast (flow) gauges get their own distinct-shape
    // layer below so the two station kinds are never visually confused, even
    // though status colour uses the same below/near/above vocabulary for both.
    const NOT_FLOW = ["!=", ["get", "station_type"], "flow"];
    // outer ring marks forecast boreholes
    map.addLayer({
      id: "stations-ring", type: "circle", source: "stations",
      filter: ["all", NOT_FLOW, ["==", ["get", "has_forecast"], true]],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 6.5, 10, 11],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": CFG.palette ? "#1a3a5c" : "#1a3a5c",
        "circle-stroke-width": 2,
      },
    });
    map.addLayer({
      id: "stations-dot", type: "circle", source: "stations",
      filter: NOT_FLOW,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 4, 10, 7],
        "circle-color": COLOR_EXPR,
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 1,
        "circle-opacity": 0.95,
      },
    });
    // selected highlight (separate layer, filtered to the chosen id) — shared
    // between boreholes and river gauges, so a selected diamond still rings.
    map.addLayer({
      id: "stations-selected", type: "circle", source: "stations",
      filter: ["==", ["get", "station_id"], "___none___"],
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 8, 10, 13],
        "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": "#111",
        "circle-stroke-width": 2.5,
      },
    });

    setupRiverLayer(geojson);

    map.on("click", "stations-dot", (e) => selectFeature(e.features[0], { focus: true }));
    map.on("mouseenter", "stations-dot", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "stations-dot", () => (map.getCanvas().style.cursor = ""));
    map.on("click", "stations-flow-dot", (e) => selectFeature(e.features[0], { focus: true }));
    map.on("mouseenter", "stations-flow-dot", () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", "stations-flow-dot", () => (map.getCanvas().style.cursor = ""));

    setupTimeline(meta, geojson);

    // hover tooltip (shared by boreholes and river gauges)
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    const showPop = (e) => {
      const p = e.features[0].properties;
      pop.setLngLat(e.lngLat).setHTML(
        `<strong>${escapeHtml(p.name || p.station_id)}</strong><br>${statusText(p)}`
      ).addTo(map);
    };
    map.on("mousemove", "stations-dot", showPop);
    map.on("mouseleave", "stations-dot", () => pop.remove());
    map.on("mousemove", "stations-flow-dot", showPop);
    map.on("mouseleave", "stations-flow-dot", () => pop.remove());

    wireFilters(geojson);
    wireGeology(meta);
    restoreSelection(boot.bh);
  }).catch((err) => {
    document.getElementById("data-asof").textContent = "failed to load data";
    console.error("pack load failed", err);
  });

  // -- selection ------------------------------------------------------------
  function selectFeature(feature, opts) {
    opts = opts || {};
    const id = feature.properties.station_id;
    selectedId = id;
    state.bh = bhKeyFor(feature);
    Hash.write();
    if (opts.flyTo && feature.geometry && feature.geometry.type === "Point") {
      // prefers-reduced-motion: jump, don't animate
      const reduce = window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      const view = { center: feature.geometry.coordinates,
                     zoom: Math.max(map.getZoom(), 9) };
      if (reduce) { map.jumpTo(view); } else { map.flyTo(view); }
    }
    map.setFilter("stations-selected", ["==", ["get", "station_id"], id]);
    const panel = document.getElementById("detail");
    const body = document.getElementById("detail-body");
    document.getElementById("detail-empty").hidden = true;
    body.hidden = false;
    panel.classList.remove("empty");
    body.innerHTML = `<p class="caption">Loading…</p>`;

    const render = (d) => {
      body.innerHTML = window.GWC_DETAIL.render(d, META, { range: state.range });
      // a11y: move focus to the detail panel and announce the selection so
      // screen-reader users hear where they landed (panel content replaced).
      panel.setAttribute("aria-label", `Details for ${d.station && (d.station.name || d.station.station_id) || "selected borehole"}`);
      if (opts.focus) {
        panel.setAttribute("tabindex", "-1");
        panel.focus({ preventScroll: false });
      }
      window.GWC_DETAIL.bindFan(body, d);
      window.GWC_DETAIL.bindData(body);
      if (window.GWC_DETAIL.bindLinkedBoreholes) window.GWC_DETAIL.bindLinkedBoreholes(body);
      // (re)wire the watchlist pin control every render — it lives in the
      // replaced innerHTML, so it needs per-render wiring (like bindFan).
      if (window.GWC_WATCH && window.GWC_WATCH.bindDetail) {
        window.GWC_WATCH.bindDetail(body, d, feature);
      }
      // (re)wire the threshold-ladder block (2.2) — same per-render wiring as
      // the watchlist pin control; it lives in the replaced innerHTML.
      if (window.GWC_LADDER && window.GWC_LADDER.bindDetail) {
        window.GWC_LADDER.bindDetail(body, d, feature);
      }
    };
    if (detailCache[id]) { render(detailCache[id]); return; }
    fetch(`${PACK}/stations/${id}.json`)
      .then((r) => r.json())
      .then((d) => { detailCache[id] = d; if (selectedId === id) render(d); })
      .catch((err) => {
        body.innerHTML = `<p class="caption">Detail unavailable for this borehole.</p>`;
        console.error("detail load failed", id, err);
      });
  }

  // -- filters --------------------------------------------------------------
  // View modes: which boreholes the map shows. "active" (default) hides the
  // stale "no current status" dots that carry no signal today, keeping only
  // boreholes with a status and/or a forecast. The dot layer draws whatever
  // is in the source; the ring layer always marks has_forecast on top — so
  // filtering the SOURCE data is all that's needed.
  function visibleUnderView(p, view) {
    if (view === "forecast") return !!p.has_forecast;
    if (view === "all") return true;
    return !!p.status || !!p.has_forecast;          // active
  }
  function wireFilters(geojson) {
    const search = document.getElementById("search");
    const seg = document.getElementById("view-mode");
    const countEl = document.getElementById("view-count");
    const resultsList = document.getElementById("search-results");

    function syncSeg() {
      seg.querySelectorAll(".seg").forEach((b) => {
        const on = b.dataset.mode === state.view;
        b.classList.toggle("active", on);
        b.setAttribute("aria-pressed", String(on));
      });
    }

    // -- keyboard-accessible search results (a11y: the map dots are only
    // reachable with a mouse, since MapLibre draws them on a canvas with no
    // native DOM/keyboard hit-targets. This list is the keyboard path: typing
    // renders up to 15 matches as a focusable listbox under the search box,
    // and Enter/click on a result calls the SAME selectFeature() the map
    // click handler uses, so both paths open the identical detail panel. --
    let searchResults = [];   // features currently listed (mirrors what's shown)
    let activeIdx = -1;       // aria-activedescendant index, -1 = none highlighted

    function hideResults() {
      if (!resultsList) return;
      resultsList.hidden = true;
      resultsList.innerHTML = "";
      searchResults = [];
      activeIdx = -1;
      search.setAttribute("aria-expanded", "false");
      search.removeAttribute("aria-activedescendant");
    }
    function highlightActive() {
      if (!resultsList) return;
      Array.prototype.forEach.call(resultsList.children, (li, i) => {
        li.classList.toggle("active", i === activeIdx);
      });
      if (activeIdx >= 0) {
        search.setAttribute("aria-activedescendant", "search-result-" + activeIdx);
        const el = resultsList.children[activeIdx];
        if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
      } else {
        search.removeAttribute("aria-activedescendant");
      }
    }
    function chooseResult(i) {
      const f = searchResults[i];
      if (!f) return;
      // every listed result already passed visibleUnderView() for the current
      // view mode (same filter the map dots use), so no view-mode switch is
      // needed here — unlike the hash-restore path, which can target a
      // borehole hidden by the CURRENT view and has to flip to "all" first.
      selectFeature(f, { flyTo: true, focus: true });
      search.value = "";
      hideResults();
      apply();      // resync the map's dot filter now the query text is cleared
    }
    function renderResults(feats, q) {
      if (!resultsList) return;
      searchResults = q ? feats.slice(0, 15) : [];
      activeIdx = -1;
      if (!searchResults.length) { hideResults(); return; }
      resultsList.innerHTML = searchResults.map((f, i) => {
        const p = f.properties;
        const kind = p.station_type === "flow" ? "RiverCast"
          : (p.has_forecast ? "forecast" : "status");
        return `<li role="option" id="search-result-${i}" class="search-result" tabindex="-1">` +
          `<span>${escapeHtml(p.name || p.station_id)}</span>` +
          `<span class="sr-kind">${kind}</span></li>`;
      }).join("");
      resultsList.hidden = false;
      search.setAttribute("aria-expanded", "true");
    }
    // mousedown (not click) fires before the input would blur, so
    // preventDefault here keeps focus in the input and the list open long
    // enough for the click to land — same interaction model as a native
    // <select>/combobox popup.
    if (resultsList) {
      resultsList.addEventListener("mousedown", (e) => {
        const li = e.target.closest(".search-result");
        if (!li) return;
        e.preventDefault();
        chooseResult(parseInt(li.id.replace("search-result-", ""), 10));
      });
    }
    search.addEventListener("keydown", (e) => {
      if (!searchResults.length) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIdx = Math.min(activeIdx + 1, searchResults.length - 1);
        highlightActive();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIdx = Math.max(activeIdx - 1, 0);
        highlightActive();
      } else if (e.key === "Enter") {
        if (activeIdx >= 0) { e.preventDefault(); chooseResult(activeIdx); }
      } else if (e.key === "Escape") {
        hideResults();
      }
    });
    // blur closes the list; mousedown above prevents a genuine blur when the
    // user is actually clicking a result, so this only fires for real
    // focus-away (tab out, click the map, etc).
    search.addEventListener("blur", () => hideResults());

    function apply() {
      const q = search.value.trim().toLowerCase();
      const feats = geojson.features.filter((f) =>
        (!q || (f.properties.name || "").toLowerCase().includes(q))
        && visibleUnderView(f.properties, state.view));
      map.getSource("stations").setData({ type: "FeatureCollection", features: feats });
      countEl.textContent = `showing ${feats.length} of ${geojson.features.length}`;
      renderResults(feats, q);
    }
    search.addEventListener("input", debounce(apply, 180));
    seg.addEventListener("click", (e) => {
      const btn = e.target.closest(".seg");
      if (!btn) return;
      state.view = btn.dataset.mode;
      Hash.write();
      syncSeg();
      apply();
    });
    syncSeg();                                      // reflect restored state.view
    apply();
    // exposed for the boot/hashchange restore (e.g. reveal a deep-linked dot)
    wireFilters._setView = (view) => { state.view = view; Hash.write(); syncSeg(); apply(); };
  }

  // Restore the deep-linked borehole once filters are wired. If the current
  // view would hide it, fall back to "all" so the link always resolves.
  function restoreSelection(bhKey) {
    const feat = resolveBh(bhKey);
    if (!feat) return;
    if (!visibleUnderView(feat.properties, state.view) && wireFilters._setView) {
      wireFilters._setView("all");
    }
    selectFeature(feat, { flyTo: true });
  }

  // Manual hash navigation (pasted link in the same tab / back-forward).
  // replaceState writes don't fire this, so there's no feedback loop.
  window.addEventListener("hashchange", () => {
    const h = Hash.read();
    // Absent params mean DEFAULTS — Hash.write omits view=active / range=90
    // for clean links, so back-navigating to a bare URL must reset them, not
    // leave the previous non-default state stuck.
    const range = RANGES.includes(h.range) ? h.range : "90";
    const view = VIEWS.includes(h.view) ? h.view : "active";
    const rangeChanged = range !== state.range;
    state.range = range;
    if (view !== state.view && wireFilters._setView) {
      wireFilters._setView(view);
    }
    if (h.rivers !== state.rivers && window.GWC_setRiversLayer) {
      window.GWC_setRiversLayer(h.rivers);
    }
    const feat = resolveBh(h.bh);
    if (feat && feat.properties.station_id !== selectedId) {
      selectFeature(feat, { flyTo: true });
    } else if (feat && rangeChanged) {
      // Same borehole, different range: re-render the open panel so the chart
      // and the active range button match the address bar (cached — instant).
      selectFeature(feat, { flyTo: false });
    }
  });

  // -- aquifer geology (lazy: fetched only when first switched on) ----------
  function wireGeology(meta) {
    const toggle = document.getElementById("geology-toggle");
    const row = document.getElementById("geo-toggle-row");
    const legend = document.getElementById("geology-legend");
    const GEO = CFG.geologyColors || {};

    // colour the legend swatches from config
    document.querySelectorAll("#geology-legend .sq").forEach((el) => {
      el.style.background = GEO[el.dataset.geo] || "#ccc";
    });

    // no geology in this pack → hide the control entirely
    const present = meta.inputs && meta.inputs.geology
      && meta.inputs.geology.status === "ok";
    if (!present) { row.hidden = true; return; }

    let loaded = false;
    toggle.addEventListener("change", async () => {
      if (toggle.checked) {
        if (!loaded) {
          row.classList.add("loading");
          try {
            const gj = await fetch(`${PACK}/geology.geojson`)
              .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
            map.addSource("geology", { type: "geojson", data: gj });
            map.addLayer({
              id: "geology-fill", type: "fill", source: "geology",
              paint: {
                "fill-color": ["match", ["get", "aquifer_class"],
                  "Principal", GEO.Principal, "Secondary", GEO.Secondary,
                  "Low", GEO.Low, "#d7d2c4"],
                "fill-opacity": 0.32,
              },
            }, "stations-ring");           // insert BELOW the dots
            loaded = true;
          } catch (e) {
            toggle.checked = false;
            console.warn("geology layer unavailable", e);
            row.classList.remove("loading");
            return;
          }
          row.classList.remove("loading");
        } else {
          map.setLayoutProperty("geology-fill", "visibility", "visible");
        }
        legend.hidden = false;
      } else {
        if (loaded) map.setLayoutProperty("geology-fill", "visibility", "none");
        legend.hidden = true;
      }
    });
  }

  // -- meta banner + about --------------------------------------------------
  // Honest network-coverage audit (pre-empts "is this just a demo?"). Uses the
  // meta.coverage block when present (structured breakdown + a proportional bar,
  // surfacing the live-feed count); falls back to prose from counts for old packs.
  function renderCoverage(covEl, cov, n) {
    const num = (v) => (v == null ? "?" : Number(v).toLocaleString("en-GB"));
    if (!cov) {
      const published = n.stations || 0, fc = n.with_forecast || 0;
      const catalogued = published + (n.no_data || 0) + (n.excluded || 0);
      covEl.textContent =
        `Coverage: ${published} boreholes with observed data are published here` +
        (catalogued > published ? ` (of ~${catalogued} catalogued — the rest have no usable record yet)` : "") +
        `. ${fc} carry a 14-day probabilistic forecast; the rest show current status only.`;
      return;
    }
    const cat = cov.catalogued || 0;
    const fc = cov.with_forecast || 0;
    const obsOnly = Math.max(0, (cov.observed || 0) - fc);
    const withheld = cov.excluded || 0;
    const nodata = cov.no_data || 0;
    const pct = (v) => (cat > 0 ? (100 * v / cat).toFixed(2) + "%" : "0%");
    const liveClause = cov.live_capable != null
      ? ` Only <b>${num(cov.live_capable)}</b> have a live (real-time) feed; the rest update when the Environment Agency next publishes.`
      : "";
    const seg = (cls, v, label) => v > 0
      ? `<span class="cov-seg ${cls}" style="width:${pct(v)}" title="${label}: ${num(v)} (${pct(v)})"></span>`
      : "";
    covEl.innerHTML =
      `<span class="cov-lead">Coverage of the monitored network</span> — of ` +
      `~${num(cat)} catalogued boreholes, <b>${num(cov.observed)}</b> are published ` +
      `with observed data and <b>${num(fc)}</b> carry a 14-day forecast.` + liveClause +
      ` ${num(nodata)} have no usable record yet` +
      (withheld ? `; ${num(withheld)} are withheld (flagged data quality)` : "") + `.` +
      `<span class="cov-bar" role="img" aria-label="Coverage: ${num(fc)} forecast, ` +
      `${num(obsOnly)} observed only, ${num(withheld)} withheld, ${num(nodata)} no data, ` +
      `of ${num(cat)} catalogued">` +
      seg("forecast", fc, "Forecast") + seg("observed", obsOnly, "Observed only") +
      seg("withheld", withheld, "Withheld (data quality)") + seg("nodata", nodata, "No usable record") +
      `</span>` +
      `<span class="cov-legend">` +
      `<span class="cov-key"><i class="forecast"></i>Forecast</span>` +
      `<span class="cov-key"><i class="observed"></i>Observed only</span>` +
      (withheld ? `<span class="cov-key"><i class="withheld"></i>Withheld</span>` : "") +
      `<span class="cov-key"><i class="nodata"></i>No record yet</span></span>`;
  }

  function showMeta(meta) {
    const run = meta.runs && meta.runs.forecast;
    const asof = run ? new Date(run).toLocaleDateString(
      "en-GB", { day: "numeric", month: "short", year: "numeric" }) : "—";
    const n = meta.counts || {};
    document.getElementById("data-asof").textContent =
      `${n.stations || 0} boreholes · ${n.with_forecast || 0} forecasts · data as of ${asof}`;
    // National one-liner ("Next up" #2): % below normal today, over stations
    // WITH a current status — honest denominator, mirrors the landing hero.
    fetch(`${PACK}/national_history.json`)
      .then((r) => (r.ok ? r.json() : null))
      .then((hist) => {
        if (!hist || !hist.length) return;
        const t = hist[hist.length - 1];
        const withStatus = (t.below || 0) + (t.near || 0) + (t.above || 0);
        if (!withStatus) return;
        const el = document.getElementById("national-note");
        if (el) {
          // Compact pill — the long sentence wrapped into the tagline at mid
          // widths and jumbled the header. Full honest phrasing in the tooltip.
          const pct = Math.round((t.below / withStatus) * 100);
          el.innerHTML = `<span class="dot"></span>${pct}% below normal today`;
          el.title = `${pct}% of the ${withStatus} boreholes with a current ` +
            `reading are below normal for the time of year`;
          el.hidden = false;
        }
      })
      .catch(() => {});
    document.getElementById("about-disclaimer").textContent = meta.disclaimer || "";
    // Honest coverage context (pre-empts "is this just a demo?"): published =
    // boreholes with usable observations; the rest of the catalogue has no
    // usable record yet, and only a subset carries a forecast.
    const covEl = document.getElementById("about-coverage");
    if (covEl) renderCoverage(covEl, meta.coverage, n);
    document.getElementById("about-attribution").textContent =
      (meta.attribution || "") + " Basemap © OpenFreeMap / OpenStreetMap contributors. Map rendering: MapLibre GL JS (BSD-3).";
    showRunBanner(meta);
  }

  // Honest stale/failed-run banner — surfaces a too-old pack or a non-ok input
  // straight from meta (no backend). Daily build → >36h means a run was missed.
  const STALE_HOURS = 36;
  function showRunBanner(meta) {
    const el = document.getElementById("run-banner");
    if (!el) return;
    const msgs = [];
    const gen = meta.generated_at ? new Date(meta.generated_at) : null;
    if (gen && !isNaN(gen.getTime())) {
      const ageH = (Date.now() - gen.getTime()) / 3.6e6;
      if (ageH > STALE_HOURS) {
        const days = Math.floor(ageH / 24);
        const when = gen.toLocaleDateString("en-GB",
          { day: "numeric", month: "short", year: "numeric" });
        msgs.push(`This data may be out of date — last updated ${when}` +
          (days >= 1 ? ` (${days} day${days > 1 ? "s" : ""} ago)` : "") +
          ". The daily build may not have run.");
      }
    }
    const ins = meta.inputs || {};
    const bad = Object.keys(ins).filter(
      (k) => ins[k] && ins[k].status && ins[k].status !== "ok");
    if (bad.length) {
      msgs.push(`Some inputs didn't update this run (${bad.join(", ")}) — ` +
        "parts of the map may show older values.");
    }
    if (!msgs.length) { el.hidden = true; el.textContent = ""; return; }
    el.textContent = "⚠ " + msgs.join("  ");
    el.hidden = false;
  }
  const aboutEl = document.getElementById("about");
  document.getElementById("about-toggle").addEventListener("click", (e) => { e.preventDefault(); aboutEl.hidden = false; });
  document.getElementById("about-close").addEventListener("click", () => (aboutEl.hidden = true));
  aboutEl.addEventListener("click", (e) => { if (e.target === aboutEl) aboutEl.hidden = true; });

  // Mobile: collapse the map legend by default (it otherwise covered ~half the
  // small-screen map). Tap the legend title to toggle; desktop keeps it open.
  (function legendCollapse() {
    const lg = document.getElementById("legend");
    const title = document.getElementById("legend-title");
    if (!lg || !title) return;
    const mq = window.matchMedia("(max-width: 760px)");
    const sync = () => lg.classList.toggle("legend-collapsed", mq.matches);
    sync();
    mq.addEventListener("change", sync);
    title.setAttribute("role", "button");
    title.tabIndex = 0;
    const toggle = () => { if (mq.matches) lg.classList.toggle("legend-collapsed"); };
    title.addEventListener("click", toggle);
    title.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  })();

  // -- helpers --------------------------------------------------------------
  function statusText(p) {
    const lab = { below: "below normal", near: "near normal", above: "above normal" }[p.status]
      || "no current status";
    if (p.station_type === "flow") {
      const river = p.river_name ? escapeHtml(p.river_name) + " · " : "";
      return river + "RiverCast · " + lab;
    }
    const fc = p.has_forecast ? " · forecast available" : "";
    return lab + fc;
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function debounce(fn, ms) {
    let t; return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }
})();

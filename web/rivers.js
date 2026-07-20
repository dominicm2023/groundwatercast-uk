// RiverCast landing page (/rivers/). Mirrors home.js: fetches the same
// stations.geojson the explorer uses, keeps only the flow gauges, and derives
// the live pieces — the "X of Y below normal flow" hero stat (+ Q95 line from
// flow_national_history.json), a stylised snapshot map with the gauges as
// diamonds, notable-gauge cards, and the winterbourne strip. No map tiles —
// fast, SEO-friendly, degrades gracefully to the static copy.
(function () {
  "use strict";
  var PACK = "/pack";

  // Matches scripts/seo_common.slug + web/detail.js slug() so card links resolve.
  function slug(s) {
    return (String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")) || "station";
  }
  var STATUS_LABEL = { below: "below normal", near: "near normal", above: "above normal" };
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function ordinal(n) {
    var v = Math.round(n), t = v % 100;
    if (t >= 11 && t <= 13) return v + "th";
    return v + ({ 1: "st", 2: "nd", 3: "rd" }[v % 10] || "th");
  }

  // Snapshot map: boreholes as faint grey context dots (the borehole↔river
  // link is the product edge — never hide groundwater entirely), flow gauges
  // as status-coloured diamonds on top. Same SDF-diamond trick as the
  // explorer's setupRiverLayer, so shape says "river", colour says status.
  function initMap(gwFeats, flowFeats) {
    var host = document.getElementById("hero-map-gl");
    if (!host || !window.maplibregl || !window.GWC_CONFIG) return;
    var CFG = window.GWC_CONFIG, PAL = CFG.palette;
    var map;
    try {
      map = new maplibregl.Map({
        container: host, style: CFG.basemapStyle,
        center: CFG.center, zoom: 5.1,
        interactive: false, attributionControl: false,
      });
    } catch (e) { return; }
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    // Start the (i) attribution collapsed over the snapshot — one tap expands
    // it (MapLibre's own map-click minimize does exactly this class removal),
    // so the licence text stays a click away without covering the teaser.
    function collapseAttrib() {
      var a = host.querySelector(".maplibregl-ctrl-attrib");
      if (a) a.classList.remove("maplibregl-compact-show");
    }
    // The control's internal update re-adds the class on early map events,
    // so collapse at several settle points; the user's tap still expands it.
    collapseAttrib();
    map.once("idle", collapseAttrib);
    setTimeout(collapseAttrib, 1200);
    map.on("load", function () {
      collapseAttrib();
      var c = document.createElement("canvas");
      c.width = c.height = 48;
      var ctx = c.getContext("2d");
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.moveTo(24, 3); ctx.lineTo(45, 24); ctx.lineTo(24, 45); ctx.lineTo(3, 24);
      ctx.closePath(); ctx.fill();
      map.addImage("river-diamond", ctx.getImageData(0, 0, 48, 48), { sdf: true });

      map.addSource("bores", {
        type: "geojson",
        data: { type: "FeatureCollection", features: gwFeats },
      });
      map.addSource("gauges", {
        type: "geojson",
        data: { type: "FeatureCollection", features: flowFeats },
      });
      // context layer: the groundwater network, dimmed
      map.addLayer({
        id: "bore-dots", type: "circle", source: "bores",
        filter: ["any",
          ["==", ["get", "has_forecast"], true],
          ["!=", ["coalesce", ["get", "status"], "none"], "none"]],
        paint: {
          "circle-color": "#9aa7b4",
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 1.6, 7, 4],
          "circle-opacity": 0.35,
        },
      });
      // All 94 gauges, always — hiding any of them on the rivers front page
      // felt wrong (Dom, 2026-07-20). Overlap is allowed; what stops a
      // cluster reading as one blob is the dark ink OUTLINE on every
      // diamond (SDF halo), which keeps each stacked mark's edge visible.
      // symbol-sort-key: MapLibre draws lower keys first (underneath), so
      // 100 − percentile puts the DRIEST gauges on top of their cluster.
      map.addLayer({
        id: "gauge-diamonds", type: "symbol", source: "gauges",
        layout: {
          "icon-image": "river-diamond",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 4, 0.28, 7, 0.5],
          "icon-allow-overlap": true,
          "symbol-sort-key": ["-", 100, ["coalesce", ["get", "percentile"], 50]],
        },
        paint: {
          "icon-color": ["match", ["coalesce", ["get", "status"], "none"],
            "below", PAL.below, "near", PAL.near, "above", PAL.above, PAL.none],
          "icon-halo-color": "#1a3a5c", "icon-halo-width": 1.5,
        },
      });
      map.fitBounds([[-6.3, 49.9], [1.8, 55.9]], { padding: 14, duration: 0 });
    });
  }

  function renderStat(counts, nFlow) {
    var withStatus = counts.below + counts.near + counts.above;
    var lede = document.getElementById("rivers-lede");
    // tiny sample ⇒ silly headline; keep the static lede (same guard idea as
    // the homepage, lower bar — the flow fleet is ~a tenth the borehole fleet)
    if (withStatus < 20) return;
    var stat = document.getElementById("rivers-stat");
    document.getElementById("rstat-num").textContent =
      counts.below + " of " + withStatus;
    stat.hidden = false;
    if (lede) {
      lede.innerHTML = "Daily low-flow forecasts for <b>" + nFlow.toLocaleString() +
        "</b> river gauges on England's chalk streams and winterbournes — " +
        "every one past its own forecast-skill gate. Today <b>" + counts.below +
        "</b> of the " + withStatus.toLocaleString() +
        " with a fresh reading sit <b>below normal flow</b> for the season.";
    }
  }

  // "N gauges below their Q95 low-flow proxy today" — from the pack's flow
  // national history (latest row). Also feeds the sparkline once ≥7 days of
  // history have accrued (same guard as the homepage's national trend).
  function renderQ95AndTrend() {
    fetch(PACK + "/flow_national_history.json")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (hist) {
        if (!hist || !hist.length) return;
        var last = hist[hist.length - 1];
        var q95line = document.getElementById("q95-line");
        if (q95line && last.n_below_q95_now != null) {
          q95line.innerHTML = "<b>" + last.n_below_q95_now + "</b> " +
            (last.n_below_q95_now === 1 ? "gauge is" : "gauges are") +
            " below their Q95 low-flow proxy right now.";
          q95line.hidden = false;
        }
        if (hist.length < 7) return;              // wait for a real week
        var pcts = hist.map(function (t) {
          var w = (t.below || 0) + (t.near || 0) + (t.above || 0);
          return w ? (t.below / w) * 100 : null;
        }).filter(function (v) { return v != null; });
        if (pcts.length < 7) return;
        pcts = pcts.slice(-90);
        var lo = Math.min.apply(null, pcts), hi = Math.max.apply(null, pcts);
        if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
        var W = 120, H = 26, n = pcts.length;
        var pts = pcts.map(function (v, i) {
          return (i / (n - 1) * W).toFixed(1) + "," +
            ((1 - (v - lo) / (hi - lo)) * (H - 4) + 2).toFixed(1);
        }).join(" ");
        var host = document.getElementById("rivers-stat");
        if (!host || host.hidden) return;
        var span = document.createElement("span");
        span.className = "stat-spark";
        span.title = "% of gauges below normal flow, last " + n + " days";
        span.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H + '">' +
          '<polyline points="' + pts + '" fill="none" stroke="#d4a017" stroke-width="2"/></svg>';
        host.appendChild(span);
      })
      .catch(function () {});
  }

  var TREND_WORD = { rising: "rising", falling: "falling", stable: "holding steady" };
  function gaugeSummary(p) {
    var bits = [];
    if (p.river_name) bits.push(esc(p.river_name));
    if (p.winterbourne) bits.push("winterbourne");
    if (p.trend && TREND_WORD[p.trend]) bits.push(TREND_WORD[p.trend]);
    if (p.percentile != null) bits.push("around the " + ordinal(p.percentile) + " percentile for the month");
    return bits.join(" · ") || "See the full forecast on its own page.";
  }
  function chip(p) {
    var s = p.status;
    if (!s) return '<span class="chip none">no current status</span>';
    var pct = p.percentile != null ? ' <span class="pct">' + ordinal(p.percentile) + "</span>" : "";
    return '<span class="chip ' + s + '">' + STATUS_LABEL[s] + pct + "</span>";
  }
  function gaugeHref(p) {
    // The pack's canonical slug (collision-suffixed) — never re-derived.
    return "/r/" + (p.slug || slug(p.name || p.station_id)) + "/";
  }

  function renderNotable(flowFeats) {
    var host = document.getElementById("notable-gauges");
    if (!host) return;
    var pool = flowFeats.filter(function (f) {
      var p = f.properties; return p.status && p.percentile != null;
    });
    pool.sort(function (a, b) { return a.properties.percentile - b.properties.percentile; });
    var picks = [];
    if (pool.length) picks.push(pool[0]);
    if (pool.length > 1) picks.push(pool[1]);
    if (pool.length > 2) picks.push(pool[pool.length - 1]);
    if (!picks.length) { host.innerHTML = '<span class="loading">No current readings available.</span>'; return; }
    host.innerHTML = picks.map(function (f) {
      var p = f.properties;
      return '<a class="card" href="' + gaugeHref(p) + '">' +
        '<div class="card-head"><span class="nm">' + esc(p.name || "Gauge") + "</span>" +
        chip(p) + "</div>" +
        '<div class="mini">' + gaugeSummary(p) + "</div></a>";
    }).join("");
  }

  // Winterbourne strip: which of the flagged winterbournes are flowing today?
  // `level` on a flow feature is the latest observed flow in m³/s — at (or
  // within a whisker of) zero the bed is dry. A flowing/dry CLAIM needs a
  // reading fresh enough to be "today": a gauge whose feed died while the
  // bed was dry must not read "dry at the gauge" months later, so anything
  // older than a week degrades to "no fresh reading" (and doesn't count in
  // the intro's "today N are dry"). Pure presentation of pack data.
  function renderWinterbournes(flowFeats) {
    var strip = document.getElementById("wb-strip");
    var intro = document.getElementById("wb-intro");
    if (!strip) return;
    var wbs = flowFeats.filter(function (f) { return f.properties.winterbourne; });
    if (!wbs.length) return;                       // keep the static copy
    function freshLevel(p) {
      if (p.level == null) return null;
      if (p.obs_age_days == null || p.obs_age_days > 7) return null;
      return p.level;
    }
    var dry = wbs.filter(function (f) {
      var lv = freshLevel(f.properties);
      return lv != null && lv <= 0.001;
    });
    if (intro) {
      intro.innerHTML = "Winterbournes are chalk streams that dry by design — " +
        "flowing only when the aquifer beneath them is high enough. " +
        "<b>" + wbs.length + "</b> of our gauged rivers are winterbournes; " +
        "today <b>" + dry.length + "</b> " + (dry.length === 1 ? "is" : "are") +
        " dry at the gauge. The question locals actually ask is not " +
        "“how high is it?” but “when will it start — or stop?”";
    }
    wbs.sort(function (a, b) {
      var la = freshLevel(a.properties), lb = freshLevel(b.properties);
      return (la == null ? 1e9 : la) - (lb == null ? 1e9 : lb);  // driest first
    });
    strip.innerHTML = wbs.slice(0, 6).map(function (f) {
      var p = f.properties;
      var lv = freshLevel(p);
      var state = lv == null ? "no fresh reading"
        : lv <= 0.001 ? "dry at the gauge" : "flowing";
      var cls = lv == null ? "none" : lv <= 0.001 ? "dry" : "flowing";
      return '<a class="wb-pill ' + cls + '" href="' + gaugeHref(p) + '">' +
        '<span class="wb-name">' + esc(p.name) + "</span>" +
        '<span class="wb-state">' + state + "</span></a>";
    }).join("");
    strip.hidden = false;
  }

  fetch(PACK + "/stations.geojson")
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (gj) {
      var feats = (gj && gj.features) || [];
      var flowFeats = feats.filter(function (f) {
        return ((f.properties || {}).station_type) === "flow";
      });
      var gwFeats = feats.filter(function (f) {
        return ((f.properties || {}).station_type) !== "flow";
      });
      var counts = { below: 0, near: 0, above: 0 };
      for (var i = 0; i < flowFeats.length; i++) {
        var p = flowFeats[i].properties || {};
        if (p.status && counts[p.status] != null) counts[p.status]++;
      }
      renderStat(counts, flowFeats.length);
      initMap(gwFeats, flowFeats);
      renderNotable(flowFeats);
      renderWinterbournes(flowFeats);
      renderQ95AndTrend();
      var lab = document.getElementById("hero-map-lab");
      if (lab) {
        lab.textContent = flowFeats.length.toLocaleString() +
          " river gauges with a daily low-flow forecast · click to open the interactive map.";
      }
    })
    .catch(function () {
      var host = document.getElementById("notable-gauges");
      if (host) host.innerHTML = '<span class="loading">Couldn’t load current readings — ' +
        '<a href="/explorer/#rivers=1">open the map</a> instead.</span>';
    });
})();

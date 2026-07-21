// GroundwaterCast landing page. Fetches the same stations.geojson the map uses
// and derives three live pieces from it: the national below/near/above split
// (the hero stat), a stylised SVG snapshot map, and a handful of "notable"
// borehole cards. No map tiles — fast, SEO-friendly, degrades gracefully.
(function () {
  "use strict";
  // Old map deep-links were served from "/" as #bh=… ; the map now lives at
  // /explorer/. Forward those so shared/bookmarked links keep working.
  if (location.hash && /(?:^|[#&])(bh|view|range)=/.test(location.hash)) {
    location.replace("/explorer/" + location.hash);
    return;
  }
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

  // A small, non-interactive MapLibre snapshot reusing the explorer's own
  // basemap + palette, with the boreholes as a status-coloured circle layer.
  // interactive:false means it reads as a picture; the .map-cover anchor sends
  // any click through to the full /explorer/.
  function initMap(gj) {
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
      map.addSource("bores", { type: "geojson", data: gj });
      // Mirror the explorer's default "active" view: show only boreholes with a
      // forecast OR a fresh status (drops the ~565 pure-stale grey dots that
      // otherwise wash the snapshot out), and mark forecast boreholes with the
      // same navy ring the interactive map uses. RiverCast flow gauges are
      // excluded — this is the GROUNDWATER snapshot (94 gauges rendered as
      // anonymous circles just cluttered it); rivers have their own teaser
      // card and /rivers/ hero map.
      var notFlow = ["!=", ["get", "station_type"], "flow"];
      var activeFilter = ["all", notFlow, ["any",
        ["==", ["get", "has_forecast"], true],
        ["!=", ["coalesce", ["get", "status"], "none"], "none"]]];
      map.addLayer({
        id: "bore-ring", type: "circle", source: "bores",
        filter: ["all", notFlow, ["==", ["get", "has_forecast"], true]],
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 4.2, 7, 9],
          "circle-color": "rgba(0,0,0,0)",
          "circle-stroke-color": "#1a3a5c", "circle-stroke-width": 1.3,
          "circle-stroke-opacity": 0.7,
        },
      });
      map.addLayer({
        id: "bore-dots", type: "circle", source: "bores", filter: activeFilter,
        paint: {
          "circle-color": ["match", ["get", "status"],
            "below", PAL.below, "near", PAL.near, "above", PAL.above, PAL.none],
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2.4, 7, 6],
          "circle-stroke-width": 0.5, "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.95,
        },
      });
      map.fitBounds([[-6.3, 49.9], [1.8, 55.9]], { padding: 14, duration: 0 });
    });
  }

  function renderStat(counts, nForecast) {
    var withStatus = counts.below + counts.near + counts.above;
    var lede = document.getElementById("national-lede");
    // a tiny sample makes the headline silly ("43% of the 7 boreholes") —
    // keep the static lede until the denominator is meaningful
    if (withStatus < 30) return;
    var pctBelow = Math.round((counts.below / withStatus) * 100);
    var stat = document.getElementById("national-stat");
    document.getElementById("stat-num").textContent = pctBelow + "%";
    stat.hidden = false;
    if (lede) {
      // Lead with reach (the forecast count), then the live signal. The old copy
      // led with `withStatus` (~288) — the smallest of our three numbers — which
      // read as the tool's scope rather than "boreholes with a fresh reading today".
      lede.innerHTML = "Daily probabilistic forecasts for <b>" +
        nForecast.toLocaleString() + "</b> boreholes across England, refreshed every " +
        "morning on open data. Of the " + withStatus.toLocaleString() +
        " with a reading fresh enough to place today, <b>" + pctBelow +
        "%</b> sit <b>below normal</b> for the season.";
    }
  }

  // A short, human, non-technical line — the map headline is too long/jargony
  // for a card. Compose from aquifer + trend + where it sits for the month.
  var TREND_WORD = { rising: "rising", falling: "falling", stable: "holding steady" };
  function statusSummary(p) {
    var bits = [];
    if (p.aquifer_designation) bits.push(esc(p.aquifer_designation) + " aquifer");
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

  function renderNotable(feats) {
    var host = document.getElementById("notable-cards");
    if (!host) return;
    // Prefer boreholes that tell a story: has a forecast + a current status,
    // most extreme first (lowest percentile = most notably below normal),
    // capped to a diverse-ish few. Fall back to any with a status.
    var withF = feats.filter(function (f) {
      var p = f.properties; return p.status && p.has_forecast && p.percentile != null;
    });
    var pool = withF.length >= 3 ? withF : feats.filter(function (f) {
      return f.properties.status && f.properties.percentile != null;
    });
    pool.sort(function (a, b) { return a.properties.percentile - b.properties.percentile; });
    // take the two lowest (driest) + the single highest (wettest) for contrast
    var picks = [];
    if (pool.length) picks.push(pool[0]);
    if (pool.length > 1) picks.push(pool[1]);
    if (pool.length > 2) picks.push(pool[pool.length - 1]);
    if (!picks.length) { host.innerHTML = '<span class="loading">No current readings stand out today — ' +
      '<a href="/explorer/">open the map</a>.</span>'; return; }
    host.classList.remove("loading");
    host.innerHTML = picks.map(function (f) {
      var p = f.properties;
      // The pack's canonical slug (collision-suffixed) — never re-derive from
      // the name, or duplicate-named stations link to the wrong page.
      var href = "/b/" + (p.slug || slug(p.name || p.station_id)) + "/";
      return '<a class="card" href="' + href + '">' +
        '<div class="card-head"><span class="nm">' + esc(p.name || "Borehole") + "</span>" +
        chip(p) + "</div>" +
        '<div class="mini">' + statusSummary(p) + "</div></a>";
    }).join("");
  }

  // Tiny "% below normal" sparkline in the hero once >=7 days of national
  // history have accrued (pack/national_history.json, one row per build day).
  function renderTrend() {
    fetch(PACK + "/national_history.json")
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (hist) {
        if (!hist || hist.length < 7) return;   // wait for a real week of data
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
        var host = document.getElementById("national-stat");
        if (!host || host.hidden) return;
        var span = document.createElement("span");
        span.className = "stat-spark";
        span.title = "% of boreholes below normal, last " + n + " days";
        span.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '" width="' + W + '" height="' + H + '">' +
          '<polyline points="' + pts + '" fill="none" stroke="#d4a017" stroke-width="2"/></svg>';
        host.appendChild(span);
      })
      .catch(function () {});
  }

  // RiverCast teaser — fill in the live pilot gauge count; hide the card
  // entirely if the pack hasn't published any flow gauges yet (a host that
  // hasn't caught up on the flow subsystem must not show a dead promise).
  function renderRiverTeaser(feats) {
    var el = document.getElementById("river-teaser");
    var mini = document.getElementById("river-teaser-mini");
    if (!el) return;
    var nFlow = 0;
    for (var i = 0; i < feats.length; i++) {
      if (feats[i].properties && feats[i].properties.station_type === "flow") nFlow++;
    }
    if (!nFlow) { el.hidden = true; return; }
    if (mini) {
      mini.textContent = "Daily low-flow outlooks for " + nFlow.toLocaleString() +
        " gauges on England's chalk streams & winterbournes — every one past " +
        "its own forecast-skill gate.";
    }
    el.hidden = false;
  }

  fetch(PACK + "/stations.geojson")
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (gj) {
      var feats = (gj && gj.features) || [];
      // The hero stat and "notable" cards are BOREHOLE statistics — RiverCast
      // flow gauges share the geojson (station_type === "flow") but must not
      // count as "boreholes" nor be featured with a /b/ link (flow stations
      // get no stub page in v1 — build_seo_stubs skips them, so the link
      // would 404). The river teaser is the one consumer of the flow rows.
      var gwFeats = feats.filter(function (f) {
        return ((f.properties || {}).station_type) !== "flow";
      });
      var counts = { below: 0, near: 0, above: 0 };
      var nForecast = 0;
      for (var i = 0; i < gwFeats.length; i++) {
        var p = gwFeats[i].properties || {};
        if (p.status && counts[p.status] != null) counts[p.status]++;
        if (p.has_forecast) nForecast++;
      }
      renderStat(counts, nForecast);
      initMap(gj);
      renderNotable(gwFeats);
      renderRiverTeaser(feats);
      renderTrend();
      var lab = document.getElementById("hero-map-lab");
      if (lab) {
        lab.textContent = nForecast.toLocaleString() +
          " boreholes with a daily forecast · click to open the interactive map.";
      }
    })
    .catch(function () {
      var host = document.getElementById("notable-cards");
      if (host) host.innerHTML = '<span class="loading">Couldn’t load current readings — ' +
        '<a href="/explorer/">open the map</a> instead.</span>';
    });
})();

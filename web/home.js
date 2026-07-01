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

  // England bbox → SVG. Simple equirectangular; good enough for a decorative
  // snapshot (latitude compression is barely visible over England's span).
  var BBOX = { lonMin: -6.4, lonMax: 1.9, latMin: 49.9, latMax: 55.9 };
  function project(lon, lat, w, h, pad) {
    var x = (lon - BBOX.lonMin) / (BBOX.lonMax - BBOX.lonMin);
    var y = 1 - (lat - BBOX.latMin) / (BBOX.latMax - BBOX.latMin);
    return [pad + x * (w - 2 * pad), pad + y * (h - 2 * pad)];
  }

  function drawMap(feats) {
    var svg = document.getElementById("hero-map-svg");
    if (!svg) return;
    var W = 300, H = 360, PAD = 18;
    // draw 'none' first (background), then coloured on top so status pops
    var order = { none: 0, near: 1, above: 2, below: 3 };
    var pts = feats.slice().filter(function (f) {
      var g = f.geometry; return g && g.type === "Point" && g.coordinates;
    }).sort(function (a, b) {
      return (order[a.properties.status] || 0) - (order[b.properties.status] || 0);
    });
    var frag = "";
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i].properties, c = pts[i].geometry.coordinates;
      var xy = project(c[0], c[1], W, H, PAD);
      var cls = p.status ? ("dot-" + p.status) : "dot-none";
      var r = p.status ? 2.6 : 1.7;
      frag += '<circle class="' + cls + '" cx="' + xy[0].toFixed(1) + '" cy="' +
        xy[1].toFixed(1) + '" r="' + r + '" fill-opacity="' + (p.status ? 0.92 : 0.5) + '"/>';
    }
    svg.innerHTML = frag;
  }

  function renderStat(counts, total) {
    var withStatus = counts.below + counts.near + counts.above;
    var lede = document.getElementById("national-lede");
    if (!withStatus) return;
    var pctBelow = Math.round((counts.below / withStatus) * 100);
    var stat = document.getElementById("national-stat");
    document.getElementById("stat-num").textContent = pctBelow + "%";
    // pick the colour/word for whichever share we lead with (below)
    stat.hidden = false;
    if (lede) {
      lede.innerHTML = "Today, <b>" + pctBelow + "%</b> of the " + withStatus +
        " boreholes with a current reading are <b>below normal</b> for the time of year — " +
        "each forecast daily, on open data.";
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
    if (!picks.length) { host.innerHTML = '<span class="loading">No current readings available.</span>'; return; }
    host.classList.remove("loading");
    host.innerHTML = picks.map(function (f) {
      var p = f.properties;
      var href = "/b/" + slug(p.name || p.station_id) + "/";
      return '<a class="card" href="' + href + '">' +
        '<div class="card-head"><span class="nm">' + esc(p.name || "Borehole") + "</span>" +
        chip(p) + "</div>" +
        '<div class="mini">' + statusSummary(p) + "</div></a>";
    }).join("");
  }

  fetch(PACK + "/stations.geojson")
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (gj) {
      var feats = (gj && gj.features) || [];
      var counts = { below: 0, near: 0, above: 0 };
      for (var i = 0; i < feats.length; i++) {
        var s = feats[i].properties && feats[i].properties.status;
        if (s && counts[s] != null) counts[s]++;
      }
      renderStat(counts, feats.length);
      drawMap(feats);
      renderNotable(feats);
      var lab = document.getElementById("hero-map-lab");
      if (lab) {
        var ws = counts.below + counts.near + counts.above;
        lab.textContent = ws + " boreholes with a current reading · click to open the interactive map.";
      }
    })
    .catch(function () {
      var host = document.getElementById("notable-cards");
      if (host) host.innerHTML = '<span class="loading">Couldn’t load current readings — ' +
        '<a href="/explorer/">open the map</a> instead.</span>';
    });
})();

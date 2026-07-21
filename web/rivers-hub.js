// River-hub mini-map. Progressive enhancement: the page ships a baked static
// SVG of the river (crawlable, zero-JS) inside #rv-mapfallback; when MapLibre
// and the geometry blob (window.RV_HUB, written inline by build_seo_stubs) are
// present, this replaces it with a real interactive basemap — the river line,
// its gauges (diamonds → /r/) and feeding boreholes (rings → /b/) over the same
// OpenFreeMap positron basemap the /rivers/ landing uses. If anything is
// missing, the SVG fallback simply stays.
(function () {
  "use strict";
  var H = window.RV_HUB;
  var host = document.getElementById("rv-hubmap");
  if (!H || !host || !window.maplibregl || !window.GWC_CONFIG) return;
  var CFG = window.GWC_CONFIG, PAL = CFG.palette || {};

  function colOf(s) {
    return s === "below" ? PAL.below : s === "near" ? PAL.near
      : s === "above" ? PAL.above : (PAL.none || "#9aa7b4");
  }
  function fc(arr) {
    return {
      type: "FeatureCollection",
      features: (arr || []).map(function (p) {
        return {
          type: "Feature",
          geometry: { type: "Point", coordinates: [p.lon, p.lat] },
          properties: { slug: p.slug, name: p.name, status: p.status || "none" },
        };
      }),
    };
  }

  var map;
  try {
    map = new maplibregl.Map({
      container: host,
      style: CFG.basemapStyle,
      bounds: H.bounds,
      fitBoundsOptions: { padding: 30 },
      scrollZoom: false,          // don't hijack page scroll; +/- controls zoom
      dragRotate: false,
      pitchWithRotate: false,
      attributionControl: false,
    });
  } catch (e) { return; }

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
  map.touchZoomRotate && map.touchZoomRotate.disableRotation();

  // Start the bottom-right attribution collapsed to the (i) button — MapLibre
  // renders the compact control expanded, so drop the show class at a few
  // settle points (the user's tap still expands it). Mirrors rivers.js.
  function collapseAttrib() {
    var a = host.querySelector(".maplibregl-ctrl-attrib");
    if (a) a.classList.remove("maplibregl-compact-show");
  }
  collapseAttrib();
  map.once("idle", collapseAttrib);
  setTimeout(collapseAttrib, 1200);

  // The real basemap is coming up — drop the paper hatch + hide the SVG fallback.
  var fig = host.closest(".rv-map");
  if (fig) fig.classList.add("rv-mapready");

  map.on("load", function () {
    var fb = document.getElementById("rv-mapfallback");
    if (fb) fb.style.display = "none";

    map.addSource("river", {
      type: "geojson",
      data: { type: "Feature", geometry: { type: "MultiLineString", coordinates: H.segs || [] } },
    });
    map.addLayer({
      id: "river-case", type: "line", source: "river",
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": "#1a3a5c", "line-opacity": 0.16, "line-width": 6 },
    });
    map.addLayer({
      id: "river-line", type: "line", source: "river",
      layout: { "line-join": "round", "line-cap": "round" },
      paint: { "line-color": "#2b6ea3", "line-width": 2.4 },
    });

    // feeding boreholes — hollow rings, status-coloured edge
    map.addSource("bores", { type: "geojson", data: fc(H.boreholes) });
    map.addLayer({
      id: "bore-rings", type: "circle", source: "bores",
      paint: {
        "circle-radius": 4.2, "circle-color": "#ffffff", "circle-stroke-width": 2,
        "circle-stroke-color": ["match", ["get", "status"],
          "below", PAL.below || "#c9a227", "near", PAL.near || "#8a949e",
          "above", PAL.above || "#2b6ea3", "#9aa7b4"],
      },
    });

    // gauges — status-coloured diamonds with a dark ink halo (matches landing)
    var c = document.createElement("canvas");
    c.width = c.height = 48;
    var ctx = c.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.beginPath();
    ctx.moveTo(24, 3); ctx.lineTo(45, 24); ctx.lineTo(24, 45); ctx.lineTo(3, 24);
    ctx.closePath(); ctx.fill();
    map.addImage("hub-diamond", ctx.getImageData(0, 0, 48, 48), { sdf: true });
    map.addSource("gauges", { type: "geojson", data: fc(H.gauges) });
    map.addLayer({
      id: "gauge-diamonds", type: "symbol", source: "gauges",
      layout: {
        "icon-image": "hub-diamond", "icon-size": 0.5, "icon-allow-overlap": true,
        "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"],
        "text-size": 11, "text-offset": [0, 1.1], "text-anchor": "top",
        "text-optional": true, "text-allow-overlap": false,
      },
      paint: {
        "icon-color": ["match", ["get", "status"],
          "below", PAL.below || "#c9a227", "near", PAL.near || "#8a949e",
          "above", PAL.above || "#2b6ea3", (PAL.none || "#9aa7b4")],
        "icon-halo-color": "#1a3a5c", "icon-halo-width": 1.5,
        "text-color": "#3a4a5a", "text-halo-color": "#ffffff", "text-halo-width": 1.3,
      },
    });

    function go(prefix) {
      return function (e) {
        var s = e.features && e.features[0] && e.features[0].properties.slug;
        if (s) window.location.href = prefix + s + "/";
      };
    }
    map.on("click", "gauge-diamonds", go("/r/"));
    map.on("click", "bore-rings", go("/b/"));
    ["gauge-diamonds", "bore-rings"].forEach(function (L) {
      map.on("mouseenter", L, function () { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", L, function () { map.getCanvas().style.cursor = ""; });
    });
  });
})();

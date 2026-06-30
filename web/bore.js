// Per-borehole page bootstrap. Renders the explorer's interactive detail
// (GWC_DETAIL.render) into #detail-body from the borehole's pack JSON — REUSING
// detail.js/charts.js/watchlist.js/ladders.js exactly as the map's side panel
// does (same #detail-body container id, same bind* calls), so the page and the
// panel never diverge. The crawler-facing content (head, banner, observations)
// is already baked into the static HTML, so this only enriches for JS users.
(function () {
  "use strict";
  var el = document.getElementById("detail-body");
  if (!el || !window.GWC_DETAIL) return;
  var id = el.getAttribute("data-station");
  if (!id) return;
  var base = (window.GWC_CONFIG && window.GWC_CONFIG.packBase) || "/pack";

  function fail(msg) {
    el.innerHTML = '<p class="caption">' + (msg || "Could not load the forecast detail.") +
      ' You can still view the <a href="' + base + "/stations/" + id + '.json">raw data</a>.</p>';
  }

  Promise.all([
    fetch(base + "/stations/" + id + ".json").then(function (r) {
      if (!r.ok) throw new Error("detail " + r.status); return r.json();
    }),
    fetch(base + "/meta.json").then(function (r) { return r.ok ? r.json() : {}; })
      .catch(function () { return {}; }),
  ]).then(function (res) {
    var detail = res[0], meta = res[1] || {};
    // expose the cache getter the watchlist's refreshPinControl reads
    if (!window.GWC_getDetail) {
      window.GWC_getDetail = function (x) { return x === id ? detail : null; };
    }
    el.innerHTML = window.GWC_DETAIL.render(detail, meta);
    window.GWC_DETAIL.bindFan(el, detail);
    window.GWC_DETAIL.bindData(el);
    if (window.GWC_WATCH && window.GWC_WATCH.bindDetail) window.GWC_WATCH.bindDetail(el, detail);
    if (window.GWC_LADDER && window.GWC_LADDER.bindDetail) window.GWC_LADDER.bindDetail(el, detail);
  }).catch(function () { fail(); });
})();

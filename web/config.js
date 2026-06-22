// GroundwaterCast explorer — deploy-time configuration.
//
// Point `packBase` at the published artifact pack (docs/artifact_contract.md).
// The local preview server (scripts/serve_explorer.py) serves it at /pack.
// On a VPS, serve web/ at / and outputs/pack at /pack (see web/README.md).
window.GWC_CONFIG = {
  packBase: "/pack",

  // Free, no-API-key, commercial-OK vector basemap (light/muted under the
  // data overlay). Self-hostable later; swap for any MapLibre style URL.
  basemapStyle: "https://tiles.openfreemap.org/styles/positron",

  // England-centred initial view.
  center: [-1.2, 52.7],
  zoom: 5.6,
  maxZoom: 12,
  minZoom: 4,

  // Status-vs-normal palette — mirrors src/dashboard/status.py STATUS_COLOR
  // so the web and the Streamlit app speak the same vocabulary.
  palette: {
    below: "#d4a017",   // amber — below normal
    near: "#8a8a8a",    // mid-grey — near normal (distinct from the paler 'none')
    above: "#1f77b4",   // blue  — above normal
    none: "#cfcfcf",    // light grey — no current status (stale / no normals)
  },

  // Indicative aquifer-potential fill colours (from BGS 625k bedrock, OGL;
  // muted earth/green tones, off the status-dot hues). Faded beneath the dots.
  geologyColors: {
    "Principal": "#4c9f8a",
    "Secondary": "#9ec79b",
    "Low": "#d7d2c4",
  },
};

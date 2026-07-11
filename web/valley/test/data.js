// River Test 3-D concept — illustrative geometry.
//
// The watercourse courses, village names and stratigraphy are real geography
// sketched from the map; the coordinates, elevations, borehole sites and
// water-table behaviour are HAND-AUTHORED to look right at valley scale.
// Nothing here is survey data. The intended production path is to replace
// `BOREHOLES` with stations from the published artifact pack (station lat/lon,
// datum, dip depths) and the terrain with an open DEM (e.g. OS Terrain 50).
//
// Conventions:
//   waypoints: [lon, lat, bedElevation_mOD]
//   headPerennial / headDrought / headWinter: fraction of the way down the
//     course (0 = source, 1 = mouth) where the flowing river starts at
//     normal / drought / winter-high groundwater. Chalk winterbournes are
//     expressed by a large perennial→drought spread (the Bourne Rill dries
//     almost completely in a hard autumn; the lower Test never does).

window.TEST3D_DATA = {
  // Domain of the block model (WGS84). Roughly Ashe → Romsey.
  bounds: { lonMin: -1.66, lonMax: -1.20, latMin: 50.87, latMax: 51.30 },

  watercourses: [
    {
      name: "River Test",
      main: true,
      headPerennial: 0.10, headDrought: 0.30, headWinter: 0.01,
      waypoints: [
        [-1.293, 51.259, 92],   // Ashe — the source springs
        [-1.310, 51.246, 84],   // Overton
        [-1.331, 51.238, 76],   // Laverstoke
        [-1.339, 51.229, 70],   // Whitchurch
        [-1.353, 51.217, 63],   // Tufton
        [-1.368, 51.211, 59],   // below Hurstbourne Priors
        [-1.395, 51.205, 55],   // Longparish
        [-1.420, 51.192, 51],   // Harewood / Middleton
        [-1.437, 51.174, 47],   // Wherwell
        [-1.440, 51.156, 43],   // Chilbolton
        [-1.455, 51.140, 39],   // Testcombe — Anton joins
        [-1.465, 51.130, 36],   // Leckford
        [-1.490, 51.113, 31],   // Stockbridge
        [-1.503, 51.096, 27],   // Houghton
        [-1.512, 51.070, 22],   // Horsebridge — Wallop Brook joins
        [-1.528, 51.048, 17],   // Mottisfont
        [-1.532, 51.024, 13],   // Kimbridge — the Dun joins
        [-1.516, 51.005, 11],   // Timsbury
        [-1.499, 50.988, 8],    // Romsey
      ],
    },
    {
      name: "Bourne Rill",
      winterbourne: true,
      headPerennial: 0.45, headDrought: 0.98, headWinter: 0.02,
      waypoints: [
        [-1.408, 51.268, 82],   // upper Bourne valley
        [-1.390, 51.257, 76],   // St Mary Bourne
        [-1.381, 51.244, 68],   // Stoke / lower Bourne
        [-1.372, 51.232, 63],   // Hurstbourne Priors
        [-1.366, 51.215, 59],   // joins the Test
      ],
    },
    {
      name: "River Dever",
      headPerennial: 0.30, headDrought: 0.70, headWinter: 0.03,
      waypoints: [
        [-1.290, 51.148, 78],   // Micheldever
        [-1.320, 51.152, 70],   // Stoke Charity
        [-1.339, 51.157, 65],   // Sutton Scotney
        [-1.372, 51.166, 60],   // Wonston / Newton Stacey
        [-1.408, 51.184, 53],   // Bullington — joins the Test
      ],
    },
    {
      name: "River Anton",
      headPerennial: 0.18, headDrought: 0.45, headWinter: 0.02,
      waypoints: [
        [-1.495, 51.222, 72],   // Andover
        [-1.480, 51.198, 62],   // Upper Clatford
        [-1.472, 51.180, 55],   // Goodworth Clatford
        [-1.460, 51.157, 47],   // Fullerton
        [-1.455, 51.141, 40],   // joins the Test at Testcombe
      ],
    },
    {
      name: "Wallop Brook",
      winterbourne: true,
      headPerennial: 0.35, headDrought: 0.85, headWinter: 0.03,
      waypoints: [
        [-1.583, 51.148, 85],   // Over Wallop
        [-1.566, 51.131, 72],   // Middle Wallop
        [-1.551, 51.117, 62],   // Nether Wallop
        [-1.549, 51.094, 45],   // Broughton
        [-1.514, 51.072, 23],   // joins the Test at Bossington
      ],
    },
    {
      name: "River Dun",
      headPerennial: 0.12, headDrought: 0.35, headWinter: 0.02,
      waypoints: [
        [-1.612, 51.043, 45],   // toward West Dean
        [-1.576, 51.033, 34],   // Lockerley
        [-1.549, 51.026, 22],   // Dunbridge
        [-1.533, 51.023, 14],   // joins the Test at Kimbridge
      ],
    },
  ],

  // Illustrative observation boreholes — realistic siting (interfluve chalk
  // sites are deep with big seasonal swings; valley-floor sites are shallow
  // and steady), fictitious values. `swingBias` staggers when each hole's
  // status flips below/near/above as the season slider moves.
  boreholes: [
    { name: "Overton Down",    lon: -1.318, lat: 51.263, depth: 85, swingBias:  0.10 },
    { name: "St Mary Bourne",  lon: -1.396, lat: 51.253, depth: 45, swingBias: -0.15 },
    { name: "Longparish Down", lon: -1.372, lat: 51.188, depth: 70, swingBias:  0.05 },
    { name: "Chilbolton Down", lon: -1.412, lat: 51.146, depth: 75, swingBias:  0.20 },
    { name: "Anton Valley",    lon: -1.478, lat: 51.192, depth: 48, swingBias: -0.05 },
    { name: "Stockbridge",     lon: -1.484, lat: 51.116, depth: 32, swingBias: -0.20 },
    { name: "Broughton Down",  lon: -1.581, lat: 51.101, depth: 90, swingBias:  0.15 },
    { name: "Mottisfont",      lon: -1.535, lat: 51.041, depth: 38, swingBias: -0.10 },
    { name: "Romsey",          lon: -1.492, lat: 50.995, depth: 55, swingBias:  0.00 },
  ],

  // Block-side stratigraphy, top-down. Thicknesses in m; the stack dips
  // gently ESE like the real Chalk Group here. Colours are muted so the
  // water table and boreholes carry the scene.
  strata: [
    { name: "Seaford & Newhaven Chalk", color: 0xf1efe6, thickness: 55 },
    { name: "Lewes Nodular Chalk",      color: 0xe4e1d3, thickness: 45 },
    { name: "New Pit & Holywell Chalk", color: 0xd6d3c2, thickness: 50 },
    { name: "Zig Zag & West Melbury",   color: 0xc2bfae, thickness: 40 },
    { name: "Upper Greensand / Gault",  color: 0x9a9484, thickness: 999 },
  ],

  // Status-vs-normal palette — mirrors web/config.js so the concept speaks
  // the product vocabulary.
  palette: { below: 0xd4a017, near: 0x8a8a8a, above: 0x1f77b4 },
};

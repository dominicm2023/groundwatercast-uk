# The River Test in 3-D — "beneath the valley"

A glass-block museum model of the Test valley, **source to sea** (Ashe →
Southampton Water), where you can see through the ground: boreholes running
down through the chalk, the water table as a translucent surface, rivers
painted onto the landscape wherever that water table stands above the ground.
Serves at `/valley/test/`.

**One timeline, measurements first.** The scrubber spans 156 observed weeks →
the *today* tick → the published 14-day fan → the seasonal months. Left of the
tick everything is EA measurements: weekly borehole levels (~80 stations),
weekly rainfall (6 gauges) falling on the block, and gauged river flow
(9 gauges) driving each reach's visible pace. Right of the tick the same
animation simply continues as the published forecast, with P10–P90 ghost
surfaces for the uncertainty. **▶ Take the tour** runs a ~70-second scripted
story over the same data.

**Two views:** *Landscape* (the surface story) and *Beneath* (ground turns to
glass; the interpolated water-table sheet and its indicative downgradient
drift become the subject).

## Data layers (all open, all attributed on screen)

| file | source | builder |
|---|---|---|
| `terrain.js` | OS Terrain 50 (OGL v3) | `scripts/build_terrain_tile.py` |
| `rivers.js` | OS Open Rivers (OGL v3), flow-directed chaining + tidal marking | `scripts/build_valley_rivers.py` |
| `mesh.js` | adaptive Delaunay render mesh over the terrain | `scripts/build_valley_mesh.py` |
| `lidar.js` | EA LIDAR Composite DTM 1 m (OGL v3), river-corridor slice | `scripts/build_lidar_corridor.py` |
| `stations.js` | GroundwaterCast artifact pack (EA data, OGL v3) incl. 3 y weekly history | `scripts/build_valley_stations.py` |
| `rainfall.js` | EA Hydrology API daily rainfall → weekly totals | `scripts/build_valley_rainfall.py` |
| `flow.js` | EA Hydrology API daily mean flow → weekly means + gauge terciles | `scripts/build_valley_flow.py` |
| `abstraction.js` | EA Water Rights Trading extract (holder identities stripped) | `scripts/build_abstraction_points.py` |

Every layer is optional — the scene degrades gracefully to an illustrative
fallback when a file is absent. Rebuild everything with one command:

```bash
python -m scripts.build_valley                 # all eight layers, this bbox
python -m scripts.build_valley --skip-remote   # offline: geometry only
```

## Run it

No build step, no external requests:

```bash
python3 -m http.server 8123 --directory web
# → http://127.0.0.1:8123/valley/test/
```

## How it works

`main.js` derives the whole scene from two scalar fields — `ground(x,z)`
(LIDAR corridor → 50 m DEM → illustrative fallback) and the water table
(IDW-blended station anomalies steering a calibrated seasonal field). Rivers
are not geometry: the terrain shader paints water on any fragment where the
local water level stands above the true ground, so winterbourne heads walk as
a *consequence* of the surfaces. Honesty rules are labelled in-scene: the
surface between boreholes is indicative, gauge discs are the measurements,
the tidal reach below Redbridge is drawn but not modelled.

`vendor/three.global.min.js` is three.js r170 (MIT, `THREE_LICENSE`) —
`build/three.module.min.js` with its trailing `export{…}` mechanically
rewritten to `globalThis.THREE = {…}` so the page needs no module tooling.

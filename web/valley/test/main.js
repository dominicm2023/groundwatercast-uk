// River Test 3-D concept — a "glass block" model of the Test valley.
//
// One deliberately simple idea: build the whole block from two scalar fields,
//   ground(x,z)      — hand-modelled chalk-downland terrain draped on the
//                      real river network (data.js), and
//   waterTable(x,z,s)— a subdued copy of the terrain tied to the river long
//                      profile, moved up and down by the season parameter s.
// Everything else (springs, winterbourne heads, borehole columns, status
// colours) is derived from where those two surfaces sit relative to each
// other — which is also the honest mental model of a chalk aquifer.
//
// Plain script, no build step. Expects window.THREE (vendor/three.global.min.js)
// and window.TEST3D_DATA (data.js).

(function () {
  "use strict";
  const THREE = window.THREE;
  const DATA = window.TEST3D_DATA;

  // ---------------------------------------------------------------- geometry
  const B = DATA.bounds;
  const KM_PER_LON = 111.32 * Math.cos((51.12 * Math.PI) / 180); // ≈ 69.8
  const KM_PER_LAT = 111.2;
  const W = (B.lonMax - B.lonMin) * KM_PER_LON;   // block width, km (east)
  const H = (B.latMax - B.latMin) * KM_PER_LAT;   // block depth, km (south)
  const VS = 0.005;                               // vertical scale: 5× (m → km·5)
  const BASE_ELEV = -80;                          // bottom of the block, m OD

  const toXZ = (lon, lat) => [
    (lon - B.lonMin) * KM_PER_LON,
    (B.latMax - lat) * KM_PER_LAT,
  ];

  // ------------------------------------------------- real terrain (optional)
  // terrain.js (built by scripts/build_terrain_tile.py from OS Terrain 50,
  // OGL v3) defines window.TEST3D_TERRAIN: a uint16-quantised heightmap over
  // the same bounds. When present it REPLACES the hand-modelled downland —
  // the valley-3D terrain spike. When absent the
  // prototype falls back to the original illustrative noise field.
  const TERRAIN = window.TEST3D_TERRAIN || null;
  let demElev = null;
  if (TERRAIN) {
    const raw = atob(TERRAIN.b64);
    const nP = TERRAIN.nx * TERRAIN.nz;
    const hts = new Float32Array(nP);
    const span = TERRAIN.elevMax - TERRAIN.elevMin;
    for (let k = 0; k < nP; k++) {
      hts[k] = TERRAIN.elevMin +
        (span * ((raw.charCodeAt(2 * k + 1) << 8) | raw.charCodeAt(2 * k))) / 65535;
    }
    const TB = TERRAIN.bounds;
    demElev = (x, z) => {                       // scene km → bilinear DEM metres OD
      const lon = B.lonMin + x / KM_PER_LON;
      const lat = B.latMax - z / KM_PER_LAT;
      let u = ((lon - TB.lonMin) / (TB.lonMax - TB.lonMin)) * (TERRAIN.nx - 1);
      let v = ((TB.latMax - lat) / (TB.latMax - TB.latMin)) * (TERRAIN.nz - 1);
      u = Math.max(0, Math.min(TERRAIN.nx - 1.001, u));
      v = Math.max(0, Math.min(TERRAIN.nz - 1.001, v));
      const i0 = Math.floor(u), j0 = Math.floor(v);
      const fu = u - i0, fv = v - j0, r0 = j0 * TERRAIN.nx, r1 = r0 + TERRAIN.nx;
      return (hts[r0 + i0] * (1 - fu) + hts[r0 + i0 + 1] * fu) * (1 - fv) +
             (hts[r1 + i0] * (1 - fu) + hts[r1 + i0 + 1] * fu) * fv;
    };
  }

  // Watercourses → sampled polylines with cumulative fraction t ∈ [0,1].
  // rivers.js (scripts/build_valley_rivers.py, OS Open Rivers OGL v3) provides
  // the real network main stems when present; the hand-sketched courses in
  // data.js remain the fallback. Same downstream pipeline either way — bed
  // elevations are re-derived from the DEM and monotonic-clamped below.
  const RIV = (window.TEST3D_RIVERS && window.TEST3D_RIVERS.watercourses) || null;
  const courses = ((RIV && RIV.length) ? RIV : DATA.watercourses).map((wc) => {
    const pts = wc.waypoints.map(([lon, lat, elev]) => {
      const [x, z] = toXZ(lon, lat);
      return { x, z, elev };
    });
    // Real terrain: waypoint bed elevations come from the DEM (see the fine-
    // grained pass below for the rationale) so segPts — which nearestRiver
    // blends water-table/valley elevations from — stays consistent with it.
    if (demElev) {
      let run = Infinity;
      for (const p of pts) {
        run = Math.min(run, demElev(p.x, p.z) - 0.6);
        p.elev = run;
      }
    }
    // subdivide to ~40 m steps: the water basis + per-vertex bed snap to the
    // NEAREST fine point, and at 150 m the ground rises enough between points
    // (centreline cutting across meanders/spurs) to punch holes in the drape
    const fine = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const a = pts[i], b = pts[i + 1];
      const d = Math.hypot(b.x - a.x, b.z - a.z);
      const n = Math.max(1, Math.round(d / 0.04));
      for (let k = 0; k < n; k++) {
        const u = k / n;
        fine.push({
          x: a.x + (b.x - a.x) * u,
          z: a.z + (b.z - a.z) * u,
          elev: a.elev + (b.elev - a.elev) * u,
        });
      }
    }
    fine.push(pts[pts.length - 1]);
    // With real terrain, re-derive bed elevations by sampling the DEM along
    // the course (hand-authored profiles no longer match the real valley
    // floors), carved slightly below ground. The waypoint line cuts corners
    // across spurs, so clamp to a downstream-monotonic profile (a running
    // min from the source) — rivers must not flow uphill.
    if (demElev) {
      let run = Infinity;
      for (const p of fine) {
        run = Math.min(run, demElev(p.x, p.z) - 0.6);
        p.elev = run;
      }
      // E: smooth the bed along-course (per-vertex attributes snap to the
      // NEAREST fine point, so raw beds step every ~150 m), then re-impose
      // downstream monotonicity so smoothing can't tilt a reach uphill.
      const sm = fine.map((p, i) => {
        let sum = 0, n = 0;
        for (let k = -2; k <= 2; k++) {
          const q = fine[i + k];
          if (q) { sum += q.elev; n++; }
        }
        return sum / n;
      });
      run = Infinity;
      fine.forEach((p, i) => { run = Math.min(run, sm[i]); p.elev = run; });
    }
    let acc = 0;
    const cum = fine.map((p, i) => {
      if (i > 0) acc += Math.hypot(p.x - fine[i - 1].x, p.z - fine[i - 1].z);
      return acc;
    });
    const total = acc;
    fine.forEach((p, i) => (p.t = cum[i] / total));
    return Object.assign({ pts: fine, segPts: pts }, wc);
  });

  // River field: distance to the nearest watercourse, plus a river elevation
  // BLENDED across watercourses (inverse-distance weighted). The blend keeps
  // the terrain and water-table fields continuous across the interfluves —
  // winner-takes-all gives cliffs along the midline between two valleys.
  function nearestRiver(x, z) {
    let dmin = 1e9, main = false, sw = 0, se = 0;
    for (const c of courses) {
      let d2 = 1e9, elev = 0;
      const p = c.segPts;
      for (let i = 0; i < p.length - 1; i++) {
        const ax = p[i].x, az = p[i].z;
        const dx = p[i + 1].x - ax, dz = p[i + 1].z - az;
        const len2 = dx * dx + dz * dz;
        let u = len2 ? ((x - ax) * dx + (z - az) * dz) / len2 : 0;
        u = Math.max(0, Math.min(1, u));
        const px = ax + u * dx, pz = az + u * dz;
        const dd = (x - px) * (x - px) + (z - pz) * (z - pz);
        if (dd < d2) { d2 = dd; elev = p[i].elev + u * (p[i + 1].elev - p[i].elev); }
      }
      const d = Math.sqrt(d2);
      if (d < dmin) { dmin = d; main = !!c.main; }
      const w = 1 / Math.pow(d + 0.25, 3);
      sw += w; se += w * elev;
    }
    return { dist: dmin, elev: se / sw, main };
  }

  // Deterministic value noise (so the hills never change between loads).
  const hash = (ix, iz) => {
    const s = Math.sin(ix * 127.1 + iz * 311.7) * 43758.5453;
    return s - Math.floor(s);
  };
  const smooth = (t) => t * t * (3 - 2 * t);
  function vnoise(x, z) {
    const ix = Math.floor(x), iz = Math.floor(z);
    const fx = smooth(x - ix), fz = smooth(z - iz);
    const v00 = hash(ix, iz), v10 = hash(ix + 1, iz);
    const v01 = hash(ix, iz + 1), v11 = hash(ix + 1, iz + 1);
    return (v00 * (1 - fx) + v10 * fx) * (1 - fz) +
           (v01 * (1 - fx) + v11 * fx) * fz - 0.5; // ∈ (−.5, .5)
  }

  // LIDAR corridor tier (lidar.js, EA Composite DTM at 8 m in sparse 256 m
  // cells near the courses) — preferred inside the corridor; the 50 m T50
  // heightmap covers everywhere else.
  const LIDAR = window.TEST3D_LIDAR || null;
  let lidarElev = null;
  if (LIDAR) {
    const raw = atob(LIDAR.b64);
    const sub = LIDAR.sub, cell = LIDAR.cellM / 1000;
    const span = LIDAR.elevMax - LIDAR.elevMin;
    const blocks = new Map();
    const per = sub * sub * 2;
    LIDAR.keys.forEach((k, bi) => {
      const h = new Float32Array(sub * sub);
      for (let i = 0; i < sub * sub; i++) {
        const o = bi * per + i * 2;
        h[i] = LIDAR.elevMin + span *
          ((raw.charCodeAt(o + 1) << 8) | raw.charCodeAt(o)) / 65535;
      }
      blocks.set(k, h);
    });
    lidarElev = (x, z) => {
      const cx = Math.floor(x / cell), cz = Math.floor(z / cell);
      const h = blocks.get(cx + cz * LIDAR.ncx);
      if (!h) return null;
      let u = ((x - cx * cell) / cell) * sub - 0.5;
      let v = ((z - cz * cell) / cell) * sub - 0.5;
      u = Math.max(0, Math.min(sub - 1.001, u));
      v = Math.max(0, Math.min(sub - 1.001, v));
      const i0 = u | 0, j0 = v | 0, fu = u - i0, fv = v - j0;
      const r0 = j0 * sub, r1 = r0 + sub;
      return (h[r0 + i0] * (1 - fu) + h[r0 + i0 + 1] * fu) * (1 - fv)
           + (h[r1 + i0] * (1 - fu) + h[r1 + i0 + 1] * fu) * fv;
    };
  }

  // ground(x,z): LIDAR corridor first, then the 50 m DEM, then the original
  // illustrative field (river long profile + valley rise + noise).
  function groundAt(x, z, nr) {
    if (lidarElev) {
      const l = lidarElev(x, z);
      if (l !== null) return l;
    }
    if (demElev) return demElev(x, z);
    nr = nr || nearestRiver(x, z);
    const floorW = nr.main ? 0.3 : 0.16;          // flat water-meadow floor
    const away = Math.max(0, nr.dist - floorW);
    const relief = 52 + 46 * (1 - z / H);         // downs higher in the north
    const rise = relief * (1 - Math.exp(-away / 1.7));
    const fade = Math.min(1, away / 0.7);         // no bumps in the floodplain
    // rotate the noise lattices so ridge lines don't align with the grid
    const rx = x * 0.83 - z * 0.56, rz = x * 0.56 + z * 0.83;
    const qx = x * 0.31 + z * 0.95, qz = -x * 0.95 + z * 0.31;
    const n = 11 * vnoise(rx / 5.5, rz / 5.5) + 4.5 * vnoise(qx / 1.9 + 7.3, qz / 1.9 + 3.1);
    return nr.elev + rise + n * 2 * fade;
  }

  // waterTable(…, s): subdued terrain + seasonal swing (bigger under the
  // interfluves than in the valley — the chalk signature), never above ground.
  function waterTableAt(ground, riverElev, s) {
    const above = ground - riverElev;
    const wt = riverElev + 0.5 * above + s * (1.2 + 0.16 * Math.max(0, above));
    return Math.min(wt, ground - 0.3);
  }

  const Y = (elevM) => elevM * VS; // metres OD → scene units

  // ------------------------------------------------------------------- scene
  const canvas = document.getElementById("scene");
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  // Museum-model rendering: filmic tone mapping (core three.js — no
  // postprocessing stack needed) + soft shadows for the studio-lit look.
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x101a2b, 70, 190);

  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 400);
  const target = new THREE.Vector3(W * 0.52, Y(40), H * 0.48);

  // Shared clock uniform for every "living water" shader (rivers, water-table
  // caustics, god-rays) — one object, updated once per tick.
  const uTime = { value: 0 };

  // Studio three-point lighting over a gallery-dark ambience: warm key with
  // raking soft shadows (the downland reads through its own self-shadowing),
  // cool low fill, pale rim from behind to catch the acrylic case edges.
  scene.add(new THREE.HemisphereLight(0xbdd2e8, 0x585144, 0.55));
  const sun = new THREE.DirectionalLight(0xfff0d8, 2.4);
  sun.position.set(W * 1.15, 34, H * 0.15);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  const sc = sun.shadow.camera;
  sc.left = -W * 0.8; sc.right = W * 0.8; sc.top = H * 0.8; sc.bottom = -H * 0.8;
  sc.near = 1; sc.far = 120;
  sun.shadow.bias = -0.0004;
  sun.target.position.set(W / 2, 0, H / 2);
  scene.add(sun); scene.add(sun.target);
  const fill = new THREE.DirectionalLight(0x8fb0d4, 0.55);
  fill.position.set(-W * 0.6, 12, H * 1.3);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0xe8f1ff, 0.7);
  rim.position.set(W * 0.3, 26, -H * 0.6);
  scene.add(rim);

  // --------------------------------------------------------- terrain surface
  // Adaptive mesh (mesh.js, Delaunay: ~55 m at the rivers → ~300 m on the
  // peaks) when present; the regular grid remains the fallback. Heights are
  // filled from groundAt() either way, so mesh and fields never disagree.
  const NX = 176, NZ = 256;
  const MESH = window.TEST3D_MESH || null;
  let terGeo;
  if (MESH) {
    const dec = (b64) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const qx = new Uint16Array(dec(MESH.x).buffer);
    const qz = new Uint16Array(dec(MESH.z).buffer);
    const tri = MESH.idx16 ? new Uint16Array(dec(MESH.tri).buffer)
                           : new Uint32Array(dec(MESH.tri).buffer);
    const posArr = new Float32Array(MESH.nVerts * 3);
    for (let i = 0; i < MESH.nVerts; i++) {
      posArr[i * 3] = (qx[i] / 65535) * MESH.w;
      posArr[i * 3 + 2] = (qz[i] / 65535) * MESH.h;
    }
    terGeo = new THREE.BufferGeometry();
    terGeo.setAttribute("position", new THREE.BufferAttribute(posArr, 3));
    terGeo.setIndex(new THREE.BufferAttribute(tri, 1));
  } else {
    terGeo = new THREE.PlaneGeometry(W, H, NX - 1, NZ - 1);
    terGeo.rotateX(-Math.PI / 2);
    terGeo.translate(W / 2, 0, H / 2);
  }
  const tPos = terGeo.attributes.position;

  // Per-vertex hydrology for the water drape: nearest course point → distance,
  // along-course fraction, bed elevation, flow direction, course id. On the
  // adaptive mesh (~55 m at the rivers) this is effectively per-pixel where it
  // matters, with no textures needed.
  // Local ground at each course point: the water BASIS tracks the thalweg's
  // actual ground. A single point-sample is not enough — the OS centreline
  // often sits a few metres up a bank, and LIDAR bumps rise BETWEEN course
  // points; any ground texel above the interpolated level punches a hole in
  // the drape. So each point takes the MAX of itself + 15 m lateral samples,
  // then a rolling max over +-2 neighbours (~60 m of reach): the basis clears
  // every texel the channel crosses and the ribbon stays continuous.
  for (const c of courses) {
    const pts = c.pts, n = pts.length;
    const gs = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const p = pts[i];
      const q = pts[Math.min(n - 1, i + 1)], q0 = pts[Math.max(0, i - 1)];
      const dx = q.x - q0.x, dz = q.z - q0.z, L = Math.hypot(dx, dz) || 1;
      const px = (-dz / L) * 0.025, pz = (dx / L) * 0.025;
      gs[i] = Math.max(groundAt(p.x, p.z),
                       groundAt(p.x + px, p.z + pz),
                       groundAt(p.x - px, p.z - pz));
    }
    for (let i = 0; i < n; i++) {
      let m = gs[i];
      for (let k = Math.max(0, i - 3); k <= Math.min(n - 1, i + 3); k++)
        m = Math.max(m, gs[k]);
      pts[i]._g = m;
    }
    // exact repair: wherever the ground between two points still rises above
    // both their bases (sharp embankment, centreline offset beyond the
    // lateral samples), lift just those two — no global widening
    for (let i = 0; i < n - 1; i++) {
      const p = pts[i], q = pts[i + 1];
      const gm = groundAt((p.x + q.x) / 2, (p.z + q.z) / 2);
      if (p._g < gm) p._g = gm;
      if (q._g < gm) q._g = gm;
    }
  }
  // Bucketed nearest-course lookup: at 100k+ mesh vertices the flat scan is
  // ~70M distance ops; a 0.5 km spatial grid brings it to ~2M.
  const _cg = new Map(), _CG = 0.5;
  courses.forEach((c, ci) => c.pts.forEach((p, i) => {
    const k = ((p.x / _CG) | 0) * 4096 + ((p.z / _CG) | 0);
    let a = _cg.get(k);
    if (!a) { a = []; _cg.set(k, a); }
    a.push([p, ci, i]);
  }));
  function nearestCourseInfo(x, z) {
    let best = { d2: 1e18, t: 0, bed: 0, dir: 0, cid: 0 };
    const bx = (x / _CG) | 0, bz = (z / _CG) | 0;
    for (let r = 0; r < 12; r++) {
      for (let ix = bx - r; ix <= bx + r; ix++)
        for (let iz = bz - r; iz <= bz + r; iz++) {
          if (r > 0 && Math.abs(ix - bx) < r && Math.abs(iz - bz) < r) continue;
          const a = _cg.get(ix * 4096 + iz);
          if (!a) continue;
          for (const [p, ci, i] of a) {
            const dx = x - p.x, dz = z - p.z;
            const d2 = dx * dx + dz * dz;
            if (d2 < best.d2) {
              const pts = courses[ci].pts;
              const q = pts[Math.min(pts.length - 1, i + 1)];
              const q0 = pts[Math.max(0, i - 1)];
              best = { d2, t: p.t, bed: Math.max(p.elev, p._g - 0.35),
                       dir: Math.atan2(q.z - q0.z, q.x - q0.x), cid: ci };
            }
          }
        }
      // found something and the next ring can't beat it -> stop
      if (best.d2 < ((r) * _CG) * ((r) * _CG)) break;
    }
    return best;
  }
  const tCol = new Float32Array(tPos.count * 3);
  const gGround = new Float32Array(tPos.count);  // ground elev per vertex (m)
  const gRiver = new Float32Array(tPos.count);   // nearest river elev (m)
  const hDist = new Float32Array(tPos.count);    // hydro: dist to course (km)
  const hT = new Float32Array(tPos.count);       //   along-course fraction
  const hBed = new Float32Array(tPos.count);     //   bed elevation (m OD)
  const hDir = new Float32Array(tPos.count);     //   flow direction (rad)
  const hCid = new Float32Array(tPos.count);     //   course index
  const cLow = new THREE.Color(0x4f9448), cMid = new THREE.Color(0x93a968),
        cHigh = new THREE.Color(0xd0d2a4);
  for (let i = 0; i < tPos.count; i++) {
    const x = tPos.getX(i), z = tPos.getZ(i);
    const nr = nearestRiver(x, z);
    const g = groundAt(x, z, nr);
    gGround[i] = g; gRiver[i] = nr.elev;
    tPos.setY(i, Y(g));
    const ci = nearestCourseInfo(x, z);
    hDist[i] = Math.sqrt(ci.d2); hT[i] = ci.t; hBed[i] = ci.bed;
    hDir[i] = ci.dir; hCid[i] = ci.cid;
    const rel = Math.max(0, Math.min(1, (g - nr.elev) / 95));
    const c = rel < 0.35 ? cLow.clone().lerp(cMid, rel / 0.35)
                         : cMid.clone().lerp(cHigh, (rel - 0.35) / 0.65);
    const tint = 1 + 0.06 * vnoise(x / 0.9, z / 0.9) * 2;
    tCol[i * 3] = c.r * tint; tCol[i * 3 + 1] = c.g * tint; tCol[i * 3 + 2] = c.b * tint;
  }
  terGeo.setAttribute("color", new THREE.BufferAttribute(tCol, 3));
  terGeo.setAttribute("elevM", new THREE.BufferAttribute(gGround.slice(), 1));
  terGeo.setAttribute("hDist", new THREE.BufferAttribute(hDist, 1));
  terGeo.setAttribute("hT", new THREE.BufferAttribute(hT, 1));
  terGeo.setAttribute("hBed", new THREE.BufferAttribute(hBed, 1));
  terGeo.setAttribute("hDir", new THREE.BufferAttribute(hDir, 1));
  terGeo.setAttribute("hCid", new THREE.BufferAttribute(hCid, 1));
  terGeo.computeVertexNormals();
  const terMat = new THREE.MeshStandardMaterial({
    vertexColors: true, roughness: 0.95, metalness: 0,
    transparent: true, opacity: 0.62, depthWrite: false, side: THREE.DoubleSide,
  });
  // The LIVING WATER TABLE, painted onto the landscape itself. One shader does
  // contours + water: per-pixel, a fragment is river wherever the local water
  // level (course bed + stage) stands above the ground — so rivers are a
  // CONSEQUENCE of the surfaces, not drawn geometry. uHeads (per-course head
  // fractions from the forecast) make reaches emerge and retreat; the
  // shoreline is the actual intersection, crisper than the mesh. Dry
  // winterbourne reaches read as a chalky bed. Contour etching (10/50 m
  // isolines) stays for the museum-model look.
  const uHeads = { value: new Float32Array(8).fill(1.0) };
  // Per-course lane-scroll factor from GAUGED flow (History mode): the water's
  // apparent pace follows the measured m3/s on that reach. 1 = default pace.
  const uFlowSpd = { value: new Float32Array(8).fill(1.0) };
  // Per-course tidal fraction (from OS Open Rivers form=tidalRiver): below
  // this t the reach is TIDAL — drawn as estuary water, never GW-coupled.
  // 2.0 = the course has no tidal reach.
  const uTidalT = { value: new Float32Array(8).fill(2.0) };
  courses.forEach((c, ci) => { if (c.tidalT != null) uTidalT.value[ci] = c.tidalT; });
  // ?nowater=1 disables the drape entirely (A/B debugging: water vs lighting)
  const NOWATER = /[?&]nowater=1/.test(location.search);
  const uStage = { value: NOWATER ? -9999 : 1.0 };  // river stage above basis, m
  // B: the TRUE-ground texture. The water test used the mesh-interpolated
  // vertex elevation, so the shoreline was mesh-resolution (32 m triangles
  // clipped the channel wherever a vertex sat on a bank). This 16 m texture
  // is sampled per FRAGMENT instead — the shoreline follows the real (LIDAR-
  // first) ground regardless of mesh density.
  const GT = 0.016;                             // texel size, km (16 m)
  const GW = Math.ceil(W / GT), GH = Math.ceil(H / GT);
  const groundTex = (() => {
    const data = new Float32Array(GW * GH);
    for (let j = 0; j < GH; j++)
      for (let i = 0; i < GW; i++)
        data[j * GW + i] = groundAt((i + 0.5) * GT, (j + 0.5) * GT);
    const tex = new THREE.DataTexture(data, GW, GH, THREE.RedFormat, THREE.FloatType);
    // float32 textures are NOT linearly filterable on many GPUs (needs
    // OES_texture_float_linear); an unfilterable texture samples as 0, which
    // painted every corridor fragment as deep water. Nearest is always safe;
    // the shader does its own 4-tap bilinear (raw nearest texels step the
    // depth at every texel edge, smearing the shore tint into a 16 m
    // "thatched" mottle across the ribbon).
    tex.magFilter = THREE.NearestFilter;
    tex.minFilter = THREE.NearestFilter;
    tex.needsUpdate = true;
    return tex;
  })();
  const uGround = { value: groundTex };
  const uWH = { value: new THREE.Vector2(W, H) };
  const uGRes = { value: new THREE.Vector2(GW, GH) };
  terMat.onBeforeCompile = (sh) => {
    sh.uniforms.uTime = uTime;
    sh.uniforms.uHeads = uHeads;
    sh.uniforms.uStage = uStage;
    sh.uniforms.uGround = uGround;
    sh.uniforms.uWH = uWH;
    sh.uniforms.uGRes = uGRes;
    sh.uniforms.uFlowSpd = uFlowSpd;
    sh.uniforms.uTidalT = uTidalT;
    sh.vertexShader =
      "attribute float elevM;\nattribute float hDist;\nattribute float hT;\n" +
      "attribute float hBed;\nattribute float hDir;\nattribute float hCid;\n" +
      "varying float vElevM;\nvarying float vDist;\nvarying float vT;\n" +
      "varying float vBed;\nvarying float vDir;\nvarying float vCid;\n" +
      "varying vec2 vXZ;\nvarying vec3 vUpVS;\n" +
      sh.vertexShader.replace("#include <begin_vertex>",
        `#include <begin_vertex>
  vElevM = elevM; vDist = hDist; vT = hT; vBed = hBed;
  vDir = hDir; vCid = hCid; vXZ = transformed.xz;
  vUpVS = normalize(normalMatrix * vec3(0.0, 1.0, 0.0));`);
    sh.fragmentShader =
      "uniform float uTime;\nuniform float uHeads[8];\nuniform float uStage;\n" +
      "uniform float uFlowSpd[8];\nuniform float uTidalT[8];\n" +
      "uniform sampler2D uGround;\nuniform vec2 uWH;\nuniform vec2 uGRes;\n" +
      "varying float vElevM;\nvarying float vDist;\nvarying float vT;\n" +
      "varying float vBed;\nvarying float vDir;\nvarying float vCid;\n" +
      "varying vec2 vXZ;\nvarying vec3 vUpVS;\n" +
      "float gwcWater = 0.0;\n" +
      // manual 4-tap bilinear: float textures can't filter in hardware, and
      // raw nearest texels step the depth at every 16 m texel edge
      "float gwcGround(vec2 xz) {\n" +
      "  vec2 st = (xz / uWH) * uGRes - 0.5;\n" +
      "  vec2 b = floor(st), f = st - b;\n" +
      "  vec2 t0 = (b + 0.5) / uGRes;\n" +
      "  float g00 = texture2D(uGround, t0).r;\n" +
      "  float g10 = texture2D(uGround, t0 + vec2(1.0, 0.0) / uGRes).r;\n" +
      "  float g01 = texture2D(uGround, t0 + vec2(0.0, 1.0) / uGRes).r;\n" +
      "  float g11 = texture2D(uGround, t0 + vec2(1.0, 1.0) / uGRes).r;\n" +
      "  return mix(mix(g00, g10, f.x), mix(g01, g11, f.x), f.y);\n" +
      "}\n" +
      sh.fragmentShader.replace("#include <color_fragment>",
        `#include <color_fragment>
  {
    float c1 = vElevM / 10.0;
    float g1 = abs(fract(c1 - 0.5) - 0.5) / max(fwidth(c1), 1e-4);
    float c2 = vElevM / 50.0;
    float g2 = abs(fract(c2 - 0.5) - 0.5) / max(fwidth(c2), 1e-4);
    diffuseColor.rgb *= 1.0 - 0.13 * (1.0 - min(g1, 1.0))
                            - 0.14 * (1.0 - min(g2, 1.0));
    float head = uHeads[int(vCid + 0.5)];
    float waterLevel = vBed + uStage;
    float gTrue = gwcGround(vXZ);
    float depth = waterLevel - gTrue;
    bool inCorridor = vDist < 0.55;
    bool tidalReach = vT >= uTidalT[int(vCid + 0.5)];
    if (gTrue < 0.5) {
      // THE SEA (and the tidal river's own level): everything below +0.5 m OD
      // is Southampton Water — flat, calm, deliberately unlike the living
      // river. Tidal water, not modelled: no forecast coupling, no lanes.
      float sd = clamp((0.5 - gTrue) / 3.0, 0.0, 1.0);
      vec3 sea = mix(vec3(0.42, 0.55, 0.62), vec3(0.16, 0.28, 0.38), sd);
      sea += vec3(0.05) * sin(vXZ.x * 3.1 + vXZ.y * 2.3 + uTime * 0.35);
      diffuseColor.rgb = sea;
      diffuseColor.a = 0.94;
      gwcWater = 1.0;
    } else if (inCorridor && tidalReach && depth > 0.0) {
      // tidal reach above the 0-contour: estuary water, same calm palette
      diffuseColor.rgb = vec3(0.34, 0.47, 0.55);
      diffuseColor.a = 0.92;
      gwcWater = 1.0;
    } else if (inCorridor && vT >= head && depth > 0.0) {
      float dn = clamp(depth / 1.4, 0.0, 1.0);
      vec3 water = mix(vec3(0.36, 0.62, 0.74), vec3(0.09, 0.28, 0.46), dn);
      vec2 dirv = vec2(cos(vDir), sin(vDir));
      float lane = dot(vXZ, dirv) * 2.6
                 - uTime * 1.4 * uFlowSpd[int(vCid + 0.5)];
      water += vec3(0.10, 0.22, 0.30)
             * smoothstep(0.72, 0.98, fract(lane)) * (0.4 + 0.6 * dn);
      float shore = 1.0 - smoothstep(0.0, 0.12, depth);
      water = mix(water, vec3(0.86, 0.93, 0.97), shore * 0.3);
      diffuseColor.rgb = water;
      diffuseColor.a = 0.92;
      gwcWater = 1.0;
    } else if (vDist < 0.045 && vT < head) {
      // upstream of the head: the dry winterbourne bed, a pale chalky trace
      diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.87, 0.84, 0.72), 0.55);
    }
  }`)
      // water shades as a flat pool, not as the bank triangles beneath it —
      // otherwise every 32 m facet tilts the ribbon's lighting into patches
      .replace("#include <normal_fragment_begin>",
        `#include <normal_fragment_begin>
  if (gwcWater > 0.5) normal = normalize(vUpVS);`);
  };
  const terMesh = new THREE.Mesh(terGeo, terMat);
  terMesh.castShadow = true;
  terMesh.receiveShadow = true;
  scene.add(terMesh);

  // ------------------------------------------------- block walls (strata) —
  // vertical cross-sections on all four sides, coloured by the chalk stack
  // dipping gently ESE; this is the "cut cake" look of a geological model.
  function stratumColor(x, z, elevM) {
    let top = 118 - 34 * (x / W) - 22 * (z / H); // eroded top of stack, dipping
    for (const s of DATA.strata) {
      if (elevM > top - s.thickness) return s.color;
      top -= s.thickness;
    }
    return DATA.strata[DATA.strata.length - 1].color;
  }
  function makeWall(samples) { // samples: [{x,z}] along one side, in order
    const cols = samples.length, rows = 26;
    const pos = [], col = [], idx = [];
    const cc = new THREE.Color();
    for (let i = 0; i < cols; i++) {
      const { x, z } = samples[i];
      const g = groundAt(x, z);
      for (let r = 0; r < rows; r++) {
        const e = g + (BASE_ELEV - g) * (r / (rows - 1));
        pos.push(x, Y(e), z);
        cc.setHex(stratumColor(x, z, e));
        const shade = 1 - 0.08 * (r / rows);
        col.push(cc.r * shade, cc.g * shade, cc.b * shade);
      }
    }
    for (let i = 0; i < cols - 1; i++)
      for (let r = 0; r < rows - 1; r++) {
        const a = i * rows + r, b = (i + 1) * rows + r;
        idx.push(a, b, a + 1, b, b + 1, a + 1);
      }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    geo.setIndex(idx);
    // flat, diagram-style shading — geological sections read best unlit
    return new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
      vertexColors: true, side: THREE.DoubleSide,
    }));
  }
  // Sample the wall tops on exactly the terrain grid so the seams are tight.
  const side = (n, f) => Array.from({ length: n }, (_, i) => f(i / (n - 1)));
  scene.add(makeWall(side(NX, (u) => ({ x: u * W, z: 0 }))));    // north
  scene.add(makeWall(side(NX, (u) => ({ x: u * W, z: H }))));    // south
  scene.add(makeWall(side(NZ, (u) => ({ x: 0, z: u * H }))));    // west
  scene.add(makeWall(side(NZ, (u) => ({ x: W, z: u * H }))));    // east
  { // bottom slab
    const geo = new THREE.PlaneGeometry(W, H);
    geo.rotateX(Math.PI / 2);
    geo.translate(W / 2, Y(BASE_ELEV), H / 2);
    scene.add(new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
      color: DATA.strata[DATA.strata.length - 1].color, roughness: 1,
    })));
  }

  // ------------------------------------------- acrylic case + plinth -------
  // The museum-model conceit made literal: a barely-there glass case around
  // the whole block (clearcoat sheen catching the rim light) with polished
  // bright edges, standing on a matte charcoal plinth. The case top sits just
  // above the highest downland, so the landscape lives INSIDE the acrylic.
  {
    const caseTop = Y(245), caseBot = Y(BASE_ELEV) - 0.06;
    const caseH = caseTop - caseBot;
    const caseGeo = new THREE.BoxGeometry(W + 0.24, caseH, H + 0.24);
    caseGeo.translate(W / 2, caseBot + caseH / 2, H / 2);
    const caseMat = new THREE.MeshPhysicalMaterial({
      color: 0xdfeaf4, transparent: true, opacity: 0.07,
      roughness: 0.06, metalness: 0, clearcoat: 1, clearcoatRoughness: 0.08,
      depthWrite: false, side: THREE.FrontSide,
    });
    const caseMesh = new THREE.Mesh(caseGeo, caseMat);
    caseMesh.renderOrder = 5;                    // over the translucent fills
    scene.add(caseMesh);
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(caseGeo),
      new THREE.LineBasicMaterial({ color: 0xa9c6e2, transparent: true, opacity: 0.55 }));
    scene.add(edges);
    const plinth = new THREE.Mesh(
      new THREE.BoxGeometry(W + 2.6, 0.55, H + 2.6),
      new THREE.MeshStandardMaterial({ color: 0x151a22, roughness: 0.92, metalness: 0.05 }));
    plinth.position.set(W / 2, caseBot - 0.3, H / 2);
    plinth.receiveShadow = true;
    scene.add(plinth);
    // compass rose on the plinth's north lip — a museum model tells you
    // which way it faces
    const cv = document.createElement("canvas");
    cv.width = 128; cv.height = 128;
    const g2 = cv.getContext("2d");
    g2.strokeStyle = g2.fillStyle = "#9fb6d4";
    g2.lineWidth = 6;
    g2.beginPath(); g2.moveTo(64, 108); g2.lineTo(64, 40); g2.stroke();
    g2.beginPath(); g2.moveTo(64, 16); g2.lineTo(46, 52); g2.lineTo(82, 52);
    g2.closePath(); g2.fill();
    g2.font = "700 34px system-ui, sans-serif";
    g2.textAlign = "center";
    g2.fillText("N", 100, 46);
    const rose = new THREE.Mesh(
      new THREE.PlaneGeometry(1.6, 1.6),
      new THREE.MeshBasicMaterial({
        map: new THREE.CanvasTexture(cv), transparent: true, opacity: 0.85,
        depthWrite: false,
      }));
    rose.rotation.x = -Math.PI / 2;          // flat on the plinth, arrow → north
    rose.position.set(W / 2, caseBot - 0.02, -1.55);
    scene.add(rose);
  }

  // --------------------------------------------------------- water table ---
  const WNX = 88, WNZ = 128;
  const wtGeo = new THREE.PlaneGeometry(W, H, WNX - 1, WNZ - 1);
  wtGeo.rotateX(-Math.PI / 2);
  wtGeo.translate(W / 2, 0, H / 2);
  const wPos = wtGeo.attributes.position;
  const wGround = new Float32Array(wPos.count), wRiver = new Float32Array(wPos.count);
  for (let i = 0; i < wPos.count; i++) {
    const x = wPos.getX(i), z = wPos.getZ(i);
    const nr = nearestRiver(x, z);
    wGround[i] = groundAt(x, z, nr);
    wRiver[i] = nr.elev;
  }
  const wtMat = new THREE.MeshPhysicalMaterial({
    color: 0x3fa4e8, transparent: true, opacity: 0.24, roughness: 0.25,
    metalness: 0, side: THREE.DoubleSide, depthWrite: false,
  });
  // Caustic shimmer: two counter-scrolling interference fields brighten the
  // water-table surface — the light-through-water dapple that makes it read
  // as WATER inside the block rather than a static blue sheet.
  wtMat.onBeforeCompile = (sh) => {
    sh.uniforms.uTime = uTime;
    sh.vertexShader = "varying vec3 vWpos;\n" +
      sh.vertexShader.replace("#include <begin_vertex>",
        "#include <begin_vertex>\n  vWpos = (modelMatrix * vec4(transformed, 1.0)).xyz;");
    sh.fragmentShader = "uniform float uTime;\nvarying vec3 vWpos;\n" +
      sh.fragmentShader.replace("#include <emissivemap_fragment>",
        `#include <emissivemap_fragment>
  {
    float a = sin(vWpos.x * 5.5 + uTime * 0.75) * sin(vWpos.z * 4.5 - uTime * 0.6);
    float b = sin((vWpos.x + vWpos.z) * 8.0 - uTime * 1.1)
            * sin((vWpos.x - vWpos.z) * 7.0 + uTime * 0.9);
    float caust = smoothstep(0.35, 1.35, a + b);
    totalEmissiveRadiance += vec3(0.12, 0.30, 0.42) * caust * 0.5;
  }`);
  };
  const wtMesh = new THREE.Mesh(wtGeo, wtMat);
  scene.add(wtMesh);

  // ------------------------------------------------------ god-ray shafts ---
  // Faint additive light shafts falling from the downland surface to the
  // water table along the Test's valley — "light filtering down to the
  // aquifer". Fake volumetrics: gradient-alpha planes, slow sway + breathe.
  const shafts = [];
  {
    const cv = document.createElement("canvas");
    cv.width = 64; cv.height = 256;
    const g2 = cv.getContext("2d");
    const grad = g2.createLinearGradient(0, 0, 0, 256);
    grad.addColorStop(0, "rgba(255,255,255,0.55)");
    grad.addColorStop(0.75, "rgba(255,255,255,0.12)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    g2.fillStyle = grad; g2.fillRect(0, 0, 64, 256);
    const alphaTex = new THREE.CanvasTexture(cv);
    const main = courses.find((c) => c.main) || courses[0];
    for (let i = 0; i < 7; i++) {
      const t = 0.18 + 0.66 * (i / 6);
      const p = main.pts[Math.round(t * (main.pts.length - 1))];
      const gTop = groundAt(p.x, p.z) + 26;
      const h = Y(gTop) - Y(p.elev - 4);
      const geo = new THREE.PlaneGeometry(1.15, h);
      const mat = new THREE.MeshBasicMaterial({
        color: 0xcfe6ff, alphaMap: alphaTex, transparent: true, opacity: 0.10,
        blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
      });
      const m = new THREE.Mesh(geo, mat);
      m.position.set(p.x, Y(p.elev - 4) + h / 2, p.z);
      m.rotation.y = hash(i, 3) * Math.PI;
      scene.add(m);
      shafts.push({ mesh: m, phase: hash(i, 11) * 6.28, baseY: m.rotation.y });
    }
  }
  function updateWaterTable(s) {
    for (let i = 0; i < wPos.count; i++)
      wPos.setY(i, Y(waterTableAt(wGround[i], wRiver[i], s)));
    wPos.needsUpdate = true;
    wtGeo.computeVertexNormals();
  }


  // ------------------------------------------------------------- rivers ----
  // Rivers are painted onto the terrain by the living-water-table drape in
  // the terrain shader (see terMat.onBeforeCompile) - no river geometry at
  // all. headT maps the season slider to a per-course head fraction.
  function headT(c, s) {
    return s >= 0
      ? c.headPerennial + (c.headWinter - c.headPerennial) * s
      : c.headPerennial + (c.headDrought - c.headPerennial) * -s;
  }
  const springSpots = []; // rebuilt with the rivers; feeds the particle system
  // thMap (forecast mode): per-course head fractions derived from where the
  // interpolated water table crosses the bed — overrides the seasonal formula.
  // Rivers are now painted by the terrain shader (the living-water-table
  // drape): moving a head is a UNIFORM update, not a geometry rebuild. This
  // only refreshes the per-course head fractions + the spring anchors.
  function rebuildRivers(s, thMap) {
    springSpots.length = 0;
    for (let ci = 0; ci < courses.length; ci++) {
      const c = courses[ci];
      const th = thMap ? thMap.get(c) : headT(c, s);
      uHeads.value[ci] = th;
      const wet = c.pts.filter((p) => p.t >= th);
      if (wet.length > 3) {
        // springs: the head itself plus a short spring-line below it
        for (const dt of [0, 0.025, 0.055]) {
          const p = wet[Math.min(wet.length - 1,
            Math.round(dt * wet.length / Math.max(0.02, 1 - th)))];
          if (p) springSpots.push({ x: p.x, y: Y(groundAt(p.x, p.z) + 0.3), z: p.z });
        }
      }
    }
  }

  // ------------------------------------------------------------- springs ---
  // One pooled particle system: little motes rising out of the ground at the
  // seasonal heads — the "sources coming up".
  const MAX_SPRINGS = 24, PER_SPRING = 26;
  const P_N = MAX_SPRINGS * PER_SPRING;
  const pGeo = new THREE.BufferGeometry();
  const pPos = new Float32Array(P_N * 3), pCol = new Float32Array(P_N * 3);
  const pPhase = new Float32Array(P_N);
  for (let i = 0; i < P_N; i++) pPhase[i] = hash(i, 17) ;
  pGeo.setAttribute("position", new THREE.BufferAttribute(pPos, 3));
  pGeo.setAttribute("color", new THREE.BufferAttribute(pCol, 3));
  const spriteTex = (() => {
    const cv = document.createElement("canvas");
    cv.width = cv.height = 64;
    const g = cv.getContext("2d");
    const rg = g.createRadialGradient(32, 32, 2, 32, 32, 30);
    rg.addColorStop(0, "rgba(255,255,255,1)");
    rg.addColorStop(0.4, "rgba(190,235,255,0.7)");
    rg.addColorStop(1, "rgba(190,235,255,0)");
    g.fillStyle = rg;
    g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(cv);
  })();
  const pMat = new THREE.PointsMaterial({
    size: 0.5, map: spriteTex, transparent: true, depthWrite: false,
    blending: THREE.AdditiveBlending, vertexColors: true, sizeAttenuation: true,
  });
  const points = new THREE.Points(pGeo, pMat);
  points.frustumCulled = false;
  scene.add(points);
  function animateSprings(time) {
    const base = new THREE.Color(0x9fe0ff);
    for (let sIdx = 0; sIdx < MAX_SPRINGS; sIdx++) {
      const spot = springSpots[sIdx % Math.max(1, springSpots.length)];
      for (let k = 0; k < PER_SPRING; k++) {
        const i = sIdx * PER_SPRING + k;
        const life = (time * 0.28 + pPhase[i]) % 1; // 0 → born, 1 → gone
        if (!spot || sIdx >= springSpots.length) {
          pCol[i * 3] = pCol[i * 3 + 1] = pCol[i * 3 + 2] = 0;
          pPos[i * 3 + 1] = -999;
          continue;
        }
        const ang = pPhase[i] * Math.PI * 2 + k;
        const r = 0.05 + 0.16 * life;
        pPos[i * 3] = spot.x + Math.cos(ang) * r * hash(k, sIdx);
        pPos[i * 3 + 1] = spot.y + 0.04 + life * 0.62;
        pPos[i * 3 + 2] = spot.z + Math.sin(ang) * r * hash(sIdx, k);
        const fade = life < 0.15 ? life / 0.15 : 1 - (life - 0.15) / 0.85;
        pCol[i * 3] = base.r * fade;
        pCol[i * 3 + 1] = base.g * fade;
        pCol[i * 3 + 2] = base.b * fade;
      }
    }
    pGeo.attributes.position.needsUpdate = true;
    pGeo.attributes.color.needsUpdate = true;
  }

  // ------------------------------------------- gradient drift (Beneath) ----
  // Motes drifting DOWNGRADIENT on the water-table sheet — groundwater moves
  // perpendicular to the table's contours, so the interpolated surface's own
  // gradient is an honest, model-free direction field. INDICATIVE: between
  // boreholes the gradient is interpolation, not measurement (legend +
  // disclaimer say so), hence broad slow motes rather than crisp streamlines.
  const sheetYm = (x, z) => {           // bilinear sheet elevation, metres OD
    let u = (x / W) * (WNX - 1), v = (z / H) * (WNZ - 1);
    u = Math.max(0, Math.min(WNX - 1.001, u));
    v = Math.max(0, Math.min(WNZ - 1.001, v));
    const i0 = u | 0, j0 = v | 0, fu = u - i0, fv = v - j0;
    const r0 = j0 * WNX + i0, r1 = r0 + WNX;
    return ((wPos.getY(r0) * (1 - fu) + wPos.getY(r0 + 1) * fu) * (1 - fv) +
            (wPos.getY(r1) * (1 - fu) + wPos.getY(r1 + 1) * fu) * fv) / VS;
  };
  const DRIFT_N = 700;
  const driftGeo = new THREE.BufferGeometry();
  const dPos = new Float32Array(DRIFT_N * 3), dCol = new Float32Array(DRIFT_N * 3);
  const dLife = new Float32Array(DRIFT_N), dX = new Float32Array(DRIFT_N),
        dZ = new Float32Array(DRIFT_N);
  const dSeed = (i) => {
    dX[i] = 0.5 + hash(i, 23) * (W - 1);
    dZ[i] = 0.5 + hash(i, 41) * (H - 1);
    dLife[i] = 4 + 6 * hash(i, 57);
  };
  for (let i = 0; i < DRIFT_N; i++) { dSeed(i); dLife[i] *= hash(i, 3); }
  driftGeo.setAttribute("position", new THREE.BufferAttribute(dPos, 3));
  driftGeo.setAttribute("color", new THREE.BufferAttribute(dCol, 3));
  const driftMat = new THREE.PointsMaterial({
    size: 0.34, map: spriteTex, transparent: true, depthWrite: false,
    blending: THREE.AdditiveBlending, vertexColors: true, sizeAttenuation: true,
  });
  const driftPts = new THREE.Points(driftGeo, driftMat);
  driftPts.frustumCulled = false;
  driftPts.visible = false;
  scene.add(driftPts);
  const driftBase = new THREE.Color(0xbfe9ff);
  function animateDrift(dt) {
    const GH2 = 0.24;                       // central-difference step, km
    for (let i = 0; i < DRIFT_N; i++) {
      dLife[i] -= dt;
      let x = dX[i], z = dZ[i];
      if (dLife[i] <= 0 || x < 0.4 || x > W - 0.4 || z < 0.4 || z > H - 0.4) {
        dSeed(i); x = dX[i]; z = dZ[i];
      }
      // downgradient of the sheet (m per km), smoothed by the coarse grid
      const gx = (sheetYm(x + GH2, z) - sheetYm(x - GH2, z)) / (2 * GH2);
      const gz = (sheetYm(x, z + GH2) - sheetYm(x, z - GH2)) / (2 * GH2);
      const gm = Math.hypot(gx, gz);
      if (gm > 0.1) {
        const speed = 0.10 + 0.45 * Math.min(1, gm / 22);   // stylised, slow
        dX[i] = x -= (gx / gm) * speed * dt;
        dZ[i] = z -= (gz / gm) * speed * dt;
      }
      const wt = sheetYm(x, z);
      // reaching the river zone (table at the surface) = discharge: respawn
      if (groundAt(x, z) - wt < 0.6) { dSeed(i); continue; }
      dPos[i * 3] = x;
      dPos[i * 3 + 1] = Y(wt) + 0.035;
      dPos[i * 3 + 2] = z;
      const l = dLife[i];
      const fade = Math.min(1, Math.min(l, 10 - l) / 1.2) * 0.75;
      dCol[i * 3] = driftBase.r * fade;
      dCol[i * 3 + 1] = driftBase.g * fade;
      dCol[i * 3 + 2] = driftBase.b * fade;
    }
    driftGeo.attributes.position.needsUpdate = true;
    driftGeo.attributes.color.needsUpdate = true;
  }

  // ------------------------------------------------------------ boreholes --
  const bhGroup = new THREE.Group();
  scene.add(bhGroup);
  const statusColor = {
    below: new THREE.Color(DATA.palette.below),
    near: new THREE.Color(DATA.palette.near),
    above: new THREE.Color(DATA.palette.above),
  };
  function makeLabel(text) {
    const cv = document.createElement("canvas");
    cv.width = 512; cv.height = 112;
    const g = cv.getContext("2d");
    g.font = "600 44px system-ui, sans-serif";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.lineWidth = 9; g.strokeStyle = "rgba(8,14,24,0.85)";
    g.strokeText(text, 256, 56);
    g.fillStyle = "#eaf2fb";
    g.fillText(text, 256, 56);
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({
      map: new THREE.CanvasTexture(cv), transparent: true, depthTest: false,
    }));
    sp.scale.set(3.9, 0.86, 1);
    return sp;
  }
  // Real pack stations (stations.js, built by scripts/build_valley_stations.py)
  // replace the illustrative boreholes when present — step 2 of the valley-3D
  // plan. Each real tube carries its latest observed level and its OWN
  // monthly-normals envelope, so the season slider swings valley-floor
  // stations ~1 m and interfluve stations ~20 m (the real chalk signature the
  // hand-authored swingBias faked).
  const STN = (window.TEST3D_STATIONS && window.TEST3D_STATIONS.stations) || null;
  const bhSource = (STN && STN.length) ? STN : DATA.boreholes;
  const boreholes = bhSource.map((b) => {
    const [x, z] = toXZ(b.lon, b.lat);
    const nr = nearestRiver(x, z);
    const ground = groundAt(x, z, nr);
    const real = b.level != null;
    // real: hole reaches comfortably below the driest normal; hand: authored depth
    const base = real
      ? Math.min(b.p10min != null ? b.p10min : b.level, b.level) - 8
      : ground - b.depth;
    const g = new THREE.Group();
    // casing — a translucent tube the full depth of the hole
    const casing = new THREE.Mesh(
      new THREE.CylinderGeometry(0.12, 0.12, 1, 12, 1, true),
      new THREE.MeshPhongMaterial({
        color: 0xdfe6ee, transparent: true, opacity: 0.38,
        side: THREE.DoubleSide, depthWrite: false,
      }));
    casing.scale.y = Y(ground) - Y(base);
    casing.position.set(x, (Y(ground) + Y(base)) / 2, z);
    g.add(casing);
    // water column — solid, coloured by status vs normal
    const colMat = new THREE.MeshPhongMaterial({
      color: statusColor.near, emissive: 0x111820,
    });
    const column = new THREE.Mesh(
      new THREE.CylinderGeometry(0.08, 0.08, 1, 10), colMat);
    g.add(column);
    // surface marker (shares the status colour) + label
    const cap = new THREE.Mesh(new THREE.ConeGeometry(0.17, 0.3, 12), colMat);
    cap.position.set(x, Y(ground) + 0.2, z);
    g.add(cap);
    // 80+ real tubes: label only the forecast stations (the rest name
    // themselves in the click card) — nine hand-authored ones all get labels.
    if (!real || b.hasForecast) {
      const label = makeLabel(b.name);
      label.position.set(x, Y(ground) + 0.72, z);
      g.add(label);
    }
    bhGroup.add(g);
    return { ...b, real, x, z, ground, base,
             depth: real ? Math.max(1, Math.round(ground - base)) : b.depth,
             riverElev: nr.elev, column, colMat, casing };
  });
  function updateBoreholes(s, frameK, histK) {
    for (const b of boreholes) {
      let wt;
      if (b.real && histK != null) {
        // history mode: observed weekly level (held where the record gaps;
        // stations with no usable history hold their latest measurement)
        const lvl = b.h50 ? b.h50[histK] : b.level;
        wt = Math.min(Math.max(lvl, b.base + 1), b.ground - 0.2);
        b.status = (b.t2m != null && lvl > b.t2m) ? "above"
          : (b.t1m != null && lvl < b.t1m) ? "below" : "near";
      } else if (b.real && frameK != null && b.s50) {
        // forecast mode: the tube follows its published P50 (held level when
        // the station has no forecast — we don't invent movement).
        const lvl = b.s50[frameK];
        wt = Math.min(Math.max(lvl, b.base + 1), b.ground - 0.2);
        b.status = (frameK === 0 && b.status0) ? b.status0
          : (b.t2m != null && lvl > b.t2m) ? "above"
          : (b.t1m != null && lvl < b.t1m) ? "below" : "near";
      } else if (b.real) {
        // slide the level through the station's OWN normals envelope:
        // s=0 → as measured; s=−1 → its driest normal; s=+1 → its wettest.
        const lvl = s >= 0
          ? b.level + s * Math.max(0, b.p90max - b.level)
          : b.level + s * Math.max(0, b.level - b.p10min);
        wt = Math.min(Math.max(lvl, b.base + 1), b.ground - 0.2);
        b.status = (Math.abs(s) < 0.05 && b.status0) ? b.status0
          : (b.t2m != null && lvl > b.t2m) ? "above"
          : (b.t1m != null && lvl < b.t1m) ? "below" : "near";
      } else {
        wt = Math.max(b.base + 2,
          waterTableAt(b.ground, b.riverElev, s + b.swingBias * 0.4));
        const sj = s + b.swingBias;
        b.status = sj > 0.33 ? "above" : sj < -0.33 ? "below" : "near";
      }
      b.wt = wt;
      b.column.scale.y = Math.max(0.02, Y(wt) - Y(b.base));
      b.column.position.set(b.x, (Y(wt) + Y(b.base)) / 2, b.z);
      b.colMat.color.copy(statusColor[b.status]);
    }
    if (selected) fillCard(selected);
  }

  // ------------------------------------------- forecast mode (step 3) ------
  // When real stations carry published fans, the scene stops being a seasonal
  // cartoon and becomes the FORECAST: the slider walks today → day 14, tubes
  // follow their P50s, the water-table surface is interpolated from the real
  // station levels (IDW residuals over the illustrative prior — an indicative
  // surface between measurement points, never a measurement), winterbourne
  // heads derive from where that surface actually crosses the DEM beds, and
  // P10/P90 render as ghost surfaces that bulge where forecasts live.
  const fanBHs = boreholes.filter((b) => b.real && b.fan && b.fan.length);
  // Seasonal frames (monthly weighted-mean quantiles) extend the timeline
  // beyond day 14 — a fortnight barely moves a chalk system; the months are
  // where the winterbournes really walk. Only builder-vetted FRESH outlooks
  // are present in stations.js (the pack currently mixes stale runs — see
  // the 2026-07-09 seasonal-staleness bug).
  const seaBHs = boreholes.filter((b) => b.real && b.seasonal && b.seasonal.length);
  const SMONTHS = seaBHs.length
    ? [...new Set(seaBHs.flatMap((b) => b.seasonal.map((r) => r[0])))].sort()
    : [];
  const NFAN = fanBHs.length ? fanBHs[0].fan.length : 0;
  const FRAMES = fanBHs.length
    ? ["today"].concat(fanBHs[0].fan.map((f) => f[0])).concat(SMONTHS)
    : null;

  if (FRAMES) {
    // Per-borehole level series aligned to FRAMES. Held flat wherever a
    // station has no forecast for a frame (day-14 value through the months
    // when it has a fan but no seasonal; measured level throughout when it
    // has neither) — we never invent movement.
    for (const b of boreholes) {
      if (!b.real) continue;
      const n = FRAMES.length;
      b.s50 = new Float32Array(n); b.s10 = new Float32Array(n); b.s90 = new Float32Array(n);
      const seaByMonth = new Map((b.seasonal || []).map((r) => [r[0], r]));
      let v10 = b.level, v50 = b.level, v90 = b.level;
      if (b.fan && b.now50 != null) v10 = v50 = v90 = b.now50;
      for (let k = 0; k < n; k++) {
        if (k >= 1 && k <= NFAN && b.fan && b.fan[k - 1]) {
          v10 = b.fan[k - 1][1]; v50 = b.fan[k - 1][2]; v90 = b.fan[k - 1][3];
        } else if (k > NFAN) {
          const row = seaByMonth.get(FRAMES[k]);
          if (row) { v10 = row[1]; v50 = row[2]; v90 = row[3]; }
        }
        b.s10[k] = v10; b.s50[k] = v50; b.s90[k] = v90;
      }
    }
    var realBHs = boreholes.filter((b) => b.real && b.s50);
  }

  // Normalised inverse-distance weights to the K nearest stations of `list` —
  // precomputed once per sample point; each frame is then a dot product.
  function idwWeights(list, x, z, K) {
    const ds = list.map((b, i) => [Math.hypot(b.x - x, b.z - z), i]);
    ds.sort((a, m) => a[0] - m[0]);
    const top = ds.slice(0, K);
    let sw = 0;
    const out = top.map(([d, i]) => {
      const w = 1 / Math.pow(d + 0.35, 2);
      sw += w;
      return [i, w];
    });
    out.forEach((e) => (e[1] /= sw));
    return out;
  }

  let wtFrame = null, headTsAtFrame = null, ghost10 = null, ghost90 = null;
  if (FRAMES) {
    // Interpolate ANOMALIES, not levels: each station's position within its
    // own normals envelope maps to the calibrated seasonal field's s ∈ [−1,1]
    // (2·(level−p10min)/(p90max−p10min) − 1), and the field is evaluated with
    // that LOCALLY-blended s. Absolute-level residuals were tried first and
    // systematically dried the rivers: chalk water tables are far flatter
    // than the prior's half-topography assumption, so interfluve stations
    // carry big negative residuals that IDW smears onto the river lines.
    // Anomalies sidestep the datum problem entirely — real data steers the
    // well-behaved field instead of fighting it.
    for (const b of realBHs) {
      const span = Math.max(0.5, b.p90max - b.p10min);
      const toS = (v) => Math.max(-1.2, Math.min(1.2,
        (2 * (v - b.p10min)) / span - 1));
      b.e50 = Float32Array.from(b.s50, toS);
      b.e10 = Float32Array.from(b.s10, toS);
      b.e90 = Float32Array.from(b.s90, toS);
    }

    // weights per water-table vertex + per course point (precomputed once)
    const vW = new Array(wPos.count);
    for (let i = 0; i < wPos.count; i++)
      vW[i] = idwWeights(realBHs, wPos.getX(i), wPos.getZ(i), 6);
    for (const c of courses)
      for (const p of c.pts) p._w = idwWeights(realBHs, p.x, p.z, 6);

    // ghost surfaces (P10 / P90) share the grid
    const gMat = () => new THREE.MeshPhysicalMaterial({
      color: 0x7fc4f0, transparent: true, opacity: 0.10, roughness: 0.4,
      side: THREE.DoubleSide, depthWrite: false,
    });
    ghost10 = new THREE.Mesh(wtGeo.clone(), gMat());
    ghost90 = new THREE.Mesh(wtGeo.clone(), gMat());
    ghost10.visible = ghost90.visible = false;
    scene.add(ghost10); scene.add(ghost90);
    const g10Pos = ghost10.geometry.attributes.position;
    const g90Pos = ghost90.geometry.attributes.position;

    const blend = (weights, series, k) => {
      let a = 0;
      for (const [bi, w] of weights) a += w * series[bi][k];
      return a;
    };

    wtFrame = function (k, ghosts) {
      // Ghost auto-hide: in the first fan days the P10–P90 spread is a few
      // scene-millimetres (0.15 m × 15× ≈ 2 mm) — three near-coincident
      // translucent surfaces just z-fight. Show the envelopes only once the
      // spread is visually meaningful; they emerge as the fan widens and are
      // fully present through the seasonal months.
      if (ghosts) {
        let maxSpread = 0;
        for (const b of realBHs)
          maxSpread = Math.max(maxSpread, b.s90[k] - b.s10[k]);
        if (maxSpread < 0.3) ghosts = false;
      }
      const e50 = realBHs.map((b) => b.e50);
      const e10 = realBHs.map((b) => b.e10);
      const e90 = realBHs.map((b) => b.e90);
      for (let i = 0; i < wPos.count; i++) {
        wPos.setY(i, Y(waterTableAt(wGround[i], wRiver[i], blend(vW[i], e50, k))));
        if (ghosts) {
          g10Pos.setY(i, Y(waterTableAt(wGround[i], wRiver[i], blend(vW[i], e10, k))));
          g90Pos.setY(i, Y(waterTableAt(wGround[i], wRiver[i], blend(vW[i], e90, k))));
        }
      }
      wPos.needsUpdate = true;
      wtGeo.computeVertexNormals();
      if (ghosts) {
        g10Pos.needsUpdate = g90Pos.needsUpdate = true;
        ghost10.geometry.computeVertexNormals();
        ghost90.geometry.computeVertexNormals();
      }
      ghost10.visible = ghost90.visible = !!ghosts;
    };

    // Winterbourne heads: the calibrated per-course head mapping (headT — the
    // course's real perennial/drought/winter behaviour), driven by the
    // stations' blended anomaly along the course's upper half (where the
    // head walks; the lower reach shouldn't dilute the signal).
    headTsAtFrame = function (k) {
      const e50 = realBHs.map((b) => b.e50);
      const map = new Map();
      for (const c of courses) {
        let sw = 0, n = 0;
        for (const p of c.pts) {
          if (p.t > 0.6) break;
          sw += blend(p._w, e50, k); n++;
        }
        map.set(c, headT(c, n ? sw / n : 0));
      }
      return map;
    };
  }

  // -------------------------------------------- history mode (hindcast) ----
  // Three years of OBSERVED weekly levels (the pack's observed tails on a
  // shared Monday axis) drive the same anomaly-blend machinery as the
  // forecast: the scene earns its trust replaying measurements before it
  // asks anyone to believe a forecast. Weeks without an observation hold the
  // last level (hObs marks the difference for the click card).
  const HWEEKS = (window.TEST3D_STATIONS && window.TEST3D_STATIONS.historyWeeks) || null;
  const histBHs = boreholes.filter((b) => b.real && b.hist && b.hist.length);
  let histFrame = null, headTsAtHist = null;
  const HN = HWEEKS ? HWEEKS.length : 0;
  if (HWEEKS && histBHs.length >= 4) {
    for (const b of histBHs) {
      const h = new Float32Array(HN), ho = new Uint8Array(HN);
      let v = b.hist.find((r) => r != null);
      for (let k = 0; k < HN; k++) {
        if (b.hist[k] != null) { v = b.hist[k]; ho[k] = 1; }
        h[k] = v;
      }
      const span = Math.max(0.5, b.p90max - b.p10min);
      b.h50 = h; b.hObs = ho;
      b.eH = Float32Array.from(h, (x) => Math.max(-1.2, Math.min(1.2,
        (2 * (x - b.p10min)) / span - 1)));
    }
    const hVW = new Array(wPos.count);
    for (let i = 0; i < wPos.count; i++)
      hVW[i] = idwWeights(histBHs, wPos.getX(i), wPos.getZ(i), 6);
    for (const c of courses)
      for (const p of c.pts) p._hw = idwWeights(histBHs, p.x, p.z, 6);
    const eH = histBHs.map((b) => b.eH);
    const blendH = (wts, k) => {
      let a = 0;
      for (const [bi, w] of wts) a += w * eH[bi][k];
      return a;
    };
    histFrame = function (k) {
      for (let i = 0; i < wPos.count; i++)
        wPos.setY(i, Y(waterTableAt(wGround[i], wRiver[i], blendH(hVW[i], k))));
      wPos.needsUpdate = true;
      wtGeo.computeVertexNormals();
    };
    headTsAtHist = function (k) {
      const map = new Map();
      for (const c of courses) {
        let sw = 0, n = 0;
        for (const p of c.pts) {
          if (p.t > 0.6) break;
          sw += blendH(p._hw, k); n++;
        }
        map.set(c, headT(c, n ? sw / n : 0));
      }
      return map;
    };
  }

  // ------------------------------------------------ rain (History mode) ----
  // Observed weekly rainfall (rainfall.js, EA Hydrology daily archive summed
  // on the same Monday axis) falls on the block while the History scrubber
  // walks a wet week — the recharge story: rain lands, and weeks later the
  // water table answers. Drop count scales with the week's measured mm.
  const RAIN = (window.TEST3D_RAIN && window.TEST3D_RAIN.mm
                && window.TEST3D_RAIN.mm.length === HN) ? window.TEST3D_RAIN : null;
  const RAIN_N = 800;
  let rainActive = 0, rainPts = null, animateRain = null;
  if (RAIN) {
    const streakTex = (() => {
      const cv = document.createElement("canvas");
      cv.width = 16; cv.height = 64;
      const g = cv.getContext("2d");
      const grad = g.createLinearGradient(0, 0, 0, 64);
      grad.addColorStop(0, "rgba(190,220,255,0)");
      grad.addColorStop(0.35, "rgba(190,220,255,0.85)");
      grad.addColorStop(1, "rgba(190,220,255,0)");
      g.fillStyle = grad;
      g.fillRect(6, 0, 4, 64);
      return new THREE.CanvasTexture(cv);
    })();
    const rGeo = new THREE.BufferGeometry();
    const rPos = new Float32Array(RAIN_N * 3);
    const rSpd = new Float32Array(RAIN_N);
    for (let i = 0; i < RAIN_N; i++) {
      rPos[i * 3] = hash(i, 71) * W;
      rPos[i * 3 + 1] = -999;
      rPos[i * 3 + 2] = hash(i, 83) * H;
      rSpd[i] = 11 + 7 * hash(i, 97);
    }
    rGeo.setAttribute("position", new THREE.BufferAttribute(rPos, 3));
    rainPts = new THREE.Points(rGeo, new THREE.PointsMaterial({
      size: 0.85, map: streakTex, transparent: true, opacity: 0.55,
      depthWrite: false, blending: THREE.AdditiveBlending,
      color: 0xbedcff, sizeAttenuation: true,
    }));
    rainPts.frustumCulled = false;
    rainPts.visible = false;
    scene.add(rainPts);
    const CASE_TOP = 245;                     // drops enter under the case lid
    animateRain = function (dt) {
      for (let i = 0; i < RAIN_N; i++) {
        if (i >= rainActive) { rPos[i * 3 + 1] = -999; continue; }
        let y = rPos[i * 3 + 1];
        if (y < -100) {                        // (re)spawn staggered from the top
          rPos[i * 3] = 0.3 + hash(i, uTime.value | 0) * (W - 0.6);
          rPos[i * 3 + 2] = 0.3 + hash(uTime.value | 0, i) * (H - 0.6);
          y = Y(CASE_TOP) * (1 + hash(i, 7));
        }
        y -= rSpd[i] * dt * VS * 60;           // fall in scene units
        const gY = Y(groundAt(rPos[i * 3], rPos[i * 3 + 2]));
        rPos[i * 3 + 1] = y < gY + 0.05 ? -999 : y;
      }
      rGeo.attributes.position.needsUpdate = true;
    };
  }

  // --------------------------------------------- abstraction layer (step 4) --
  // Licensed abstraction points (EA Water Rights Trading NALD extract, OGL v3;
  // holder identities stripped at build time). Visually distinct from the thin
  // monitoring instruments: groundwater licences are heavier bronze tubes
  // sized by licensed daily quantity, surface-water licences are downward
  // offtake cones on the surface. The layer shows LICENSED CAPACITY — no live
  // pumping feed exists — and is off by default.
  const ABS = (window.TEST3D_ABSTRACTION && window.TEST3D_ABSTRACTION.points) || null;
  const absGroup = new THREE.Group();
  absGroup.visible = false;
  scene.add(absGroup);
  if (ABS && ABS.length) {
    const gwAbsMat = new THREE.MeshPhongMaterial({
      color: 0x8a5a33, emissive: 0x1a0e04, transparent: true, opacity: 0.85 });
    const swAbsMat = new THREE.MeshPhongMaterial({
      color: 0xb5773d, emissive: 0x1a0e04, transparent: true, opacity: 0.85 });
    const qMax = Math.max(1, ...ABS.map((p) => p.maxDaily || 0));
    for (const p of ABS) {
      const [x, z] = toXZ(p.lon, p.lat);
      if (x < 0 || x > W || z < 0 || z > H) continue;
      const ground = groundAt(x, z);
      const q = Math.sqrt((p.maxDaily || 50) / qMax);      // 0..1, sub-linear
      let mesh;
      if (p.source === "Groundwater") {
        const depth = 22 + 18 * q;                          // ILLUSTRATIVE depth
        const r = 0.10 + 0.22 * q;
        mesh = new THREE.Mesh(new THREE.CylinderGeometry(r, r, 1, 10), gwAbsMat);
        mesh.scale.y = Y(ground) - Y(ground - depth);
        mesh.position.set(x, (Y(ground) + Y(ground - depth)) / 2, z);
      } else {
        const r = 0.14 + 0.30 * q;
        mesh = new THREE.Mesh(new THREE.ConeGeometry(r, 0.55, 10), swAbsMat);
        mesh.rotation.x = Math.PI;                          // point down: offtake
        mesh.position.set(x, Y(ground) + 0.30, z);
      }
      mesh.userData.abs = { ...p, ground };
      absGroup.add(mesh);
    }
  }
  // ------------------------------------------- flow gauges (Rivers pilot) --
  // Gauged river flow (flow.js, EA Hydrology daily means → weekly on the
  // history axis). A gauge is a MEASUREMENT at a point — the drawn ribbon
  // between gauges stays an indication (the card says so). In History mode
  // the markers colour by the gauge's own record terciles and each course's
  // lane-scroll pace follows the measured flow (uFlowSpd).
  const FLOW = (window.TEST3D_FLOW && window.TEST3D_FLOW.gauges) || null;
  const flowGauges = [];
  if (FLOW && FLOW.length) {
    for (const g of FLOW) {
      const [x, z] = toXZ(g.lon, g.lat);
      if (x < 0 || x > W || z < 0 || z > H) continue;
      const ci = nearestCourseInfo(x, z);
      const ground = groundAt(x, z);
      const mat = new THREE.MeshPhongMaterial({
        color: 0x36c9c9, emissive: 0x0a2e2e });
      // instrument look: a flat measuring disc floating over the reach on a
      // thin stem — deliberately unlike boreholes (cones) and licences (tubes)
      const disc = new THREE.Mesh(
        new THREE.CylinderGeometry(0.26, 0.26, 0.07, 16), mat);
      disc.position.set(x, Y(ground) + 0.34, z);
      const stem = new THREE.Mesh(
        new THREE.CylinderGeometry(0.03, 0.03, 0.34, 8), mat);
      stem.position.set(x, Y(ground) + 0.17, z);
      const grp = new THREE.Group();
      grp.add(disc); grp.add(stem);
      disc.userData.flow = g;
      scene.add(grp);
      flowGauges.push({ ...g, x, z, cid: ci.cid, mat, disc, v: null, held: false });
    }
  }
  function updateFlowGauges(histKk) {
    const byCourse = new Map();
    for (const g of flowGauges) {
      let v = null, held = false;
      if (histKk != null) {
        v = g.weekly[histKk];
        if (v == null) {                       // record gap: hold the last value
          for (let k = histKk; k >= 0 && v == null; k--) v = g.weekly[k];
          held = true;
        }
      } else {
        for (let k = g.weekly.length - 1; k >= 0 && v == null; k--) v = g.weekly[k];
      }
      g.v = v; g.held = held;
      const st = v == null ? "near"
        : v < g.t33 ? "below" : v > g.t67 ? "above" : "near";
      g.status = st;
      g.mat.color.copy(statusColor[st]);
      if (histKk != null && v != null && g.p50 > 0) {
        if (!byCourse.has(g.cid)) byCourse.set(g.cid, []);
        byCourse.get(g.cid).push(v / g.p50);
      }
    }
    // lane pace per course: mean flow ratio of its gauges, gentle clamp;
    // courses without a gauge (and every non-history mode) run at 1
    uFlowSpd.value.fill(1);
    if (histKk != null)
      for (const [cid, rs] of byCourse) {
        const r = rs.reduce((a, b) => a + b, 0) / rs.length;
        uFlowSpd.value[cid] = Math.max(0.35, Math.min(2.5, r));
      }
  }
  if (flowGauges.length) updateFlowGauges(null);
  // The tidal limit: where the freshwater story ends. One label per course
  // with a tidal reach (the Test at Redbridge) — the model stops here; the
  // water beyond is the sea's, not the chalk's.
  for (const c of courses) {
    if (c.tidalT == null) continue;
    const p = c.pts[Math.min(c.pts.length - 1,
                             Math.round(c.tidalT * (c.pts.length - 1)))];
    const lbl = makeLabel("· tidal limit ·");
    lbl.position.set(p.x, Y(groundAt(p.x, p.z)) + 0.9, p.z);
    scene.add(lbl);
  }

  function fillFlowCard(g) {
    const wkTxt = (typeof mode !== "undefined" && mode === "history")
      ? (g.held ? `held (record gap), week of ${HWEEKS[histK]}`
                : `observed weekly mean, week of ${HWEEKS[histK]}`)
      : "latest observed weekly mean";
    const pct = g.v != null && g.p50 > 0 ? Math.round((g.v / g.p50) * 100) : null;
    document.getElementById("card").innerHTML =
      `<h3>${g.name}</h3>
       <div class="row"><b>${g.river}</b> — flow gauge</div>
       <div class="row">Flow <b>${g.v != null ? g.v.toFixed(2) : "–"} m³/s</b>
         <span style="color:#8fb0d0">(${wkTxt})</span></div>
       <div class="row">${pct != null ? `<b>${pct}%</b> of its 3-year median (${g.p50.toFixed(2)} m³/s)` : ""}
       </div>
       <div class="row">Status vs its record:
         <b style="color:#${statusColor[g.status].getHexString()}">${g.status}</b></div>
       <div class="note">Gauged measurement (EA Hydrology, OGL v3). Terciles
         from this gauge's own 3-year weekly record, not long-term flow
         statistics. The drawn river between gauges is indicative — only the
         gauges are measured.</div>`;
  }

  function fillAbsCard(a) {
    const fmt = (v) => v == null ? "–" : v.toLocaleString("en-GB");
    document.getElementById("card").innerHTML =
      `<h3>Abstraction licence</h3>
       <div class="row"><b>${a.purpose}</b> — ${a.source.toLowerCase()}</div>
       <div class="row">Licensed max <b>${fmt(a.maxDaily)} m³/day</b>
         · ${fmt(a.maxAnnual)} m³/year</div>
       <div class="row">Licence ${a.licence}</div>
       <div class="note">Licensed capacity, NOT live pumping (no such feed
         exists). Licence-level maxima; tube depth illustrative. Extract covers
         &gt;100 m³/day returns-submitting licences (Jan 2025); some licences,
         incl. security-sensitive supplies, are excluded. EA data, OGL v3.</div>`;
  }

  // ------------------------------------------------------- interaction -----
  // Orbit + PAN control — the block is ~32 x 48 km now, so the eye can move:
  // left-drag orbits, right-drag (or two-finger drag) pans, arrows/WASD pan,
  // double-click on empty ground recentres. Gentle idle spin until touched.
  // Default eye is due SOUTH of the block looking north — map orientation.
  let theta = Math.PI / 2, phi = 1.12, radius = 55, spinning = true;
  function applyCamera() {
    camera.position.set(
      target.x + radius * Math.sin(phi) * Math.cos(theta),
      target.y + radius * Math.cos(phi),
      target.z + radius * Math.sin(phi) * Math.sin(theta));
    camera.lookAt(target);
  }
  // move the look-at point in the ground plane, camera-relative, clamped to
  // the block (dx = screen-right, dy = screen-down, in scene km)
  function pan(dx, dy) {
    const rx = Math.sin(theta), rz = -Math.cos(theta);   // screen-right on ground
    const fx = -Math.cos(theta), fz = -Math.sin(theta);  // screen-up on ground
    target.x = Math.max(0, Math.min(W, target.x + rx * dx - fx * dy));
    target.z = Math.max(0, Math.min(H, target.z + rz * dx - fz * dy));
  }
  const stopSpin = () => {
    spinning = false;
    document.getElementById("spin").checked = false;
  };
  // HUD compass (top right): the needle tracks where north is as the eye
  // orbits; clicking snaps back to the default north-up view.
  const HOME = { theta: Math.PI / 2, phi: 1.12, radius: 55,
                 x: W * 0.52, z: H * 0.48 };
  const needleEl = document.querySelector("#compass svg");
  function updateCompass() {
    if (!needleEl) return;
    // screen bearing of world north (-z) for the current azimuth
    const deg = Math.atan2(Math.cos(theta), Math.sin(theta)) * 180 / Math.PI;
    needleEl.style.transform = `rotate(${deg}deg)`;
  }
  const compassBtn = document.getElementById("compass");
  if (compassBtn) compassBtn.addEventListener("click", () => {
    interruptStory();
    stopSpin();
    theta = HOME.theta; phi = HOME.phi; radius = HOME.radius;
    target.x = HOME.x; target.z = HOME.z;
  });
  const pointers = new Map();
  let pinchDist = 0;
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener("pointerdown", (e) => {
    interruptStory();
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, pan: e.button === 2 });
    stopSpin();
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    const p = pointers.get(e.pointerId);
    if (!p) { hover(e); return; }
    if (pointers.size === 1) {
      if (p.pan) {
        const k = radius * 0.0011;                       // zoomed out = faster
        pan(-(e.clientX - p.x) * k, -(e.clientY - p.y) * k);
      } else {
        theta += (e.clientX - p.x) * 0.005;
        phi = Math.max(0.25, Math.min(1.45, phi - (e.clientY - p.y) * 0.004));
      }
    }
    if (pointers.size === 2) {
      // two fingers: pinch zooms, the midpoint's travel pans
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinchDist) radius = Math.max(9, Math.min(120, radius * pinchDist / d));
      pinchDist = d;
      const k = radius * 0.0011;
      pan(-(e.clientX - p.x) * k / 2, -(e.clientY - p.y) * k / 2);
    }
    p.x = e.clientX; p.y = e.clientY;
  });
  const endPointer = (e) => { pointers.delete(e.pointerId); pinchDist = 0; };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    interruptStory();
    radius = Math.max(9, Math.min(120, radius * (1 + e.deltaY * 0.001)));
  }, { passive: false });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { interruptStory(); return; }
    const k = e.key.toLowerCase();
    const step = radius * 0.045;
    const moves = {
      arrowleft: [-step, 0], a: [-step, 0], arrowright: [step, 0], d: [step, 0],
      arrowup: [0, -step], w: [0, -step], arrowdown: [0, step], s: [0, step],
    };
    if (!(k in moves)) return;
    e.preventDefault();
    interruptStory();
    stopSpin();
    pan(...moves[k]);
  });

  // hover / click → borehole card
  const ray = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  let selected = null, selectedFlow = null;
  const pickV = new THREE.Vector3();
  function pick(e) {
    const r = canvas.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1,
              -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const targets = boreholes.map((b) => b.casing)
      .concat(flowGauges.map((g) => g.disc))
      .concat(absGroup.visible ? absGroup.children : []);
    const hits = ray.intersectObjects(targets);
    if (hits.length) {
      const obj = hits[0].object;
      if (obj.userData.abs) return { abs: obj.userData.abs };
      if (obj.userData.flow) return { flow: flowGauges.find((g) => g.disc === obj) };
      return boreholes.find((b) => b.casing === obj) || null;
    }
    // Forgiving fallback: the tubes are 2-3 px wide from a distance, so a
    // precise-miss picks the nearest instrument within a fingertip's radius
    // on SCREEN instead of demanding a pixel hunt.
    const ex = e.clientX - r.left, ey = e.clientY - r.top;
    let best = null, bestD = 16;             // px
    const consider = (x, y, z, tag) => {
      pickV.set(x, y, z).project(camera);
      if (pickV.z > 1) return;               // behind the camera
      const px = ((pickV.x + 1) / 2) * r.width;
      const py = ((1 - pickV.y) / 2) * r.height;
      const d = Math.hypot(px - ex, py - ey);
      if (d < bestD) { bestD = d; best = tag; }
    };
    for (const b of boreholes)
      consider(b.x, Y(b.ground) + 0.2, b.z, b);
    for (const g of flowGauges)
      consider(g.x, Y(groundAt(g.x, g.z)) + 0.34, g.z, { flow: g });
    if (absGroup.visible)
      for (const m of absGroup.children)
        consider(m.position.x, m.position.y, m.position.z, { abs: m.userData.abs });
    return best;
  }
  function hover(e) { canvas.style.cursor = pick(e) ? "pointer" : "grab"; }
  canvas.addEventListener("dblclick", (e) => {
    if (pick(e)) return;                    // instruments keep their click cards
    const r = canvas.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1,
              -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hits = ray.intersectObject(terMesh);
    if (!hits.length) return;
    target.x = Math.max(0, Math.min(W, hits[0].point.x));
    target.z = Math.max(0, Math.min(H, hits[0].point.z));
  });
  canvas.addEventListener("click", (e) => {
    const hit = pick(e);
    document.getElementById("card").style.display = hit ? "block" : "none";
    if (hit && hit.abs) {
      selected = null;                    // abstraction cards are static
      fillAbsCard(hit.abs);
    } else if (hit && hit.flow) {
      selected = null; selectedFlow = hit.flow;
      fillFlowCard(hit.flow);
    } else {
      selectedFlow = null;
      selected = hit || null;
      if (hit) fillCard(hit);
    }
  });
  function fillCard(b) {
    const dtw = b.ground - b.wt;
    const statusRow =
      `<div class="row">Status vs normal:
         <b style="color:#${statusColor[b.status].getHexString()}">${b.status}</b></div>`;
    const shown = (typeof mode !== "undefined" && mode === "forecast")
      ? (frame === 0
          ? (b.fan ? "modelled today" : "as measured")
          : frame <= NFAN
            ? (b.fan ? `P50 for ${FRAMES[frame]}` : "held (no forecast)")
            : (b.seasonal ? `seasonal monthly P50, ${monthWord(FRAMES[frame])} — experimental`
                          : b.fan ? "held (no seasonal outlook)" : "held (no forecast)"))
      : (typeof mode !== "undefined" && mode === "history")
        ? (b.h50
            ? (b.hObs && b.hObs[histK]
                ? `observed, week of ${HWEEKS[histK]}`
                : "held (record gap)")
            : "held (no usable history)")
        : "at slider season";
    document.getElementById("card").innerHTML = b.real
      ? `<h3>${b.name}</h3>
         <div class="row">Ground <b>${b.ground.toFixed(0)} m OD</b> (OS Terrain 50)</div>
         <div class="row">Level <b>${b.wt.toFixed(2)} m OD</b> — ${dtw.toFixed(1)} m below ground
           <span style="color:#8fb0d0">(${shown})</span></div>
         <div class="row">Measured <b>${b.level.toFixed(2)} m OD</b> on ${b.obsDate};
           normal envelope ${b.p10min.toFixed(1)}–${b.p90max.toFixed(1)} m</div>
         ${statusRow}
         <div class="row"><a href="https://groundwatercast.com/b/${b.slug}/"
           target="_blank" rel="noopener" style="color:#9fd0ff">Open its forecast page ↗</a></div>
         <div class="note">Real station (EA data, OGL v3). Slider = its own
           normals envelope, indicative away from the measured date.</div>`
      : `<h3>${b.name}</h3>
         <div class="row">Ground <b>${b.ground.toFixed(0)} m OD</b> · hole <b>${b.depth} m</b> deep</div>
         <div class="row">Water table <b>${b.wt.toFixed(1)} m OD</b> — ${dtw.toFixed(1)} m below ground</div>
         ${statusRow}
         <div class="note">Illustrative values — concept only.</div>`;
  }

  // --------------------------------------------------------------- UI ------
  const seasonEl = document.getElementById("season");
  const opacityEl = document.getElementById("opacity");
  const playEl = document.getElementById("play");
  const spinEl = document.getElementById("spin");
  const wordEl = document.getElementById("season-word");
  const ghostsEl = document.getElementById("ghosts");
  let season = Number(seasonEl.value) / 100;
  let playing = false;
  // mode is DERIVED from the timeline position now: "history" left of the
  // today tick, "forecast" from it on; "season" only as the no-data fallback
  let mode = FRAMES ? "forecast" : "season";
  let frame = 0, histK = HN ? HN - 1 : 0;
  function setSeason(s) {
    season = s;
    if (mode === "season") seasonEl.value = String(Math.round(s * 100));
    updateWaterTable(s);
    rebuildRivers(s);
    updateBoreholes(s);
    wordEl.textContent =
      s < -0.55 ? "deep drought" : s < -0.18 ? "dry autumn" :
      s < 0.18 ? "normal" : s < 0.55 ? "wet spring" : "winter high";
  }
  function setFrame(k) {
    frame = Math.max(0, Math.min(FRAMES.length - 1, Math.round(k)));
    wtFrame(frame, ghostsEl.checked);
    rebuildRivers(0, headTsAtFrame(frame));
    updateBoreholes(0, frame);
    wordEl.textContent = frame === 0 ? "today"
      : frame <= NFAN ? FRAMES[frame].slice(5) + " P50"
      : monthWord(FRAMES[frame]) + " szn";
    if (selected) fillCard(selected);
  }
  function setHist(k) {
    histK = Math.max(0, Math.min(HN - 1, Math.round(k)));
    histFrame(histK);
    rebuildRivers(0, headTsAtHist(histK));
    updateBoreholes(0, null, histK);
    let word = HWEEKS[histK].slice(8, 10) + " " + monthWord(HWEEKS[histK]);
    if (RAIN) {
      const mm = RAIN.mm[histK];
      rainActive = mm ? Math.min(RAIN_N, Math.round(mm * 10)) : 0;
      if (mm != null) word += " · " + Math.round(mm) + "mm";
    }
    wordEl.textContent = word;
    updateFlowGauges(histK);
    if (selectedFlow) fillFlowCard(selectedFlow);
    if (selected) fillCard(selected);
  }
  function monthWord(monthStart) {
    const m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return m[Number(monthStart.slice(5, 7)) - 1] + " ’" + monthStart.slice(2, 4);
  }
  // ------------------------------------------- THE UNIFIED TIMELINE --------
  // One axis: three observed years, the today tick, the 14-day fan, the
  // seasonal months. History and Forecast stopped being modes — they are
  // REGIONS of the same slider, and the scene's dressing (rain, ghosts,
  // gauge pace) follows the region the thumb is in.
  const HN0 = histFrame ? HN : 0;
  const TL = HN0 + (FRAMES ? FRAMES.length : 0);
  let tk = HN0;                              // timeline position; HN0 = today
  // The TRACK is piecewise: 156 weekly frames would leave the forecast a
  // sliver, so history gets SPLIT of the physical slider and the forecast
  // gets the rest — the thumb moves week-by-week on the left of the today
  // tick and day/month-by-frame on the right.
  const SLMAX = 1000;
  const SPLIT = (HN0 && FRAMES) ? 0.7 : (HN0 ? 1 : 0);
  const NF = FRAMES ? FRAMES.length : 0;
  // Within the forecast section the fan is 14 near-identical daily frames
  // while the months are where the story lives — so the fan gets FSPLIT of
  // the section and the seasonal months get the rest.
  const NSEA = Math.max(0, NF - 1 - NFAN);    // seasonal frames after the fan
  const FSPLIT = NSEA ? 0.2 : 1;
  function fcFrac(j) {                        // forecast frame j -> [0, 1]
    if (j <= NFAN) return NFAN ? (j / NFAN) * FSPLIT : 0;
    return FSPLIT + (1 - FSPLIT) * ((j - NFAN) / NSEA);
  }
  function tkToSlider(k) {
    if (k < HN0)                              // history: [0, split) exclusive —
      return (k / HN0) * SPLIT * SLMAX;       // the split itself IS today
    return SLMAX * (SPLIT + (1 - SPLIT) * fcFrac(k - HN0));
  }
  function sliderToTk(v) {
    if (HN0 && v < SPLIT * SLMAX)
      return Math.min(HN0 - 1, Math.round((v / (SPLIT * SLMAX)) * HN0));
    const f = (v - SPLIT * SLMAX) / ((1 - SPLIT) * SLMAX || 1);
    const j = f <= FSPLIT
      ? Math.round((f / FSPLIT) * NFAN)
      : NFAN + Math.round(((f - FSPLIT) / (1 - FSPLIT)) * NSEA);
    return HN0 + Math.max(0, Math.min(NF - 1, j));
  }
  function setTimeline(k) {
    tk = Math.max(0, Math.min(TL - 1, Math.round(k)));
    seasonEl.value = String(Math.round(tkToSlider(tk)));
    mode = tk < HN0 ? "history" : "forecast";
    if (rainPts) rainPts.visible = mode === "history";
    if (ghost10 && mode !== "forecast") ghost10.visible = ghost90.visible = false;
    if (mode !== "history" && flowGauges.length) {
      updateFlowGauges(null);                // latest colours, lane pace to 1
      if (selectedFlow) fillFlowCard(selectedFlow);
    }
    const rw = document.getElementById("region-word");
    if (rw) rw.textContent = mode === "history"
      ? "observed history (EA measurements)"
      : tk - HN0 > NFAN
        ? "seasonal outlook — experimental"
        : "published 14-day forecast (P50)";
    if (mode === "history") setHist(tk);
    else setFrame(tk - HN0);
  }
  if (TL) {
    seasonEl.min = "0"; seasonEl.max = String(SLMAX); seasonEl.step = "1";
    document.getElementById("slider-label").textContent = "Timeline";
    document.getElementById("mode-ctl").style.display = "flex";
    // (the old caption under the track overlapped the tick labels — the
    // markers now carry its content, and "experimental" lives in the
    // Showing readout)
    // the today tick sits exactly at the history/forecast split
    if (HN0 && FRAMES) {
      const tick = document.getElementById("today-tick");
      tick.style.display = "block";
      tick.style.left = (SPLIT * 100).toFixed(1) + "%";
    }
    // track markers: each January in the observed years + the end of the fan
    const marks = document.getElementById("tl-marks");
    if (marks && HN0) {
      const addTick = (frac, label) => {
        const el = document.createElement("i");
        el.className = "tl-tick";
        el.style.left = (100 * frac).toFixed(2) + "%";
        const b = document.createElement("b");
        b.textContent = label;
        el.appendChild(b);
        marks.appendChild(el);
      };
      let seen = HWEEKS[0].slice(0, 4);
      for (let k = 1; k < HN0; k++) {
        const y = HWEEKS[k].slice(0, 4);
        if (y !== seen) {
          seen = y;
          addTick(tkToSlider(k) / SLMAX, y);
        }
      }
      if (NF > 1) addTick(tkToSlider(HN0 + NFAN) / SLMAX, "day 14");
      // unlabelled ticks give the seasonal months their rhythm; the readout
      // names whichever one the thumb is on
      for (let j = NFAN + 1; j < NF; j++)
        addTick(tkToSlider(HN0 + j) / SLMAX, "");
    }
    ghostsEl.addEventListener("change", () => { if (mode === "forecast") setTimeline(tk); });
  }
  seasonEl.addEventListener("input", () => {
    playing = false; playEl.textContent = "▶";
    if (TL) setTimeline(sliderToTk(Number(seasonEl.value)));
    else setSeason(Number(seasonEl.value) / 100);
  });
  opacityEl.addEventListener("input", () => { terMat.opacity = Number(opacityEl.value) / 100; });

  // View: "landscape" (the surface story) / "beneath" (ground turns to glass,
  // the water table + its gradient drift become the subject). Presets only —
  // the opacity slider and orbit stay live either way.
  let view = "landscape";
  function setView(v) {
    view = v;
    document.getElementById("view-landscape").classList.toggle("active", v === "landscape");
    document.getElementById("view-beneath").classList.toggle("active", v === "beneath");
    const beneath = v === "beneath";
    terMat.opacity = beneath ? 0.16 : 0.62;
    opacityEl.value = String(Math.round(terMat.opacity * 100));
    wtMat.opacity = beneath ? 0.4 : 0.24;
    driftPts.visible = beneath;
    document.getElementById("drift-key").style.display = beneath ? "" : "none";
    phi = beneath ? 1.32 : 1.12;            // lower eye: look INTO the block
  }
  document.getElementById("view-landscape").addEventListener("click", () => setView("landscape"));
  document.getElementById("view-beneath").addEventListener("click", () => setView("beneath"));

  // ------------------------------------------------------------ story ------
  // A ~70 s guided tour riding the unified timeline: declarative steps of
  // {camera pose, timeline span, view, caption}, eased camera moves, real
  // data throughout. Any input hands control straight back to the visitor.
  const storyBtn = document.getElementById("story");
  const capEl = document.getElementById("story-cap");
  const capText = document.getElementById("story-text");
  const REDUCED = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) { spinning = false; spinEl.checked = false; }
  let story = null;
  const wkIdx = (iso) => {
    const i = HWEEKS ? HWEEKS.findIndex((w) => w >= iso) : -1;
    return i < 0 ? Math.max(0, HN0 - 1) : i;
  };
  function storySteps() {
    const home = { theta: HOME.theta, phi: HOME.phi, radius: HOME.radius,
                   x: HOME.x, z: HOME.z };
    const [bx, bz] = toXZ(-1.37, 51.21);        // Bourne Rivulet headwaters
    const [cx, cz] = toXZ(-1.451, 51.152);      // Chilbolton gauge
    const bourne = { theta: 1.9, phi: 1.15, radius: 18, x: bx, z: bz };
    const chalk = { ...home, phi: 1.32, radius: 42 };
    const mid = { theta: 1.35, phi: 1.05, radius: 30, x: home.x - 2, z: home.z - 3 };
    const chilb = { theta: 1.2, phi: 1.15, radius: 14, x: cx, z: cz };
    return [
      { dur: 6, cam: { ...home, radius: 48 }, view: "landscape", tl: [HN0, HN0],
        cap: "The River Test — a chalk stream fed almost entirely by groundwater. Sixty kilometres, source to sea." },
      { dur: 8, cam: chalk, view: "beneath", tl: [HN0, HN0],
        cap: "Beneath the downs the chalk holds a water table. These boreholes measure it, week after week." },
      { dur: 9, cam: mid, view: "landscape", tl: [wkIdx("2023-10-01"), wkIdx("2024-02-05")],
        cap: "Real rain, replayed: through the winter of 2023–24 it soaks into the chalk…" },
      { dur: 8, cam: bourne, view: "landscape", tl: [wkIdx("2024-02-05"), wkIdx("2024-05-01")],
        cap: "…and weeks later the aquifer answers. The winterbournes walk upstream as the chalk fills." },
      { dur: 8, cam: { ...bourne, radius: 24 }, view: "landscape", tl: [wkIdx("2024-05-01"), wkIdx("2025-09-01")],
        cap: "Dry seasons pull the table back down — the young rivers retreat. Every frame is a measurement." },
      { dur: 7, cam: chilb, view: "landscape", tl: [wkIdx("2025-09-01"), wkIdx("2026-03-01")],
        cap: "River gauges record the flow. The water's pace here follows what they measured." },
      { dur: 6, cam: { ...home, radius: 50 }, view: "landscape", tl: [wkIdx("2026-03-01"), HN0],
        cap: "Which brings us to today." },
      { dur: 10, cam: home, view: "landscape", tl: [HN0, TL - 1], ghosts: true,
        cap: "And the forecast: the same aquifer, continued six months ahead — with its uncertainty shown honestly." },
      { dur: 6, cam: home, view: "landscape", tl: [TL - 1, TL - 1],
        cap: "Explore for yourself — drag to orbit, pan with the arrow keys, click any instrument." },
    ];
  }
  function enterStep() {
    const st = story.steps[story.i];
    capText.textContent = st.cap;
    story.from = { theta, phi, radius, x: target.x, z: target.z };
    setView(st.view);
    phi = story.from.phi;                     // the tour eases phi itself
    if (st.ghosts != null) ghostsEl.checked = st.ghosts;
    story.t = 0;
  }
  function startStory() {
    stopSpin();
    playing = false; playEl.textContent = "▶";
    document.getElementById("controls").style.display = "none";
    document.getElementById("timeline-bar").style.display = "none";
    document.getElementById("card").style.display = "none";
    selected = null; selectedFlow = null;
    storyBtn.textContent = "■ Stop the tour";
    capEl.classList.add("on");
    story = { steps: storySteps(), i: 0, t: 0, from: null };
    enterStep();
  }
  function stopStory() {
    if (!story) return;
    story = null;
    capEl.classList.remove("on");
    document.getElementById("controls").style.display = "";
    document.getElementById("timeline-bar").style.display = "";
    storyBtn.textContent = "▶ Take the tour";
    setView("landscape");
  }
  function interruptStory() { if (story) stopStory(); }
  function advanceStory(dt) {
    const st = story.steps[story.i];
    story.t += dt;
    const p = Math.min(1, story.t / st.dur);
    const e = REDUCED ? 1 : p * p * (3 - 2 * p);          // smoothstep ease
    const f = story.from, c = st.cam;
    theta = f.theta + (c.theta - f.theta) * e;
    phi = f.phi + (c.phi - f.phi) * e;
    radius = f.radius + (c.radius - f.radius) * e;
    target.x = f.x + (c.x - f.x) * e;
    target.z = f.z + (c.z - f.z) * e;
    const k = Math.round(st.tl[0] + (st.tl[1] - st.tl[0]) * p);
    if (k !== tk) setTimeline(k);
    if (p >= 1) {
      story.i += 1;
      if (story.i >= story.steps.length) stopStory();
      else enterStep();
    }
  }
  if (TL && storyBtn) {
    storyBtn.style.display = "";
    storyBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (story) stopStory(); else startStory();
    });
  }

  playEl.addEventListener("click", () => { playing = !playing; playEl.textContent = playing ? "❚❚" : "▶"; });
  spinEl.addEventListener("change", () => { spinning = spinEl.checked; });
  const absEl = document.getElementById("abs");
  if (absEl) absEl.addEventListener("change", () => { absGroup.visible = absEl.checked; });

  // Mobile attribution: the OGL credit collapses to an (i) chip on small
  // screens instead of vanishing — tapping toggles the full text.
  const attrEl = document.getElementById("disclaimer");
  if (attrEl) {
    if (window.innerWidth <= 640) attrEl.classList.add("collapsed");
    attrEl.addEventListener("click", () => {
      if (window.innerWidth > 640) return;
      attrEl.classList.toggle("collapsed");
      attrEl.classList.toggle("expanded");
    });
  }

  function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }
  window.addEventListener("resize", resize);

  // Dev/verification hook: lets a console (or an automated check) probe the
  // scalar fields and course profiles without reaching into closures.
  window.TEST3D_DEBUG = {
    groundAt: (x, z) => groundAt(x, z),
    waterTableAt, toXZ, courses, boreholes, updateBoreholes,
    usingDEM: !!demElev,
    usingRealStations: !!STN,
    usingRealRivers: !!(RIV && RIV.length),
    FRAMES, setFrame: FRAMES ? setFrame : null,
    setTimeline: TL ? setTimeline : null, TL, HN0, tk: () => tk,
    startStory, stopStory, advanceStory, storyActive: () => !!story,
    pick, camera, tkToSlider, sliderToTk,
    headTsAtFrame, wtMeshY: (i) => wPos.getY(i),
    absGroup, nAbstraction: absGroup.children.length,
    setView, sheetYm, animateDrift, driftPts, renderer,
    HWEEKS, setHist: histFrame ? setHist : null, nHistBHs: histBHs.length,
    rainPts, animateRain, rainActive: () => rainActive,
    flowGauges, updateFlowGauges, uFlowSpd,
    target, pan, W, H,
    // forces a frame even in a hidden tab (rAF never fires there) — lets an
    // automated check surface shader-compile errors, read renderer.info and
    // capture the canvas (toDataURL is valid in the same tick as the render)
    renderOnce: () => {
      uTime.value += 0.5;
      resize(); applyCamera(); updateCompass();
      renderer.render(scene, camera);
      const loader = document.getElementById("loader");
      if (loader) { loader.classList.add("done"); setTimeout(() => loader.remove(), 800); }
      return renderer.info.render;
    },
  };

  // -------------------------------------------------------------- run ------
  if (TL) setTimeline(HN0); else setSeason(0.15);   // open at the today tick
  let last = performance.now(), playT = Math.asin(0.15), frameAcc = 0;
  function tick(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    resize();
    if (story) advanceStory(dt);
    if (spinning) theta += dt * 0.055;
    if (playing) {
      if (TL) {
        // one continuous tour: brisk through the observed years, slowing to
        // savour the forecast fortnight and the seasonal months
        frameAcc += dt * (tk < HN0 ? 7 : 2.2);
        if (frameAcc >= 1) {
          frameAcc = 0;
          setTimeline((tk + 1) % TL);
        }
      } else {
        playT += dt * 0.35;
        setSeason(Math.sin(playT));
      }
    }
    applyCamera();
    updateCompass();
    animateSprings(now / 1000);
    if (driftPts.visible) animateDrift(dt);
    if (rainPts && rainPts.visible && (rainActive || 0) >= 0) animateRain(dt);
    uTime.value = now / 1000;                    // drives every water shader
    for (const s of shafts) {                    // shafts sway + breathe
      s.mesh.rotation.y = s.baseY + 0.12 * Math.sin(uTime.value * 0.18 + s.phase);
      s.mesh.material.opacity = 0.075 + 0.045 * (0.5 + 0.5 * Math.sin(uTime.value * 0.33 + s.phase));
    }
    renderer.render(scene, camera);
    const loader = document.getElementById("loader");
    if (loader) {                              // first real frame is on screen
      loader.classList.add("done");
      setTimeout(() => loader.remove(), 800);
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // tiny hook for headless smoke tests (and console tinkering)
  window.TEST3D = {
    setSeason,
    view(t, p, r) { theta = t; phi = p; radius = r; spinning = false; },
    select(name) {
      selected = boreholes.find((b) => b.name === name) || null;
      document.getElementById("card").style.display = selected ? "block" : "none";
      if (selected) fillCard(selected);
      return selected && { ...selected, column: undefined, colMat: undefined, casing: undefined };
    },
  };
})();

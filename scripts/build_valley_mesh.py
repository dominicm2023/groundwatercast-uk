"""Adaptive terrain mesh for the valley-3D prototype (valley3d visual step 2).

The uniform render grid wastes triangles on featureless interfluves while the
valley floors — where the water lives and the eye goes — get ~143 m cells.
This builds an ADAPTIVE Delaunay triangulation instead: ~55 m sampling within
the river corridors, coarsening in rings to ~300 m on the peaks. Heights are
NOT stored — the prototype fills vertex y from its own groundAt() (the 50 m
heightmap), so the render mesh and every scalar field stay exactly consistent.

Emits mesh.js: quantised uint16 x/z (over the block extent) + uint32 triangle
indices, base64. ~5x more useful detail in the valleys for fewer total
triangles than the regular grid.

Usage:
  python -m scripts.build_valley_mesh --js web/valley/test/mesh.js
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from scipy.spatial import Delaunay, cKDTree

ROOT = Path(__file__).resolve().parents[1]
RIVERS_JS = ROOT / "web/valley/test/rivers.js"
DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)

# (max distance to a course in km, sample spacing in km) — innermost first.
# Sized to keep the source-to-sea block under 65,536 verts (uint16 indices,
# half the payload): the water drape is fragment-true against the 16 m ground
# texture since the B+E shader, so the MESH no longer needs 32 m fidelity at
# the river — the ~28 m thalweg densification below keeps the channel crisp.
RINGS = [(0.30, 0.052), (1.2, 0.13), (3.0, 0.22), (1e9, 0.34)]


def load_course_points(bbox):
    """Course waypoints from rivers.js, converted to scene km (same mapping
    as main.js toXZ)."""
    txt = RIVERS_JS.read_text(encoding="ascii")
    data = json.loads(re.search(r"window\.TEST3D_RIVERS = (.*);", txt).group(1))
    lon_min, lat_min, lon_max, lat_max = bbox
    kx = 111.32 * math.cos(math.radians(51.12))
    kz = 111.2
    pts = []
    for wc in data["watercourses"]:
        for lon, lat, _ in wc["waypoints"]:
            pts.append(((lon - lon_min) * kx, (lat_max - lat) * kz))
    return np.array(pts), (lon_max - lon_min) * kx, (lat_max - lat_min) * kz


def build(bbox):
    course_pts, W, H = load_course_points(bbox)
    tree = cKDTree(course_pts)
    rng = np.random.default_rng(42)          # deterministic jitter
    pts = []
    # ring-graded jittered grids, keeping each ring's band only
    prev_d = 0.0
    for max_d, spacing in RINGS:
        nx = int(W / spacing) + 1
        nz = int(H / spacing) + 1
        xs = np.linspace(0, W, nx)
        zs = np.linspace(0, H, nz)
        gx, gz = np.meshgrid(xs, zs)
        g = np.column_stack([gx.ravel(), gz.ravel()])
        jitter = (rng.random(g.shape) - 0.5) * spacing * 0.55
        # keep the block border unjittered so the walls seam tightly
        border = (g[:, 0] < 1e-9) | (g[:, 0] > W - 1e-9) \
               | (g[:, 1] < 1e-9) | (g[:, 1] > H - 1e-9)
        jitter[border] = 0
        g = g + jitter
        g[:, 0] = np.clip(g[:, 0], 0, W)
        g[:, 1] = np.clip(g[:, 1], 0, H)
        d, _ = tree.query(g)
        band = (d >= prev_d) & (d < max_d) if prev_d else (d < max_d)
        # border points always belong to the ring that reaches them first
        pts.append(g[band | (border & (d >= prev_d))])
        prev_d = max_d
    # the course polylines themselves, densified — the water drape needs the
    # mesh to actually bend along the thalweg
    seg = []
    for i in range(len(course_pts) - 1):
        a, b = course_pts[i], course_pts[i + 1]
        if np.hypot(*(b - a)) > 1.0:          # different course — don't bridge
            continue
        n = max(2, int(np.hypot(*(b - a)) / 0.028))
        for k in range(n):
            seg.append(a + (b - a) * (k / n))
    pts.append(np.array(seg))
    P = np.vstack(pts)
    # dedupe near-coincident points (Delaunay hates duplicates)
    P = np.unique(np.round(P / 0.012).astype(np.int64), axis=0) * 0.012
    P[:, 0] = np.clip(P[:, 0], 0, W)
    P[:, 1] = np.clip(P[:, 1], 0, H)
    tri = Delaunay(P)
    return P, tri.simplices.astype(np.uint32), W, H


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float, default=list(DEFAULT_BBOX))
    ap.add_argument("--js", type=Path, required=True)
    args = ap.parse_args()
    P, tris, W, H = build(tuple(args.bbox))
    qx = np.round(P[:, 0] / W * 65535).astype(np.uint16)
    qz = np.round(P[:, 1] / H * 65535).astype(np.uint16)
    idx16 = len(P) < 65536                     # uint16 indices halve the payload
    tri_bytes = (tris.astype(np.uint16) if idx16 else tris).tobytes()
    payload = {
        "nVerts": int(len(P)), "nTris": int(len(tris)),
        "w": W, "h": H, "idx16": idx16,
        "x": base64.b64encode(qx.tobytes()).decode(),
        "z": base64.b64encode(qz.tobytes()).decode(),
        "tri": base64.b64encode(tri_bytes).decode(),
    }
    args.js.write_text(
        "// Generated by scripts/build_valley_mesh.py - adaptive terrain mesh\n"
        "// (Delaunay, ~55 m at the rivers -> ~300 m on the interfluves).\n"
        f"window.TEST3D_MESH = {json.dumps(payload)};\n", encoding="ascii")
    print(f"{len(P):,} verts / {len(tris):,} tris "
          f"-> {args.js} ({args.js.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

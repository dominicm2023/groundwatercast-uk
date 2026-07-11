"""EA LIDAR corridor tier for the valley-3D prototype (the data upgrade).

Consumes EA **LIDAR Composite DTM 1m** GeoTIFF tiles (OGL v3) dropped into
``data/raw/lidar/`` (any folder layout; ``.tif`` discovered recursively) and
bakes a SPARSE corridor elevation layer: the block is divided into 256 m
cells; cells within ``CORRIDOR_KM`` of a watercourse get a 32x32 grid of 8 m
elevations (mean-downsampled from the 1 m data). The prototype's groundAt()
prefers this layer inside the corridor and falls back to the 50 m OS Terrain
50 heightmap elsewhere — so riverbank landforms become real where the water
lives, at a fraction of a full-resolution payload.

Getting the tiles (one-time, ~24 x 5 km tiles for the Test block):
  1. https://environment.data.gov.uk/survey  (Defra Survey Data Download)
  2. Draw a box over the Test valley (Ashe -> Romsey) or select the tiles
     printed by --list-tiles.
  3. Product: "LIDAR Composite DTM" / latest year / 1 m -> download zips.
  4. Unzip anywhere under data/raw/lidar/ and rerun this script.

Georeferencing is read from the TIFF tags (ModelTiepoint + PixelScale, EPSG
27700) — no GDAL needed, just tifffile.
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

from scripts.build_terrain_tile import wgs84_to_osgb
from scripts.build_valley_rivers import osgb_to_wgs84  # noqa: F401 (parity)

ROOT = Path(__file__).resolve().parents[1]
LIDAR_DIR = ROOT / "data/raw/lidar"
RIVERS_JS = ROOT / "web/valley/test/rivers.js"
DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)
# Corridor + resolution sized for the consumers: the water drape paints only
# within 0.55 km of a course, and the 16 m ground texture / ~50 m mesh sample
# through bilinear anyway — ~10.7 m cells lose nothing visible and cost half
# the payload of the original 8 m tier.
CORRIDOR_KM = 0.6
CELL_M = 256.0            # sparse cell edge
SUB = 24                  # samples per cell edge (=> ~10.7 m)


def course_scene_points(bbox):
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


def _pool4(arr):
    """4x4 nan-mean pooling: 1 m -> 4 m, 1/16th the memory. The corridor is
    sampled at 8 m anyway, so nothing visible is lost."""
    h, w = arr.shape
    h4, w4 = h - h % 4, w - w % 4
    a = arr[:h4, :w4].reshape(h4 // 4, 4, w4 // 4, 4)
    with np.errstate(invalid="ignore"):
        return np.nanmean(np.nanmean(a, axis=3), axis=1)


def load_tiffs(wanted=None):
    """[(E0, N0, pixel_m, 2D array)] from .tif files AND inside .zip archives
    under data/raw/lidar. ``wanted``: set of 5 km tile codes (e.g. 'su33nw')
    to load — anything else is skipped (the survey box often over-covers)."""
    import io
    import tifffile
    import zipfile

    def read_pages(name, fh):
        with tifffile.TiffFile(fh) as tf:
            page = tf.pages[0]
            tags = {t.name: t.value for t in page.tags.values()}
            tie, scale = tags.get("ModelTiepointTag"), tags.get("ModelPixelScaleTag")
            if tie is None or scale is None:
                print(f"  ! {name}: no georef tags - skipped")
                return None
            arr = page.asarray().astype(np.float32)
            nod = tags.get("GDAL_NODATA")
            if nod is not None:
                arr[arr == float(nod)] = np.nan
            arr[arr < -100] = np.nan
            pm = float(scale[0])
            if pm < 3.9:                       # pool 1 m / 2 m data to ~4 m
                f = int(round(4 / pm))
                arr = _pool4(arr) if f == 4 else arr[::f, ::f]
                pm *= f
            return (float(tie[3]), float(tie[4]), pm, arr)

    def code(name):
        m = re.search(r"(su\d\d[ns][ew])", name.lower())
        return m.group(1) if m else None

    out = []
    for fp in sorted(LIDAR_DIR.rglob("*")):
        if fp.suffix.lower() == ".tif":
            if wanted and code(fp.name) not in wanted:
                continue
            r = read_pages(fp.name, fp)
            if r: out.append(r); print(f"  {fp.name}: @{r[2]:g} m E{r[0]:.0f} N{r[1]:.0f}")
        elif fp.suffix.lower() == ".zip":
            if wanted and code(fp.name) not in wanted:
                continue
            with zipfile.ZipFile(fp) as z:
                for nm in z.namelist():
                    if nm.lower().endswith(".tif"):
                        r = read_pages(f"{fp.name}:{nm}", io.BytesIO(z.read(nm)))
                        if r: out.append(r); print(f"  {fp.name}: @{r[2]:g} m E{r[0]:.0f} N{r[1]:.0f}")
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float, default=list(DEFAULT_BBOX))
    ap.add_argument("--js", type=Path, required=True)
    args = ap.parse_args()
    bbox = tuple(args.bbox)
    lon_min, lat_min, lon_max, lat_max = bbox
    course, W, H = course_scene_points(bbox)
    kx0 = 111.32 * math.cos(math.radians(51.12))
    wanted = set()
    for (sx, sz) in course:
        lon = lon_min + sx / kx0
        lat = lat_max - sz / 111.2
        E, N = wgs84_to_osgb(lon, lat)
        for de in (-600, 0, 600):
            for dn in (-600, 0, 600):
                e, n = E + de, N + dn
                wanted.add(f"su{int(e % 100000 // 10000)}{int(n % 100000 // 10000)}"
                           f"{'s' if (n % 10000) < 5000 else 'n'}"
                           f"{'w' if (e % 10000) < 5000 else 'e'}")
    print(f"corridor needs {len(wanted)} tiles")
    tiffs = load_tiffs(wanted)
    if not tiffs:
        print("No LIDAR tiles found under data/raw/lidar/ — see the module "
              "docstring for the download steps.", file=sys.stderr)
        return 2

    from scipy.spatial import cKDTree
    tree = cKDTree(course)
    kx = 111.32 * math.cos(math.radians(51.12))
    kz = 111.2

    def scene_to_en(x, z):
        lon = lon_min + x / kx
        lat = lat_max - z / kz
        return wgs84_to_osgb(lon, lat)

    ncx = int(W * 1000 / CELL_M) + 1
    ncz = int(H * 1000 / CELL_M) + 1
    cells = []
    lo, hi = 1e9, -1e9
    n_nan = 0
    for cz in range(ncz):
        for cx in range(ncx):
            x0 = cx * CELL_M / 1000.0
            z0 = cz * CELL_M / 1000.0
            ctr = (x0 + CELL_M / 2000.0, z0 + CELL_M / 2000.0)
            d, _ = tree.query(ctr)
            if d > CORRIDOR_KM + CELL_M / 1414000.0:
                continue
            # sample SUB x SUB points at 8 m, mean of the 1 m data via nearest
            grid = np.full((SUB, SUB), np.nan, np.float32)
            for j in range(SUB):
                for i in range(SUB):
                    x = x0 + (i + 0.5) * (CELL_M / SUB) / 1000.0
                    z = z0 + (j + 0.5) * (CELL_M / SUB) / 1000.0
                    E, N = scene_to_en(x, z)
                    for (E0, N0, pm, arr) in tiffs:
                        col = int((E - E0) / pm)
                        row = int((N0 - N) / pm)
                        if 0 <= row < arr.shape[0] and 0 <= col < arr.shape[1]:
                            k = max(1, int(4 / pm))       # ~4 m mean patch
                            v = np.nanmean(arr[row:row + k, col:col + k])
                            if np.isfinite(v):
                                grid[j, i] = v
                            break
            if np.isnan(grid).all():
                continue
            if np.isnan(grid).any():
                n_nan += int(np.isnan(grid).sum())
                med = np.nanmedian(grid)
                grid = np.where(np.isnan(grid), med, grid)
            lo = min(lo, float(grid.min()))
            hi = max(hi, float(grid.max()))
            cells.append((cx, cz, grid))
    if not cells:
        print("No corridor cells covered by the supplied tiles.", file=sys.stderr)
        return 2
    span = max(hi - lo, 1e-6)
    keys, blob = [], bytearray()
    for cx, cz, grid in cells:
        keys.append(cx + cz * ncx)
        q = np.round((grid - lo) / span * 65535).astype("<u2")
        blob += q.tobytes()
    payload = {
        "source": "EA LIDAR Composite DTM 1m (OGL v3), corridor slice at 8 m",
        "cellM": CELL_M, "sub": SUB, "ncx": ncx, "ncz": ncz,
        "w": W, "h": H, "elevMin": round(lo, 2), "elevMax": round(hi, 2),
        "keys": keys,
        "b64": base64.b64encode(bytes(blob)).decode(),
    }
    args.js.write_text(
        "// Generated by scripts/build_lidar_corridor.py.\n"
        "// Contains EA LIDAR data (c) Environment Agency, OGL v3.\n"
        f"window.TEST3D_LIDAR = {json.dumps(payload)};\n", encoding="ascii")
    print(f"{len(cells)} corridor cells ({len(cells) * SUB * SUB:,} samples @ 8 m, "
          f"{n_nan} voids filled) elev {lo:.1f}-{hi:.1f} m "
          f"-> {args.js} ({args.js.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

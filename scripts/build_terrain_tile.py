"""Build a quantised heightmap tile from OS Terrain 50 (valley-3D terrain spike).

Reads the OS Terrain 50 "ASCII Grid" GB zip (OGL v3 — free + commercial-clean,
one-time download via the OS Downloads API into ``data/raw/os_terrain50/``),
mosaics the 10 km tiles covering a WGS84 bbox, samples the DEM on a regular
lon/lat grid, and writes a compact uint16-quantised heightmap.

The WGS84→OSGB36 conversion is the standard 7-parameter Helmert + Airy
Transverse Mercator (~5 m accuracy without the OSTN15 correction grid) — fine
for a visualisation heightmap on a 50 m DEM; do NOT reuse it for survey maths.

Outputs (two shapes, same content):
  --js  PATH   classic-script global for the prototype (no fetch/CORS):
               ``window.TEST3D_TERRAIN = {nx, nz, bounds, elevMin, elevMax, b64}``
               where b64 is little-endian uint16, row-major from the NW corner
               (z = north→south, matching the prototype's scene axes).
  --json PATH  the pack-shaped artifact (same fields, for downstream steps).

Usage (the Test-valley spike):
  python -m scripts.build_terrain_tile --js web/valley/test/terrain.js
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T50_ZIP = ROOT / "data/raw/os_terrain50/terr50_gagg_gb.zip"

# Default bbox = the prototype's block (web/valley/test/data.js bounds).
DEFAULT_BBOX = (-1.66, 50.87, -1.20, 51.30)   # lonMin, latMin, lonMax, latMax
DEFAULT_STEP_M = 100.0                        # ~sample spacing on the ground


# ---------------------------------------------------------------------------
# WGS84 → OSGB36 easting/northing (Helmert + Airy TM; ~5 m, viz-grade only)
# ---------------------------------------------------------------------------

def wgs84_to_osgb(lon_deg: float, lat_deg: float) -> tuple[float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    # -- geodetic → cartesian on GRS80/WGS84
    a1, f1 = 6378137.0, 1 / 298.257223563
    e2_1 = f1 * (2 - f1)
    nu = a1 / math.sqrt(1 - e2_1 * math.sin(lat) ** 2)
    x = nu * math.cos(lat) * math.cos(lon)
    y = nu * math.cos(lat) * math.sin(lon)
    z = nu * (1 - e2_1) * math.sin(lat)
    # -- Helmert WGS84→OSGB36 (official small-angle parameters)
    tx, ty, tz = -446.448, 125.157, -542.060
    rx, ry, rz = (-0.1502 / 3600 * math.pi / 180,
                  -0.2470 / 3600 * math.pi / 180,
                  -0.8421 / 3600 * math.pi / 180)
    s = 20.4894e-6
    x2 = tx + (1 + s) * x - rz * y + ry * z
    y2 = ty + rz * x + (1 + s) * y - rx * z
    z2 = tz - ry * x + rx * y + (1 + s) * z
    # -- cartesian → geodetic on Airy 1830
    a, b = 6377563.396, 6356256.909
    e2 = 1 - (b * b) / (a * a)
    p = math.hypot(x2, y2)
    lat2 = math.atan2(z2, p * (1 - e2))
    for _ in range(6):
        nu2 = a / math.sqrt(1 - e2 * math.sin(lat2) ** 2)
        lat2 = math.atan2(z2 + e2 * nu2 * math.sin(lat2), p)
    lon2 = math.atan2(y2, x2)
    # -- Airy TM projection (National Grid)
    F0, lat0, lon0, E0, N0 = 0.9996012717, math.radians(49), math.radians(-2), 400000.0, -100000.0
    n = (a - b) / (a + b)
    nu2 = a * F0 / math.sqrt(1 - e2 * math.sin(lat2) ** 2)
    rho = a * F0 * (1 - e2) / (1 - e2 * math.sin(lat2) ** 2) ** 1.5
    eta2 = nu2 / rho - 1
    dlat, slat = lat2 - lat0, lat2 + lat0
    M = b * F0 * (
        (1 + n + 1.25 * n * n + 1.25 * n ** 3) * dlat
        - (3 * n + 3 * n * n + 2.625 * n ** 3) * math.sin(dlat) * math.cos(slat)
        + (1.875 * n * n + 1.875 * n ** 3) * math.sin(2 * dlat) * math.cos(2 * slat)
        - (35 / 24) * n ** 3 * math.sin(3 * dlat) * math.cos(3 * slat))
    cos_l, sin_l, tan_l = math.cos(lat2), math.sin(lat2), math.tan(lat2)
    I = M + N0
    II = nu2 / 2 * sin_l * cos_l
    III = nu2 / 24 * sin_l * cos_l ** 3 * (5 - tan_l ** 2 + 9 * eta2)
    IIIA = nu2 / 720 * sin_l * cos_l ** 5 * (61 - 58 * tan_l ** 2 + tan_l ** 4)
    IV = nu2 * cos_l
    V = nu2 / 6 * cos_l ** 3 * (nu2 / rho - tan_l ** 2)
    VI = nu2 / 120 * cos_l ** 5 * (5 - 18 * tan_l ** 2 + tan_l ** 4 + 14 * eta2
                                   - 58 * tan_l ** 2 * eta2)
    dl = lon2 - lon0
    N = I + II * dl ** 2 + III * dl ** 4 + IIIA * dl ** 6
    E = E0 + IV * dl + V * dl ** 3 + VI * dl ** 5
    return E, N


# ---------------------------------------------------------------------------
# OS Terrain 50 ASCII-grid mosaic (lazy: loads only the 10 km tiles the bbox needs)
# ---------------------------------------------------------------------------

_100KM = {  # National Grid 100 km square letters (southern Britain subset + spares)
    "SV": (0, 0), "SW": (1, 0), "SX": (2, 0), "SY": (3, 0), "SZ": (4, 0), "TV": (5, 0),
    "SR": (1, 1), "SS": (2, 1), "ST": (3, 1), "SU": (4, 1), "TQ": (5, 1), "TR": (6, 1),
    "SM": (1, 2), "SN": (2, 2), "SO": (3, 2), "SP": (4, 2), "TL": (5, 2), "TM": (6, 2),
    "SH": (2, 3), "SJ": (3, 3), "SK": (4, 3), "TF": (5, 3), "TG": (6, 3),
    "SC": (2, 4), "SD": (3, 4), "SE": (4, 4), "TA": (5, 4),
    "NW": (1, 5), "NX": (2, 5), "NY": (3, 5), "NZ": (4, 5),
}
_SQ = {v: k for k, v in _100KM.items()}


def _tile_name(e: float, n: float) -> str:
    """10 km tile name for an easting/northing, e.g. (443210, 132900) → 'su41'."""
    sq = _SQ.get((int(e // 100000), int(n // 100000)))
    if sq is None:
        raise ValueError(f"E/N outside supported squares: {e:.0f},{n:.0f}")
    return f"{sq.lower()}{int(e % 100000 // 10000)}{int(n % 100000 // 10000)}"


class Terrain50:
    """Bilinear sampler over the OS T50 GB zip, loading 10 km tiles on demand."""

    def __init__(self, gb_zip: Path):
        self.zf = zipfile.ZipFile(gb_zip)
        # index: 'su41' -> inner zip member path
        self.index = {}
        for nm in self.zf.namelist():
            if nm.endswith(".zip"):
                self.index[Path(nm).stem.split("_")[0].lower()] = nm
        self.tiles: dict[str, tuple] = {}

    def _load(self, name: str):
        if name in self.tiles:
            return self.tiles[name]
        member = self.index.get(name)
        if member is None:
            self.tiles[name] = None            # sea / outside coverage
            return None
        with self.zf.open(member) as fh:
            inner = zipfile.ZipFile(io.BytesIO(fh.read()))
        asc_name = next(n for n in inner.namelist() if n.lower().endswith(".asc"))
        txt = inner.read(asc_name).decode("ascii", errors="replace").split("\n")
        hdr = {}
        i = 0
        while i < len(txt):
            parts = txt[i].split()
            if len(parts) == 2 and parts[0].lower() in (
                    "ncols", "nrows", "xllcorner", "yllcorner", "cellsize", "nodata_value"):
                hdr[parts[0].lower()] = float(parts[1]); i += 1
            else:
                break
        ncols, nrows = int(hdr["ncols"]), int(hdr["nrows"])
        cell = hdr["cellsize"]
        x0, y0 = hdr["xllcorner"], hdr["yllcorner"]
        nodata = hdr.get("nodata_value", -9999.0)
        rows = []
        for r in range(nrows):
            rows.append([float(v) for v in txt[i + r].split()])
        tile = (x0, y0, cell, ncols, nrows, rows, nodata)
        self.tiles[name] = tile
        return tile

    def elev(self, e: float, n: float) -> float:
        """Bilinear elevation (m OD) at easting/northing; 0.0 over sea/nodata."""
        tile = self._load(_tile_name(e, n))
        if tile is None:
            return 0.0
        x0, y0, cell, ncols, nrows, rows, nodata = tile
        cx = (e - x0) / cell - 0.5             # cell centres at ll + (i+0.5)*cell
        cy = (n - y0) / cell - 0.5
        i0 = max(0, min(ncols - 2, int(math.floor(cx))))
        j0 = max(0, min(nrows - 2, int(math.floor(cy))))
        fx = min(1.0, max(0.0, cx - i0))
        fy = min(1.0, max(0.0, cy - j0))
        def v(i, j):                            # ASCII grid rows run N→S
            val = rows[nrows - 1 - j][i]
            return 0.0 if val == nodata else val
        return ((v(i0, j0) * (1 - fx) + v(i0 + 1, j0) * fx) * (1 - fy)
                + (v(i0, j0 + 1) * (1 - fx) + v(i0 + 1, j0 + 1) * fx) * fy)


# ---------------------------------------------------------------------------
# Tile builder
# ---------------------------------------------------------------------------

def build(bbox, step_m: float):
    lon_min, lat_min, lon_max, lat_max = bbox
    km_per_lon = 111.32 * math.cos(math.radians((lat_min + lat_max) / 2))
    km_per_lat = 111.2
    nx = int(round((lon_max - lon_min) * km_per_lon * 1000 / step_m)) + 1
    nz = int(round((lat_max - lat_min) * km_per_lat * 1000 / step_m)) + 1
    dem = Terrain50(T50_ZIP)
    heights = []
    lo, hi = 1e9, -1e9
    for j in range(nz):                        # row-major from the NW corner
        lat = lat_max - (lat_max - lat_min) * j / (nz - 1)
        for i in range(nx):
            lon = lon_min + (lon_max - lon_min) * i / (nx - 1)
            e, n = wgs84_to_osgb(lon, lat)
            h = dem.elev(e, n)
            heights.append(h)
            lo, hi = min(lo, h), max(hi, h)
    span = max(hi - lo, 1e-6)
    q = struct.pack("<%dH" % len(heights),
                    *(int(round((h - lo) / span * 65535)) for h in heights))
    return {
        "source": "OS Terrain 50 (Ordnance Survey, OGL v3)",
        "bounds": {"lonMin": lon_min, "latMin": lat_min,
                   "lonMax": lon_max, "latMax": lat_max},
        "nx": nx, "nz": nz, "stepM": step_m,
        "elevMin": round(lo, 2), "elevMax": round(hi, 2),
        "b64": base64.b64encode(q).decode("ascii"),
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("LONMIN", "LATMIN", "LONMAX", "LATMAX"),
                    default=list(DEFAULT_BBOX))
    ap.add_argument("--step-m", type=float, default=DEFAULT_STEP_M)
    ap.add_argument("--js", type=Path, help="write window.TEST3D_TERRAIN classic script")
    ap.add_argument("--json", type=Path, help="write the pack-shaped JSON artifact")
    args = ap.parse_args()
    if not T50_ZIP.exists():
        print(f"ERROR: {T50_ZIP} missing — download OS Terrain 50 'ASCII Grid' GB zip\n"
              "  curl -L -o data/raw/os_terrain50/terr50_gagg_gb.zip \\\n"
              "    'https://api.os.uk/downloads/v1/products/Terrain50/downloads?area=GB&format=ASCII+Grid+and+GML+(Grid)&redirect'",
              file=sys.stderr)
        return 2
    tile = build(tuple(args.bbox), args.step_m)
    print(f"heightmap {tile['nx']}×{tile['nz']} ({tile['nx']*tile['nz']:,} samples), "
          f"elev {tile['elevMin']}–{tile['elevMax']} m OD, "
          f"payload ≈ {len(tile['b64'])//1024} KB base64")
    if args.js:
        args.js.write_text(
            "// Generated by scripts/build_terrain_tile.py - OS Terrain 50 heightmap.\n"
            "// Contains OS data (c) Crown copyright and database right 2026 (OGL v3).\n"
            f"window.TEST3D_TERRAIN = {json.dumps(tile)};\n", encoding="ascii")
        print(f"wrote {args.js}")
    if args.json:
        args.json.write_text(json.dumps(tile), encoding="ascii")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

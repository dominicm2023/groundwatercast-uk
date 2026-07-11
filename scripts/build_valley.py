"""Build EVERY valley-3D data layer in dependency order — one command.

Eight builders feed web/valley/test/, and the order matters (rivers
need terrain for bed elevations; the mesh, LIDAR corridor and flow gauges
need rivers; rainfall and flow need the stations' week axis). This runs them
all against one bbox and prints a payload summary at the end — the single
entry point a second catchment will need.

Usage:
  python -m scripts.build_valley                      # the Test block
  python -m scripts.build_valley --skip-remote        # offline: geometry only
  python -m scripts.build_valley --bbox ... --out web/prototypes/itchen3d

--skip-remote skips the three builders that hit the network (stations from
the pack, rainfall + flow from the EA Hydrology API) — the local geometry
stack (terrain, rivers, mesh, lidar) still rebuilds. Abstraction is local
(the NALD xlsx) but needs stations' bbox conventions only, so it always runs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = "web/valley/test"

# (module, output file, needs_network, extra args)
STEPS = [
    ("scripts.build_terrain_tile",       "terrain.js",     False, ["--step-m", "50"]),
    ("scripts.build_valley_rivers",      "rivers.js",      False, []),
    ("scripts.build_valley_mesh",        "mesh.js",        False, []),
    ("scripts.build_lidar_corridor",     "lidar.js",       False, []),
    ("scripts.build_valley_stations",    "stations.js",    True,  []),
    ("scripts.build_valley_rainfall",    "rainfall.js",    True,  []),
    ("scripts.build_valley_flow",        "flow.js",        True,  []),
    ("scripts.build_abstraction_points", "abstraction.js", False, []),
]
# builders that take --bbox (rainfall derives its reach from CENTRE/stations)
TAKES_BBOX = {"scripts.build_terrain_tile", "scripts.build_valley_rivers",
              "scripts.build_valley_mesh", "scripts.build_lidar_corridor",
              "scripts.build_valley_stations", "scripts.build_valley_flow",
              "scripts.build_abstraction_points"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("LONMIN", "LATMIN", "LONMAX", "LATMAX"),
                    help="defaults to each builder's own DEFAULT_BBOX (the Test block)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"output directory (default {DEFAULT_OUT})")
    ap.add_argument("--skip-remote", action="store_true",
                    help="skip pack/EA-API builders (stations, rainfall, flow)")
    ap.add_argument("--only", nargs="*",
                    help="run only these outputs (e.g. --only mesh.js lidar.js)")
    ap.add_argument("--pack-dir", type=Path,
                    help="read the artifact pack from a local dir instead of "
                         "the live site (production cron: the box's own "
                         "outputs/pack is fresher than the CDN)")
    args = ap.parse_args()
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    built, skipped = [], []
    for module, out_name, needs_net, extra in STEPS:
        if args.only and out_name not in args.only:
            continue
        if args.skip_remote and needs_net:
            skipped.append(out_name)
            continue
        cmd = [sys.executable, "-m", module, "--js", str(out_dir / out_name)] + extra
        if args.bbox and module in TAKES_BBOX:
            cmd += ["--bbox"] + [str(v) for v in args.bbox]
        if args.pack_dir and module == "scripts.build_valley_stations":
            cmd += ["--pack-dir", str(args.pack_dir)]
        print(f"\n=== {module} → {out_name} " + "=" * max(1, 40 - len(out_name)))
        r = subprocess.run(cmd, cwd=ROOT)
        if r.returncode != 0:
            print(f"FAILED: {module} (exit {r.returncode}) — stopping; later "
                  "layers depend on this one.", file=sys.stderr)
            return r.returncode
        built.append(out_name)

    print(f"\n{'=' * 60}\nbuilt {len(built)} layers in {time.time() - t0:.0f}s"
          + (f" (skipped remote: {', '.join(skipped)})" if skipped else ""))
    total = 0
    for name in sorted(p.name for p in out_dir.glob("*.js") if p.name != "main.js"):
        kb = (out_dir / name).stat().st_size // 1024
        total += kb
        print(f"  {name:16s} {kb:6,d} KB")
    print(f"  {'TOTAL':16s} {total:6,d} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

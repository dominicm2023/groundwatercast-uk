"""Licence→borehole capture-zone join -> data/processed/abstraction_influence.csv (H7).

Screens every catalogued groundwater borehole against EA abstraction-licence
points (``scripts/build_abstraction_points.py`` output) using a volume-banded
influence radius — a capture-zone *proxy*, not a drawdown model. Radii bands
live in ``config.diagnostics.abstraction_influence`` and are justified in
``docs/abstraction_screen_design.md`` (chalk-typical transmissivity reasoning).

Honesty labels (carried in the CSV itself):
  - every capacity figure is **licensed maximum, not actual pumping**
    (``capacity_basis`` column) — no live abstraction feed exists;
  - the extract covers >100 m³/day returns-submitting licences only, so
    ``influence_tier="none"`` means "no large returns-submitting licence
    nearby", not "no abstraction";
  - multi-point licences carry licence-level maxima per row — capacity sums
    are over distinct licences, never over rows (ingest invariant).

Report-only: nothing here excludes a station. The influence tier feeds
(1) the licence-proximity gate of ``scripts/build_abstraction_screen.py``
(proximity prior × amplitude evidence) and (2) human review of register
candidates (reason ``abstraction_influenced`` in known_bad_stations.yaml).

    python -m scripts.build_abstraction_influence
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.io_encoding import force_utf8_stdio
from src.diagnostics.abstraction_influence import (
    CAPACITY_BASIS, prepare_points, screen_borehole,
)

ROOT = Path(__file__).resolve().parents[1]
_CSV_COLS = [
    "station_id", "station_name", "lat", "lon", "aquifer_class",
    "nearest_licence_no", "nearest_licence_km", "licences_within_radius",
    "licensed_daily_m3_within", "licensed_annual_m3_within",
    "influence_tier", "capacity_basis", "licence_vintage",
]


def main(argv=None) -> int:
    force_utf8_stdio()
    cfg = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))
    cfg = cfg.get("diagnostics", {}).get("abstraction_influence", {})

    pts_path = ROOT / cfg.get("points_path", "data/processed/abstraction_points.csv")
    if not pts_path.exists():
        print(f"ERROR: {pts_path} missing — run scripts.build_abstraction_points "
              "first (see its docstring for the source download).")
        return 2
    points = pd.read_csv(pts_path, dtype={"licence_no": str})
    vintage = str(points["vintage"].iloc[0]) if "vintage" in points else ""
    lic = prepare_points(points, cfg)
    print(f"{lic['licence_no'].nunique()} groundwater licences "
          f"({len(lic)} points, vintage {vintage})")

    cat = pd.read_csv(ROOT / "data/processed/catalogue.csv")
    gw = (cat[cat["measure_type"] == "groundwater"].dropna(subset=["lat", "lon"])
          .drop_duplicates("station_id").copy())
    gw["station_id"] = gw["station_id"].astype(str)
    aq_col = next((c for c in ("aquifer_designation", "aquifer_class", "aquifer_name")
                   if c in gw.columns), None)

    rows = []
    for _, b in gw.iterrows():
        r = screen_borehole(float(b["lat"]), float(b["lon"]), lic, cfg)
        rows.append({
            "station_id": b["station_id"], "station_name": b["station_name"],
            "lat": b["lat"], "lon": b["lon"],
            "aquifer_class": b[aq_col] if aq_col else None,
            "nearest_licence_no": r["nearest_licence_no"],
            "nearest_licence_km": round(r["nearest_licence_km"], 3)
            if np.isfinite(r["nearest_licence_km"]) else None,
            "licences_within_radius": r["licences_within_radius"],
            "licensed_daily_m3_within": round(r["licensed_daily_m3_within"], 1),
            "licensed_annual_m3_within": round(r["licensed_annual_m3_within"], 1),
            "influence_tier": r["influence_tier"],
            "capacity_basis": CAPACITY_BASIS,
            "licence_vintage": vintage,
        })

    df = pd.DataFrame(rows, columns=_CSV_COLS)
    out = ROOT / cfg.get("output_path", "data/processed/abstraction_influence.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    n = df["influence_tier"].value_counts()
    print(f"{len(df)} boreholes screened -> {out.relative_to(ROOT)}")
    print(f"  tiers: likely {int(n.get('likely', 0))} / "
          f"possible {int(n.get('possible', 0))} / none {int(n.get('none', 0))}")
    print("  NOTE: capacities are licensed maxima, not actual pumping; "
          ">100 m3/day returns-submitting licences only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

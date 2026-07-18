"""Run the abstraction screen over the fleet -> outputs/abstraction_flags.csv (H7).

RE-ENABLED 2026-07-18 behind the licence-proximity gate: each borehole's
``influence_tier`` from the H7 capture-zone screen
(scripts/build_abstraction_influence.py -> data/processed/abstraction_influence.csv,
run it first) is passed into ``classify`` as the proximity prior, so excess
amplitude only flags where a licensed groundwater abstraction is actually in
range. Ungated history (2026-06-17): 575 evaluated → 125 flagged at 25 km —
over-flagged natural high-amplitude Chalk. Licence proximity = licensed
capacity nearby, NOT observed pumping (>100 m³/day returns-submitting licences
only). See docs/abstraction_screen_design.md.

Report-only: this changes NOTHING in the forecast. It surfaces boreholes whose
seasonal drawdown amplitude greatly exceeds their SAME-aquifer-class neighbours'
AND that sit within a licensed abstraction's banded radius — the cyclic /
seasonal-pumping case the trend screen misses — and recommends a human
metadata / abstraction-licence check. Confirmed sites are added to
data/external/known_bad_stations.yaml by hand (reason: abstraction_influenced),
which scope.py / exclusions.py already honour.

    python -m scripts.build_abstraction_influence   # the proximity prior
    python -m scripts.build_abstraction_screen

Reuses trend_screen.screen_series for the per-borehole metrics (seasonal amplitude,
record length, rainfall coherence). See docs/abstraction_screen_design.md.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.io_encoding import force_utf8_stdio
from src.diagnostics.trend_screen import screen_series
from src.diagnostics.abstraction_screen import (
    amplitude_isolation, classify, passes_severity, _SEV_RANK,
)
from src.dashboard.exclusions import excluded_station_ids

ROOT = Path(__file__).resolve().parents[1]
_CSV_COLS = [
    "station_id", "station_name", "lat", "lon", "aquifer_class", "n_obs",
    "record_years", "first_date", "last_date", "seasonal_amp_m", "neighbour_count",
    "neighbour_median_amp_m", "amp_ratio", "rain_corr", "amplitude_isolation_class",
    "influence_tier", "nearest_licence_km", "licences_within_radius",
    "licensed_daily_m3_within",
    "provenance_class", "recommended_action", "severity", "already_in_register",
]


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1 = np.radians(lat1)
    p2 = np.radians(np.asarray(lat2, dtype=float))
    dlat = np.radians(np.asarray(lat2, dtype=float) - lat1)
    dlon = np.radians(np.asarray(lon2, dtype=float) - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def _round(v, n):
    return round(float(v), n) if v is not None and np.isfinite(v) else None


def main(argv=None) -> int:
    force_utf8_stdio()
    cfg = json.loads((ROOT / "config/config.json").read_text(encoding="utf-8"))
    cfg = cfg.get("diagnostics", {}).get("abstraction_screen", {})
    if not cfg.get("enabled", True):
        print("abstraction_screen disabled in config — nothing to do.")
        return 0

    cat = pd.read_csv(ROOT / "data/processed/catalogue.csv")
    gw = (cat[cat["measure_type"] == "groundwater"].dropna(subset=["lat", "lon"])
          .drop_duplicates("station_id").copy())
    gw["station_id"] = gw["station_id"].astype(str)
    meta = gw.set_index("station_id")
    aq_col = next((c for c in ("aquifer_designation", "aquifer_class", "aquifer_name")
                   if c in gw.columns), None)

    j = pd.read_csv(ROOT / "data/features/joined_timeseries.csv",
                    usecols=["dateTime", "station_id", "GW_Level", "Rainfall"],
                    parse_dates=["dateTime"])
    j["station_id"] = j["station_id"].astype(str)

    min_years = float(cfg["min_years"])
    min_obs = int(cfg["min_obs"])
    metrics = {}
    for sid, g in j.groupby("station_id", sort=False):
        if sid not in meta.index:
            continue
        g = g.set_index("dateTime").sort_index()
        m = screen_series(g["GW_Level"], g["Rainfall"], cfg)
        m.pop("monthly_gw", None)  # not needed: amplitude is a per-borehole scalar
        if m.get("n_obs", 0) < min_obs or m.get("record_years", 0) < min_years:
            continue
        if not np.isfinite(m.get("seasonal_amp_m", np.nan)):
            continue
        metrics[sid] = m
    print(f"evaluated {len(metrics)} boreholes (>= {min_years} yr, >= {min_obs} obs)")

    gate = cfg.get("licence_gate", {})
    influence = {}
    if gate.get("enabled", False):
        inf_path = ROOT / gate.get("influence_path",
                                   "data/processed/abstraction_influence.csv")
        if not inf_path.exists():
            print(f"ERROR: licence gate enabled but {inf_path.relative_to(ROOT)} "
                  "missing — run scripts.build_abstraction_influence first.")
            return 2
        inf = pd.read_csv(inf_path, dtype={"station_id": str})
        influence = inf.set_index("station_id").to_dict("index")
        n_t = inf["influence_tier"].value_counts()
        print(f"licence gate ON (min_tier={gate.get('min_tier', 'possible')}): "
              f"{len(inf)} boreholes screened — likely {int(n_t.get('likely', 0))} / "
              f"possible {int(n_t.get('possible', 0))} / none {int(n_t.get('none', 0))}"
              " [licensed capacity, not actual pumping]")

    excluded = excluded_station_ids()
    nb = cfg.get("neighbour", {})
    radius = float(nb.get("radius_km", 25.0))
    same_aq = bool(nb.get("require_same_aquifer_class", True))
    ev_ids = list(metrics)
    ev_lat = meta.loc[ev_ids, "lat"].to_numpy(float)
    ev_lon = meta.loc[ev_ids, "lon"].to_numpy(float)

    rows = []
    for sid, m in metrics.items():
        d = _haversine_km(meta.loc[sid, "lat"], meta.loc[sid, "lon"], ev_lat, ev_lon)
        subj_aq = meta.loc[sid, aq_col] if aq_col else None
        namps = []
        for k, nsid in enumerate(ev_ids):
            if nsid == sid or d[k] > radius:
                continue
            if same_aq and aq_col and meta.loc[nsid, aq_col] != subj_aq:
                continue
            namps.append(metrics[nsid]["seasonal_amp_m"])
        inf = influence.get(sid, {})
        tier = inf.get("influence_tier", "none")
        iso = amplitude_isolation(m["seasonal_amp_m"], namps, cfg)
        cls = classify({**m, **iso, "influence_tier": tier}, cfg)
        rows.append({
            "station_id": sid,
            "station_name": meta.loc[sid, "station_name"],
            "lat": meta.loc[sid, "lat"], "lon": meta.loc[sid, "lon"],
            "aquifer_class": subj_aq,
            "n_obs": m["n_obs"], "record_years": _round(m["record_years"], 2),
            "first_date": m["first_date"].date(), "last_date": m["last_date"].date(),
            "seasonal_amp_m": _round(m["seasonal_amp_m"], 3),
            "neighbour_count": iso["neighbour_count"],
            "neighbour_median_amp_m": _round(iso["neighbour_median_amp"], 3),
            "amp_ratio": _round(iso["amp_ratio"], 2),
            "rain_corr": _round(m.get("rain_corr", np.nan), 3),
            "amplitude_isolation_class": iso["amplitude_isolation_class"],
            "influence_tier": tier,
            "nearest_licence_km": inf.get("nearest_licence_km"),
            "licences_within_radius": inf.get("licences_within_radius"),
            "licensed_daily_m3_within": inf.get("licensed_daily_m3_within"),
            "provenance_class": cls["provenance_class"],
            "recommended_action": cls["recommended_action"],
            "severity": cls["severity"], "already_in_register": sid in excluded,
        })

    df = pd.DataFrame(rows, columns=_CSV_COLS)
    n_gated_out = int((df["provenance_class"] == "excess_amplitude_no_licence").sum())
    if gate.get("enabled", False):
        print(f"licence gate suppressed {n_gated_out} excess-amplitude boreholes "
              "with no licensed groundwater abstraction in range")
    emit = cfg.get("emit_min_severity", "low")
    df = df[df["severity"].apply(lambda s: passes_severity(s, emit))]
    df = (df.assign(_s=df["severity"].map(_SEV_RANK))
          .sort_values(["_s", "amp_ratio"], ascending=False, na_position="last")
          .drop(columns="_s"))

    out = ROOT / cfg.get("output_path", "outputs/abstraction_flags.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    n_high = int((df["severity"] == "high").sum())
    print(f"flagged {len(df)} boreholes ({n_high} HIGH) -> {out.relative_to(ROOT)}")
    for _, r in df[df["severity"] == "high"].head(15).iterrows():
        tag = "  [in register]" if r["already_in_register"] else ""
        print(f"  HIGH {str(r['station_name'])[:22]:<22} amp={r['seasonal_amp_m']} "
              f"vs nbr {r['neighbour_median_amp_m']} (×{r['amp_ratio']}) "
              f"aq={r['aquifer_class']} tier={r['influence_tier']} "
              f"lic@{r['nearest_licence_km']}km{tag}")

    hist = ROOT / cfg.get("history_path", "outputs/abstraction_flags_history.parquet")
    stamp = pd.Timestamp.now().normalize()
    snap = df.assign(run_date=stamp)
    if hist.exists():
        prev = pd.read_parquet(hist)
        snap = pd.concat([prev[prev["run_date"] != stamp], snap], ignore_index=True)
    snap.to_parquet(hist, index=False)
    print(f"history appended -> {hist.relative_to(ROOT)} ({len(snap)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

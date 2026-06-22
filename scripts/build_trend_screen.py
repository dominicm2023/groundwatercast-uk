"""Run the trend screen over the fleet -> outputs/trend_flags.csv  (Tier 1).

Report-only: this changes NOTHING in the forecast. It surfaces and ranks
boreholes with a strong multi-year trend, cross-checks each against nearby
boreholes (radius-based), and recommends an action for human review. Confirmed
artefacts are added to data/external/known_bad_stations.yaml by hand (which
scope.py/exclusions.py already honour); real regional trends are left in place.

    python -m scripts.build_trend_screen

See docs/trend_screen.md. Re-run after any joined_timeseries rebuild / retrain.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.io_encoding import force_utf8_stdio
from src.diagnostics.trend_screen import (
    classify, fit_trend, neighbour_isolation, screen_series,
)
from src.dashboard.exclusions import excluded_station_ids

ROOT = Path(__file__).resolve().parents[1]
_SEV = {"high": 3, "medium": 2, "low": 1, "none": 0}
_CSV_COLS = [
    "station_id", "station_name", "lat", "lon", "aquifer_class", "n_obs",
    "record_years", "first_date", "last_date", "slope_ols_m_yr", "slope_sen_m_yr",
    "r2", "trend_change_m", "seasonal_amp_m", "drift_ratio", "max_daily_step_m",
    "n_steps_gt_thr", "rain_corr", "neighbour_count", "neighbour_median_slope_m_yr",
    "isolation_class", "provenance_class", "recommended_action", "severity",
    "already_in_register",
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
    cfg = cfg.get("diagnostics", {}).get("trend_screen", {})
    if not cfg.get("enabled", True):
        print("trend_screen disabled in config — nothing to do.")
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
    metrics, monthly_gw = {}, {}
    for sid, g in j.groupby("station_id", sort=False):
        if sid not in meta.index:
            continue
        g = g.set_index("dateTime").sort_index()
        m = screen_series(g["GW_Level"], g["Rainfall"], cfg)
        if m.get("n_obs", 0) < min_obs or m.get("record_years", 0) < min_years:
            continue
        monthly_gw[sid] = m.pop("monthly_gw")
        metrics[sid] = m
    print(f"evaluated {len(metrics)} boreholes (>= {min_years} yr, >= {min_obs} obs)")

    excluded = excluded_station_ids()
    nb = cfg.get("neighbour", {})
    radius = float(nb.get("radius_km", 15.0))
    min_overlap = float(nb.get("min_overlap_years", 3.0))
    ev_ids = list(metrics)
    ev_lat = meta.loc[ev_ids, "lat"].to_numpy(float)
    ev_lon = meta.loc[ev_ids, "lon"].to_numpy(float)

    rows = []
    for sid, m in metrics.items():
        cls = classify(m, cfg)
        iso = dict(isolation_class="not_evaluated", neighbour_count=0,
                   neighbour_median_slope=np.nan)
        if cls["is_trend"]:
            d = _haversine_km(meta.loc[sid, "lat"], meta.loc[sid, "lon"], ev_lat, ev_lon)
            subj = monthly_gw[sid]
            nslopes = []
            for k, nsid in enumerate(ev_ids):
                if nsid == sid or d[k] > radius:
                    continue
                nm = monthly_gw[nsid]
                lo = max(subj.index.min(), nm.index.min())
                hi = min(subj.index.max(), nm.index.max())
                if (hi - lo).days / 365.25 < min_overlap:
                    continue
                ov = nm[(nm.index >= lo) & (nm.index <= hi)]
                if len(ov) >= 12:
                    nslopes.append(fit_trend(ov)["slope_sen"])
            iso = neighbour_isolation(m["slope_sen"], nslopes, cfg)
            cls = classify({**m, **iso}, cfg)
        rows.append({
            "station_id": sid,
            "station_name": meta.loc[sid, "station_name"],
            "lat": meta.loc[sid, "lat"], "lon": meta.loc[sid, "lon"],
            "aquifer_class": meta.loc[sid, aq_col] if aq_col else None,
            "n_obs": m["n_obs"], "record_years": _round(m["record_years"], 2),
            "first_date": m["first_date"].date(), "last_date": m["last_date"].date(),
            "slope_ols_m_yr": _round(m["slope_ols"], 4),
            "slope_sen_m_yr": _round(m["slope_sen"], 4), "r2": _round(m["r2"], 3),
            "trend_change_m": _round(m["trend_change_m"], 3),
            "seasonal_amp_m": _round(m["seasonal_amp_m"], 3),
            "drift_ratio": _round(m["drift_ratio"], 2),
            "max_daily_step_m": _round(m["max_daily_step"], 3),
            "n_steps_gt_thr": m["n_steps"], "rain_corr": _round(m["rain_corr"], 3),
            "neighbour_count": iso["neighbour_count"],
            "neighbour_median_slope_m_yr": _round(iso["neighbour_median_slope"], 4),
            "isolation_class": iso["isolation_class"],
            "provenance_class": cls["provenance_class"],
            "recommended_action": cls["recommended_action"],
            "severity": cls["severity"], "already_in_register": sid in excluded,
        })

    df = pd.DataFrame(rows, columns=_CSV_COLS)
    emit = cfg.get("emit_min_severity", "low")
    df = df[df["severity"].map(_SEV).fillna(0) >= _SEV.get(emit, 1)]
    df = (df.assign(_s=df["severity"].map(_SEV))
          .sort_values(["_s", "trend_change_m"], ascending=False, na_position="last")
          .drop(columns="_s"))

    out = ROOT / cfg.get("output_path", "outputs/trend_flags.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    n_high = int((df["severity"] == "high").sum())
    n_excl = int((df["recommended_action"] == "review_exclude").sum())
    print(f"flagged {len(df)} boreholes ({n_high} HIGH, {n_excl} -> review_exclude) "
          f"-> {out.relative_to(ROOT)}")
    for _, r in df[df["severity"] == "high"].head(15).iterrows():
        tag = "  [in register]" if r["already_in_register"] else ""
        print(f"  HIGH {str(r['station_name'])[:22]:<22} slope={r['slope_sen_m_yr']:+.3f} "
              f"r2={r['r2']} iso={r['isolation_class']} rain_corr={r['rain_corr']} "
              f"-> {r['provenance_class']}/{r['recommended_action']}{tag}")

    hist = ROOT / cfg.get("history_path", "outputs/trend_flags_history.parquet")
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

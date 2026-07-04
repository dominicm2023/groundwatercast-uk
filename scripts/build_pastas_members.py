"""Drive calibrated Pastas models with the ensemble members (module 2 of B).

Reads the bias-corrected per-member forecast rainfall from
`forecast_ensemble_members.parquet` (produced by the main-env stage 8d
`build_ensemble_members`), bridges it onto each in-scope borehole's observed
rainfall/PET, rolls every member forward with the module-1 calibrated Pastas
model, and writes `forecast_pastas_members.parquet` (the Pastas analogue of the
roll's member parquet). The main GW-pipeline env never needs pastas.

Run with the pastas venv python from the repo root, AFTER build_ensemble_members
and build_pastas_models:
  .venv-pastas\\Scripts\\python -m scripts.build_pastas_members
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

from src.forecast.ensemble.members import gauge_rainfall_for
from src.forecast.pastas import recharge as R
from src.forecast.pastas import ensemble as E
from src.forecast.pastas.io import load_pet

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "data/features/joined_timeseries.csv"
LINKS = ROOT / "data/processed/station_links.csv"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = json.loads((ROOT / "config/config.json").read_text())
    pcfg = cfg["forecast"]["ensemble"]["pastas"]
    if not pcfg.get("enabled", True):
        print("forecast.ensemble.pastas.enabled = false — nothing to do"); return 0

    models = R.load_models(ROOT / pcfg["models_cache"])
    if not models:
        print("No calibrated models — run scripts.build_pastas_models first."); return 1

    members_path = ROOT / "data/model/forecast_ensemble_members.parquet"
    if not members_path.exists():
        print("No ensemble members parquet — run build_ensemble_members (main env) first.")
        return 1
    members = pd.read_parquet(members_path)
    members["date"] = pd.to_datetime(members["date"])
    run_dates = sorted(members["date"].unique())
    # Provenance: prefer the scope stamped on the members parquet (what 8d
    # actually ran with); fall back to config for parquets pre-dating the column.
    scope = (str(members["scope"].iloc[0]) if "scope" in members.columns
             else pcfg.get("scope", "live"))
    print(f"Ensemble members: {members['station_id'].nunique()} stations, "
          f"{members['member'].nunique()} members, "
          f"{run_dates[0].date()}..{run_dates[-1].date()}  (scope={scope})")

    # Coverage check: this stage can only forecast models ∩ members — make any
    # gap loud instead of letting stations silently vanish.
    member_sids, model_sids = set(members["station_id"].unique()), set(models)
    no_model = sorted(member_sids - model_sids)
    no_members = sorted(model_sids - member_sids)
    if no_model or no_members:
        bang = "!" * 72
        print(f"{bang}\n! WARNING: model/member coverage mismatch — likely a scope"
              f"\n! mismatch between scripts.build_pastas_models and"
              f"\n! scripts.build_ensemble_members. Re-run them with matching scope.")
        if no_model:
            print(f"!   {len(no_model)} member station(s) WITHOUT a calibrated model"
                  f" (dropped from output):\n!     "
                  + ", ".join(s[:8] for s in no_model[:10])
                  + (" …" if len(no_model) > 10 else ""))
        if no_members:
            print(f"!   {len(no_members)} model station(s) WITHOUT ensemble members"
                  f" (skipped):\n!     "
                  + ", ".join(s[:8] for s in no_members[:10])
                  + (" …" if len(no_members) > 10 else ""))
        print(bang)

    joined = pd.read_csv(JOINED, index_col=0, parse_dates=True)
    # Observed rainfall must come from the raw v19-extended gauge files (which
    # reach ~today), like the roll driver (build_ensemble_members) — NOT from
    # the joined CSV, whose Rainfall is indexed on GW-observation dates only:
    # every missing day (including the weeks-stale archive tail) would be
    # zero-filled by recharge._daily, driving the published fan with a
    # fabricated drought over exactly the nowcast window.
    raw_root = cfg["download"]["raw_root"]
    links = (pd.read_csv(LINKS).drop_duplicates("GWStationID")
             .set_index("GWStationID") if LINKS.exists() else None)

    out_frames, skipped = [], []
    for sid, rec in models.items():
        mdf = members[members["station_id"] == sid][["member", "date", "precip_mm"]]
        if mdf.empty:
            skipped.append((sid, "no ensemble members")); continue
        g = joined[joined["station_id"] == sid].sort_index()
        rain = gauge_rainfall_for(sid, links, raw_root)
        if rain.empty:
            rain = g["Rainfall"]
        # Seed at the freshest GW (per-station shard incl. live tail), not the
        # staler joined level — shrinks the obs→window gap (Phase-4 refinement).
        head = E.freshest_gw(sid, fallback=g["GW_Level"])
        pet = load_pet(sid)
        if pet is None or head.dropna().empty:
            skipped.append((sid, "no PET cache" if pet is None else "no GW")); continue
        df = E.drive_borehole(sid, rec, head, rain, pet, mdf)
        if df.empty:
            skipped.append((sid, "empty trajectories")); continue
        out_frames.append(df)
        origin = pd.Timestamp(df["origin_date"].iloc[0])
        stale = (run_dates[0].tz_localize(None) - origin).days
        p50 = df.groupby("date")["gw_pred"].median()
        print(f"  {sid[:8]}  origin {origin.date()} ({stale}d stale)  "
              f"P50 {p50.iloc[0]:.2f}→{p50.iloc[-1]:.2f} mAOD  "
              f"meanσ={df['gw_sigma'].mean():.2f}m")

    if not out_frames:
        print("No Pastas member trajectories produced."); return 1
    out = pd.concat(out_frames, ignore_index=True)
    out["scope"] = pd.Categorical([scope] * len(out))    # provenance
    dest = ROOT / pcfg["members_cache"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(dest, index=False)
    print(f"\nWrote {len(out)} rows ({out['station_id'].nunique()} boreholes × "
          f"{out['member'].nunique()} members) → {dest.relative_to(ROOT)}")
    for sid, why in skipped:
        print(f"  skipped {sid[:8]}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

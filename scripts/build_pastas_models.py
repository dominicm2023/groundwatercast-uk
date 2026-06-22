"""Calibrate & cache production Pastas TFN recharge models for every borehole
in the configured forecast scope (config ``forecast.ensemble.pastas.scope``).

Module 1 of the "Production Pastas recharge" build (docs/backlog.md → In progress).
Calibrates one Pastas FlexModel per in-scope borehole on ALL available history
and writes the small JSON model cache that downstream stages (ensemble driver,
aggregation) will load — the main GW-pipeline env never needs pastas.

Run with the pastas venv python from the repo root:
  .venv-pastas\\Scripts\\python -m scripts.build_pastas_models

Prereq: PET cached per borehole in data/raw/pet/<sid>.csv (main-env step:
``python -m scripts.refresh_pet --scope <scope>``). Boreholes without a PET
cache are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.forecast.pastas import recharge as R
from src.forecast.pastas.io import load_pet
from src.forecast.ensemble.scope import MIN_ROWS, select_scope

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "data/features/joined_timeseries.csv"
CATALOGUE = ROOT / "data/processed/catalogue.csv"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["user", "live", "fleet"], default=None,
                    help="borehole scope (default: config forecast.ensemble.pastas.scope)")
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config/config.json").read_text())
    pcfg = cfg["forecast"]["ensemble"]["pastas"]
    if not pcfg.get("enabled", True):
        print("forecast.ensemble.pastas.enabled = false — nothing to do"); return 0
    scope = args.scope or pcfg.get("scope", "live")

    joined = pd.read_csv(JOINED, index_col=0, parse_dates=True)
    cat = (pd.read_csv(CATALOGUE).query("measure_type == 'groundwater'")
           .drop_duplicates("station_id").set_index("station_id"))
    ids = sorted(select_scope(scope))
    print(f"Scope={scope}: {len(ids)} boreholes  |  rfunc={pcfg['rfunc']} "
          f"recharge={pcfg['recharge']}")

    recs: dict[str, dict] = {}
    skipped = []
    for sid in ids:
        g = joined[joined["station_id"] == sid].sort_index()
        head = g["GW_Level"].dropna()
        if len(head) < MIN_ROWS or sid not in cat.index:
            skipped.append((sid, f"insufficient history ({len(head)})")); continue
        evap = load_pet(sid)
        if evap is None:
            skipped.append((sid, "no PET cache")); continue
        prec = g["Rainfall"]
        try:
            rec = R.calibrate(sid, head, prec, evap,
                              rfunc=pcfg["rfunc"], recharge=pcfg["recharge"])
        except Exception as exc:
            skipped.append((sid, f"calibration error: {exc}")); continue
        recs[sid] = rec
        print(f"  {sid[:8]}  n={rec['n_obs']:5d}  EVP={rec['evp']:5.1f}%  "
              f"sigma={rec['sigma']:.3f}m  alpha={rec['alpha']:.0f}d")

    out = R.save_models(recs, ROOT / pcfg["models_cache"])
    print(f"\nCalibrated {len(recs)} models -> {out.relative_to(ROOT)}")
    for sid, why in skipped:
        print(f"  skipped {sid[:8]}: {why}")

    # AR1 residual-fit diagnostic (roadmap 0.3) — report-only. Flags stations
    # whose calibrated noise breaks the AR1 assumption the predictive band rests
    # on (autocorrelated / seasonal / heteroscedastic innovations).
    qa_rows = [{"station_id": sid, **rec["noise_qa"]}
               for sid, rec in recs.items() if rec.get("noise_qa")]
    if qa_rows:
        qa_df = pd.DataFrame(qa_rows)
        qa_path = ROOT / "outputs" / "noise_qa.csv"
        qa_path.parent.mkdir(parents=True, exist_ok=True)
        qa_df.to_csv(qa_path, index=False)
        flagged = qa_df[~qa_df["passes"]]
        print(f"\nNoise QA -> {qa_path.relative_to(ROOT)}  "
              f"({len(flagged)}/{len(qa_df)} flagged)")
        for _, r in flagged.iterrows():
            print(f"  ! {r['station_id'][:8]}: {r['flags']}  "
                  f"(lag1={r['lag1_autocorr']:.2f} seas={r['seasonal_frac']:.2f} "
                  f"het={r['hetero_corr']:.2f}, basis={r['basis']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

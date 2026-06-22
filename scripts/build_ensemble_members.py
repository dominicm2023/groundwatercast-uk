"""Phase 2 build — per-member GW forecast trajectories for the pilot boreholes.

Ties the chain together: provider members → bias-correct (f_bh) → bridge with
observed gauge rainfall → Weibull recharge → reduced-form GW roll. Writes
`data/model/forecast_ensemble_members.parquet` and prints a per-pilot GW fan.

Network: one ensemble fetch + one reanalysis (bias) fetch per pilot. The
--provider default resolves config `forecast.ensemble.provider` (production:
ecmwf_opendata), then `dev_provider`, then "open_meteo". If the config-default
provider can't run here because the GRIB stack (ecmwf-opendata / cfgrib /
eccodes) is missing — the usual case on the Windows dev box, see
docs/runbook.md — the run warns LOUDLY and falls back to the dev provider
rather than crashing. An *explicit* --provider that fails still errors.

Usage:
    python -m scripts.build_ensemble_members
    python -m scripts.build_ensemble_members --stations 6 --provider open_meteo
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
import pandas as pd

from src.features.io import load_features
from src.features.build import compute_weibull_kernel
from src.forecast.ensemble import get_provider
from src.forecast.ensemble.members import observed_daily_rainfall, member_trajectories
from src.forecast.ensemble import bias
from src.utils.io_encoding import force_utf8_stdio

CONFIG = Path("config/config.json")
CATALOGUE = Path("data/processed/catalogue.csv")
LINKS = Path("data/processed/station_links.csv")
OUT_PARQUET = Path("data/model/forecast_ensemble_members.parquet")


def _select_pilots(history_counts, coords, links, n, scope):
    """Boreholes to forecast, requiring coords + rain links + history.

    scope='user'     → boreholes with a user-supplied breach threshold.
    scope='live'     → live-feed + calibratable boreholes ∪ user set (default).
    scope='fleet'    → all calibratable boreholes ∪ user set.
    scope='coverage' → top stations by history coverage (legacy quick set).
    All require coords + rain links + history; `n` caps the count (0 = no cap).
    """
    have = lambda sid: sid in coords.index and sid in links.index
    if scope in ("user", "live", "fleet"):
        from src.forecast.ensemble.scope import select_scope
        want = select_scope(scope)
        ranked = [sid for sid in history_counts.index if sid in want and have(sid)]
    else:                                       # 'coverage' = legacy top-N
        ranked = [sid for sid in history_counts.index if have(sid)]
    return ranked[:n] if n else ranked


def resolve_provider_name(ens: dict, cli_value: str | None = None) -> tuple[str, bool]:
    """--provider resolution: explicit CLI > config `provider` > `dev_provider`
    > "open_meteo". Returns (name, explicit); `explicit` gates the GRIB-stack
    fallback — an explicitly requested provider that fails must error."""
    if cli_value is not None:
        return cli_value, True
    return ens.get("provider", ens.get("dev_provider", "open_meteo")), False


def _dev_fallback(ens, name, exc, cache_root):
    """Loudly swap to the dev provider when the config-default provider can't
    run here (missing GRIB stack — docs/runbook.md: open_meteo for dev)."""
    dev = ens.get("dev_provider", "open_meteo")
    bang = "!" * 72
    print(f"{bang}\n! WARNING: configured provider {name!r} is unusable on this box:"
          f"\n!   {exc}"
          f"\n! Falling back to dev provider {dev!r}. Members are NOT the"
          f"\n! production ECMWF GRIB feed — install ecmwf-opendata/cfgrib/eccodes"
          f"\n! to restore it (or pass --provider explicitly to force an error)."
          f"\n{bang}")
    return get_provider(dev, cache_root=cache_root)


def main() -> int:
    force_utf8_stdio()
    cfg = json.loads(CONFIG.read_text())
    ens = cfg.get("forecast", {}).get("ensemble", {})

    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None,
                    help="ensemble source (default: config forecast.ensemble"
                         ".provider, else dev_provider)")
    ap.add_argument("--scope", choices=["user", "live", "fleet", "coverage"],
                    default=ens.get("pastas", {}).get("scope", "live"),
                    help="user (user-threshold BHs) | live (live-feed+"
                         "calibratable ∪ user; default) | fleet (all "
                         "calibratable) | coverage (legacy top-N)")
    ap.add_argument("--stations", type=int, default=0,
                    help="cap on borehole count (0 = no cap)")
    ext = ens.get("extended", {})
    ext_on = bool(ext.get("enabled", False))
    default_horizon = (int(ext.get("horizon_days", 46)) if ext_on
                       else int(ens.get("horizon_days", 14)))
    ap.add_argument("--horizon", type=int, default=default_horizon)
    ap.add_argument("--no-extended", action="store_true",
                    help="force the plain 14-day primary provider even when "
                         "config forecast.ensemble.extended.enabled is true")
    args = ap.parse_args()
    if args.no_extended:
        ext_on = False
        if args.horizon == default_horizon:
            args.horizon = int(ens.get("horizon_days", 14))

    raw_root = cfg["download"]["raw_root"]
    wb = cfg["features"]["weibull"]
    kernel = compute_weibull_kernel(float(wb["k"]), float(wb["lambda"]),
                                    int(wb["lag_days"]))

    print("Loading feature history…")
    df, _ = load_features(cfg)
    history_counts = df["station_id"].value_counts()

    cat = pd.read_csv(CATALOGUE)
    coords = (cat[cat["measure_type"] == "groundwater"]
              .dropna(subset=["lat", "lon"])
              .drop_duplicates("station_id").set_index("station_id"))
    links = pd.read_csv(LINKS).drop_duplicates("GWStationID").set_index("GWStationID")

    pilots = _select_pilots(history_counts, coords, links, args.stations, args.scope)
    print(f"Boreholes ({len(pilots)}, scope={args.scope}): "
          + ", ".join(s[:8] for s in pilots))

    provider_name, explicit = resolve_provider_name(ens, args.provider)
    cache_root = ens.get("raw_cache_root", "data/raw/ensemble")
    try:
        provider = get_provider(provider_name, cache_root=cache_root)
    except ImportError as exc:                  # GRIB stack absent at import
        if explicit:
            raise
        provider = _dev_fallback(ens, provider_name, exc, cache_root)
    if ext_on and args.horizon > int(ext.get("splice_day", 15)):
        from src.forecast.ensemble.open_meteo_ec46 import OpenMeteoEC46
        from src.forecast.ensemble.splice import SplicedEnsemble
        provider = SplicedEnsemble(
            provider, OpenMeteoEC46(cache_root=cache_root),
            splice_day=int(ext.get("splice_day", 15)))
    print(f"Provider: {provider.name}" + ("" if explicit else " (config default)"))
    bias_cache = bias.load_bias_factors()
    overlap_end = date.today() - timedelta(days=30)
    overlap_start = overlap_end - timedelta(days=365 * 2)

    all_traj, bias_rows, skipped = [], [], []
    for sid in pilots:
        name = str(coords.loc[sid, "station_name"])
        lat, lon = float(coords.loc[sid, "lat"]), float(coords.loc[sid, "lon"])
        rain_ids = [links.loc[sid].get(f"RainMeasureID_{i}") for i in (1, 2, 3)]
        history = df[df["station_id"] == sid].sort_index()
        observed = observed_daily_rainfall(rain_ids, raw_root)

        # Bias factor: cached (its existing CSV row IS the provenance — leave
        # untouched), else fit on the fly against ERA5 reanalysis and record
        # a fresh provenance row.
        f_bh = bias_cache.get(sid)
        if f_bh is None:
            note = ""
            try:
                ref = bias.reference_archive_daily(lat, lon, overlap_start, overlap_end)
                f_bh = bias.fit_bias_factor(observed, ref)
            except Exception as exc:
                print(f"  ! {name}: bias fit failed ({exc}); using 1.0")
                f_bh, note = 1.0, f"bias fit failed -> 1.0 ({exc})"
            bias_rows.append({"station_id": sid, "f_bh": round(float(f_bh), 4),
                              "overlap_start": overlap_start,
                              "overlap_end": overlap_end,
                              "fitted_on": date.today(), "note": note})

        # Fetch ensemble members with retries. A transient provider failure
        # (e.g. an open-meteo ReadTimeout) on a single borehole must NOT abort
        # the whole batch — at fleet scale a timeout somewhere is near-certain
        # over a run. Retry a few times, then skip the borehole and carry on;
        # it's idempotent, so a re-run picks up anything skipped.
        members = None
        for attempt in range(3):
            try:
                members = provider.fetch(lat=lat, lon=lon, start=date.today(),
                                         horizon_days=args.horizon)
                break
            except ImportError as exc:          # GRIB stack absent at fetch
                if explicit:
                    raise
                provider = _dev_fallback(ens, provider_name, exc, cache_root)
                members = provider.fetch(lat=lat, lon=lon, start=date.today(),
                                         horizon_days=args.horizon)
                break
            except Exception as exc:            # transient network/provider error
                if attempt < 2:
                    print(f"  ! {name}: fetch failed ({type(exc).__name__}); "
                          f"retry {attempt + 1}/2…")
                    time.sleep(2 * (attempt + 1))
                else:
                    print(f"  ! {name}: SKIP after 3 attempts "
                          f"({type(exc).__name__}: {exc})")
        if members is None:
            skipped.append(sid)
            continue
        try:
            traj = member_trajectories(sid, members, history, kernel,
                                       f_bh=f_bh, observed_rain=observed,
                                       method=ens.get("gw_roll_method", "reduced_form_ar"))
        except Exception as exc:                # bad single-borehole data
            print(f"  ! {name}: SKIP — roll failed ({type(exc).__name__}: {exc})")
            skipped.append(sid)
            continue
        if traj.empty:
            print(f"  ! {name}: no trajectories produced")
            continue
        all_traj.append(traj)

        end_day = traj["date"].max()
        fan = traj[traj["date"] == end_day]["gw_pred"]
        print(f"  {name[:22]:<22} f_bh={f_bh:.2f}  members={traj['member'].nunique()}  "
              f"GW@{end_day.date()} P10/50/90 = "
              f"{fan.quantile(.1):.2f}/{fan.median():.2f}/{fan.quantile(.9):.2f} mAOD")

    if skipped:
        print(f"\n{len(skipped)} borehole(s) skipped after fetch/roll failures "
              f"(transient — picked up on the next run).")
    if not all_traj:
        print("No trajectories produced.")
        return 1

    out = pd.concat(all_traj, ignore_index=True)
    # Provenance: stamp what this artefact was built with, so mixed-scope /
    # mixed-provider runs stay distinguishable downstream (categorical = cheap).
    out["scope"] = pd.Categorical([args.scope] * len(out))
    out["provider"] = pd.Categorical([provider.name] * len(out))
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_PARQUET, compression="snappy", index=False)
    # Upsert only the freshly fitted factors; cached stations and stations
    # outside this scope keep their existing rows (auditable provenance).
    if bias_rows:
        bias.upsert_bias_factors(pd.DataFrame(bias_rows))

    print(f"\nWrote {len(out)} rows ({out['station_id'].nunique()} pilots x "
          f"{out['member'].nunique()} members x {out['date'].nunique()} days)")
    print(f"  -> {OUT_PARQUET}")
    if bias_rows:
        print(f"  -> {bias.BIAS_PATH.relative_to(_PROJECT_ROOT)} "
              f"({len(bias_rows)} freshly fitted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

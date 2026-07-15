"""Phase 2 build — per-member GW forecast trajectories for the pilot boreholes.

Ties the chain together: provider members → bias-correct (f_bh) → bridge with
observed gauge rainfall → Weibull recharge → reduced-form GW roll. Writes
`data/model/forecast_ensemble_members.parquet` and prints a per-pilot GW fan.

Also emits the low-flow Rivers pilot's ENS bridge
(`data/model/flow_ens_members.parquet`, see ``build_flow_ens_bridge``) from
the same decoded cycle — the on-disk artifact that carries the ENS member
forcing across the venv boundary to the pastas-env ``build_flow_members``
stage, exactly as the GW members parquet does for ``build_pastas_members``.
Skipped harmlessly when the low-flow pilot isn't set up on this host.

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

from src.download.flow import resolve_flow_pilot_path
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
FLOW_PILOT = Path("data/processed/flow_pilot.csv")
FLOW_CATALOGUE = Path("data/processed/flow_catalogue.csv")


def _select_pilots(history_counts, coords, links, n, scope, include_short=False):
    """Boreholes to forecast, requiring coords + rain links + history.

    scope='user'     → boreholes with a user-supplied breach threshold.
    scope='live'     → live-feed + calibratable boreholes ∪ user set (default).
    scope='fleet'    → all calibratable boreholes ∪ user set.
    scope='coverage' → top stations by history coverage (legacy quick set).
    ``include_short`` also fetches members for the short-record fan candidates,
    so their gated Pastas models (build_pastas_models) have members to drive —
    both stages must resolve the SAME set or short-record boreholes silently
    drop out of build_pastas_members.
    All require coords + rain links + history; `n` caps the count (0 = no cap).
    """
    have = lambda sid: sid in coords.index and sid in links.index
    if scope in ("user", "live", "fleet"):
        from src.forecast.ensemble.scope import select_scope
        want = select_scope(scope, include_short=include_short)
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


def build_flow_ens_bridge(provider, cfg, *,
                          pilot_path: Path = FLOW_PILOT,
                          catalogue_path: Path = FLOW_CATALOGUE,
                          out_path: Path | None = None) -> pd.DataFrame | None:
    """Emit the on-disk ENS bridge for the low-flow Rivers pilot gauges —
    ``data/model/flow_ens_members.parquet`` (build_plan.md Stage 6).

    Why here and not in the flow stage itself: ``build_flow_members`` (8h-flow)
    runs in PASTAS_ENV, which has no GRIB stack — a ``get_provider`` call there
    can never reach ``ecmwf_opendata`` and would silently fall through to
    Open-Meteo, whose free tier is non-commercial (the exact licensing landmine
    the June free-data migration exists to avoid) and which breaks the "fully
    open ECMWF" claim. So the ENS crosses the venv boundary the same way it
    does for GW: THIS main-env stage fetches once (the decoded UK grid for
    today's cycle is already cached in-process on ``provider`` from the GW
    borehole loop above, so each gauge is a near-zero-cost point lookup) and
    writes an on-disk artifact; the pastas-env stage reads only the artifact.

    Schema mirrors the GW members parquet's forcing columns:
    ``[gauge_id, member, date, precip_mm]`` + a ``provider`` provenance
    categorical (the GW parquet's ``station_id`` becomes ``gauge_id``; no
    ``f_bh`` bias correction is applied to flow — out of Stage-6 scope,
    flagged in the build plan review).

    Optional subsystem, never fatal: a missing pilot CSV / catalogue (host
    without the low-flow build) or zero fetchable gauges returns None with a
    log line — the GW members this stage exists for are untouched. Per-gauge
    fetch failures degrade to a skipped gauge, same retry discipline as the
    borehole loop.

    Returns the written frame, or None when skipped/nothing written.
    """
    ens = cfg.get("forecast", {}).get("ensemble", {})
    fcfg = ens.get("flow", {})
    if not fcfg.get("enabled", True):
        print("flow ENS bridge: forecast.ensemble.flow.enabled = false — skipped")
        return None
    pilot_path = Path(pilot_path)
    catalogue_path = Path(catalogue_path)
    if not pilot_path.exists():
        print(f"flow ENS bridge: {pilot_path} not found — skipped (run "
              f"'python -m scripts.select_flow_pilot' to enable it on this host).")
        return None
    if not catalogue_path.exists():
        print(f"flow ENS bridge: {catalogue_path} not found — skipped.")
        return None

    pilot = pd.read_csv(pilot_path, dtype={"gauge_id": str})
    if pilot.empty:
        print(f"flow ENS bridge: {pilot_path} is empty — skipped.")
        return None
    cat = (pd.read_csv(catalogue_path, dtype={"station_id": str})
           .dropna(subset=["lat", "lon"])
           .drop_duplicates("station_id").set_index("station_id"))
    horizon = int(fcfg.get("window_days", 14))

    frames, skipped = [], []
    for gauge_id in sorted(pilot["gauge_id"]):
        if gauge_id not in cat.index:
            skipped.append((gauge_id, "no catalogue row/coords"))
            continue
        lat = float(cat.loc[gauge_id, "lat"])
        lon = float(cat.loc[gauge_id, "lon"])
        members = None
        for attempt in range(3):
            try:
                members = provider.fetch(lat=lat, lon=lon, start=date.today(),
                                         horizon_days=horizon)
                break
            except Exception as exc:
                if attempt < 2:
                    print(f"  ! flow bridge {gauge_id[:8]}: fetch failed "
                          f"({type(exc).__name__}); retry {attempt + 1}/2…")
                    time.sleep(2 * (attempt + 1))
                else:
                    skipped.append((gauge_id, f"{type(exc).__name__}: {exc}"))
        if members is None or members.empty:
            if members is not None:
                skipped.append((gauge_id, "empty member frame"))
            continue
        mdf = members[["member", "date", "precip_mm"]].copy()
        mdf.insert(0, "gauge_id", gauge_id)
        frames.append(mdf)

    if not frames:
        print(f"flow ENS bridge: no member series produced "
              f"({len(skipped)} gauge(s) failed) — nothing written.")
        return None

    out = pd.concat(frames, ignore_index=True)
    out["provider"] = pd.Categorical([provider.name] * len(out))
    out_path = Path(out_path if out_path is not None
                    else fcfg.get("ens_bridge_cache",
                                  "data/model/flow_ens_members.parquet"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, compression="snappy", index=False)
    print(f"flow ENS bridge: {out['gauge_id'].nunique()} gauge(s) x "
          f"{out['member'].nunique()} members x {out['date'].nunique()} days "
          f"(provider={provider.name}) -> {out_path}")
    for gauge_id, why in skipped:
        print(f"  flow bridge skipped {gauge_id[:8]}: {why}")
    return out


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

    short_enabled = bool(ens.get("pastas", {}).get("short_record", {})
                         .get("enabled", True))
    pilots = _select_pilots(history_counts, coords, links, args.stations,
                            args.scope, include_short=short_enabled)
    print(f"Boreholes ({len(pilots)}, scope={args.scope}"
          f"{'+short' if short_enabled else ''}): "
          + ", ".join(s[:8] for s in pilots))

    provider_name, explicit = resolve_provider_name(ens, args.provider)
    cache_root = ens.get("raw_cache_root", "data/raw/ensemble")
    # Only ecmwf_opendata's GRIB cache needs pruning (see provider.py /
    # ecmwf_opendata.py) — other providers' constructors don't take this
    # kwarg, so it's only forwarded when that's the provider in play.
    provider_kwargs = {}
    if provider_name == "ecmwf_opendata":
        provider_kwargs["cache_retention_days"] = int(ens.get("cache_retention_days", 7))
    try:
        provider = get_provider(provider_name, cache_root=cache_root, **provider_kwargs)
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

    # Low-flow Rivers pilot ENS bridge (build_plan.md Stage 6): reuse THIS
    # process's already-decoded cycle to emit the per-gauge member forcing
    # that the pastas-env build_flow_members reads. Optional subsystem — a
    # bridge failure must never fail the GW members this stage exists for;
    # build_flow_members graceful-skips on a missing/stale bridge.
    #
    # pilot_path resolves from config (forecast.ensemble.flow.pilot_path,
    # falling back to the FLOW_PILOT default) via the SAME helper
    # build_flow_seasonal_shadow.py / refresh_seasonal_inputs.py use, so all
    # four flow-pilot consumers agree on where the pilot CSV lives — cfg is
    # already in hand here from the caller.
    try:
        build_flow_ens_bridge(
            provider, cfg,
            pilot_path=resolve_flow_pilot_path(cfg, _PROJECT_ROOT),
        )
    except Exception as exc:
        print(f"! flow ENS bridge failed ({type(exc).__name__}: {exc}) — "
              f"the daily flow member drive will skip today; GW members are "
              f"unaffected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

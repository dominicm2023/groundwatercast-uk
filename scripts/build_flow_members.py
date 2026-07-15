"""Daily flow production run for the low-flow Rivers layer pilot — Stage 6 of
``docs/product/lowflow/build_plan.md``.

For each pilot gauge with a calibrated model (``data/model/flow_models.json``,
from ``scripts/build_flow_models.py``):

  1. seed at the gauge's latest flow-shard observation (the shard tail IS the
     freshest available reading — flow has no separate live-overlay concept
     the way GW does);
  2. read the gauge's 51-member ECMWF ENS rainfall forecast from the ON-DISK
     BRIDGE ``data/model/flow_ens_members.parquet`` and drive it through the
     two-pathway model via ``src.forecast.pastas.ensemble.drive_borehole``,
     reused byte-for-byte unchanged: for a ``model_kind="flow_2s"`` rec its
     ``gw_pred``/``gw_sigma`` output columns hold logQ mean/sigma, not raw
     m3/s or head — Pastas was fit on logQ and "exponentiate only at publish"
     (analysis.md §3/§4);
  3. Monte-Carlo the member-spread + AR1-noise band on top
     (``src.forecast.pastas.flow_summary.aggregate_flow`` — the flow analogue
     of ``build_pastas_summary``'s sampling, in logQ) and aggregate to
     per-lead P10/P50/P90 (exponentiated to m3/s at this final boundary) plus
     LOW-flow breach statistics against the gauge's own Q95 (crossing BELOW,
     not above — ``flow_summary`` reuses ``summary._breach_from_samples``'s
     ``direction="below"`` path rather than forking it);
  4. archive every fan append-only, keyed ``(gauge_id, run, lead)``.

ENS sourcing — the bridge, deliberately: this stage runs in .venv-pastas,
which has no GRIB stack (ecmwf-opendata/cfgrib/eccodes live in the main env),
so a provider fetch HERE could never reach the production ``ecmwf_opendata``
source — it would fall through to Open-Meteo, whose free tier is
non-commercial (the licensing landmine the June free-data migration exists to
avoid) and which breaks the "fully open ECMWF" claim. The ENS therefore
crosses the venv boundary exactly the way it does for GW: the MAIN_ENV stage
8d (``scripts.build_ensemble_members`` → ``build_flow_ens_bridge``) extracts
each pilot gauge's member point series from the day's decoded GRIB cycle and
writes ``flow_ens_members.parquet``; this stage reads ONLY that artifact — no
provider import, no network ENS fetch, ever. When the bridge is missing or
stale for today's run, the daily flow update is SKIPPED with a clear log line
(exit 0 — yesterday's fans remain published, which is honest; the fans are
advertised as ENS-driven, so silently substituting climatological rain is not
an option).

``--climatological`` is an explicitly-invoked dev/offline mode ONLY (drives
with day-of-year rainfall climatology replicated across 51 zero-spread
"members", bypassing the bridge) — the chain can never reach it on its own.

GRACEFUL SKIP (exit 0) when ``data/model/flow_models.json`` is absent or
empty — see ``scripts/build_flow_shards.py``'s links-missing path (PR #126):
a missing optional-subsystem input must never kill ``run_chain``'s daily
forecast group. The missing/stale-bridge skip above follows the same rule.

Run with the pastas venv python from the repo root, AFTER
build_ensemble_members (which emits the bridge) and build_flow_models:
    .venv-pastas\\Scripts\\python -m scripts.build_flow_members
    .venv-pastas\\Scripts\\python -m scripts.build_flow_members --limit 5
    .venv-pastas\\Scripts\\python -m scripts.build_flow_members --climatological  # offline/dev
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.pet import fetch_station_pet
from src.download.build import load_config
from src.download.flow import (
    FLOW_CATALOGUE_PATH,
    FLOW_LINKS_PATH,
    FLOW_SHARD_DIR,
)
from src.forecast.ensemble.members import gauge_rainfall_for
from src.forecast.pastas import ensemble as E
from src.forecast.pastas import flow_summary as FS
from src.forecast.pastas import recharge as R
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]
_PET_LAG_DAYS = 5
# Oldest acceptable bridge: its forecast window must start no earlier than
# yesterday. Older means 8d hasn't run (or failed) today AND yesterday —
# driving today's "14-day ENS fan" off a stale cycle would misrepresent it,
# so the daily flow update skips instead (yesterday's archived fans remain).
MAX_BRIDGE_AGE_DAYS = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only drive the first N gauges (by gauge_id) with a "
                         "calibrated model (smoke test)")
    ap.add_argument("--climatological", action="store_true",
                    help="EXPLICIT dev/offline mode: drive with day-of-year "
                         "rainfall climatology instead of the ENS bridge "
                         "(never a fallback the chain reaches on its own)")
    return ap.parse_args(argv)


# ---------------------------------------------------------------------------
# Climatological members (EXPLICIT dev/offline mode only — never a fallback)
# ---------------------------------------------------------------------------

def climatological_members(observed_rain: pd.Series, forecast_dates: pd.DatetimeIndex,
                           n_members: int = 51) -> pd.DataFrame:
    """Dev/offline member set: every "member" is the SAME day-of-year
    rainfall climatology — zero forecast spread (honestly documents the
    degraded mode rather than fabricating spread that isn't there). Reached
    ONLY via the explicit ``--climatological`` flag."""
    s = observed_rain.copy()
    s.index = pd.to_datetime(s.index)
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    clim = s.groupby(s.index.day_of_year).mean()
    fallback = float(s.mean()) if len(s) else 0.0
    vals = np.array([float(clim.get(int(pd.Timestamp(d).day_of_year), fallback))
                     for d in forecast_dates])
    vals = np.nan_to_num(vals, nan=0.0)
    rows = []
    for m in range(n_members):
        for i, d in enumerate(forecast_dates):
            rows.append({"member": m, "date": d, "precip_mm": float(vals[i])})
    return pd.DataFrame(rows, columns=["member", "date", "precip_mm"])


# ---------------------------------------------------------------------------
# ENS bridge reader (the ONLY production member source — see module docstring)
# ---------------------------------------------------------------------------

def load_ens_bridge(bridge_path: Path, *, today: date | None = None,
                    max_age_days: int = MAX_BRIDGE_AGE_DAYS
                    ) -> tuple[pd.DataFrame | None, str]:
    """Read + freshness-check the flow ENS bridge parquet.

    Returns ``(frame, reason)``: the frame with ``date`` parsed when present
    and fresh, else ``(None, why)`` — the caller turns a None into a clean
    exit-0 skip (never an error; run_chain aborts everything on non-zero).
    Freshness = the bridge's forecast window starts within ``max_age_days``
    of today (the window start is the fetch day's first forecast date, so it
    IS the bridge's build date for all practical purposes).
    """
    if not bridge_path.exists():
        return None, f"{bridge_path} not found"
    bridge = pd.read_parquet(bridge_path)
    if bridge.empty:
        return None, f"{bridge_path} is empty"
    bridge["date"] = pd.to_datetime(bridge["date"])
    win_start = bridge["date"].min()
    today_ts = pd.Timestamp(today if today is not None else date.today())
    age_days = int((today_ts - win_start.normalize()).days)
    if age_days > max_age_days:
        return None, (f"{bridge_path} is stale (forecast window starts "
                      f"{win_start.date()}, {age_days}d before today)")
    return bridge, ""


# ---------------------------------------------------------------------------
# Archive append (mirrors scripts.build_pastas_summary — kept duplicated to
# avoid a shared module, same rationale as that script's own comment)
# ---------------------------------------------------------------------------

def append_archive(prior: pd.DataFrame | None, summary: pd.DataFrame) -> pd.DataFrame:
    combined = (pd.concat([prior, summary], ignore_index=True)
               if prior is not None else summary.copy())
    return combined.drop_duplicates(subset=["gauge_id", "run"], keep="last")


def append_fan_archive(prior: pd.DataFrame | None, fan: pd.DataFrame) -> pd.DataFrame:
    if fan is None or fan.empty:
        return prior if prior is not None else fan
    combined = (pd.concat([prior, fan], ignore_index=True)
               if prior is not None else fan.copy())
    return combined.drop_duplicates(subset=["gauge_id", "run", "lead"], keep="last")


def run(args: argparse.Namespace, cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    ens_cfg = cfg.get("forecast", {}).get("ensemble", {})
    fcfg = ens_cfg.get("flow", {})
    if not fcfg.get("enabled", True):
        print("forecast.ensemble.flow.enabled = false — nothing to do")
        return 0

    models_path = ROOT / fcfg.get("models_cache", "data/model/flow_models.json")
    if not models_path.exists():
        print(f"{models_path} not found — flow member drive skipped (run "
             f"'python -m scripts.build_flow_models' to enable it on this host).")
        return 0
    models = R.load_models(models_path)
    if not models:
        print(f"{models_path} has no calibrated flow models — nothing to drive.")
        return 0

    # The ENS bridge is the ONLY production member source (module docstring).
    # Missing/stale → skip today's flow update, exit 0: yesterday's archived
    # fans remain published, which is honest — the fans are advertised as
    # ENS-driven, so climatological rain must NOT silently stand in.
    bridge = None
    if not args.climatological:
        bridge_path = ROOT / fcfg.get("ens_bridge_cache",
                                      "data/model/flow_ens_members.parquet")
        bridge, why = load_ens_bridge(bridge_path)
        if bridge is None:
            print(f"{why} — daily flow member drive skipped (the MAIN_ENV "
                 f"stage build_ensemble_members emits the bridge; yesterday's "
                 f"fans remain).")
            return 0

    links_path = ROOT / FLOW_LINKS_PATH
    catalogue_path = ROOT / FLOW_CATALOGUE_PATH
    if not links_path.exists() or not catalogue_path.exists():
        print("flow_links.csv / flow_catalogue.csv not found — flow member "
             "drive skipped.")
        return 0
    links_df = pd.read_csv(links_path, dtype=str).set_index("GaugeID")
    cat_df = (pd.read_csv(catalogue_path, dtype={"station_id": str})
             .set_index("station_id"))

    gauge_ids = sorted(g for g in models if g in cat_df.index)
    dropped_no_cat = sorted(g for g in models if g not in cat_df.index)
    if args.limit is not None:
        gauge_ids = gauge_ids[: args.limit]
    print(f"Flow member drive: {len(gauge_ids)} gauge(s) with a calibrated model"
         + (f" (--limit {args.limit})" if args.limit is not None else "")
         + ("  [EXPLICIT --climatological dev mode]" if args.climatological else ""))
    if dropped_no_cat:
        print(f"  ! {len(dropped_no_cat)} model(s) with no catalogue row "
             f"(skipped): {[g[:8] for g in dropped_no_cat[:10]]}")
    if bridge is not None:
        src_name = (str(bridge["provider"].iloc[0])
                    if "provider" in bridge.columns else "ens_bridge")
        print(f"ENS bridge: {bridge['gauge_id'].nunique()} gauge(s) x "
             f"{bridge['member'].nunique()} members, window "
             f"{bridge['date'].min().date()}..{bridge['date'].max().date()} "
             f"(provider={src_name})")

    raw_root = cfg["download"]["raw_root"]
    horizon = int(fcfg.get("window_days", 14))

    frames, skipped = [], []
    members_source_tally: dict[str, int] = {}
    for gauge_id in gauge_ids:
        rec = models[gauge_id]
        name = str(cat_df.loc[gauge_id, "station_name"])
        lat, lon = float(cat_df.loc[gauge_id, "lat"]), float(cat_df.loc[gauge_id, "lon"])

        fp = FLOW_SHARD_DIR / f"{gauge_id}.parquet"
        if not fp.exists():
            skipped.append((gauge_id, "no flow shard")); continue
        shard = pd.read_parquet(fp)
        if shard.empty:
            skipped.append((gauge_id, "empty flow shard")); continue
        q = pd.Series(shard["Flow_m3s"].to_numpy(float),
                     index=pd.to_datetime(shard["date"]), name="Flow_m3s").sort_index()

        if gauge_id not in links_df.index:
            skipped.append((gauge_id, "no rain link")); continue
        rain = gauge_rainfall_for(gauge_id, links_df, raw_root)
        if rain.empty:
            skipped.append((gauge_id, "no rain data")); continue

        start = pd.Timestamp(q.index.min()).date()
        end = date.today() - timedelta(days=_PET_LAG_DAYS)
        pet = fetch_station_pet(gauge_id, lat, lon, start, end)
        if pet.empty:
            skipped.append((gauge_id, "no PET")); continue

        if args.climatological:
            forecast_dates = pd.date_range(date.today(), periods=horizon, freq="D")
            mdf = climatological_members(rain, forecast_dates)
            source = "climatological"
        else:
            mdf = bridge[bridge["gauge_id"] == gauge_id][["member", "date", "precip_mm"]]
            if mdf.empty:
                # In the bridge's pilot but with no rows = its fetch failed on
                # the main-env side. Skip the gauge (it keeps yesterday's
                # fan) — do NOT substitute climatological rain.
                skipped.append((gauge_id, "not in ENS bridge")); continue
            source = (str(bridge["provider"].iloc[0])
                      if "provider" in bridge.columns else "ens_bridge")
        members_source_tally[source] = members_source_tally.get(source, 0) + 1

        try:
            traj = E.drive_borehole(gauge_id, rec, q, rain, pet, mdf)
        except Exception as exc:
            skipped.append((gauge_id, f"drive error: {exc}")); continue
        if traj.empty:
            skipped.append((gauge_id, "empty trajectories")); continue
        frames.append(traj)

        eps = float(rec.get("eps", 0.001))
        end_day = traj["date"].max()
        last_logq = traj[traj["date"] == end_day]["gw_pred"]
        q_end = FS.exp_q(last_logq.to_numpy(float), eps)
        print(f"  {name[:22]:<22} members={source:<13} origin "
             f"{pd.Timestamp(traj['origin_date'].iloc[0]).date()}  "
             f"Q@{pd.Timestamp(end_day).date()} P10/50/90 = "
             f"{np.quantile(q_end, .1):.3f}/{np.median(q_end):.3f}/"
             f"{np.quantile(q_end, .9):.3f} m3/s")

    if not frames:
        print("\nNo flow member trajectories produced.")
        return 1

    members_df = pd.concat(frames, ignore_index=True)
    members_path = ROOT / fcfg.get("members_cache", "data/model/forecast_flow_members.parquet")
    members_path.parent.mkdir(parents=True, exist_ok=True)
    members_df.to_parquet(members_path, index=False)

    q95_by_gauge = {g: float(models[g]["q95_m3s"]) for g in gauge_ids
                    if models[g].get("q95_m3s") is not None}
    run_ts = pd.Timestamp.now(tz="UTC").floor("h")
    summary, fan = FS.aggregate_flow(members_df, models, run=run_ts,
                                     q95_by_gauge=q95_by_gauge,
                                     n_samples=int(fcfg.get("mc_samples", 4000)))

    summary_path = ROOT / fcfg.get("summary_cache", "data/model/forecast_flow_summary.csv")
    fan_path = ROOT / fcfg.get("fan_cache", "data/model/forecast_flow_fan.csv")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(summary_path, index=False)
    fan.to_csv(fan_path, index=False)

    archive_path = ROOT / fcfg.get("archive_cache", "data/model/forecast_flow_archive.parquet")
    combined = append_archive(
        pd.read_parquet(archive_path) if archive_path.exists() else None, summary)
    combined.to_parquet(archive_path, compression="snappy", index=False)

    fan_archive_path = ROOT / fcfg.get("fan_archive_cache",
                                       "data/model/forecast_flow_fan_archive.parquet")
    fan_combined = append_fan_archive(
        pd.read_parquet(fan_archive_path) if fan_archive_path.exists() else None, fan)
    fan_combined.to_parquet(fan_archive_path, compression="snappy", index=False)

    print(f"\nRun {run_ts}  ·  {len(summary)} gauge(s) (flow)  ·  "
         f"members source: {members_source_tally}\n")
    for _, r in summary.iterrows():
        print(f"• {r['gauge_id'][:8]}  [{r['threshold_source']}]  "
             f"P50@end {r['q_p50_end_m3s']:.3f} m3/s")
        print(f"    {r['headline']}")
    print(f"\nWrote {members_path.relative_to(ROOT)}, {summary_path.relative_to(ROOT)}, "
         f"{fan_path.relative_to(ROOT)}, {archive_path.relative_to(ROOT)} "
         f"({len(combined)} archived rows), {fan_archive_path.relative_to(ROOT)} "
         f"({len(fan_combined)} archived fan rows)")
    for gauge_id, why in skipped:
        print(f"  skipped {gauge_id[:8]}: {why}")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    return run(parse_args())


if __name__ == "__main__":
    force_utf8_stdio()
    raise SystemExit(main())

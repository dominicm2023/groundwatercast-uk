"""Shadow-mode monthly flow seasonal outlook — Stage 6b of
``docs/product/lowflow/build_plan.md``.

**PUBLISHES NOTHING.** No pack fields, no UI, no ``meta.counts``. This is an
internal evidence archive: flow seasonal forecasts verify slowly (months to
close), so the archive must start accruing long before any public-launch
decision (build_plan.md Stage 8's "PUBLIC flow seasonal outlook" is gated on
this archive demonstrating skill over closed months — the Phase-0 lesson
applied to rivers).

Runs the SAME weighted-ESP method ``scripts/build_seasonal_outlook.py`` uses
for groundwater (historic-year ERA5 forcing traces, re-anchored at the fan
terminal, SEAS5-tilted — ``src/forecast/seasonal/esp.py`` reused unchanged)
through each pilot gauge's calibrated two-pathway flow model
(``model_kind="flow_2s"``, ``data/model/flow_models.json``), entirely in
logQ: ``recharge.simulate_path`` already returns logQ for a flow_2s rec —
"exponentiate only at publish", the same rule ``src/forecast/pastas/
flow_summary.py`` follows for the daily fan.

For each ``(gauge_id, run, month_ahead)`` this archives BOTH candidate target
statistics — undecided which becomes the eventual public headline, so both
are kept and scored:
  - monthly-mean flow quantiles, exponentiated to m3/s (p10/p50/p90), via the
    SAME ``esp.additive_band``/``esp.weighted_quantiles`` machinery
    ``build_seasonal_outlook.py`` uses for GW's monthly gw_p10/p50/p90.
  - P(that calendar month has >=1 day with Q < the gauge's own stored Q95) —
    from a Monte-Carlo of daily AR1-correlated logQ paths: a trace year is
    resampled proportional to its ESP/SEAS5 weight, then correlated daily
    noise is added around that trace's own mean path — the same
    "member-spread + AR1-noise" MC ``src/forecast/pastas/summary.py``'s
    ``mc_trajectories`` uses for the daily fan/breach stats, with ESP trace
    years standing in for ENS members and the ESP weights standing in for a
    uniform member draw. ``_ar1_noise`` is reused unchanged (not forked) —
    the same "one seam, not a fork" discipline ``flow_summary.py``'s breach
    stats already apply to ``_breach_from_samples``.

Anchoring, ESP/SEAS5 weighting, and the staleness gate all mirror
``build_seasonal_outlook.py``'s design 1:1 (see that module's comments for
the full rationale); the ``_fit_f_g``/``_stamp`` helpers below are a
deliberate DUPLICATE of that script's private ``_fit_fbh``/``_stamp`` (same
"small module per script, no cross-script coupling" convention
``scripts/build_flow_members.py``'s ``append_archive`` comment documents) —
with one real difference: flow gauges have no persisted bias-factor CSV
fallback (``data/model/ensemble_bias_factors.csv`` is GW-only), so a
thin-overlap gauge falls back to ``f_g = 1.0`` with a loud per-gauge warning
instead of a persisted value.

ERA5/PET/SEAS5 inputs: ``scripts/refresh_seasonal_inputs.py`` (main env,
step 9) fetches these for the pilot gauges too (folded into the same fleet
fetch it already does for GW boreholes — see that script's
``_flow_pilot_points`` helper) — this script is PURE COMPUTE from those
caches, exactly like ``build_seasonal_outlook.py`` is for GW.

GRACEFUL SKIP (exit 0) when the low-flow pilot / flow models aren't set up
on this host (``data/processed/flow_pilot.csv``, ``data/model/
flow_models.json``) — same discipline as ``build_flow_models.py`` /
``build_flow_members.py``. Also exits 0 (not 1) when every gauge is skipped
for a data reason (e.g. no ERA5/PET cache yet): this is an evidence archive,
not a hard dependency of anything downstream, so it must never fail
``run_chain``.

Run monthly with the pastas venv python, AFTER build_seasonal_outlook (per
build_plan.md Stage 6b — "alongside the GW seasonal run"):
    .venv-pastas\\Scripts\\python -m scripts.build_flow_seasonal_shadow
    .venv-pastas\\Scripts\\python -m scripts.build_flow_seasonal_shadow --limit 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.era5_precip import load_station_precip
from src.download.build import load_config
from src.download.flow import (
    FLOW_CATALOGUE_PATH,
    FLOW_LINKS_PATH,
    FLOW_SHARD_DIR,
    resolve_flow_pilot_path,
)
from src.forecast.ensemble.members import gauge_rainfall_for
from src.forecast.pastas import flow_summary as FS
from src.forecast.pastas import recharge as R
from src.forecast.pastas.io import load_pet
from src.forecast.pastas.summary import _ar1_noise
from src.forecast.seasonal import esp, normals, seas5
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]

SHADOW_COLS = [
    "gauge_id", "run", "origin_date", "month_ahead", "month_start",
    "q_p10_m3s", "q_p50_m3s", "q_p90_m3s",
    "p_sub_q95", "q95_m3s",
    "n_traces", "n_mc_samples", "seas5_weighted", "band_mode", "days_covered",
]
SHADOW_KEY = ["gauge_id", "run", "month_ahead"]

MIN_TRACES = 10
MAX_TRACE_GAP_FRAC = 0.05      # a trace year missing >5% of days is dropped
_Z = 1.2815515594              # 90th-percentile z-score (matches esp.additive_band)


# ---------------------------------------------------------------------------
# Duplicated twins of build_seasonal_outlook.py's private helpers (module
# docstring explains why this is a deliberate duplication, not a fork).
# ---------------------------------------------------------------------------

def _fit_f_g(gauge: pd.Series, ref: pd.Series,
            lo: float = 0.2, hi: float = 5.0) -> float | None:
    """f_g = mean(gauge) / mean(reference ERA5 precip) on common dates,
    clamped to [lo, hi]. None when the overlap is too thin (<30 common days)
    or the reference mean is ~0 — caller falls back to 1.0 (no persisted
    per-gauge factor exists for flow, unlike GW's ensemble_bias_factors.csv)."""
    def _naive(s: pd.Series) -> pd.Series:
        s = s.dropna()
        idx = pd.DatetimeIndex(s.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        s.index = idx.normalize()
        return s
    g, r = _naive(gauge), _naive(ref)
    common = g.index.intersection(r.index)
    if len(common) < 30:
        return None
    rm = float(r.loc[common].mean())
    if rm <= 1e-6:
        return None
    return float(min(max(float(g.loc[common].mean()) / rm, lo), hi))


def _stamp(values: pd.Series, onto: pd.DatetimeIndex) -> pd.Series:
    """Re-stamp a historic-window slice onto the forecast calendar."""
    return pd.Series(values.to_numpy(float)[:len(onto)], index=onto[:len(values)])


# ---------------------------------------------------------------------------
# Pure aggregation (no I/O) — unit-tested directly with synthetic traces.
# ---------------------------------------------------------------------------

def weighted_daily_mc(mu_by_year: dict[int, np.ndarray], sig_daily: np.ndarray,
                      weights: dict[int, float], alpha: float, n: int,
                      rng: np.random.Generator) -> np.ndarray:
    """n x H Monte-Carlo daily logQ paths: resample a trace year weighted by
    its ESP/SEAS5 weight, then add correlated AR1 daily noise around that
    trace's own mean path.

    The same "member-spread + AR1-noise" MC ``summary.mc_trajectories`` uses
    for the daily fan (reusing its ``_ar1_noise`` unchanged), with ESP trace
    years standing in for ENS members and the ESP weights standing in for a
    uniform member draw — the sub-Q95-day statistic below is exactly the
    fan's ``_breach_from_samples`` idea applied to a weighted ESP ensemble
    instead of a uniform one.
    """
    years = sorted(mu_by_year)
    W = np.stack([mu_by_year[y] for y in years])           # n_years x H
    w = np.array([weights[y] for y in years], dtype=float)
    w = w / w.sum()
    idx = rng.choice(len(years), size=n, p=w)
    base = W[idx]
    phi = float(np.exp(-1.0 / alpha))
    return base + _ar1_noise(n, np.asarray(sig_daily, float), phi, rng)


def compute_gauge_shadow(
    gauge_id: str, rec: dict, *,
    mu_by_year: dict[int, np.ndarray], sig_daily: np.ndarray,
    weights: dict[int, float], f_dates: pd.DatetimeIndex,
    periods: list, origin: pd.Timestamp, obs_last: pd.Timestamp,
    q95_m3s: float, run: pd.Timestamp, seas5_weighted: bool,
    ft_m3s: tuple[float, float, float] | None = None,
    band_mode: str = "additive", mc_samples: int = 2000, seed: int = 12345,
) -> list[dict]:
    """Per outlook month: BOTH shadow target statistics for one gauge, from
    precomputed per-trace-year daily logQ mean paths (``mu_by_year``) and the
    canonical daily predictive sd (``sig_daily`` — identical across traces:
    ``recharge.simulate_path``'s sigma depends only on lead time from the
    origin, never on the trace's own precip, exactly like the fan's
    ``gw_sigma``). Pure numpy/pandas — no I/O.

    ``ft_m3s`` = ``(p10, p50, p90)`` at the flow fan's terminal (m3/s), or
    None when no fan exists for this gauge (falls back to
    ``esp.weighted_quantiles`` regardless of ``band_mode``, mirroring
    ``build_seasonal_outlook.py``'s ``use_additive = band_mode == "additive"
    and ft is not None``).
    """
    eps = float(rec.get("eps", 0.001))
    thr_logq = float(np.log(max(float(q95_m3s), 0.0) + eps))

    years_used = sorted(mu_by_year)
    if len(years_used) < MIN_TRACES:
        return []

    w_vec = np.array([weights[y] for y in years_used], dtype=float)
    alpha = R._safe_alpha(rec.get("alpha"))

    use_additive = band_mode == "additive" and ft_m3s is not None
    if use_additive:
        ft_p10, _ft_p50, ft_p90 = ft_m3s
        ft_p10_logq = float(np.log(max(float(ft_p10), 0.0) + eps))
        ft_p90_logq = float(np.log(max(float(ft_p90), 0.0) + eps))
        sd46 = (ft_p90_logq - ft_p10_logq) / (2 * _Z)
        dt46 = max((origin - obs_last).days, 1)
        tau_state = esp.state_memory_timescale(rec)

    rng = np.random.default_rng(seed)
    mc_logq = weighted_daily_mc(mu_by_year, sig_daily, weights, alpha,
                                mc_samples, rng)                  # n x H
    month_index = pd.PeriodIndex(f_dates, freq="M")

    rows = []
    for m_idx, p in enumerate(periods):
        day_mask = np.asarray(month_index == p)
        days_covered = int(day_mask.sum())
        if days_covered == 0:
            continue
        mu_vec = np.array([float(np.nanmean(mu_by_year[y][day_mask]))
                           for y in years_used])
        if not np.isfinite(mu_vec).any():
            continue
        if use_additive:
            mid = p.to_timestamp() + pd.Timedelta(days=float(p.days_in_month) / 2)
            q10, q50, q90 = esp.additive_band(
                mu_vec, w_vec, sigma=float(rec["sigma"]), alpha=alpha,
                tau_state=tau_state, sd46=sd46, dt46=dt46,
                dt_month=max((mid - obs_last).days, 1),
                lead_gap=max((mid - origin).days, 0))
        else:
            q10, q50, q90 = esp.weighted_quantiles(mu_vec, w_vec)
        if not np.isfinite(q50):
            continue

        p_sub_q95 = float((mc_logq[:, day_mask] <= thr_logq).any(axis=1).mean())

        rows.append({
            "gauge_id": gauge_id, "run": run,
            "origin_date": origin.date().isoformat(),
            "month_ahead": m_idx + 1,
            "month_start": p.to_timestamp().date().isoformat(),
            "q_p10_m3s": float(FS.exp_q(np.array([q10]), eps)[0]),
            "q_p50_m3s": float(FS.exp_q(np.array([q50]), eps)[0]),
            "q_p90_m3s": float(FS.exp_q(np.array([q90]), eps)[0]),
            "p_sub_q95": p_sub_q95,
            "q95_m3s": float(q95_m3s),
            "n_traces": len(years_used),
            "n_mc_samples": int(mc_samples),
            "seas5_weighted": bool(seas5_weighted),
            "band_mode": "additive" if use_additive else "weighted_quantiles",
            "days_covered": days_covered,
        })
    return rows


def append_shadow_archive(prior: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Append-only archive, keyed ``(gauge_id, run, month_ahead)``: a rerun
    of the SAME monthly run replaces its own rows; distinct runs accumulate
    (mirrors ``scripts.build_flow_members.append_archive``)."""
    combined = (pd.concat([prior, new], ignore_index=True)
               if prior is not None and not prior.empty else new.copy())
    return combined.drop_duplicates(subset=SHADOW_KEY, keep="last")


# ---------------------------------------------------------------------------
# Orchestration (I/O) — mirrors build_seasonal_outlook.py's main() shape.
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N pilot gauges with a model "
                         "(smoke test)")
    ap.add_argument("--seed", type=int, default=12345)
    return ap.parse_args(argv)


def run(args: argparse.Namespace, cfg: dict | None = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    scfg = cfg.get("forecast", {}).get("seasonal", {})
    fscfg = cfg.get("forecast", {}).get("flow_seasonal", {})
    fcfg = cfg.get("forecast", {}).get("ensemble", {}).get("flow", {})

    if not scfg.get("enabled", True):
        print("forecast.seasonal.enabled = false — nothing to do "
             "(the flow shadow shares the GW seasonal gate)")
        return 0
    if not fscfg.get("enabled", True):
        print("forecast.flow_seasonal.enabled = false — nothing to do")
        return 0
    if not fcfg.get("enabled", True):
        print("forecast.ensemble.flow.enabled = false — nothing to do")
        return 0

    # resolve_flow_pilot_path: the same helper build_flow_models.py's default
    # and the flow ENS bridge call site (build_ensemble_members.py) use, so
    # all four flow-pilot consumers agree on where the pilot CSV lives.
    pilot_path = resolve_flow_pilot_path(cfg, ROOT)
    if not pilot_path.exists():
        print(f"{pilot_path} not found — flow seasonal shadow skipped (run "
             f"'python -m scripts.select_flow_pilot' to enable it on this host).")
        return 0
    pilot = pd.read_csv(pilot_path, dtype={"gauge_id": str})
    if pilot.empty:
        print(f"{pilot_path} is empty — nothing to shadow-forecast.")
        return 0

    models_path = ROOT / fcfg.get("models_cache", "data/model/flow_models.json")
    if not models_path.exists():
        print(f"{models_path} not found — flow seasonal shadow skipped (run "
             f"'python -m scripts.build_flow_models' to enable it on this host).")
        return 0
    models = R.load_models(models_path)
    if not models:
        print(f"{models_path} has no calibrated flow models — nothing to "
             f"shadow-forecast.")
        return 0

    links_path = ROOT / FLOW_LINKS_PATH
    catalogue_path = ROOT / FLOW_CATALOGUE_PATH
    if not links_path.exists() or not catalogue_path.exists():
        print("flow_links.csv / flow_catalogue.csv not found — flow seasonal "
             "shadow skipped.")
        return 0
    links_df = pd.read_csv(links_path, dtype=str).set_index("GaugeID")
    cat_df = (pd.read_csv(catalogue_path, dtype={"station_id": str})
             .set_index("station_id"))

    months = int(scfg.get("months", 6))
    start_year = int(scfg.get("trace_start_year", 1991))
    use_seas5 = bool(scfg.get("seas5_weighting", True))
    weight_months = int(scfg.get("weight_months", 3))
    band_mode = str(scfg.get("band_mode", "additive"))
    max_age = int(scfg.get("max_anchor_age_days", 45))
    mc_samples = int(fscfg.get("mc_samples", 2000))
    raw_root = cfg.get("download", {}).get("raw_root", "data/raw")

    # Anchor at the flow fan's terminal (continuity with the 14-day fan),
    # same reason build_seasonal_outlook.py anchors GW's ESP at the GW fan
    # terminal — falls back to the last flow observation when no fan row
    # exists for a gauge (e.g. build_flow_members hasn't run yet on this host).
    fan_terminal: dict[str, tuple] = {}
    fan_path = ROOT / fcfg.get("fan_cache", "data/model/forecast_flow_fan.csv")
    if fan_path.exists():
        fdf = pd.read_csv(fan_path, dtype={"gauge_id": str}, parse_dates=["date"])
        if "segment" in fdf.columns:
            fdf = fdf[fdf["segment"] == "forecast"]
        for gid, fg in fdf.groupby("gauge_id"):
            if fg.empty:
                continue
            last = fg.loc[fg["lead"].idxmax()]
            fan_terminal[str(gid)] = (
                pd.Timestamp(last["date"]).tz_localize(None).normalize(),
                float(last["q_p10_m3s"]), float(last["q_p50_m3s"]),
                float(last["q_p90_m3s"]))

    run_ts = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)

    gauge_ids = sorted(g for g in pilot["gauge_id"] if g in models and g in cat_df.index)
    if args.limit is not None:
        gauge_ids = gauge_ids[: args.limit]
    print(f"Flow seasonal shadow — {len(gauge_ids)} pilot gauge(s) with a model, "
         f"{months} months, traces {start_year}→, SEAS5 weighting "
         f"{'ON' if use_seas5 else 'OFF'}")

    all_rows: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for gauge_id in gauge_ids:
        rec = models[gauge_id]
        q95 = rec.get("q95_m3s")
        if q95 is None:
            skipped.append((gauge_id, "no q95_m3s on the calibrated rec")); continue

        fp = FLOW_SHARD_DIR / f"{gauge_id}.parquet"
        if not fp.exists():
            skipped.append((gauge_id, "no flow shard")); continue
        shard = pd.read_parquet(fp)
        if shard.empty:
            skipped.append((gauge_id, "empty flow shard")); continue
        q = pd.Series(shard["Flow_m3s"].to_numpy(float),
                     index=pd.to_datetime(shard["date"]), name="Flow_m3s").sort_index()
        h = q.dropna()
        if h.empty:
            skipped.append((gauge_id, "no flow observations")); continue
        obs_last = pd.Timestamp(h.index.max()).tz_localize(None).normalize()

        ft = fan_terminal.get(gauge_id)
        if ft is not None:
            origin, ft_p10, ft_p50, ft_p90 = ft
            seed_head = pd.concat([h, pd.Series({origin: ft_p50})]).sort_index()
            seed_head = seed_head[~seed_head.index.duplicated(keep="last")]
            ft_m3s = (ft_p10, ft_p50, ft_p90)
        else:
            origin = obs_last
            seed_head = h
            ft_m3s = None

        # STALENESS GATE — mirrors build_seasonal_outlook.py's anchor-age
        # check exactly: no honest outlook exists from a stale seed.
        anchor_age = (run_ts - origin).days
        if anchor_age > max_age:
            skipped.append((gauge_id, f"anchor {origin.date()} is {anchor_age}d old "
                                     f"(> {max_age}) — refusing a stale-seeded outlook"))
            continue

        pet_s = load_pet(gauge_id)
        era5 = load_station_precip(gauge_id)
        if pet_s is None or pet_s.empty or era5.empty:
            skipped.append((gauge_id, "missing ERA5/PET cache — run "
                                     "refresh_seasonal_inputs")); continue

        periods = esp.monthly_anchors(origin, months)
        years = [y for y in range(start_year, origin.year)]
        f_dates = pd.date_range(origin + pd.Timedelta(days=1),
                                periods=esp.TRACE_DAYS, freq="D")

        rain_hist = gauge_rainfall_for(gauge_id, links_df, raw_root)
        if rain_hist.empty:
            skipped.append((gauge_id, "no rain data")); continue
        f_g = _fit_f_g(rain_hist, era5)
        if f_g is None:
            f_g = 1.0
            print(f"  ! {gauge_id[:8]}: thin gauge/ERA5 rain overlap — using "
                 f"f_g=1.0 (no persisted per-gauge fallback for flow)")

        obs_rain = R._norm(rain_hist)
        obs_pet = R._norm(pet_s)
        obs_end = min(
            (obs_rain.index.max() if len(obs_rain) else origin),
            (obs_pet.index.max() if len(obs_pet) else origin),
            origin,
        )
        obs_end = pd.Timestamp(obs_end).normalize()
        obs_rain = obs_rain[obs_rain.index <= obs_end]
        obs_pet = obs_pet[obs_pet.index <= obs_end]
        trace_dates = pd.date_range(obs_end + pd.Timedelta(days=1),
                                    f_dates[-1], freq="D")
        windows = esp.trace_windows(obs_end, years, days=len(trace_dates))

        mu_by_year: dict[int, np.ndarray] = {}
        sig_daily = None
        monthly_precip_raw: dict[int, np.ndarray] = {}
        for y, win in windows.items():
            tr_p = era5.reindex(win)
            tr_e = pet_s.reindex(win)
            if (tr_p.isna().mean() > MAX_TRACE_GAP_FRAC
                    or tr_e.isna().mean() > MAX_TRACE_GAP_FRAC):
                continue
            prec_f = _stamp(tr_p.fillna(0.0) * f_g, trace_dates)
            evap_f = _stamp(tr_e.fillna(0.0), trace_dates)
            bridged_prec = pd.concat([obs_rain, prec_f]).sort_index()
            bridged_prec = bridged_prec[~bridged_prec.index.duplicated(keep="last")]
            bridged_evap = pd.concat([obs_pet, evap_f]).sort_index()
            bridged_evap = bridged_evap[~bridged_evap.index.duplicated(keep="last")]
            mean, sigma = R.simulate_path(rec, seed_head, bridged_prec, bridged_evap,
                                          origin, f_dates)
            mu_by_year[y] = mean
            sig_daily = sigma          # independent of the trace — see docstring
            stamped_raw = _stamp(tr_p.fillna(0.0), trace_dates).reindex(f_dates)
            monthly_precip_raw[y] = esp.monthly_means(
                pd.Series(stamped_raw.to_numpy(float), index=f_dates), periods
            ) * np.array([p.days_in_month for p in periods], float)

        if len(mu_by_year) < MIN_TRACES:
            skipped.append((gauge_id, f"only {len(mu_by_year)} usable traces "
                                     f"(<{MIN_TRACES})"))
            continue

        probs = None
        seas5_weighted = False
        clim_bounds = normals.precip_monthly_clim_bounds(era5, periods)
        if use_seas5 and np.isfinite(clim_bounds).all():
            lat = float(cat_df.loc[gauge_id, "lat"])
            lon = float(cat_df.loc[gauge_id, "lon"])
            totals = seas5.load_cds_totals(gauge_id, periods)
            if totals is None:
                payload = seas5.load_cached_payload(lat, lon)
                totals = (seas5.monthly_member_totals(
                    seas5.member_daily_frame(payload), periods)
                    if payload is not None else None)
            if totals is None:
                print(f"  ! {gauge_id[:8]}: no cached SEAS5 (CDS or OM) — equal weights")
            else:
                probs = seas5.tercile_probs(totals, clim_bounds)
                seas5_weighted = True
        weights = esp.trace_weights(monthly_precip_raw, probs, clim_bounds,
                                    weight_months=weight_months)

        rows = compute_gauge_shadow(
            gauge_id, rec, mu_by_year=mu_by_year, sig_daily=sig_daily,
            weights=weights, f_dates=f_dates, periods=periods, origin=origin,
            obs_last=obs_last, q95_m3s=float(q95), run=run_ts,
            seas5_weighted=seas5_weighted, ft_m3s=ft_m3s, band_mode=band_mode,
            mc_samples=mc_samples, seed=args.seed)
        if not rows:
            skipped.append((gauge_id, "no shadow rows produced")); continue
        all_rows.extend(rows)

        m1 = next((r for r in rows if r["month_ahead"] == 1), None)
        if m1:
            print(f"  {gauge_id[:8]}  traces={m1['n_traces']}  "
                 f"seas5={'Y' if seas5_weighted else 'n'}  "
                 f"M1 Q p10/50/90={m1['q_p10_m3s']:.3f}/{m1['q_p50_m3s']:.3f}/"
                 f"{m1['q_p90_m3s']:.3f} m3/s  P(sub-Q95)={m1['p_sub_q95']:.2f}")

    if not all_rows:
        print("\nNo flow seasonal shadow rows produced.")
        for gauge_id, why in skipped:
            print(f"  skipped {gauge_id[:8]}: {why}")
        # An evidence archive is never a hard dependency — never fail the chain.
        return 0

    out = pd.DataFrame(all_rows, columns=SHADOW_COLS)
    archive_path = ROOT / fscfg.get("archive_cache",
                                    "data/model/flow_seasonal_shadow_archive.parquet")
    prior = pd.read_parquet(archive_path) if archive_path.exists() else None
    combined = append_shadow_archive(prior, out)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(archive_path, compression="snappy", index=False)

    print(f"\nWrote {len(out)} shadow rows ({out['gauge_id'].nunique()} gauge(s) x "
         f"{months} months) → {archive_path.relative_to(ROOT)} "
         f"({len(combined)} archived rows total)")
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

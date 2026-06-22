"""Seasonal groundwater outlook (months 1-6) — ESP traces through the
calibrated Pastas models, SEAS5-weighted (pastas-env step).

Pure compute from caches: scripts/refresh_seasonal_inputs.py (main env) must
have fetched the ERA5 precip, PET, and SEAS5 payloads first. For each
in-scope borehole with a calibrated model:

  1. Cut ~34 historic-year forcing traces from the ERA5 precip + ET0 caches
     (esp.trace_windows), re-stamp each onto the forecast calendar, scale
     rainfall by the borehole's f_bh (gauge/ERA5 by construction).
  2. Bridge each trace onto the observed gauge/PET history and run
     recharge.simulate_path → per-trace GW trajectory + predictive sigma.
  3. Weight traces by SEAS5 monthly tercile probabilities (seas5.py;
     equal weights when disabled or the payload is missing).
  4. Per outlook month: weighted-mixture P(below/near/above normal GW) vs
     the borehole's own monthly climatology + weighted P10/P50/P90.

Writes forecast_seasonal_summary.csv + appends forecast_seasonal_archive.parquet
and (re)writes gw_monthly_normals.csv.

Run monthly with the pastas venv python, after refresh_seasonal_inputs:
  .venv-pastas\\Scripts\\python -m scripts.build_seasonal_outlook
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.data.era5_precip import load_station_precip
from src.forecast.pastas import recharge as R
from src.forecast.pastas import ensemble as E
from src.forecast.pastas.io import load_pet
from src.forecast.seasonal import esp, normals, seas5

ROOT = Path(__file__).resolve().parents[1]
JOINED = ROOT / "data/features/joined_timeseries.csv"
CATALOGUE = ROOT / "data/processed/catalogue.csv"
BIAS = ROOT / "data/model/ensemble_bias_factors.csv"

SUMMARY_COLS = ["station_id", "run", "origin_date", "month_ahead", "month_start",
                "p_below", "p_near", "p_above", "gw_p10", "gw_p50", "gw_p90",
                "n_traces", "seas5_weighted"]
MIN_TRACES = 10
MAX_TRACE_GAP_FRAC = 0.05      # a trace year missing >5% of days is dropped


def _load_fbh() -> dict[str, float]:
    if not BIAS.exists():
        return {}
    df = pd.read_csv(BIAS)
    return dict(zip(df["station_id"].astype(str), df["f_bh"].astype(float)))


def _fit_fbh(gauge: pd.Series, ref: pd.Series,
             lo: float = 0.2, hi: float = 5.0):
    """f_bh = mean(gauge) / mean(reference precip) on common dates, clamped to
    [lo, hi]. Returns None when the overlap is too thin or the reference mean is
    ~0 (caller falls back to the persisted ensemble factor).

    Fitting against THIS borehole's cached reanalysis precip keeps the seasonal
    traces self-consistent with their source: post free-data migration the cache
    is raw CDS-ERA5 (0.25°), which is NOT Open-Meteo's downscaled ~9 km blend, so
    the carried-over ensemble f_bh would mis-scale the traces (W4)."""
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


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cfg = json.loads((ROOT / "config/config.json").read_text())
    scfg = cfg.get("forecast", {}).get("seasonal", {})
    if not scfg.get("enabled", True):
        print("forecast.seasonal.enabled = false — nothing to do")
        return 0
    pcfg = cfg["forecast"]["ensemble"]["pastas"]

    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", type=int, default=0,
                    help="cap on borehole count (0 = no cap; for probing)")
    args = ap.parse_args()

    months = int(scfg.get("months", 6))
    start_year = int(scfg.get("trace_start_year", 1991))
    use_seas5 = bool(scfg.get("seas5_weighting", True))
    weight_months = int(scfg.get("weight_months", 3))
    # "additive" (default) = corrected-additive uncertainty inheritance from the
    # fan terminal (esp.additive_band); "legacy" = the old between-year-spread band.
    band_mode = str(scfg.get("band_mode", "additive"))

    models = R.load_models(ROOT / pcfg["models_cache"])
    if not models:
        print("No calibrated models — run scripts.build_pastas_models first.")
        return 1
    cat = (pd.read_csv(CATALOGUE).query("measure_type == 'groundwater'")
           .dropna(subset=["lat", "lon"]).drop_duplicates("station_id")
           .set_index("station_id"))
    fbh = _load_fbh()
    joined = pd.read_csv(JOINED, index_col=0, parse_dates=True)

    # Anchor each ESP to the END of the 46-day Pastas fan (the forecast-segment
    # terminal date + P50), so the seasonal continues smoothly from the fan
    # instead of re-seeding at the stale observation and reverting to
    # climatology (the old "seasonal starts high" seam). Months then run AFTER
    # the fan rather than overlapping it. Falls back to the obs anchor where no
    # fan row exists (e.g. a borehole without a Pastas forecast).
    fan_terminal: dict[str, tuple] = {}
    fan_path = ROOT / pcfg["fan_cache"]
    if fan_path.exists():
        fdf = pd.read_csv(fan_path, parse_dates=["date"])
        if "segment" in fdf.columns:
            fdf = fdf[fdf["segment"] == "forecast"]
        for fsid, fg in fdf.groupby("station_id"):
            last = fg.loc[fg["lead"].idxmax()]
            fan_terminal[str(fsid)] = (
                pd.Timestamp(last["date"]).tz_localize(None).normalize(),
                float(last["gw_p50"]), float(last["gw_p10"]), float(last["gw_p90"]))

    # Monthly GW normals — the tercile yardstick. Built by the main-env
    # core stage (scripts/build_gw_normals.py); this pastas-env step only
    # reads it.
    norm_path = ROOT / scfg.get("normals_cache", "data/model/gw_monthly_normals.csv")
    if not norm_path.exists():
        print(f"No monthly normals at {norm_path.relative_to(ROOT)} — run "
              f"`python -m scripts.build_gw_normals` (run_chain --core) first.")
        return 1
    norm_df = pd.read_csv(norm_path)
    norm_ix = norm_df.set_index(["station_id", "month"])

    run = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    # Sorted for determinism — keeps --stations N probes aligned with
    # refresh_seasonal_inputs' (also sorted) cap.
    sids = sorted(s for s in models if s in cat.index)
    if args.stations:
        sids = sids[:args.stations]
    print(f"Seasonal outlook — {len(sids)} boreholes, {months} months, "
          f"traces {start_year}→, SEAS5 weighting {'ON' if use_seas5 else 'OFF'}")

    rows, skipped = [], []
    for sid in sids:
        rec = models[sid]
        g = joined[joined["station_id"] == sid].sort_index()
        head = E.freshest_gw(sid, fallback=g["GW_Level"])
        h = head.dropna()
        if h.empty:
            skipped.append((sid, "no GW")); continue
        # Re-seed at the fan terminal (continuity) when a fan exists; else the
        # last observation. seed_head injects the anchor level so simulate_path's
        # origin residual snaps the ESP to where the fan ended.
        obs_last = pd.Timestamp(h.index.max()).tz_localize(None).normalize()
        ft = fan_terminal.get(sid)
        if ft is not None:
            origin, anchor_p50, ft_p10, ft_p90 = ft
            seed_head = pd.concat([h, pd.Series({origin: anchor_p50})]).sort_index()
            seed_head = seed_head[~seed_head.index.duplicated(keep="last")]
        else:
            origin = obs_last
            seed_head = head
        pet_s = load_pet(sid)
        era5 = load_station_precip(sid)
        if pet_s is None or pet_s.empty or era5.empty:
            skipped.append((sid, "missing ERA5/PET cache — run "
                                 "refresh_seasonal_inputs")); continue

        periods = esp.monthly_anchors(origin, months)
        # usable trace years: window fully inside the archive, not the
        # origin's own year
        years = [y for y in range(start_year, origin.year)]
        windows = esp.trace_windows(origin, years)
        f_dates = pd.date_range(origin + pd.Timedelta(days=1),
                                periods=esp.TRACE_DAYS, freq="D")

        # Fit f_bh inline against this borehole's cached CDS-ERA5 (era5) so the
        # traces sit on the calibrated gauge scale; fall back to the persisted
        # ensemble factor if the gauge/ERA5 overlap is too thin.
        f_bh = _fit_fbh(g["Rainfall"], era5)
        if f_bh is None:
            f_bh = float(fbh.get(sid, 1.0))
        obs_rain = R._norm(g["Rainfall"])
        obs_rain = obs_rain[obs_rain.index < f_dates.min()]
        obs_pet = R._norm(pet_s)
        obs_pet = obs_pet[obs_pet.index < f_dates.min()]

        mu, sig, monthly_precip_raw = {}, {}, {}
        for y, win in windows.items():
            tr_p = era5.reindex(win)
            tr_e = pet_s.reindex(win)
            if (tr_p.isna().mean() > MAX_TRACE_GAP_FRAC
                    or tr_e.isna().mean() > MAX_TRACE_GAP_FRAC):
                continue
            prec_f = _stamp(tr_p.fillna(0.0) * f_bh, f_dates)
            evap_f = _stamp(tr_e.fillna(0.0), f_dates)
            bridged_prec = pd.concat([obs_rain, prec_f]).sort_index()
            bridged_prec = bridged_prec[~bridged_prec.index.duplicated(keep="last")]
            bridged_evap = pd.concat([obs_pet, evap_f]).sort_index()
            bridged_evap = bridged_evap[~bridged_evap.index.duplicated(keep="last")]
            mean, sigma = R.simulate_path(rec, seed_head, bridged_prec, bridged_evap,
                                          origin, f_dates)
            traj = pd.Series(mean, index=f_dates)
            mu[y] = esp.monthly_means(traj, periods)
            # monthly-mean sigma ≈ the sigma at each month's mid-window day
            # (conservative: daily AR1 sigma, not the smaller mean-of-month sd)
            sig_s = pd.Series(sigma, index=f_dates)
            sig[y] = esp.monthly_means(sig_s, periods)
            # raw-ERA5 monthly totals (pre-f_bh) for SEAS5 tercile weighting —
            # SEAS5 members and traces are classified in the same raw space.
            # Re-stamped onto the forecast calendar first (the historic index
            # would land the totals in the trace year's periods, not ours);
            # mean × days_in_month = total for the full months the weighting
            # uses (weight_months ≤ 3 keeps clear of the partial last month).
            stamped_raw = _stamp(tr_p.fillna(0.0), f_dates)
            monthly_precip_raw[y] = esp.monthly_means(
                stamped_raw, periods) * np.array(
                [p.days_in_month for p in periods], float)

        if len(mu) < MIN_TRACES:
            skipped.append((sid, f"only {len(mu)} usable traces (<{MIN_TRACES})"))
            continue

        # SEAS5 weighting (loud, optional, never fatal)
        probs = None
        seas5_weighted = False
        clim_bounds = normals.precip_monthly_clim_bounds(era5, periods)
        if use_seas5 and np.isfinite(clim_bounds).all():
            lat, lon = float(cat.loc[sid, "lat"]), float(cat.loc[sid, "lon"])
            # CDS monthly source first (free, commercial-OK; W3); fall back
            # to the Open-Meteo daily payload; else equal weights.
            totals = seas5.load_cds_totals(sid, periods)
            if totals is None:
                payload = seas5.load_cached_payload(lat, lon)
                totals = (seas5.monthly_member_totals(
                    seas5.member_daily_frame(payload), periods)
                    if payload is not None else None)
            if totals is None:
                print(f"  ! {sid[:8]}: no cached SEAS5 (CDS or OM) — equal weights")
            else:
                probs = seas5.tercile_probs(totals, clim_bounds)
                seas5_weighted = True
        weights = esp.trace_weights(monthly_precip_raw, probs, clim_bounds,
                                    weight_months=weight_months)

        years_used = sorted(mu)
        w_vec = np.array([weights[y] for y in years_used])
        # Corrected-additive band: inherit the fan's terminal uncertainty
        # (decaying on the aquifer-memory timescale) + the model AR1, so the
        # seasonal band is continuous with the fan instead of collapsing at
        # month-1. origin == the fan terminal date when a fan exists.
        use_additive = band_mode == "additive" and ft is not None
        if use_additive:
            sd46 = (ft_p90 - ft_p10) / (2 * 1.2815515594)
            dt46 = max((origin - obs_last).days, 1)
            tau_state = esp.state_memory_timescale(rec)
        for m_idx, p in enumerate(periods):
            key = (sid, p.month)
            if key not in norm_ix.index:
                continue                       # no defensible normal → no row
            t1, t2 = float(norm_ix.loc[key, "t1"]), float(norm_ix.loc[key, "t2"])
            mu_vec = np.array([mu[y][m_idx] for y in years_used])
            sig_vec = np.array([sig[y][m_idx] for y in years_used])
            pb, pn, pa = esp.weighted_tercile_probs(mu_vec, sig_vec, w_vec, t1, t2)
            if use_additive:
                mid = p.to_timestamp() + pd.Timedelta(days=p.days_in_month / 2)
                q10, q50, q90 = esp.additive_band(
                    mu_vec, w_vec, sigma=float(rec["sigma"]), alpha=float(rec["alpha"]),
                    tau_state=tau_state, sd46=sd46, dt46=dt46,
                    dt_month=max((mid - obs_last).days, 1),
                    lead_gap=max((mid - origin).days, 0))
            else:
                q10, q50, q90 = esp.weighted_quantiles(mu_vec, w_vec)
            rows.append({"station_id": sid, "run": run,
                         "origin_date": origin.date().isoformat(),
                         "month_ahead": m_idx + 1,
                         "month_start": p.to_timestamp().date().isoformat(),
                         "p_below": pb, "p_near": pn, "p_above": pa,
                         "gw_p10": q10, "gw_p50": q50, "gw_p90": q90,
                         "n_traces": len(years_used),
                         "seas5_weighted": seas5_weighted})
        first = next((r for r in rows
                      if r["station_id"] == sid and r["month_ahead"] == 1), None)
        m1 = (f"M1 P(b/n/a)={first['p_below']:.2f}/{first['p_near']:.2f}/"
              f"{first['p_above']:.2f}" if first else "no M1 normal")
        print(f"  {sid[:8]}  traces={len(years_used)}  "
              f"seas5={'Y' if seas5_weighted else 'n'}  {m1}")

    if not rows:
        print("No seasonal outlook rows produced.")
        for sid, why in skipped:
            print(f"  skipped {sid[:8]}: {why}")
        return 1

    out = pd.DataFrame(rows, columns=SUMMARY_COLS)
    dest = ROOT / scfg.get("summary_cache", "data/model/forecast_seasonal_summary.csv")
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(dest, index=False)

    arch_path = ROOT / scfg.get("archive_cache",
                                "data/model/forecast_seasonal_archive.parquet")
    if arch_path.exists():
        arch = pd.read_parquet(arch_path)
        keep = arch[~((arch["run"] == run)
                      & arch["station_id"].isin(out["station_id"]))]
        arch = pd.concat([keep, out], ignore_index=True)
    else:
        arch = out
    arch.to_parquet(arch_path, index=False)

    print(f"\nWrote {len(out)} rows ({out['station_id'].nunique()} boreholes × "
          f"{months} months) → {dest.relative_to(ROOT)}")
    print(f"Archive: {len(arch)} rows → {arch_path.relative_to(ROOT)}")
    for sid, why in skipped:
        print(f"  skipped {sid[:8]}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

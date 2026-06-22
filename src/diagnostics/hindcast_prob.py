"""Probabilistic verification on the leakage-safe roll hindcast (Phase 3 / A).

Wraps the existing point-MAE hindcast (``hindcast.py``) in a predictive band and
scores its CALIBRATION + probabilistic skill — the 3.1 "verification surface"
engine core, doable now (no winter-of-fans wait), using the verification
primitives in ``verification.py``.

Scope / honesty (see ``docs/phase3_verification_scope.md``): this scores the
**operational GW-roll method under perfect-forecast** — the roll point forecast
wrapped in its own **train-estimated per-lead error spread**, evaluated on
out-of-sample TEST origins. It tells us whether that band is calibrated and beats
the naive baselines; it is **NOT** the headline Pastas-ensemble fan, and weather
error is excluded by the perfect-forecast design. A partial — but real and
quantified — surface, the first GWC can publish without the winter archive.

Leakage safety: the roll params, the per-lead band sd, and the baseline spreads
are all estimated on the TRAIN split (dates < test_start); only TEST origins are
scored. Mirrors ``hindcast.py`` and reuses its split/pilot/origin helpers.

Pure aggregation (``summarise``, ``pit_histogram``) is unit-tested; the driver
(``run_prob_hindcast``) is smoke-run on the real feature set.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.ensemble import gw_roll
from src.forecast.ensemble.hindcast import (
    _split_train_test, _select_pilot_stations,
)
from src.diagnostics import verification as V

ROOT = Path(__file__).parents[3]
_MIN_TRAIN_ORIGINS = 8        # per-lead band sd is too noisy below this
_SD_FLOOR = 1e-3              # mAOD; avoid a degenerate zero-width band


# ---------------------------------------------------------------------------
# Pure aggregation (unit-tested)
# ---------------------------------------------------------------------------

def summarise(rows: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Per-lead probabilistic summary from scored test rows.

    `rows` columns: lead, crps, crps_persist, crps_clim, pit, sq_err, sd.
    Returns per-lead: n, mean_crps, crpss_persist, crpss_clim, pit_mean,
    spread (mean predictive sd), rmse, spread_skill (spread / rmse — ~1 is
    well-dispersed, <1 under-dispersed/over-confident, >1 over-dispersed)."""
    out = []
    for lead in range(1, horizon + 1):
        g = rows[rows["lead"] == lead]
        if g.empty:
            continue
        rmse = float(np.sqrt(np.mean(g["sq_err"]))) if len(g) else float("nan")
        spread = float(np.mean(g["sd"]))
        out.append({
            "lead": lead,
            "n": int(len(g)),
            "mean_crps": float(np.mean(g["crps"])),
            "crpss_persist": V.skill_score(g["crps"], g["crps_persist"]),
            "crpss_clim": V.skill_score(g["crps"], g["crps_clim"]),
            "pit_mean": float(np.mean(g["pit"])),
            "spread": spread,
            "rmse": rmse,
            "spread_skill": spread / rmse if rmse > 1e-9 else float("nan"),
        })
    return pd.DataFrame(out)


def pit_histogram(pits, bins: int = 10) -> pd.DataFrame:
    """PIT histogram + a flatness deviation. A calibrated forecast is ~uniform
    (each of `bins` holds 1/bins of the mass); the `dev` column is observed minus
    that uniform expectation (∑|dev| is a simple miscalibration score)."""
    p = np.asarray(pits, dtype=float)
    p = p[np.isfinite(p)]
    edges = np.linspace(0.0, 1.0, bins + 1)
    counts, _ = np.histogram(p, bins=edges)
    frac = counts / counts.sum() if counts.sum() else counts.astype(float)
    return pd.DataFrame({
        "bin_lo": edges[:-1], "bin_hi": edges[1:],
        "frac": frac, "dev": frac - (1.0 / bins),
    })


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _origins_in(idx_positions: int, lo_ok, horizon: int, stride: int,
                max_origins: int) -> list[int]:
    """Positional origins i with ≥7 rows of prior context, a full horizon ahead,
    and lo_ok(i) true (used to take TRAIN vs TEST origins from the same series)."""
    out: list[int] = []
    for i in range(7, idx_positions - horizon):
        if not lo_ok(i):
            continue
        if out and (i - out[-1]) < stride:
            continue
        out.append(i)
        if len(out) >= max_origins:
            break
    return out


def _band_sd_by_lead(errors_by_lead: dict[int, list[float]], horizon: int) -> np.ndarray:
    """Per-lead sd of the collected (train) errors; floored, NaN-safe."""
    sd = np.full(horizon, np.nan)
    for lead, errs in errors_by_lead.items():
        e = np.asarray(errs, dtype=float)
        e = e[np.isfinite(e)]
        if e.size >= 2:
            sd[lead - 1] = max(float(np.std(e, ddof=1)), _SD_FLOOR)
    return sd


def run_prob_hindcast(config: dict, *, method: str = "reduced_form_ar",
                      n_stations: int = 12, horizon: int = 14, stride: int = 15,
                      max_origins_per_station: int = 14) -> dict:
    from src.features.io import load_features
    df, _ = load_features(config)
    ratio = float(config.get("forecast", {}).get("hindcast_split_ratio", 0.8))
    _, test = _split_train_test(df, ratio)
    test_start = test.index.min()
    pilots = _select_pilot_stations(test, n_stations)

    fitters = {"reduced_form": gw_roll.fit_reduced_form,
               "reduced_form_ar": gw_roll.fit_reduced_form_ar,
               "reduced_form_cr": gw_roll.fit_reduced_form_cr}
    rollers = {"reduced_form": gw_roll.roll_reduced_form,
               "reduced_form_ar": gw_roll.roll_reduced_form_ar,
               "reduced_form_cr": gw_roll.roll_reduced_form_cr}
    fit_fn, roll_fn = fitters[method], rollers[method]

    def _roll(seed_gw, seed_dgw, fut, params, dgw_clip, gw_clip):
        if method == "reduced_form":
            return roll_fn(seed_gw, fut, params, gw_clip=gw_clip)
        return roll_fn(seed_gw, seed_dgw, fut, params,
                       dgw_clip=dgw_clip, gw_clip=gw_clip)

    rows: list[dict] = []
    n_pilots_used = 0
    for sid in pilots:
        s_all = df[df["station_id"] == sid].sort_index()
        s_train = s_all[s_all.index < test_start]
        if len(s_train) < 60:
            continue
        params = fit_fn(s_train)
        dgw_clip, gw_clip = gw_roll.station_guardrails(s_train)
        dgw_all = s_all["GW_Level"] - s_all["GW_Lag1"]
        clim_mean = float(s_train["GW_Level"].mean())
        clim_sd = float(s_train["GW_Level"].std(ddof=1))

        # --- TRAIN: per-lead band sd for the method + persistence ---
        pos = {ts: k for k, ts in enumerate(s_all.index)}
        train_cut = pos.get(test_start, sum(s_all.index < test_start))
        err_op: dict[int, list[float]] = {}
        err_pe: dict[int, list[float]] = {}
        for i in _origins_in(len(s_all), lambda i: s_all.index[i] < test_start,
                             horizon, stride, max_origins_per_station):
            seed_gw = float(s_all.iloc[i]["GW_Level"])
            seed_dgw = float(dgw_all.iloc[i])
            fut = s_all.iloc[i + 1: i + 1 + horizon]
            actual = fut["GW_Level"].to_numpy(float)
            if np.isnan(actual).any():
                continue
            pred = _roll(seed_gw, seed_dgw, fut, params, dgw_clip, gw_clip)
            for L in range(horizon):
                err_op.setdefault(L + 1, []).append(pred[L] - actual[L])
                err_pe.setdefault(L + 1, []).append(seed_gw - actual[L])
        sd_op = _band_sd_by_lead(err_op, horizon)
        sd_pe = _band_sd_by_lead(err_pe, horizon)
        if np.isnan(sd_op).all() or sum(len(v) for v in err_op.values()) \
                < _MIN_TRAIN_ORIGINS * horizon:
            continue
        n_pilots_used += 1

        # --- TEST: score the band out-of-sample ---
        for i in _origins_in(len(s_all), lambda i: s_all.index[i] >= test_start,
                             horizon, stride, max_origins_per_station):
            seed_gw = float(s_all.iloc[i]["GW_Level"])
            seed_dgw = float(dgw_all.iloc[i])
            fut = s_all.iloc[i + 1: i + 1 + horizon]
            actual = fut["GW_Level"].to_numpy(float)
            if np.isnan(actual).any():
                continue
            pred = _roll(seed_gw, seed_dgw, fut, params, dgw_clip, gw_clip)
            for L in range(horizon):
                lead = L + 1
                s_op = sd_op[L] if np.isfinite(sd_op[L]) else clim_sd
                s_pe = sd_pe[L] if np.isfinite(sd_pe[L]) else clim_sd
                rows.append({
                    "lead": lead,
                    "crps": float(V.crps_gaussian(pred[L], s_op, actual[L])),
                    "crps_persist": float(V.crps_gaussian(seed_gw, s_pe, actual[L])),
                    "crps_clim": float(V.crps_gaussian(clim_mean, clim_sd, actual[L])),
                    "pit": float(V.pit_gaussian(pred[L], s_op, actual[L])),
                    "sq_err": float((pred[L] - actual[L]) ** 2),
                    "sd": float(s_op),
                })

    rec = pd.DataFrame(rows)
    summary = summarise(rec, horizon) if not rec.empty else pd.DataFrame()
    pit = pit_histogram(rec["pit"]) if not rec.empty else pd.DataFrame()
    return {"summary": summary, "pit": pit, "n_rows": len(rec),
            "n_pilots": n_pilots_used, "method": method, "horizon": horizon}

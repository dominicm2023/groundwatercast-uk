"""Pastas TFN recharge — calibration, persistence, seeded forecasting.

Pure modelling layer: it takes plain pandas Series (head, precip, PET) and
returns/consumes a small serialisable ``ModelRec`` dict. All IO (reading the
joined timeseries, PET cache, ensemble members; writing parquet) lives in the
driver scripts, so this module stays testable in isolation.

``pastas`` is imported lazily inside each function so that importing this module
does not require pastas to be installed (the main pipeline never calls these).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import json

import numpy as np
import pandas as pd

# Cap the AR1 residual decorrelation used to decay the seed residual forward.
# A fitted alpha beyond ~1 year is a degenerate near-unit-root noise fit (e.g.
# the ~999.5-day calibrate fallback when the residual lag-1 autocorr clamps to
# 0.999), which would carry the seed residual almost undecayed across the whole
# fan and flat-line it at a possibly-wrong (and now QC'd-but-historically-bad)
# level. Bounding it lets the predictive band widen toward the marginal sigma.
_ALPHA_MAX_DAYS: float = 365.0

# Day-1 band floor (2026-07-18, from the first verification dry-run). The AR1
# conditional sd sigma*sqrt(1-exp(-2k/alpha)) is near-zero at k=1 for
# long-memory stations (alpha ~ months), so the published day-1 band averaged
# ~0.16 m wide and covered only 45-54% of observations vs the nominal 80%
# (docs/phase3_verification_scope.md §First dry-run). Real levels carry daily
# measurement/micro-event noise the long-memory AR1 cannot represent, so a
# per-station noise floor is added in quadrature: robust (MAD-based) sd of
# CONSECUTIVE-calendar-day changes over the recent window, scaled by
# _NOISE_FLOOR_K and capped. Quadrature makes it self-fading — at lead 14
# (sigma ~ 0.5 m) a 0.05 m floor adds <1% width. K=2.0 and the 0.10 m cap were
# chosen on the 2026-07 archive A/B (lead-1 coverage 0.51→0.78 overall /
# 0.82 current-era, leads 2+ +~1pp); one tuned scalar, disclosed — the winter
# archive re-verifies it out-of-sample. GW models only: flow logQ daily diffs
# encode real flashiness, not observation noise.
_NOISE_FLOOR_WINDOW_DAYS: int = 90
_NOISE_FLOOR_MIN_PAIRS: int = 10
_NOISE_FLOOR_K: float = 2.0
_NOISE_FLOOR_CAP_M: float = 0.10


def daily_innovation_sigma(series: pd.Series, origin: pd.Timestamp,
                           window_days: int = _NOISE_FLOOR_WINDOW_DAYS,
                           min_pairs: int = _NOISE_FLOOR_MIN_PAIRS) -> float:
    """Robust sd (m) of day-to-day level changes near ``origin``.

    Gap-aware by construction: the series is laid on a daily calendar grid
    first, so ``diff()`` only ever pairs consecutive calendar days — a dipped
    or gappy record contributes only its genuinely-daily stretches, and a
    station with fewer than ``min_pairs`` consecutive-day pairs in the window
    returns 0.0 (no floor). MAD-based so a single telemetry spike doesn't
    inflate the estimate."""
    s = _norm(series).dropna()
    if s.empty:
        return 0.0
    origin = pd.Timestamp(origin).tz_localize(None).normalize()
    s = s[(s.index > origin - pd.Timedelta(days=window_days))
          & (s.index <= origin)]
    if s.empty:
        return 0.0
    s = s.groupby(s.index).mean()          # defend against duplicate dates
    d = s.asfreq("D").diff().dropna()      # gap-spanning diffs become NaN
    if len(d) < min_pairs:
        return 0.0
    return 1.4826 * float((d - d.median()).abs().median())


def _safe_alpha(alpha) -> float:
    """Finite, positive AR1 decay in days, capped at ``_ALPHA_MAX_DAYS``.
    NaN / inf / non-positive / unparseable → the cap (a long-but-bounded decay)."""
    try:
        a = float(alpha)
    except (TypeError, ValueError):
        return _ALPHA_MAX_DAYS
    if not np.isfinite(a) or a <= 0:
        return _ALPHA_MAX_DAYS
    return min(a, _ALPHA_MAX_DAYS)


# A calibrated model is fully described by this small dict (JSON-serialisable),
# so we never pickle a pastas object — the model is reconstructed from params.
#   station_id, rfunc, recharge          — structure
#   model_kind                           — "gw" (single recharge stress, the
#                                           historical default — absent on any
#                                           ModelRec serialised before this field
#                                           existed means "gw") or "flow_2s"
#                                           (recharge stress + a second raw-rain
#                                           "quickflow" stress, low-flow build)
#   params, param_names                  — calibrated optimal parameters (both
#                                           stresses + noise, for flow_2s)
#   sigma                                — residual std (m, or log-m3/s for flow_2s),
#                                           for the predictive band
#   alpha                                — AR1 decay (days), for residual carry-forward
#   eps                                  — flow_2s only: the log-transform epsilon
#                                           (logq = log(Q + eps)), so zero-flow days
#                                           round-trip through save/load unchanged
#   evp, n_obs, train_max, fitted_on     — provenance / fit quality
ModelRec = dict


def _norm(s: pd.Series) -> pd.Series:
    s = s.copy()
    # .as_unit("ns"): pastas 1.14 silently produces a DEGENERATE fit (all-NaN
    # residuals -> sigma/EVP NaN) on a datetime64[us] index — exactly what a
    # parquet round-trip yields (seen live 2026-07-14: every flow shard read
    # back from data/features/flow_by_station gave EVP NaN, while the same
    # values on a ns index gave EVP ~93). Coerce here, the single choke point
    # every calibrate/simulate series passes through.
    s.index = (pd.DatetimeIndex(pd.to_datetime(s.index))
               .as_unit("ns").tz_localize(None).normalize())
    return s


def _daily(s: pd.Series, *, fill: str) -> pd.Series:
    """Reindex a stress series onto a gap-free daily grid (pastas requires a
    regular time step). fill='zero' for precip; 'ffill' for PET."""
    s = _norm(s).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    idx = pd.date_range(s.index.min(), s.index.max(), freq="D")
    s = s.reindex(idx)
    return s.fillna(0.0) if fill == "zero" else s.ffill().bfill().fillna(0.0)


def _build_model(head: pd.Series, prec: pd.Series, evap: pd.Series,
                 rfunc: str, recharge: str, *, model_kind: str = "gw"):
    """Construct an (unsolved) pastas Model with the standard recharge stress +
    AR1 noise. Lazy pastas import. Used by both calibrate() and forecasting.

    model_kind="flow_2s" (docs/product/lowflow/analysis.md §3, the two-pathway
    flow model) adds a second stress — ``ps.StressModel(prec, rfunc=ps.Gamma(),
    name="quickflow")`` on raw rain, direct (no FlexModel front-end) — alongside
    the existing FlexModel recharge stress, so the solver can resolve a fast
    rain-driven pathway (quickflow) separately from the slow aquifer-drainage
    pathway (the recharge stress). ``head`` is whatever target series the model
    is fit to — real head for "gw", logQ for "flow_2s" (the log transform is the
    caller's job — see calibrate_flow / simulate_path).
    """
    import pastas as ps
    rfunc_obj = getattr(ps, rfunc)()
    recharge_obj = getattr(ps.rch, recharge)()
    ml = ps.Model(_norm(head).dropna(), name="bh")
    ml.add_stressmodel(ps.RechargeModel(_daily(prec, fill="zero"),
                                        _daily(evap, fill="ffill"),
                                        rfunc=rfunc_obj, name="rch",
                                        recharge=recharge_obj))
    if model_kind == "flow_2s":
        ml.add_stressmodel(ps.StressModel(_daily(prec, fill="zero"),
                                          rfunc=ps.Gamma(), name="quickflow",
                                          settings="prec"))
    try:
        ml.add_noisemodel(ps.ArNoiseModel())
    except Exception:
        pass
    return ml


def _fit_alpha(ml) -> float | None:
    """The solved AR1 noise decay (days), or None if no noise model solved."""
    for name in ml.parameters.index:
        if "alpha" in name.lower():
            return float(ml.parameters.loc[name, "optimal"])
    return None


def _solve_with_alpha_rescue(build_fn, train_max: pd.Timestamp | None):
    """Solve a fresh unsolved model from ``build_fn()`` (a zero-arg callable so
    a rescue re-solve starts from a clean model, not a mutated one); on a
    degenerate fit, re-solve with noise_alpha bounded and keep whichever
    explains more variance. Shared by calibrate() and calibrate_flow() — see
    calibrate()'s "Degenerate-fit rescue" comment for the full rationale.
    Returns (ml, evp) for the kept fit."""
    def _solve(bound_noise: bool):
        m = build_fn()
        if bound_noise:
            try:
                m.set_parameter("noise_alpha", pmax=_ALPHA_MAX_DAYS)
            except Exception:
                pass                      # no noise model — nothing to bound
        m.solve(tmax=train_max, report=False)
        try:
            e = float(m.stats.evp())
        except Exception:
            e = float("nan")
        return m, e

    ml, evp = _solve(bound_noise=False)
    a0 = _fit_alpha(ml)
    if (not np.isfinite(evp) or evp < 5.0
            or (a0 is not None and np.isfinite(a0) and a0 > 10 * _ALPHA_MAX_DAYS)):
        ml2, evp2 = _solve(bound_noise=True)
        if np.isfinite(evp2) and (not np.isfinite(evp) or evp2 > evp):
            ml, evp = ml2, evp2
    return ml, evp


def _noise_qa(ml, resid: pd.Series, head_norm: pd.Series) -> dict:
    """AR1 residual-fit diagnostic for this calibration (roadmap 0.3). Checks the
    innovations (``ml.noise()``, ~white if AR1 holds), falling back to residuals
    when no noise model solved. Never raises — a QA failure must not fail a fit."""
    try:
        from src.diagnostics.noise_qa import residual_diagnostics
        try:
            noise = ml.noise()
        except Exception:
            noise = None
        use_noise = noise is not None and len(noise) > 0
        series = pd.Series(noise if use_noise else resid)
        qa = residual_diagnostics(series, head_norm.reindex(series.index))
        qa["basis"] = "noise" if use_noise else "residual"
        return qa
    except Exception as exc:                                  # pragma: no cover
        return {"passes": True, "flags": f"qa_error:{type(exc).__name__}",
                "basis": "none"}


def calibrate(station_id: str, head: pd.Series, prec: pd.Series, evap: pd.Series,
              *, train_max: pd.Timestamp | None = None,
              rfunc: str = "Gamma", recharge: str = "FlexModel",
              precip_source: str = "joined") -> ModelRec:
    """Calibrate a Pastas TFN on [start, train_max] and return a ModelRec.

    train_max=None calibrates on all available head (the production default — use
    every observation). Pass a cutoff only for leakage-safe evaluation.

    precip_source : provenance only (not used in the fit) — "gauge" when `prec`
    is the raw top-3-gauge series (src.forecast.ensemble.members.observed_daily_
    rainfall, the same forcing the fan is DRIVEN with) or "joined" when it's the
    GW-date-limited joined_timeseries.csv column (the historical default, whose
    gaps recharge._daily zero-fills — a fit/drive mismatch for any station still
    on this fallback). Recorded so a stale "joined" model is easy to spot after
    a recalibration pass.
    """
    # Degenerate-fit rescue: on a marginal borehole the optimizer can park
    # noise_alpha at its huge default upper bound (~5000 d), laundering the
    # whole signal into a pseudo-random-walk noise term — EVP collapses to ~0
    # while the "fit" looks converged. That noise memory is fiction anyway:
    # simulate_path caps alpha at _ALPHA_MAX_DAYS (365 d), so re-solve with the
    # SAME bound at fit time and keep whichever fit explains more variance.
    # (Seen live: EVP 0.0 -> 59.0 on a real borehole.)
    ml, evp = _solve_with_alpha_rescue(
        lambda: _build_model(head, prec, evap, rfunc, recharge), train_max)

    resid = ml.residuals()
    sigma = float(resid.std())
    alpha = _fit_alpha(ml)
    if not alpha or not np.isfinite(alpha) or alpha <= 0:
        phi = float(pd.Series(resid).autocorr(lag=1))
        phi = min(max(phi, 1e-3), 0.999)
        alpha = -1.0 / np.log(phi)

    return {
        "station_id": station_id,
        "model_kind": "gw",
        "rfunc": rfunc,
        "recharge": recharge,
        "params": [float(x) for x in ml.parameters["optimal"].to_numpy()],
        "param_names": [str(x) for x in ml.parameters.index],
        "sigma": sigma,
        "alpha": alpha,
        "evp": evp,
        "noise_qa": _noise_qa(ml, resid, _norm(head)),
        "n_obs": int(_norm(head).dropna().shape[0]),
        "train_max": (None if train_max is None
                      else pd.Timestamp(train_max).date().isoformat()),
        "fitted_on": date.today().isoformat(),
        "precip_source": precip_source,
    }


def calibrate_flow(gauge_id: str, q: pd.Series, prec: pd.Series, evap: pd.Series,
                   *, train_max: pd.Timestamp | None = None,
                   rfunc: str = "Gamma", recharge: str = "FlexModel",
                   precip_source: str = "gauge") -> ModelRec:
    """Calibrate the two-pathway flow model (docs/product/lowflow/analysis.md
    §3) on RAW flow ``q`` (m3/s, the Stage-2 shards' Flow_m3s column) and
    return a ModelRec with ``model_kind="flow_2s"``.

    Clone of ``calibrate()`` (same degenerate-fit noise-alpha rescue, same
    provenance fields) plus a second stress: ``ps.StressModel(prec,
    rfunc=ps.Gamma(), name="quickflow")`` on raw rain alongside the existing
    FlexModel recharge stress (the exact construction transplanted from
    docs/product/lowflow/scripts/modelB_twopath.py — fast path steered with
    ``quickflow_a`` initial=2.0, pmax=15.0 so the solver doesn't let the two
    pathways swap roles).

    The model is fit on ``logq = np.log(Q + eps)``, not raw Q — a river's
    quickflow pathway only resolves cleanly on log-flow (analysis.md §2/§3).
    ``eps = max(0.001, Q[Q>0].min()/10)`` is stored on the returned rec so
    zero-flow days (winterbournes) round-trip through the same transform on
    reload, and so ``simulate_path`` can apply it to raw Q it's given later.
    """
    q_norm = _norm(q).dropna()
    positive = q_norm[q_norm > 0]
    eps = max(0.001, float(positive.min()) / 10) if len(positive) else 0.001
    logq = np.log(q_norm + eps)

    def _build():
        ml = _build_model(logq, prec, evap, rfunc, recharge, model_kind="flow_2s")
        # steer the fast (quickflow) path fast + the slow (recharge) path slow
        # so the solver doesn't let them swap roles (modelB_twopath.py).
        ml.set_parameter("quickflow_a", initial=2.0, pmax=15.0)
        ml.set_parameter("rch_A", initial=1.0)
        return ml

    ml, evp = _solve_with_alpha_rescue(_build, train_max)

    resid = ml.residuals()
    sigma = float(resid.std())
    alpha = _fit_alpha(ml)
    if not alpha or not np.isfinite(alpha) or alpha <= 0:
        phi = float(pd.Series(resid).autocorr(lag=1))
        phi = min(max(phi, 1e-3), 0.999)
        alpha = -1.0 / np.log(phi)

    return {
        "station_id": gauge_id,
        "model_kind": "flow_2s",
        "rfunc": rfunc,
        "recharge": recharge,
        "params": [float(x) for x in ml.parameters["optimal"].to_numpy()],
        "param_names": [str(x) for x in ml.parameters.index],
        "sigma": sigma,
        "alpha": alpha,
        "eps": eps,
        "evp": evp,
        "noise_qa": _noise_qa(ml, resid, logq),
        "n_obs": int(logq.shape[0]),
        "train_max": (None if train_max is None
                      else pd.Timestamp(train_max).date().isoformat()),
        "fitted_on": date.today().isoformat(),
        "precip_source": precip_source,
    }


def simulate_path(rec: ModelRec, head: pd.Series, prec: pd.Series,
                  evap: pd.Series, origin: pd.Timestamp,
                  target_dates: pd.DatetimeIndex,
                  noise_floor: bool = True
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Forecast GW (or, for a ``model_kind="flow_2s"`` rec, logQ) at arbitrary
    ``target_dates``, seeded at the observed ``origin`` level via AR1
    carry-forward of the origin residual.

    The deterministic Pastas simulation provides the trajectory shape from the
    (bridged) stresses; the origin residual r0 = obs(origin) − sim(origin) is
    carried forward with exp(−Δt/alpha) decay (Δt = calendar days from origin),
    anchoring the forecast to the last observation. Returns (mean[N], sigma[N])
    aligned to ``target_dates``.

    ``prec``/``evap`` are the *bridged* daily series (observed history + forecast
    scenario over the window); they need only span enough history for warmup
    through max(target_dates). Gaps are handled by the daily-grid reindex.

    ``head`` is always the caller's raw observed series (real head for "gw",
    raw flow Q m3/s for "flow_2s" — the same units passed to calibrate() /
    calibrate_flow()). For "flow_2s" it is log-transformed here with the rec's
    stored ``eps`` before the model is built/simulated and before the residual
    anchor is computed — Pastas was fit on logQ, and the rebuilt model + the
    AR1 band math below must operate on that same logQ (analysis.md §3/§4:
    "exponentiate only at publish", not here). Both stresses are rebuilt via
    ``_build_model``'s ``model_kind`` dispatch — nothing downstream of this
    point (the AR1 band, seeding, sigma_inflation) changes for flow.
    """
    origin = pd.Timestamp(origin).tz_localize(None).normalize()
    target = pd.DatetimeIndex(pd.to_datetime(target_dates)).tz_localize(None).normalize()
    model_kind = rec.get("model_kind", "gw")
    if model_kind == "flow_2s":
        eps = float(rec.get("eps", 0.001))
        series = np.log(_norm(head).dropna() + eps)
    else:
        series = head
    ml = _build_model(series, prec, evap, rec["rfunc"], rec["recharge"],
                      model_kind=model_kind)
    p = np.asarray(rec["params"], dtype=float)
    sim = _norm(ml.simulate(p=p, tmin=min(origin, target.min()), tmax=target.max()))

    r0 = float(_norm(series).get(origin, np.nan) - sim.get(origin, np.nan))
    if not np.isfinite(r0):
        r0 = 0.0
    k = (target - origin).days.to_numpy(dtype=float)
    decay = np.exp(-np.clip(k, 0, None) / _safe_alpha(rec.get("alpha")))
    mean = sim.reindex(target).to_numpy(float) + r0 * decay
    # sigma_inflation (default 1.0): the short-record band-widening factor
    # (src.forecast.pastas.screen) — a per-borehole multiplier calibrated so the
    # published band covers observations at the nominal rate. Applied HERE so the
    # SAME widened band drives both the gate hindcast (seeded_forecast) and the
    # production fan (ensemble.drive_borehole → gw_sigma). Absent on full-record
    # models → 1.0 → unchanged.
    infl = float(rec.get("sigma_inflation", 1.0) or 1.0)
    sig = infl * float(rec["sigma"]) * np.sqrt(np.clip(1.0 - decay ** 2, 1e-6, None))
    # Day-1 noise floor (GW only — see the _NOISE_FLOOR_* block above): a
    # per-station daily-innovation sd added in quadrature wherever the target
    # is AFTER the origin (k > 0). The origin day itself is the observation —
    # it stays exact. Self-fading: negligible once the AR1 band has grown.
    if noise_floor and model_kind == "gw":
        nf = min(_NOISE_FLOOR_K * daily_innovation_sigma(series, origin),
                 _NOISE_FLOOR_CAP_M)
        if nf > 0.0:
            sig = np.sqrt(sig ** 2 + np.where(k > 0, nf, 0.0) ** 2)
    return mean, sig


def seeded_forecast(rec: ModelRec, head: pd.Series, prec: pd.Series,
                    evap: pd.Series, origin: pd.Timestamp, horizon: int
                    ) -> tuple[pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """Convenience wrapper: forecast the ``horizon`` days immediately after
    ``origin``. Returns (window dates, mean[H], sigma[H])."""
    origin = pd.Timestamp(origin).tz_localize(None).normalize()
    window = pd.date_range(origin + pd.Timedelta(days=1), periods=horizon, freq="D")
    mean, sig = simulate_path(rec, head, prec, evap, origin, window)
    return window, mean, sig


def save_models(recs: dict[str, ModelRec], path: str | Path) -> Path:
    """Persist {station_id: ModelRec} to JSON (params are small lists)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recs, indent=2), encoding="utf-8")
    return path


def load_models(path: str | Path) -> dict[str, ModelRec]:
    path = Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

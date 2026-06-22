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
#   params, param_names                  — calibrated optimal parameters
#   sigma                                — residual std (m), for the predictive band
#   alpha                                — AR1 decay (days), for residual carry-forward
#   evp, n_obs, train_max, fitted_on     — provenance / fit quality
ModelRec = dict


def _norm(s: pd.Series) -> pd.Series:
    s = s.copy()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
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
                 rfunc: str, recharge: str):
    """Construct an (unsolved) pastas Model with the standard recharge stress +
    AR1 noise. Lazy pastas import. Used by both calibrate() and forecasting."""
    import pastas as ps
    rfunc_obj = getattr(ps, rfunc)()
    recharge_obj = getattr(ps.rch, recharge)()
    ml = ps.Model(_norm(head).dropna(), name="bh")
    ml.add_stressmodel(ps.RechargeModel(_daily(prec, fill="zero"),
                                        _daily(evap, fill="ffill"),
                                        rfunc=rfunc_obj, name="rch",
                                        recharge=recharge_obj))
    try:
        ml.add_noisemodel(ps.ArNoiseModel())
    except Exception:
        pass
    return ml


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
              rfunc: str = "Gamma", recharge: str = "FlexModel") -> ModelRec:
    """Calibrate a Pastas TFN on [start, train_max] and return a ModelRec.

    train_max=None calibrates on all available head (the production default — use
    every observation). Pass a cutoff only for leakage-safe evaluation.
    """
    ml = _build_model(head, prec, evap, rfunc, recharge)
    ml.solve(tmax=train_max, report=False)

    resid = ml.residuals()
    sigma = float(resid.std())
    alpha = None
    for name in ml.parameters.index:
        if "alpha" in name.lower():
            alpha = float(ml.parameters.loc[name, "optimal"])
            break
    if not alpha or not np.isfinite(alpha) or alpha <= 0:
        phi = float(pd.Series(resid).autocorr(lag=1))
        phi = min(max(phi, 1e-3), 0.999)
        alpha = -1.0 / np.log(phi)

    try:
        evp = float(ml.stats.evp())
    except Exception:
        evp = float("nan")

    return {
        "station_id": station_id,
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
    }


def simulate_path(rec: ModelRec, head: pd.Series, prec: pd.Series,
                  evap: pd.Series, origin: pd.Timestamp,
                  target_dates: pd.DatetimeIndex
                  ) -> tuple[np.ndarray, np.ndarray]:
    """Forecast GW at arbitrary ``target_dates``, seeded at the observed
    ``origin`` level via AR1 carry-forward of the origin residual.

    The deterministic Pastas simulation provides the trajectory shape from the
    (bridged) stresses; the origin residual r0 = obs(origin) − sim(origin) is
    carried forward with exp(−Δt/alpha) decay (Δt = calendar days from origin),
    anchoring the forecast to the last observation. Returns (mean[N], sigma[N])
    aligned to ``target_dates``.

    ``prec``/``evap`` are the *bridged* daily series (observed history + forecast
    scenario over the window); they need only span enough history for warmup
    through max(target_dates). Gaps are handled by the daily-grid reindex.
    """
    origin = pd.Timestamp(origin).tz_localize(None).normalize()
    target = pd.DatetimeIndex(pd.to_datetime(target_dates)).tz_localize(None).normalize()
    ml = _build_model(head, prec, evap, rec["rfunc"], rec["recharge"])
    p = np.asarray(rec["params"], dtype=float)
    sim = _norm(ml.simulate(p=p, tmin=min(origin, target.min()), tmax=target.max()))

    r0 = float(_norm(head).get(origin, np.nan) - sim.get(origin, np.nan))
    if not np.isfinite(r0):
        r0 = 0.0
    k = (target - origin).days.to_numpy(dtype=float)
    decay = np.exp(-np.clip(k, 0, None) / _safe_alpha(rec.get("alpha")))
    mean = sim.reindex(target).to_numpy(float) + r0 * decay
    sig = float(rec["sigma"]) * np.sqrt(np.clip(1.0 - decay ** 2, 1e-6, None))
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

"""GW forward-roll methods for the probabilistic ensemble (design §6).

Two candidate ways to project groundwater forward day-by-day given a recharge
(and exogenous) trajectory. The choice between them is resolved empirically by
the perfect-forecast hindcast in ``hindcast.py``, not asserted here.

A — recursive RandomForest stepping (``roll_recursive_rf``)
    Drives the trained delta model (predicts dGW = GW − GW_Lag1) one day at a
    time, feeding reconstructed GW back in via GW_Lag7. Guardrails clip each
    step's dGW and the cumulative GW to per-station historical ranges to
    contain RandomForest out-of-distribution drift.

B — reduced-form recharge→GW response (``fit_reduced_form`` / ``roll_reduced_form``)
    A per-station linear model dGW = a + b·GW_prev + c·Recharge + seasonal,
    fit by OLS on history and rolled forward analytically. Stable and
    interpretable; no extrapolation pathology.

Both take the *observed* exogenous trajectory in a hindcast (perfect-forecast),
so they isolate GW-roll skill from weather error.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_REDUCED_FORM_COLS = ["const", "GW_prev", "Recharge_Weibull", "Sin_DOY", "Cos_DOY"]

# AR / momentum variant (design Phase 2.5): adds dGW_prev (carries an ongoing
# rise forward) and Recharge² (convex response — steeper at high recharge), to
# fix the reduced-form's under-response during active winter recharge.
_REDUCED_FORM_AR_COLS = ["const", "GW_prev", "Recharge_Weibull", "dGW_prev",
                         "Recharge_sq", "Sin_DOY", "Cos_DOY"]

# Conditional-recession variant (design Phase 2.6): the recession acts on the
# centred level u = GW_prev − mean(GW), but is *gated by recharge* via the
# interaction u·Recharge. Effective recession on u = (b + g·Recharge); with
# b<0, g>0 the recession is suppressed when recharge is high, so active winter
# recharge rises unchecked instead of being damped by the level term.
_REDUCED_FORM_CR_COLS = ["const", "u", "u_x_R", "Recharge_Weibull", "dGW_prev",
                         "Recharge_sq", "Sin_DOY", "Cos_DOY"]


# ---------------------------------------------------------------------------
# B — reduced-form
# ---------------------------------------------------------------------------

def fit_reduced_form(hist: pd.DataFrame) -> dict:
    """OLS fit of dGW on [1, GW_Lag1, Recharge_Weibull, Sin_DOY, Cos_DOY].

    `hist` needs columns GW_Level, GW_Lag1, Recharge_Weibull, Sin_DOY, Cos_DOY.
    Returns {"coef": np.ndarray, "cols": [...]}. b (the GW_prev coefficient) is
    the recession term; it should come out slightly negative (mean reversion).
    """
    d = hist.dropna(subset=["GW_Level", "GW_Lag1", "Recharge_Weibull",
                            "Sin_DOY", "Cos_DOY"])
    if len(d) < 30:
        # Too little history — fall back to pure persistence (dGW = 0).
        return {"coef": np.zeros(len(_REDUCED_FORM_COLS)), "cols": _REDUCED_FORM_COLS}
    y = (d["GW_Level"] - d["GW_Lag1"]).to_numpy(dtype=float)
    X = np.column_stack([
        np.ones(len(d)),
        d["GW_Lag1"].to_numpy(dtype=float),
        d["Recharge_Weibull"].to_numpy(dtype=float),
        d["Sin_DOY"].to_numpy(dtype=float),
        d["Cos_DOY"].to_numpy(dtype=float),
    ])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {"coef": coef, "cols": _REDUCED_FORM_COLS}


def roll_reduced_form(seed_gw: float, exog_future: pd.DataFrame,
                      params: dict, *, gw_clip: tuple[float, float] | None = None) -> np.ndarray:
    """Roll GW forward. `exog_future` rows (one per forecast day) need
    Recharge_Weibull, Sin_DOY, Cos_DOY. Returns the GW level per forecast day."""
    coef = params["coef"]
    gw = float(seed_gw)
    out = []
    for _, r in exog_future.iterrows():
        x = np.array([1.0, gw, float(r["Recharge_Weibull"]),
                      float(r["Sin_DOY"]), float(r["Cos_DOY"])])
        gw = gw + float(x @ coef)
        if gw_clip is not None:
            gw = min(max(gw, gw_clip[0]), gw_clip[1])
        out.append(gw)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# B2 — reduced-form with momentum (AR) + convex recharge
# ---------------------------------------------------------------------------

def fit_reduced_form_ar(hist: pd.DataFrame) -> dict:
    """OLS fit of dGW on [1, GW_prev, Recharge, dGW_prev, Recharge², Sin, Cos].

    The dGW_prev term gives the roll momentum (so an ongoing rise is carried
    forward); Recharge² lets the response be convex (steeper in high-recharge
    winters), countering the single-line under-response. Linear in parameters,
    so still a plain OLS fit.
    """
    d = hist.dropna(subset=["GW_Level", "GW_Lag1", "Recharge_Weibull",
                            "Sin_DOY", "Cos_DOY"]).copy()
    d["dGW"] = d["GW_Level"] - d["GW_Lag1"]
    d["dGW_prev"] = d["dGW"].shift(1)
    d = d.dropna(subset=["dGW_prev"])
    if len(d) < 40:
        return {"coef": np.zeros(len(_REDUCED_FORM_AR_COLS)),
                "cols": _REDUCED_FORM_AR_COLS}
    R = d["Recharge_Weibull"].to_numpy(dtype=float)
    X = np.column_stack([
        np.ones(len(d)), d["GW_Lag1"].to_numpy(float), R,
        d["dGW_prev"].to_numpy(float), R * R,
        d["Sin_DOY"].to_numpy(float), d["Cos_DOY"].to_numpy(float),
    ])
    y = d["dGW"].to_numpy(dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {"coef": coef, "cols": _REDUCED_FORM_AR_COLS}


def roll_reduced_form_ar(seed_gw: float, seed_dgw: float, exog_future: pd.DataFrame,
                         params: dict, *, dgw_clip: tuple[float, float],
                         gw_clip: tuple[float, float]) -> np.ndarray:
    """Roll GW forward with the AR/convex reduced-form. `exog_future` rows need
    Recharge_Weibull, Sin_DOY, Cos_DOY. Per-step dGW and cumulative GW are
    clipped to the per-station guardrail ranges to keep the AR term stable."""
    coef = params["coef"]
    gw = float(seed_gw)
    dgw_prev = float(seed_dgw)
    out = []
    for _, r in exog_future.iterrows():
        R = float(r["Recharge_Weibull"])
        x = np.array([1.0, gw, R, dgw_prev, R * R,
                      float(r["Sin_DOY"]), float(r["Cos_DOY"])])
        dgw = float(x @ coef)
        dgw = min(max(dgw, dgw_clip[0]), dgw_clip[1])
        gw = min(max(gw + dgw, gw_clip[0]), gw_clip[1])
        dgw_prev = dgw
        out.append(gw)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# B3 — reduced-form with conditional (recharge-gated) recession
# ---------------------------------------------------------------------------

def fit_reduced_form_cr(hist: pd.DataFrame) -> dict:
    """OLS fit of dGW on [1, u, u·R, R, dGW_prev, R², Sin, Cos] where
    u = GW_prev − mean(GW). The u·R interaction lets the recession be gated by
    recharge (see _REDUCED_FORM_CR_COLS). Returns params incl. ``gw_center``."""
    d = hist.dropna(subset=["GW_Level", "GW_Lag1", "Recharge_Weibull",
                            "Sin_DOY", "Cos_DOY"]).copy()
    d["dGW"] = d["GW_Level"] - d["GW_Lag1"]
    d["dGW_prev"] = d["dGW"].shift(1)
    d = d.dropna(subset=["dGW_prev"])
    gw_center = float(d["GW_Lag1"].mean()) if len(d) else 0.0
    if len(d) < 50:
        return {"coef": np.zeros(len(_REDUCED_FORM_CR_COLS)),
                "cols": _REDUCED_FORM_CR_COLS, "gw_center": gw_center}
    u = d["GW_Lag1"].to_numpy(float) - gw_center
    R = d["Recharge_Weibull"].to_numpy(float)
    X = np.column_stack([
        np.ones(len(d)), u, u * R, R, d["dGW_prev"].to_numpy(float), R * R,
        d["Sin_DOY"].to_numpy(float), d["Cos_DOY"].to_numpy(float),
    ])
    y = d["dGW"].to_numpy(dtype=float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return {"coef": coef, "cols": _REDUCED_FORM_CR_COLS, "gw_center": gw_center}


def roll_reduced_form_cr(seed_gw: float, seed_dgw: float, exog_future: pd.DataFrame,
                         params: dict, *, dgw_clip: tuple[float, float],
                         gw_clip: tuple[float, float]) -> np.ndarray:
    """Roll GW forward with the conditional-recession reduced-form. Same exog
    needs as the AR roll; recession is gated by recharge through u·R."""
    coef = params["coef"]
    gc = float(params.get("gw_center", 0.0))
    gw = float(seed_gw)
    dgw_prev = float(seed_dgw)
    out = []
    for _, r in exog_future.iterrows():
        R = float(r["Recharge_Weibull"])
        u = gw - gc
        x = np.array([1.0, u, u * R, R, dgw_prev, R * R,
                      float(r["Sin_DOY"]), float(r["Cos_DOY"])])
        dgw = float(x @ coef)
        dgw = min(max(dgw, dgw_clip[0]), dgw_clip[1])
        gw = min(max(gw + dgw, gw_clip[0]), gw_clip[1])
        dgw_prev = dgw
        out.append(gw)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# A — recursive RandomForest
# ---------------------------------------------------------------------------

def roll_recursive_rf(model, feature_cols: list[str], seed_gw: float,
                      exog_future: pd.DataFrame, *,
                      dgw_clip: tuple[float, float],
                      gw_clip: tuple[float, float]) -> np.ndarray:
    """Recursively step the delta model.

    `exog_future` is a DatetimeIndexed frame of the *observed* feature rows for
    each forecast day (all `feature_cols` present, incl. GW_Lag7/GW_Lag30).
    GW_Lag7 is overwritten with the reconstructed GW once the lag date falls
    inside the forecast window. dGW and cumulative GW are clipped to the given
    per-station ranges (guardrails).
    """
    reconstructed: dict[pd.Timestamp, float] = {}
    gw = float(seed_gw)
    out = []
    for dt, row in exog_future.iterrows():
        feat = row[feature_cols].to_dict()
        lag7_date = pd.Timestamp(dt) - pd.Timedelta(days=7)
        if lag7_date in reconstructed:
            feat["GW_Lag7"] = reconstructed[lag7_date]
        # Pass a named-column frame so sklearn doesn't warn about feature names.
        X = pd.DataFrame([[feat[c] for c in feature_cols]], columns=feature_cols)
        dgw = float(model.predict(X)[0])
        dgw = min(max(dgw, dgw_clip[0]), dgw_clip[1])
        gw = min(max(gw + dgw, gw_clip[0]), gw_clip[1])
        reconstructed[pd.Timestamp(dt)] = gw
        out.append(gw)
    return np.asarray(out, dtype=float)


# ---------------------------------------------------------------------------
# Guardrail ranges + dispatcher
# ---------------------------------------------------------------------------

def station_guardrails(hist: pd.DataFrame, *, gw_margin_frac: float = 0.10
                       ) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (dgw_clip, gw_clip) from a station's history.

    dgw_clip = [P1, P99] of observed daily ΔGW.
    gw_clip  = [min, max] of observed GW ± gw_margin_frac of the range.
    """
    dgw = (hist["GW_Level"] - hist["GW_Lag1"]).dropna()
    if dgw.empty:
        dgw_clip = (-np.inf, np.inf)
    else:
        dgw_clip = (float(dgw.quantile(0.01)), float(dgw.quantile(0.99)))
    gw = hist["GW_Level"].dropna()
    if gw.empty:
        gw_clip = (-np.inf, np.inf)
    else:
        lo, hi = float(gw.min()), float(gw.max())
        m = (hi - lo) * gw_margin_frac
        gw_clip = (lo - m, hi + m)
    return dgw_clip, gw_clip


def fit(method: str, hist: pd.DataFrame) -> dict:
    """Fit the params for a linear roll method ('reduced_form',
    'reduced_form_ar', 'reduced_form_cr'). 'rf_guarded' uses an external model
    and is not fit here."""
    if method == "reduced_form":
        return fit_reduced_form(hist)
    if method == "reduced_form_ar":
        return fit_reduced_form_ar(hist)
    if method == "reduced_form_cr":
        return fit_reduced_form_cr(hist)
    raise ValueError(f"no linear fit for method: {method!r}")


def roll(method: str, **kwargs) -> np.ndarray:
    """Dispatch a roll: 'reduced_form', 'reduced_form_ar', 'reduced_form_cr',
    'rf_guarded'."""
    if method == "reduced_form":
        return roll_reduced_form(kwargs["seed_gw"], kwargs["exog_future"],
                                 kwargs["params"], gw_clip=kwargs.get("gw_clip"))
    if method == "reduced_form_ar":
        return roll_reduced_form_ar(
            kwargs["seed_gw"], kwargs["seed_dgw"], kwargs["exog_future"],
            kwargs["params"], dgw_clip=kwargs["dgw_clip"], gw_clip=kwargs["gw_clip"])
    if method == "reduced_form_cr":
        return roll_reduced_form_cr(
            kwargs["seed_gw"], kwargs["seed_dgw"], kwargs["exog_future"],
            kwargs["params"], dgw_clip=kwargs["dgw_clip"], gw_clip=kwargs["gw_clip"])
    if method == "rf_guarded":
        return roll_recursive_rf(
            kwargs["model"], kwargs["feature_cols"], kwargs["seed_gw"],
            kwargs["exog_future"], dgw_clip=kwargs["dgw_clip"],
            gw_clip=kwargs["gw_clip"])
    raise ValueError(f"unknown roll method: {method!r}")

"""Unit tests for the GW-roll methods (design §6), on synthetic systems."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.forecast.ensemble import gw_roll


# ---------------------------------------------------------------------------
# Reduced-form
# ---------------------------------------------------------------------------

class TestReducedForm:
    def test_ols_recovers_known_coefficients(self):
        rng = np.random.RandomState(0)
        n = 600
        gw_lag1 = rng.uniform(40, 60, n)
        recharge = rng.uniform(0, 5, n)
        doy = rng.randint(1, 366, n)
        sin = np.sin(2 * np.pi * doy / 365.25)
        cos = np.cos(2 * np.pi * doy / 365.25)
        true = np.array([0.2, -0.01, 0.5, 0.3, -0.1])
        dgw = (true[0] + true[1] * gw_lag1 + true[2] * recharge
               + true[3] * sin + true[4] * cos)
        hist = pd.DataFrame({
            "GW_Lag1": gw_lag1, "GW_Level": gw_lag1 + dgw,
            "Recharge_Weibull": recharge, "Sin_DOY": sin, "Cos_DOY": cos,
        })
        params = gw_roll.fit_reduced_form(hist)
        assert np.allclose(params["coef"], true, atol=1e-6)

    def test_roll_applies_recharge_response(self):
        # coef = [const=0, GW_prev=0, Recharge=1, sin=0, cos=0] -> dGW = Recharge
        params = {"coef": np.array([0.0, 0.0, 1.0, 0.0, 0.0]),
                  "cols": gw_roll._REDUCED_FORM_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [1.0, 2.0, 3.0],
                             "Sin_DOY": [0, 0, 0], "Cos_DOY": [0, 0, 0]})
        out = gw_roll.roll_reduced_form(5.0, exog, params)
        assert np.allclose(out, [6.0, 8.0, 11.0])

    def test_roll_respects_gw_clip(self):
        params = {"coef": np.array([0.0, 0.0, 1.0, 0.0, 0.0]),
                  "cols": gw_roll._REDUCED_FORM_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [10.0, 10.0],
                             "Sin_DOY": [0, 0], "Cos_DOY": [0, 0]})
        out = gw_roll.roll_reduced_form(5.0, exog, params, gw_clip=(0.0, 12.0))
        assert out.max() <= 12.0

    def test_too_little_history_falls_back_to_persistence(self):
        hist = pd.DataFrame({"GW_Lag1": [50.0], "GW_Level": [50.1],
                             "Recharge_Weibull": [1.0], "Sin_DOY": [0.0],
                             "Cos_DOY": [1.0]})
        params = gw_roll.fit_reduced_form(hist)
        assert np.allclose(params["coef"], 0.0)  # dGW = 0 → persistence


# ---------------------------------------------------------------------------
# Reduced-form AR (momentum + convex recharge)
# ---------------------------------------------------------------------------

class TestReducedFormAR:
    def test_recovers_ar_coefficients(self):
        # Generate GW recursively from a known AR system, then refit.
        true = {"const": 0.05, "gw": -0.004, "R": 0.10, "dgw": 0.40,
                "Rsq": 0.002, "sin": 0.02, "cos": -0.01}
        rng = np.random.RandomState(0)
        n = 400
        R = rng.uniform(0, 4, n)
        doy = rng.randint(1, 366, n)
        sin = np.sin(2 * np.pi * doy / 365.25)
        cos = np.cos(2 * np.pi * doy / 365.25)
        gw = np.zeros(n)
        dgw = np.zeros(n)
        gw[0], dgw[0] = 50.0, 0.0
        for t in range(1, n):
            dgw[t] = (true["const"] + true["gw"] * gw[t - 1] + true["R"] * R[t]
                      + true["dgw"] * dgw[t - 1] + true["Rsq"] * R[t] ** 2
                      + true["sin"] * sin[t] + true["cos"] * cos[t])
            gw[t] = gw[t - 1] + dgw[t]
        hist = pd.DataFrame({"GW_Level": gw, "GW_Lag1": np.r_[gw[0], gw[:-1]],
                             "Recharge_Weibull": R, "Sin_DOY": sin, "Cos_DOY": cos})
        params = gw_roll.fit_reduced_form_ar(hist)
        # cols: const, GW_prev, Recharge, dGW_prev, Recharge_sq, Sin, Cos
        expected = np.array([true["const"], true["gw"], true["R"], true["dgw"],
                             true["Rsq"], true["sin"], true["cos"]])
        assert np.allclose(params["coef"], expected, atol=1e-5)

    def test_momentum_carries_rise_forward(self):
        # dGW = 0.8 * dGW_prev ; seed_dgw=1 -> decaying-but-positive increments
        coef = np.zeros(7); coef[3] = 0.8       # dGW_prev coefficient
        params = {"coef": coef, "cols": gw_roll._REDUCED_FORM_AR_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [0, 0, 0],
                             "Sin_DOY": [0, 0, 0], "Cos_DOY": [0, 0, 0]})
        out = gw_roll.roll_reduced_form_ar(10.0, 1.0, exog, params,
                                           dgw_clip=(-5, 5), gw_clip=(-100, 100))
        assert out[0] == pytest.approx(10.8)        # +0.8
        assert out[1] == pytest.approx(10.8 + 0.64)  # +0.8^2
        assert np.all(np.diff(out) > 0)             # keeps rising

    def test_ar_roll_clips(self):
        coef = np.zeros(7); coef[0] = 1e9           # explode via const
        params = {"coef": coef, "cols": gw_roll._REDUCED_FORM_AR_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [0, 0], "Sin_DOY": [0, 0],
                             "Cos_DOY": [0, 0]})
        out = gw_roll.roll_reduced_form_ar(10.0, 0.0, exog, params,
                                           dgw_clip=(-2, 2), gw_clip=(0, 13))
        assert out[0] == pytest.approx(12.0)
        assert out.max() <= 13.0

    def test_dispatch_reduced_form_ar(self):
        coef = np.zeros(7); coef[3] = 0.5
        params = {"coef": coef, "cols": gw_roll._REDUCED_FORM_AR_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [0.0], "Sin_DOY": [0.0],
                             "Cos_DOY": [0.0]})
        out = gw_roll.roll("reduced_form_ar", seed_gw=5.0, seed_dgw=2.0,
                           exog_future=exog, params=params,
                           dgw_clip=(-9, 9), gw_clip=(-99, 99))
        assert out[0] == pytest.approx(6.0)         # 5 + 0.5*2


# ---------------------------------------------------------------------------
# Reduced-form CR (conditional / recharge-gated recession) — tested baseline
# (rejected by the hindcast, but kept for reproducibility)
# ---------------------------------------------------------------------------

class TestReducedFormCR:
    def test_fit_shape_and_center(self):
        rng = np.random.RandomState(0)
        n = 120
        gw = 50 + np.cumsum(rng.normal(0, 0.2, n))
        hist = pd.DataFrame({
            "GW_Level": gw, "GW_Lag1": np.r_[gw[0], gw[:-1]],
            "Recharge_Weibull": rng.uniform(0, 4, n),
            "Sin_DOY": rng.uniform(-1, 1, n), "Cos_DOY": rng.uniform(-1, 1, n),
        })
        params = gw_roll.fit_reduced_form_cr(hist)
        assert len(params["coef"]) == 8
        assert "gw_center" in params and np.isfinite(params["gw_center"])

    def test_recharge_suppresses_recession(self):
        # coef: const=0, u=-0.1 (recession), u_x_R=+0.05 (gate), rest 0.
        coef = np.zeros(8); coef[1] = -0.1; coef[2] = 0.05
        params = {"coef": coef, "cols": gw_roll._REDUCED_FORM_CR_COLS, "gw_center": 0.0}
        dry = gw_roll.roll_reduced_form_cr(
            10.0, 0.0, pd.DataFrame({"Recharge_Weibull": [0.0], "Sin_DOY": [0],
            "Cos_DOY": [0]}), params, dgw_clip=(-9, 9), gw_clip=(-99, 99))
        wet = gw_roll.roll_reduced_form_cr(
            10.0, 0.0, pd.DataFrame({"Recharge_Weibull": [5.0], "Sin_DOY": [0],
            "Cos_DOY": [0]}), params, dgw_clip=(-9, 9), gw_clip=(-99, 99))
        assert dry[0] < 10.0 < wet[0]      # dry recesses; wet suppresses recession

    def test_dispatch_and_fit_helper(self):
        coef = np.zeros(8); coef[3] = 1.0   # dGW = Recharge
        params = {"coef": coef, "cols": gw_roll._REDUCED_FORM_CR_COLS, "gw_center": 0.0}
        out = gw_roll.roll("reduced_form_cr", seed_gw=5.0, seed_dgw=0.0,
                           exog_future=pd.DataFrame({"Recharge_Weibull": [2.0],
                           "Sin_DOY": [0.0], "Cos_DOY": [0.0]}),
                           params=params, dgw_clip=(-9, 9), gw_clip=(-99, 99))
        assert out[0] == pytest.approx(7.0)

    def test_fit_helper_routes_methods(self):
        hist = pd.DataFrame({
            "GW_Level": np.linspace(50, 55, 80),
            "GW_Lag1": np.r_[50.0, np.linspace(50, 55, 80)[:-1]],
            "Recharge_Weibull": np.linspace(0, 4, 80),
            "Sin_DOY": np.zeros(80), "Cos_DOY": np.ones(80)})
        assert len(gw_roll.fit("reduced_form", hist)["coef"]) == 5
        assert len(gw_roll.fit("reduced_form_ar", hist)["coef"]) == 7
        assert len(gw_roll.fit("reduced_form_cr", hist)["coef"]) == 8
        with pytest.raises(ValueError):
            gw_roll.fit("rf_guarded", hist)


# ---------------------------------------------------------------------------
# Recursive RF
# ---------------------------------------------------------------------------

class _ConstModel:
    def __init__(self, value):
        self.value = value
    def predict(self, X):
        return np.array([self.value] * len(np.asarray(X)))


class _Lag7Model:
    """Returns the GW_Lag7 feature as the predicted dGW — lets us observe
    whether GW_Lag7 was reconstructed."""
    def __init__(self, feature_cols):
        self.j = feature_cols.index("GW_Lag7")
    def predict(self, X):
        arr = np.asarray(X)
        return np.array([row[self.j] for row in arr])


def _exog(days, cols, gw_lag7=0.0):
    idx = pd.date_range("2026-01-01", periods=days, freq="D")
    data = {c: np.zeros(days) for c in cols}
    if "GW_Lag7" in data:
        data["GW_Lag7"] = np.full(days, gw_lag7)
    return pd.DataFrame(data, index=idx)


class TestRecursiveRF:
    def test_constant_model_increments(self):
        cols = ["GW_Lag7", "Recharge_Weibull"]
        exog = _exog(5, cols)
        out = gw_roll.roll_recursive_rf(
            _ConstModel(0.5), cols, seed_gw=10.0, exog_future=exog,
            dgw_clip=(-10, 10), gw_clip=(-100, 100))
        assert np.allclose(out, [10.5, 11.0, 11.5, 12.0, 12.5])

    def test_dgw_and_gw_clip(self):
        cols = ["GW_Lag7", "Recharge_Weibull"]
        exog = _exog(3, cols)
        out = gw_roll.roll_recursive_rf(
            _ConstModel(1e9), cols, seed_gw=10.0, exog_future=exog,
            dgw_clip=(-2.0, 2.0), gw_clip=(0.0, 13.0))
        # +2/step clipped, then cumulative clipped at 13
        assert out[0] == pytest.approx(12.0)
        assert out.max() <= 13.0

    def test_gw_lag7_is_reconstructed_after_7_days(self):
        cols = ["GW_Lag7", "Recharge_Weibull"]
        exog = _exog(9, cols, gw_lag7=100.0)
        out = gw_roll.roll_recursive_rf(
            _Lag7Model(cols), cols, seed_gw=10.0, exog_future=exog,
            dgw_clip=(-1e9, 1e9), gw_clip=(-1e9, 1e9))
        # days 1..7 use observed GW_Lag7=100 -> +100/day
        inc_d7 = out[6] - out[5]
        # day 8 (index 7): GW_Lag7 reconstructed from day1 (= 10+100=110), not 100
        inc_d8 = out[7] - out[6]
        assert inc_d7 == pytest.approx(100.0)
        assert inc_d8 == pytest.approx(110.0)
        assert inc_d8 != pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Guardrails + dispatcher
# ---------------------------------------------------------------------------

class TestGuardrailsAndDispatch:
    def test_guardrails_ranges(self):
        rng = np.random.RandomState(1)
        gw = 50 + np.cumsum(rng.normal(0, 0.2, 300))
        hist = pd.DataFrame({"GW_Level": gw, "GW_Lag1": np.r_[gw[0], gw[:-1]]})
        dgw_clip, gw_clip = gw_roll.station_guardrails(hist, gw_margin_frac=0.1)
        assert dgw_clip[0] < dgw_clip[1]                       # varied ΔGW
        assert gw_clip[0] < gw.min() and gw_clip[1] > gw.max() # margin applied

    def test_dispatch_unknown_raises(self):
        with pytest.raises(ValueError):
            gw_roll.roll("nope")

    def test_dispatch_reduced_form(self):
        params = {"coef": np.array([0.0, 0.0, 1.0, 0.0, 0.0]),
                  "cols": gw_roll._REDUCED_FORM_COLS}
        exog = pd.DataFrame({"Recharge_Weibull": [1.0],
                             "Sin_DOY": [0.0], "Cos_DOY": [0.0]})
        out = gw_roll.roll("reduced_form", seed_gw=5.0, exog_future=exog,
                           params=params, gw_clip=None)
        assert out[0] == pytest.approx(6.0)

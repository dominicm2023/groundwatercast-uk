"""Offline tests for the self-computed ET0 module (src/data/et0.py, W5).

The live formula validation vs cached Open-Meteo ET0 lives in
scripts/validate_et0.py (r ≈ 0.992-0.994 at three chalk boreholes); these
tests pin the unit conversions and the input contract.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pyet")

from src.data.et0 import (  # noqa: E402
    ea_from_dewpoint,
    et0_fao56_daily,
    et0_hargreaves_daily,
    wind2_from_wind10,
)


def _met(days=10):
    idx = pd.date_range("2025-07-01", periods=days, freq="D")
    return pd.DataFrame({
        "tmean_c": 18.0, "tmax_c": 24.0, "tmin_c": 12.0,
        "dewpoint_c": 11.0, "wind10_ms": 3.0, "srad_mj": 22.0,
    }, index=idx)


class TestConversions:
    def test_ea_from_dewpoint_fao_reference(self):
        # FAO-56: e0(15 degC) = 1.705 kPa (table 2.3 class value)
        assert ea_from_dewpoint(pd.Series([15.0])).iloc[0] == pytest.approx(
            1.705, abs=0.01)

    def test_wind_profile_factor(self):
        # FAO-56 eq. 47 at z=10 m -> factor ~0.748
        assert wind2_from_wind10(pd.Series([1.0])).iloc[0] == pytest.approx(
            0.748, abs=0.002)


class TestEt0:
    def test_summer_day_plausible(self):
        # A warm sunny UK July day: FAO-56 ET0 ~ 3-5.5 mm/day
        et0 = et0_fao56_daily(_met(), lat_deg=51.0, elevation_m=50.0)
        assert len(et0) == 10
        assert ((et0 > 2.5) & (et0 < 6.0)).all()

    def test_missing_column_raises(self):
        with pytest.raises(ValueError, match="missing columns"):
            et0_fao56_daily(_met().drop(columns=["srad_mj"]), lat_deg=51.0)

    def test_nan_inputs_give_nan(self):
        met = _met()
        met.loc[met.index[3], "tmean_c"] = np.nan
        et0 = et0_fao56_daily(met, lat_deg=51.0)
        assert np.isnan(et0.iloc[3]) and np.isfinite(et0.iloc[4])

    def test_hargreaves_cruder_but_same_ballpark(self):
        met = _met()
        pm = et0_fao56_daily(met, lat_deg=51.0)
        hg = et0_hargreaves_daily(met, lat_deg=51.0)
        # same order of magnitude on a clear summer day
        assert (hg > 0).all()
        assert abs(float(hg.mean()) - float(pm.mean())) < 2.5

    def test_more_radiation_more_et0(self):
        met_hi = _met()
        met_hi["srad_mj"] = 28.0
        assert (et0_fao56_daily(met_hi, 51.0).mean()
                > et0_fao56_daily(_met(), 51.0).mean())

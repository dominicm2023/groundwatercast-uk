"""Offline tests for the CDS ERA5 fetcher (src/data/cds_era5.py, W4).

No cdsapi, no network: the request shape is pinned as a pure dict, the
parsing/unit conversions run on synthetic xarray Datasets, and the cache
writers are exercised against tmp_path. The live transport is validated
once a CDS key exists (see docs/free_data_migration.md W4).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")

from src.data.cds_era5 import (  # noqa: E402
    UK_AREA,
    _merge_into_cache,
    build_request,
    met_frame_for_point,
    point_series,
    precip_mm_from_daily_mean,
    srad_mj_from_daily_mean,
)


def _box(values, var="tp", days=4, base="2020-01-01"):
    times = pd.date_range(base, periods=days, freq="D")
    lats = np.array([52.0, 51.75, 51.5])
    lons = np.array([-1.5, -1.25, -1.0])
    data = np.full((days, len(lats), len(lons)), np.nan)
    for d in range(days):
        data[d, :, :] = values[d]
    return xr.Dataset(
        {var: (("valid_time", "latitude", "longitude"), data)},
        coords={"valid_time": times, "latitude": lats, "longitude": lons})


class TestRequestShape:
    def test_build_request_pinned(self):
        req = build_request("total_precipitation", "daily_mean", [2020, 2021])
        assert req["product_type"] == "reanalysis"
        assert req["variable"] == ["total_precipitation"]
        assert req["year"] == ["2020", "2021"]
        assert req["month"] == [f"{m:02d}" for m in range(1, 13)]
        assert len(req["day"]) == 31
        assert req["daily_statistic"] == "daily_mean"
        assert req["time_zone"] == "utc+00:00"
        assert req["area"] == UK_AREA

    def test_uk_area_covers_england(self):
        n, w, s, e = UK_AREA
        assert s < 50.0 < n and s < 55.8 < n     # Lizard .. Berwick
        assert w < -5.7 < e and w < 1.8 < e      # Land's End .. Lowestoft


class TestParsing:
    def test_point_series_nearest(self):
        ds = _box([1.0, 2.0, 3.0, 4.0])
        s = point_series(ds, 51.8, -1.3)
        assert len(s) == 4 and s.iloc[2] == 3.0
        assert s.index[0] == pd.Timestamp("2020-01-01")

    def test_precip_units(self):
        # daily mean of hourly accumulations: 0.0005 m -> 12 mm/day
        s = pd.Series([0.0005], index=pd.DatetimeIndex(["2020-01-01"]))
        assert precip_mm_from_daily_mean(s).iloc[0] == pytest.approx(12.0)

    def test_srad_units(self):
        # 1e6 J/m2 hourly-mean -> 24 MJ/m2/day
        s = pd.Series([1e6], index=pd.DatetimeIndex(["2020-01-01"]))
        assert srad_mj_from_daily_mean(s).iloc[0] == pytest.approx(24.0)

    def test_met_frame_contract(self):
        k = 273.15
        fields = {
            "t2m_mean": _box([k + 10] * 4, "t2m"),
            "t2m_max": _box([k + 15] * 4, "t2m"),
            "t2m_min": _box([k + 5] * 4, "t2m"),
            "d2m_mean": _box([k + 7] * 4, "d2m"),
            "wind10_mean": _box([3.0] * 4, "ws10"),
            "ssrd_mean": _box([5e5] * 4, "ssrd"),
        }
        met = met_frame_for_point(fields, 51.75, -1.25)
        assert list(met.columns) == ["tmean_c", "tmax_c", "tmin_c",
                                     "dewpoint_c", "wind10_ms", "srad_mj"]
        assert met["tmean_c"].iloc[0] == pytest.approx(10.0)
        assert met["srad_mj"].iloc[0] == pytest.approx(12.0)


class TestCacheMerge:
    def test_merge_same_contract_as_era5_precip(self, tmp_path):
        path = tmp_path / "S1.csv"
        old = pd.Series([1.0, 2.0],
                        index=pd.DatetimeIndex(["2020-01-01", "2020-01-02"]))
        _merge_into_cache(path, old, "precip_mm")
        # overlapping fresh value wins; new dates extend
        fresh = pd.Series([9.0, 3.0],
                          index=pd.DatetimeIndex(["2020-01-02", "2020-01-03"]))
        _merge_into_cache(path, fresh, "precip_mm")
        df = pd.read_csv(path, parse_dates=["date"])
        assert list(df["precip_mm"]) == [1.0, 9.0, 3.0]
        # readable by the existing loader
        from src.data.era5_precip import load_station_precip
        s = load_station_precip("S1", cache_root=tmp_path)
        assert list(s.values) == [1.0, 9.0, 3.0]

    def test_nan_fresh_values_dropped(self, tmp_path):
        path = tmp_path / "S2.csv"
        fresh = pd.Series([1.0, np.nan],
                          index=pd.DatetimeIndex(["2020-01-01", "2020-01-02"]))
        _merge_into_cache(path, fresh, "et0_mm")
        df = pd.read_csv(path)
        assert len(df) == 1

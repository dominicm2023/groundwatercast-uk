"""
Unit tests for src/features/build.py.
No file I/O — all tests use inline DataFrames.
"""

import numpy as np
import pandas as pd
import pytest

from src.features.build import (
    _GROUP_CODES,
    apply_weibull_recharge,
    average_rainfall,
    clean_groundwater_series,
    compute_weibull_kernel,
    create_features,
    get_station_group,
    join_timeseries,
    resample_to_daily,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_daily(values: list, start: str = "2020-01-01") -> pd.DataFrame:
    """Daily UTC DatetimeIndex DataFrame with a 'value' column."""
    idx = pd.date_range(start, periods=len(values), freq="1D", tz="UTC")
    return pd.DataFrame({"value": values}, index=idx)


def _make_sub_daily(n_per_day: int = 4, n_days: int = 5,
                    start: str = "2020-01-01", agg_val: float = 1.0) -> pd.DataFrame:
    """Sub-daily UTC DatetimeIndex DataFrame; each reading = agg_val."""
    periods = n_per_day * n_days
    idx = pd.date_range(start, periods=periods, freq="6h", tz="UTC")
    return pd.DataFrame({"value": [agg_val] * periods}, index=idx)


def _joined_frame(n: int = 60) -> pd.DataFrame:
    """Minimal joined frame with GW_Level and Rainfall."""
    idx = pd.date_range("2020-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({
        "GW_Level":              np.linspace(10, 12, n),
        "Rainfall":              np.ones(n),
    }, index=idx)


# ---------------------------------------------------------------------------
# resample_to_daily
# ---------------------------------------------------------------------------

def test_resample_mean_aggregation():
    df = _make_sub_daily(n_per_day=4, n_days=3, agg_val=2.0)
    result = resample_to_daily(df, agg="mean")
    assert len(result) == 3
    assert (result["value"] == 2.0).all()


def test_resample_sum_aggregation():
    df = _make_sub_daily(n_per_day=4, n_days=3, agg_val=1.0)
    result = resample_to_daily(df, agg="sum")
    assert len(result) == 3
    assert (result["value"] == 4.0).all()


def test_resample_drops_empty_days():
    # One day with data, one day gap (all NaN from min_count=1 when no data)
    idx = pd.date_range("2020-01-01", periods=4, freq="6h", tz="UTC")
    df = pd.DataFrame({"value": [1.0, 1.0, 1.0, 1.0]}, index=idx)
    result = resample_to_daily(df, agg="sum")
    # Only 2020-01-01 should appear
    assert len(result) == 1


# ---------------------------------------------------------------------------
# average_rainfall
# ---------------------------------------------------------------------------

def test_average_rainfall_three_stations():
    s1 = _make_daily([2.0, 4.0])
    s2 = _make_daily([4.0, 8.0])
    s3 = _make_daily([0.0, 0.0])
    result = average_rainfall([s1, s2, s3])
    assert result is not None
    assert list(result["value"]) == pytest.approx([2.0, 4.0])


def test_average_rainfall_with_none_station():
    s1 = _make_daily([3.0, 6.0])
    result = average_rainfall([s1, None, None])
    assert result is not None
    assert list(result["value"]) == pytest.approx([3.0, 6.0])


def test_average_rainfall_all_none_returns_none():
    assert average_rainfall([None, None, None]) is None


def test_average_rainfall_partial_overlap():
    s1 = _make_daily([1.0, 2.0], start="2020-01-01")
    s2 = _make_daily([3.0, 4.0], start="2020-01-02")
    result = average_rainfall([s1, s2])
    assert result is not None
    # 2020-01-01: only s1 → 1.0; 2020-01-02: both → mean(2,3)=2.5; 2020-01-03: only s2 → 4.0
    vals = result["value"].tolist()
    assert vals[0] == pytest.approx(1.0)
    assert vals[1] == pytest.approx(2.5)
    assert vals[2] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# join_timeseries
# ---------------------------------------------------------------------------

def test_join_left_anchored_on_gw_dates():
    gw   = _make_daily([10.0, 11.0, 12.0], start="2020-01-01")
    # Rain starts one day before GW (extra day) and covers all 3 GW dates
    rain = _make_daily([0.5, 1.0, 1.0, 1.0], start="2019-12-31")
    result = join_timeseries(gw, rain)
    assert result is not None
    assert len(result) == 3  # only GW dates kept; the extra Dec-31 rain date is excluded


def test_join_drops_rows_missing_rainfall():
    gw   = _make_daily([10.0, 11.0, 12.0], start="2020-01-01")
    # Rain only covers first 2 GW dates
    rain = _make_daily([1.0, 2.0], start="2020-01-01")
    result = join_timeseries(gw, rain)
    assert result is not None
    assert len(result) == 2


def test_join_returns_none_if_gw_missing():
    rain = _make_daily([1.0])
    assert join_timeseries(None, rain) is None


def test_join_returns_none_if_rainfall_missing():
    gw = _make_daily([10.0])
    assert join_timeseries(gw, None) is None


# ---------------------------------------------------------------------------
# create_features
# ---------------------------------------------------------------------------

def test_create_features_lag_columns_exist():
    df = _joined_frame(60)
    result = create_features(df)
    for col in ["GW_Lag1", "GW_Lag7", "GW_Lag30",
                "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
                "day_of_year", "Sin_DOY", "Cos_DOY"]:
        assert col in result.columns, f"Missing column: {col}"


def test_create_features_drops_initial_nan_rows():
    df = _joined_frame(60)
    result = create_features(df)
    # First 30 rows dropped (GW_Lag30 window)
    assert len(result) == 30


def test_create_features_no_nulls_in_required_cols():
    df = _joined_frame(60)
    result = create_features(df)
    required = ["GW_Lag1", "GW_Lag7", "GW_Lag30",
                "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum"]
    assert result[required].isna().sum().sum() == 0


def test_create_features_seasonality_bounds():
    df = _joined_frame(400)
    result = create_features(df)
    assert result["Sin_DOY"].between(-1, 1).all()
    assert result["Cos_DOY"].between(-1, 1).all()


def test_create_features_rolling_sums_non_negative():
    df = _joined_frame(60)
    result = create_features(df)
    assert (result["Rain_1d_sum"] >= 0).all()
    assert (result["Rain_3d_sum"] >= 0).all()
    assert (result["Rain_7d_sum"] >= 0).all()


# ---------------------------------------------------------------------------
# compute_weibull_kernel
# ---------------------------------------------------------------------------

def test_weibull_kernel_length():
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=60)
    assert len(kernel) == 60


def test_weibull_kernel_sums_to_one():
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=60)
    assert kernel.sum() == pytest.approx(1.0, abs=1e-9)


def test_weibull_kernel_all_positive():
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=60)
    assert (kernel > 0).all()


# ---------------------------------------------------------------------------
# apply_weibull_recharge
# ---------------------------------------------------------------------------

def test_weibull_recharge_first_rows_are_nan():
    rainfall = pd.Series(np.ones(100))
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=60)
    result = apply_weibull_recharge(rainfall, kernel)
    # First lag_days rows must be NaN (insufficient prior history)
    assert result.iloc[:60].isna().all()


def test_weibull_recharge_values_non_negative():
    rainfall = pd.Series(np.abs(np.random.default_rng(0).normal(2, 1, 200)))
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=60)
    result = apply_weibull_recharge(rainfall, kernel)
    valid = result.dropna()
    assert (valid >= 0).all()


# ---------------------------------------------------------------------------
# create_features — Weibull integration
# ---------------------------------------------------------------------------

_WEIBULL_CFG = {"enabled": True, "k": 1.5, "lambda": 15.0, "lag_days": 60}


def test_create_features_weibull_column_present():
    df = _joined_frame(n=120)  # needs > lag_days rows to survive dropna
    result = create_features(df, weibull_cfg=_WEIBULL_CFG)
    assert "Recharge_Weibull" in result.columns


def test_create_features_weibull_column_absent_when_disabled():
    df = _joined_frame(n=120)
    result = create_features(df, weibull_cfg={"enabled": False, "k": 1.5, "lambda": 15.0, "lag_days": 60})
    assert "Recharge_Weibull" not in result.columns


def test_create_features_weibull_drops_lag60_rows():
    # With lag_days=60, first 60 rows NaN; GW_Lag30 drops 30 → binding constraint is 60
    n = 120
    df = _joined_frame(n=n)
    result = create_features(df, weibull_cfg=_WEIBULL_CFG)
    assert len(result) == n - 60


# ---------------------------------------------------------------------------
# clean_groundwater_series
# ---------------------------------------------------------------------------

def _make_gw_with_spikes(n: int = 200, spike_indices: list | None = None,
                          spike_value: float = 500.0,
                          normal_value: float = 50.0) -> pd.DataFrame:
    """Sub-daily GW DataFrame with realistic seasonal variation plus optional spikes.

    Normal values oscillate ±1 m around normal_value (sine wave), giving a
    non-zero IQR so the IQR-fence code path is exercised.
    """
    idx = pd.date_range("2020-01-01", periods=n, freq="6h", tz="UTC")
    # Seasonal oscillation: period ~1 year, amplitude 1 m — guarantees IQR > 0
    t = np.arange(n)
    values = list(normal_value + np.sin(2 * np.pi * t / (365.25 * 4)))
    if spike_indices:
        for i in spike_indices:
            values[i] = spike_value
    return pd.DataFrame({"value": values}, index=idx)


def test_clean_gw_flags_spikes():
    df = _make_gw_with_spikes(n=200, spike_indices=[10, 50, 100], spike_value=500.0)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=5.0)
    assert n_flagged == 3
    assert cleaned["value"].isna().sum() == 3


def test_clean_gw_no_outliers_returns_unchanged():
    df = _make_gw_with_spikes(n=100, spike_indices=None)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=5.0)
    assert n_flagged == 0
    assert cleaned["value"].isna().sum() == 0


def test_clean_gw_negative_values_flagged():
    # Use a seasonal sweep 34–38 mAOD (IQR ≈ 1 m) plus two impossible negatives
    idx = pd.date_range("2020-01-01", periods=100, freq="1D", tz="UTC")
    t = np.linspace(0, 2 * np.pi, 100)
    vals = list(36.0 + 2 * np.sin(t))
    vals[50] = -120.0
    vals[51] = -119.0
    df = pd.DataFrame({"value": vals}, index=idx)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=20.0)
    assert n_flagged == 2
    assert cleaned["value"].dropna().min() > 0


def test_clean_gw_zero_iqr_returns_unchanged():
    # Completely flat series → IQR=0, should be returned as-is
    idx = pd.date_range("2020-01-01", periods=10, freq="1D", tz="UTC")
    df = pd.DataFrame({"value": [39.5] * 10}, index=idx)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=5.0)
    assert n_flagged == 0
    assert len(cleaned) == 10


def test_clean_gw_empty_df_returns_unchanged():
    idx = pd.DatetimeIndex([], tz="UTC")
    df = pd.DataFrame({"value": []}, index=idx)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=5.0)
    assert n_flagged == 0
    assert cleaned.empty


def test_clean_gw_preserves_normal_values():
    """After cleaning, non-spike values must be identical to the original."""
    df = _make_gw_with_spikes(n=200, spike_indices=[5], spike_value=300.0,
                               normal_value=50.0)
    cleaned, n_flagged = clean_groundwater_series(df, iqr_fence=5.0)
    assert n_flagged == 1
    # Non-spike rows in cleaned must equal original (only row 5 changed to NaN)
    mask_not_spike = ~df.index.isin([df.index[5]])
    pd.testing.assert_series_equal(
        cleaned.loc[mask_not_spike, "value"],
        df.loc[mask_not_spike, "value"],
    )


# ---------------------------------------------------------------------------
# get_station_group
# ---------------------------------------------------------------------------

class TestGetStationGroup:
    _LONS = {"east_st": 0.5, "boundary": -1.0, "west_st": -1.5}

    def test_east_of_split(self):
        assert get_station_group("east_st", self._LONS, lon_split=-1.0) == "east"

    def test_exactly_on_boundary_is_east(self):
        assert get_station_group("boundary", self._LONS, lon_split=-1.0) == "east"

    def test_west_of_split(self):
        assert get_station_group("west_st", self._LONS, lon_split=-1.0) == "west"

    def test_missing_station_is_unknown(self):
        assert get_station_group("no_such", self._LONS, lon_split=-1.0) == "unknown"

    def test_nan_longitude_is_unknown(self):
        lons = {"s1": float("nan")}
        assert get_station_group("s1", lons, lon_split=-1.0) == "unknown"

    def test_empty_dict_is_unknown(self):
        assert get_station_group("s1", {}, lon_split=-1.0) == "unknown"

    def test_custom_lon_split(self):
        lons = {"left": -0.6, "right": -0.4}
        assert get_station_group("right", lons, lon_split=-0.5) == "east"
        assert get_station_group("left",  lons, lon_split=-0.5) == "west"

    def test_group_codes_match_constant(self):
        """get_station_group output must be a key in _GROUP_CODES."""
        for result in ("east", "west", "unknown"):
            assert result in _GROUP_CODES


# ---------------------------------------------------------------------------
# create_features — multi-kernel Weibull integration
# ---------------------------------------------------------------------------

_WEIBULL_MULTI_CFG = {
    "enabled": True,
    "kernels": {
        "east": {"k": 1.3, "lambda": 10.0, "lag_days": 30},
        "west": {"k": 1.0, "lambda": 10.0, "lag_days": 30},
    },
    "add_masked": True,
    "add_pooled": False,
}


class TestCreateFeaturesMultiKernel:
    def test_masked_columns_present(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="east")
        assert "Recharge_East_masked" in result.columns
        assert "Recharge_West_masked" in result.columns
        assert "region_group_code" in result.columns

    def test_east_station_east_masked_nonzero(self):
        """East station: East masked column has recharge values; West is all zeros."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="east")
        assert (result["Recharge_East_masked"] > 0).any()
        assert (result["Recharge_West_masked"] == 0.0).all()

    def test_west_station_west_masked_nonzero(self):
        """West station: West masked column has recharge values; East is all zeros."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="west")
        assert (result["Recharge_West_masked"] > 0).any()
        assert (result["Recharge_East_masked"] == 0.0).all()

    def test_region_group_code_east(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="east")
        assert (result["region_group_code"] == _GROUP_CODES["east"]).all()

    def test_region_group_code_west(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="west")
        assert (result["region_group_code"] == _GROUP_CODES["west"]).all()

    def test_region_group_code_unknown(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="unknown")
        assert (result["region_group_code"] == _GROUP_CODES["unknown"]).all()

    def test_drops_active_kernel_lag_rows(self):
        """Binding lag constraint = max(GW_Lag30=30, active kernel lag_days=30) = 30."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="east")
        assert len(result) == 120 - 30

    def test_disabled_produces_no_masked_cols(self):
        disabled_cfg = dict(_WEIBULL_MULTI_CFG, enabled=False)
        df = _joined_frame(n=120)
        result = create_features(df, weibull_multi_cfg=disabled_cfg,
                                 region_group="east")
        assert "Recharge_East_masked" not in result.columns
        assert "region_group_code" not in result.columns

    def test_compatible_with_single_kernel_weibull(self):
        """Single-kernel and multi-kernel can be active simultaneously."""
        single_cfg = {"enabled": True, "k": 1.8, "lambda": 10.0, "lag_days": 45}
        df = _joined_frame(n=120)
        result = create_features(df, weibull_cfg=single_cfg,
                                 weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                 region_group="east")
        assert "Recharge_Weibull" in result.columns
        assert "Recharge_East_masked" in result.columns
        # Binding lag = max(30 GW_Lag, 45 pooled Weibull) = 45
        assert len(result) == 120 - 45


# ---------------------------------------------------------------------------
# create_features — weibull_by_group (single column, kernel per group)
# ---------------------------------------------------------------------------

_WEIBULL_BY_GROUP_CFG = {
    "enabled": True,
    "east": {"k": 1.3, "lambda": 10.0, "lag_days": 30},
    "west": {"k": 1.0, "lambda": 10.0, "lag_days": 30},
}


class TestCreateFeaturesWeibullByGroup:
    def test_east_produces_recharge_weibull(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="east")
        assert "Recharge_Weibull" in result.columns
        assert (result["Recharge_Weibull"] > 0).any()

    def test_west_produces_recharge_weibull(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="west")
        assert "Recharge_Weibull" in result.columns
        assert (result["Recharge_Weibull"] > 0).any()

    def test_no_masked_columns_produced(self):
        """By-group mode must NOT produce masked columns."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="east")
        assert "Recharge_East_masked" not in result.columns
        assert "Recharge_West_masked" not in result.columns

    def test_region_group_code_present(self):
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="west")
        assert "region_group_code" in result.columns

    def test_unknown_group_uses_fallback_kernel(self):
        """Stations with unknown group must still get a Recharge_Weibull column."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="unknown")
        assert "Recharge_Weibull" in result.columns
        assert (result["Recharge_Weibull"] > 0).any()

    def test_east_west_produce_different_values(self):
        """Different kernels must produce distinct recharge estimates on variable rainfall."""
        # Constant rainfall gives identical weighted sums for any normalised kernel,
        # so use a varying signal to make kernel shape matter.
        idx = pd.date_range("2020-01-01", periods=120, freq="1D", tz="UTC")
        df_varied = pd.DataFrame({
            "GW_Level":               np.linspace(10, 12, 120),
            "Rainfall":               np.abs(np.sin(np.arange(120) * 0.3)) * 5,
        }, index=idx)
        east = create_features(df_varied.copy(), weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                               region_group="east")
        west = create_features(df_varied.copy(), weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                               region_group="west")
        # k=1.3 vs k=1.0 gives different kernel shapes → different weighted sums
        assert not np.allclose(
            east["Recharge_Weibull"].values,
            west["Recharge_Weibull"].values,
        )

    def test_drops_active_lag_rows(self):
        """Binding lag = max(GW_Lag30=30, east lag_days=30) = 30 rows dropped."""
        df = _joined_frame(n=120)
        result = create_features(df, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                 region_group="east")
        assert len(result) == 120 - 30

    def test_disabled_produces_no_recharge_weibull(self):
        disabled = dict(_WEIBULL_BY_GROUP_CFG, enabled=False)
        df = _joined_frame(n=60)
        result = create_features(df, weibull_by_group_cfg=disabled, region_group="east")
        assert "Recharge_Weibull" not in result.columns

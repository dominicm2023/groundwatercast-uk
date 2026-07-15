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
    reindex_to_calendar,
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


# ---------------------------------------------------------------------------
# reindex_to_calendar
# ---------------------------------------------------------------------------

def test_reindex_to_calendar_no_gap_is_noop():
    """A fully contiguous daily index is unchanged by the calendar reindex."""
    df = _joined_frame(30)
    result = reindex_to_calendar(df)
    pd.testing.assert_index_equal(result.index, df.index)
    pd.testing.assert_frame_equal(result, df)


def test_reindex_to_calendar_fills_gap_with_nan():
    idx = pd.date_range("2020-01-01", periods=10, freq="1D", tz="UTC")
    keep = idx.delete([4, 5])  # remove two consecutive days -> a gap
    df = pd.DataFrame({"GW_Level": np.arange(len(keep), dtype=float)}, index=keep)
    result = reindex_to_calendar(df)
    assert len(result) == 10
    assert result.index.equals(idx)
    assert result.loc[idx[4], "GW_Level"].__class__ is float or pd.isna(result.loc[idx[4], "GW_Level"])
    assert pd.isna(result.loc[idx[4], "GW_Level"])
    assert pd.isna(result.loc[idx[5], "GW_Level"])


def test_reindex_to_calendar_preserves_tz():
    idx = pd.date_range("2020-01-01", periods=5, freq="1D", tz="UTC")
    df = pd.DataFrame({"v": range(5)}, index=idx)
    result = reindex_to_calendar(df)
    assert result.index.tz is not None
    assert str(result.index.tz) == "UTC"


def test_reindex_to_calendar_empty_input_returns_empty():
    df = pd.DataFrame({"v": []}, index=pd.DatetimeIndex([], tz="UTC"))
    result = reindex_to_calendar(df)
    assert result.empty


# ---------------------------------------------------------------------------
# create_features — calendar-true equivalence guarantee (BUGS.md: lag /
# rolling / Weibull features computed positionally over a gap-collapsed
# daily index)
# ---------------------------------------------------------------------------

def _old_create_features_positional(
    df: pd.DataFrame,
    weibull_cfg: dict | None = None,
) -> pd.DataFrame:
    """Pre-fix behaviour, replicated verbatim (not imported) as the baseline
    for the mandatory no-gap equivalence test: lag/rolling/Weibull features
    computed directly on the input's own (possibly gap-collapsed) index, with
    NO calendar reindex step first. This is deliberately a frozen copy of the
    old algorithm, not a call into the fixed create_features."""
    df = df.copy()
    dates = df.index.tz_convert("UTC").normalize()

    df["GW_Lag1"] = df["GW_Level"].shift(1)
    df["GW_Lag7"] = df["GW_Level"].shift(7)
    df["GW_Lag30"] = df["GW_Level"].shift(30)

    df["Rain_1d_sum"] = df["Rainfall"].rolling(1).sum()
    df["Rain_3d_sum"] = df["Rainfall"].rolling(3).sum()
    df["Rain_7d_sum"] = df["Rainfall"].rolling(7).sum()

    doy = dates.day_of_year.values
    df["day_of_year"] = doy
    df["Sin_DOY"] = np.sin(2 * np.pi * doy / 365.25)
    df["Cos_DOY"] = np.cos(2 * np.pi * doy / 365.25)

    required_dropna = [
        "GW_Lag1", "GW_Lag7", "GW_Lag30",
        "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
    ]

    if weibull_cfg is not None and weibull_cfg.get("enabled", False):
        kernel = compute_weibull_kernel(
            float(weibull_cfg["k"]), float(weibull_cfg["lambda"]),
            int(weibull_cfg["lag_days"]),
        )
        shifted = df["Rainfall"].shift(1)
        df["Recharge_Weibull"] = shifted.rolling(
            window=len(kernel), min_periods=len(kernel)
        ).apply(lambda x: float(np.dot(x[::-1], kernel)), raw=True)
        required_dropna.append("Recharge_Weibull")

    df = df.dropna(subset=required_dropna)
    return df


def test_create_features_equivalence_no_gaps_plain():
    """MANDATORY equivalence guarantee: for a station with a fully contiguous
    daily index and no NaN rainfall, the calendar-reindex fix must produce
    EXACTLY the same output (values and row set) as the pre-fix positional
    computation — the reindex is a no-op on gap-free data."""
    df = _joined_frame(150)
    old = _old_create_features_positional(df)
    new = create_features(df)
    pd.testing.assert_frame_equal(old, new)


def test_create_features_equivalence_no_gaps_with_weibull():
    df = _joined_frame(150)
    old = _old_create_features_positional(df, weibull_cfg=_WEIBULL_CFG)
    new = create_features(df, weibull_cfg=_WEIBULL_CFG)
    pd.testing.assert_frame_equal(old, new)


# ---------------------------------------------------------------------------
# create_features / reindex_to_calendar — gap behaviour (the bug fix itself)
# ---------------------------------------------------------------------------

def _frame_with_gap(n: int = 150, gap_start: int = 60, gap_len: int = 21,
                    start: str = "2020-01-01") -> tuple[pd.DataFrame, pd.DatetimeIndex]:
    """A joined daily frame with a `gap_len`-day outage starting at position
    `gap_start` removed ENTIRELY from the index (simulating what
    resample_to_daily's dropna + join_timeseries's required-rainfall drop
    leave behind: no row at all for a missing day, not a NaN-valued row).
    Returns (frame, full_calendar_index) so callers can reference gap dates.
    """
    full_idx = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    keep = full_idx[:gap_start].append(full_idx[gap_start + gap_len:])
    df = pd.DataFrame({
        "GW_Level": np.linspace(10, 12, len(keep)),
        "Rainfall": np.ones(len(keep)),
    }, index=keep)
    return df, full_idx


def test_gw_lags_calendar_true_across_21day_outage():
    """A 21-day GW outage: post-gap GW_Lag1/7/30 must be NaN until the
    calendar lag is genuinely available, not merely 'N surviving rows back'
    (the pre-fix positional bug) — checked at specific dates."""
    df, full_idx = _frame_with_gap(n=150, gap_start=60, gap_len=21)
    grid = reindex_to_calendar(df)
    gw_lag1 = grid["GW_Level"].shift(1)
    gw_lag7 = grid["GW_Level"].shift(7)
    gw_lag30 = grid["GW_Level"].shift(30)

    first_obs_after_gap = full_idx[60 + 21]  # first real reading after the outage

    # GW_Lag1: NaN the day the outage ends (references the still-missing
    # prior day), valid exactly one calendar day later.
    assert pd.isna(gw_lag1.loc[first_obs_after_gap])
    assert not pd.isna(gw_lag1.loc[first_obs_after_gap + pd.Timedelta(days=1)])

    # GW_Lag7: valid only once the calendar lag reaches back to the first
    # post-gap reading (7 calendar days after it), not 7 sooner.
    lag7_from = first_obs_after_gap + pd.Timedelta(days=7)
    assert pd.isna(gw_lag7.loc[lag7_from - pd.Timedelta(days=1)])
    assert not pd.isna(gw_lag7.loc[lag7_from])

    # GW_Lag30: same logic, 30 calendar days after the first post-gap reading.
    lag30_from = first_obs_after_gap + pd.Timedelta(days=30)
    assert pd.isna(gw_lag30.loc[lag30_from - pd.Timedelta(days=1)])
    assert not pd.isna(gw_lag30.loc[lag30_from])


def test_rain_3d_sum_nan_across_gap():
    """A 3-day rolling rainfall sum spanning a gap-collapsed missing day must
    be NaN (strict, min_periods=3), not silently sum 3 rows spanning more
    than 3 calendar days."""
    idx = pd.date_range("2020-01-01", periods=20, freq="1D", tz="UTC")
    keep = idx.delete(10)  # a single missing calendar day
    df = pd.DataFrame({
        "GW_Level": np.linspace(10, 11, len(keep)),
        "Rainfall": np.ones(len(keep)),
    }, index=keep)
    grid = reindex_to_calendar(df)
    rain_3d = grid["Rainfall"].rolling(3, min_periods=3).sum()

    gap_date = idx[10]
    for offset in range(3):
        d = gap_date + pd.Timedelta(days=offset)
        assert pd.isna(rain_3d.loc[d]), f"expected NaN at {d} (window touches the gap)"
    clear_date = gap_date + pd.Timedelta(days=3)
    assert not pd.isna(rain_3d.loc[clear_date]), "first fully-clear window should compute"


def test_create_features_drops_more_rows_on_gappy_station():
    """The training table SHRINKS at gappy stations relative to positional
    computation (which kept wrong rows) — that is the point of the fix."""
    gap_free = _joined_frame(150)
    gappy, _ = _frame_with_gap(n=150, gap_start=60, gap_len=21)
    result_full = create_features(gap_free)
    result_gap = create_features(gappy)
    assert len(result_gap) < len(result_full)
    # No NaNs leak into the required columns of the surviving rows.
    required = ["GW_Lag1", "GW_Lag7", "GW_Lag30",
                "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum"]
    assert result_gap[required].isna().sum().sum() == 0
    # Row set is still "days with a reading": every surviving date was one of
    # the actually-observed (non-gap) dates.
    assert result_gap.index.isin(gappy.index).all()


# ---------------------------------------------------------------------------
# apply_weibull_recharge — bounded tolerance (item 4 of the gap-features fix)
# ---------------------------------------------------------------------------

def test_weibull_tolerance_96pct_coverage_computes():
    """A lag window with 96% coverage (2/50 days missing) is within
    tolerance: NaN rainfall in the window is treated as zero and a value is
    computed."""
    lag_days = 50
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=lag_days)
    vals = np.ones(200)
    vals[50:52] = np.nan  # 2 missing days inside the window feeding index 100
    rainfall = pd.Series(vals)
    result = apply_weibull_recharge(rainfall, kernel)
    assert not np.isnan(result.iloc[100])


def test_weibull_tolerance_90pct_coverage_is_nan():
    """A lag window with 90% coverage (5/50 days missing) exceeds the
    tolerance bound: the result is NaN rather than a number built on a
    mostly-missing window."""
    lag_days = 50
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=lag_days)
    vals = np.ones(200)
    vals[50:55] = np.nan  # 5 missing days inside the window feeding index 100
    rainfall = pd.Series(vals)
    result = apply_weibull_recharge(rainfall, kernel)
    assert np.isnan(result.iloc[100])


def test_weibull_recharge_no_gaps_matches_dot_product_exactly():
    """Sanity: with zero missing rainfall, the tolerance path is a no-op and
    the convolution matches the direct dot-product definition."""
    rng = np.random.default_rng(1)
    rainfall = pd.Series(np.abs(rng.normal(2, 1, 150)))
    kernel = compute_weibull_kernel(k=1.5, lam=15.0, lag_days=50)
    result = apply_weibull_recharge(rainfall, kernel)
    expected = float(np.dot(rainfall.iloc[49:99].to_numpy()[::-1], kernel))
    assert result.iloc[99] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# create_features — gap handling across multi-kernel / by-group Weibull
# variants (item 4/5 must cover ALL kernel variants, not just pooled)
# ---------------------------------------------------------------------------

class TestGapHandlingAcrossKernelVariants:
    def _gappy_frame(self, n=150, gap_start=60, gap_len=10):
        df, _ = _frame_with_gap(n=n, gap_start=gap_start, gap_len=gap_len)
        return df

    def test_multi_kernel_survives_gap_with_reduced_rows(self):
        gap_free = _joined_frame(150)
        gappy = self._gappy_frame()
        result_full = create_features(gap_free, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                      region_group="east")
        result_gap = create_features(gappy, weibull_multi_cfg=_WEIBULL_MULTI_CFG,
                                     region_group="east")
        assert len(result_gap) < len(result_full)
        required = ["GW_Lag1", "GW_Lag7", "GW_Lag30",
                    "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
                    "Recharge_East_masked"]
        assert result_gap[required].isna().sum().sum() == 0

    def test_by_group_survives_gap_with_reduced_rows(self):
        gap_free = _joined_frame(150)
        gappy = self._gappy_frame()
        result_full = create_features(gap_free, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                      region_group="west")
        result_gap = create_features(gappy, weibull_by_group_cfg=_WEIBULL_BY_GROUP_CFG,
                                     region_group="west")
        assert len(result_gap) < len(result_full)
        assert result_gap["Recharge_Weibull"].isna().sum() == 0

    def test_pooled_weibull_survives_gap_with_reduced_rows(self):
        gap_free = _joined_frame(150)
        gappy = self._gappy_frame(gap_len=21)
        result_full = create_features(gap_free, weibull_cfg=_WEIBULL_CFG)
        result_gap = create_features(gappy, weibull_cfg=_WEIBULL_CFG)
        assert len(result_gap) < len(result_full)
        assert result_gap["Recharge_Weibull"].isna().sum() == 0

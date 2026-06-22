"""
Feature engineering for groundwater forecasting.

Reads raw time series from data/raw/, resamples to daily frequency,
joins predictors, and creates lag/rolling/seasonality/recharge features.

Usage:
    python -m src.features.build
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.utils.io_encoding import force_utf8_stdio


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Integer codes for East/West/Unknown groups — must match regional.lon_split logic
_GROUP_CODES: dict[str, int] = {"east": 0, "west": 1, "unknown": 2}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parents[2] / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Group assignment
# ---------------------------------------------------------------------------

def get_station_group(
    station_id: str,
    gw_lons: dict[str, float],
    lon_split: float,
) -> str:
    """Return 'east', 'west', or 'unknown' based on station longitude.

    Parameters
    ----------
    station_id : GW station identifier.
    gw_lons    : dict mapping station_id → longitude (from catalogue).
    lon_split  : stations with lon >= lon_split are 'east', else 'west'.
    """
    lon = gw_lons.get(station_id)
    if lon is None or pd.isna(lon):
        return "unknown"
    return "east" if float(lon) >= lon_split else "west"


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_timeseries(measure_id: str, measure_type: str, raw_root: str) -> pd.DataFrame | None:
    """Load a raw CSV for a single measure. Returns None if file not found.

    Expects columns: dateTime, value.
    Parses dateTime as UTC, returns a DataFrame indexed by dateTime.
    """
    path = Path(raw_root) / measure_type / f"{measure_id}.csv"
    if not path.exists():
        return None

    try:
        df = pd.read_csv(path, parse_dates=["dateTime"], low_memory=False)
    except Exception:
        return None

    if "dateTime" not in df.columns or "value" not in df.columns:
        return None

    df["dateTime"] = pd.to_datetime(df["dateTime"], utc=True, errors="coerce")
    df = df.dropna(subset=["dateTime", "value"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    df = df.set_index("dateTime").sort_index()
    return df


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def clean_groundwater_series(df: pd.DataFrame, iqr_fence: float) -> tuple[pd.DataFrame, int]:
    """Set GW values outside median ± iqr_fence * IQR to NaN.

    Applied to sub-daily raw data before resampling. Handles instrument
    spikes and physically impossible values (e.g., deeply negative mAOD).

    If the IQR is zero (genuinely flat signal) the series is returned
    unchanged — the min_daily_std filter downstream handles flat stations.

    Returns:
        (cleaned_df, n_flagged) — cleaned copy and count of rows set to NaN.
    """
    if df is None or df.empty:
        return df, 0
    v = df["value"].dropna()
    if v.empty:
        return df, 0
    q1, q3 = float(v.quantile(0.25)), float(v.quantile(0.75))
    iqr = q3 - q1
    if iqr == 0.0:
        return df, 0
    med = float(v.median())
    lo = med - iqr_fence * iqr
    hi = med + iqr_fence * iqr
    cleaned = df.copy()
    mask = (cleaned["value"] < lo) | (cleaned["value"] > hi)
    n_flagged = int(mask.sum())
    if n_flagged:
        cleaned.loc[mask, "value"] = np.nan
    return cleaned, n_flagged


# ---------------------------------------------------------------------------
# Resample
# ---------------------------------------------------------------------------

def resample_to_daily(df: pd.DataFrame, agg: str) -> pd.DataFrame:
    """Resample a dateTime-indexed DataFrame to daily frequency.

    agg: "mean" or "sum"
    Returns a DataFrame with a DatetimeTZAware UTC index at midnight.
    """
    if agg == "sum":
        daily = df["value"].resample("1D").sum(min_count=1)
    else:
        daily = df["value"].resample("1D").mean()

    daily = daily.dropna()
    return daily.rename("value").to_frame()


# ---------------------------------------------------------------------------
# Averaging
# ---------------------------------------------------------------------------

def average_rainfall(series_list: list[pd.DataFrame | None]) -> pd.DataFrame | None:
    """Average daily rainfall across up to 3 stations.

    Accepts None entries (station not linked or data absent).
    Returns None if no valid series provided.
    Averages across available stations on each date (ignores NaN stations per date).
    """
    valid = [s for s in series_list if s is not None and not s.empty]
    if not valid:
        return None

    combined = pd.concat(
        [s.rename(columns={"value": f"v{i}"}) for i, s in enumerate(valid)],
        axis=1, sort=True,
    )
    combined["value"] = combined.mean(axis=1)
    return combined[["value"]]


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def join_timeseries(
    gw: pd.DataFrame,
    rainfall: pd.DataFrame | None,
) -> pd.DataFrame | None:
    """Left-join predictors onto groundwater daily dates.

    GW and Rainfall are both required: rows missing either are dropped.

    Returns None if GW or Rainfall is absent/empty.
    """
    if gw is None or gw.empty:
        return None
    if rainfall is None or rainfall.empty:
        return None

    base = gw.rename(columns={"value": "GW_Level"})

    rain = rainfall.rename(columns={"value": "Rainfall"})
    joined = base.join(rain, how="left")

    # Drop rows where rainfall is missing (required predictor)
    joined = joined.dropna(subset=["Rainfall"])

    return joined


# ---------------------------------------------------------------------------
# Weibull recharge
# ---------------------------------------------------------------------------

def compute_weibull_kernel(k: float, lam: float, lag_days: int) -> np.ndarray:
    """Compute a normalised Weibull PDF weight array over lag_days days.

    Weight index i corresponds to a lag of (i + 1) days, so:
        kernel[0]  = w(1 day ago)
        kernel[-1] = w(lag_days days ago)

    Weights are normalised to sum to 1.0.
    """
    i = np.arange(1, lag_days + 1, dtype=float)
    w = (k / lam) * (i / lam) ** (k - 1) * np.exp(-(i / lam) ** k)
    return w / w.sum()


def apply_weibull_recharge(rainfall: pd.Series, kernel: np.ndarray) -> pd.Series:
    """Convolve daily rainfall with a Weibull kernel to produce recharge estimates.

    Recharge at time t = sum_{i=1}^{lag_days} kernel[i-1] * Rainfall[t - i]

    The first lag_days rows are NaN (insufficient prior history).
    rainfall must be non-negative; NaN entries are treated as zero for the
    convolution but propagated in the min_periods guard.

    Returns a Series aligned to rainfall's index.
    """
    lag_days = len(kernel)
    # Shift by 1 so that the rolling window at t covers rainfall[t-lag_days..t-1]
    shifted = rainfall.shift(1)
    result = shifted.rolling(window=lag_days, min_periods=lag_days).apply(
        # x is ordered oldest→newest; reverse so x[0] = t-1, x[-1] = t-lag_days
        lambda x: float(np.dot(x[::-1], kernel)),
        raw=True,
    )
    return result


# ---------------------------------------------------------------------------
# Feature creation
# ---------------------------------------------------------------------------

def create_features(
    df: pd.DataFrame,
    weibull_cfg: dict | None = None,
    weibull_multi_cfg: dict | None = None,
    weibull_by_group_cfg: dict | None = None,
    region_group: str = "unknown",
) -> pd.DataFrame:
    """Add lag, rolling, seasonality, and optional Weibull recharge features.

    Lag features (days): GW_Level 1/7/30
    Rolling rainfall sums: 1d, 3d, 7d
    Seasonality: day_of_year, Sin_DOY, Cos_DOY
    Weibull recharge — three mutually-exclusive modes:
      weibull_cfg        : single pooled kernel → Recharge_Weibull
      weibull_multi_cfg  : per-group masked columns → Recharge_{Name}_masked
      weibull_by_group_cfg: single column, kernel chosen by group
                           → Recharge_Weibull (same name, no masking)
                           Also adds region_group_code for context.
                           Unknown stations fall back to the first kernel.

    Rows are dropped where any required lag/rolling column is NaN.
    Effective minimum rows dropped = max(30 for GW_Lag30, lag_days of active kernel).

    Parameters
    ----------
    df                  : joined daily timeseries for a single station.
    weibull_cfg         : single-kernel pooled Weibull config.
    weibull_multi_cfg   : multi-kernel masked config (east/west separate columns).
    weibull_by_group_cfg: group-conditional config (one kernel per group, one column).
    region_group        : 'east', 'west', or 'unknown' for this station.
    """
    df = df.copy()

    dates = df.index.tz_convert("UTC").normalize()

    df["GW_Lag1"]  = df["GW_Level"].shift(1)
    df["GW_Lag7"]  = df["GW_Level"].shift(7)
    df["GW_Lag30"] = df["GW_Level"].shift(30)

    df["Rain_1d_sum"]  = df["Rainfall"].rolling(1).sum()
    df["Rain_3d_sum"]  = df["Rainfall"].rolling(3).sum()
    df["Rain_7d_sum"]  = df["Rainfall"].rolling(7).sum()

    doy = dates.day_of_year.values
    df["day_of_year"] = doy
    df["Sin_DOY"]     = np.sin(2 * np.pi * doy / 365.25)
    df["Cos_DOY"]     = np.cos(2 * np.pi * doy / 365.25)

    required_dropna = [
        "GW_Lag1", "GW_Lag7", "GW_Lag30",
        "Rain_1d_sum", "Rain_3d_sum", "Rain_7d_sum",
    ]

    # Optional single-kernel Weibull recharge (pooled)
    use_weibull = (
        weibull_cfg is not None
        and weibull_cfg.get("enabled", False)
    )
    if use_weibull:
        wk       = float(weibull_cfg["k"])
        lam      = float(weibull_cfg["lambda"])
        lag_days = int(weibull_cfg["lag_days"])
        kernel   = compute_weibull_kernel(wk, lam, lag_days)
        df["Recharge_Weibull"] = apply_weibull_recharge(df["Rainfall"], kernel)
        required_dropna.append("Recharge_Weibull")

    # Optional multi-kernel Weibull recharge (group-specific masked features)
    use_multi = (
        weibull_multi_cfg is not None
        and weibull_multi_cfg.get("enabled", False)
    )
    if use_multi:
        add_masked = weibull_multi_cfg.get("add_masked", True)
        for name, kern_cfg in weibull_multi_cfg.get("kernels", {}).items():
            mk      = float(kern_cfg["k"])
            mlam    = float(kern_cfg["lambda"])
            mlag    = int(kern_cfg["lag_days"])
            mkernel = compute_weibull_kernel(mk, mlam, mlag)
            recharge = apply_weibull_recharge(df["Rainfall"], mkernel)
            if add_masked:
                masked_col = f"Recharge_{name.capitalize()}_masked"
                # For this station's group: actual recharge values
                # For all other groups: 0.0 (feature is masked out)
                df[masked_col] = np.where(region_group == name, recharge, 0.0)
                # Only add to required_dropna for the active group — the other
                # group's masked column is 0.0 everywhere so no rows need dropping
                if region_group == name:
                    required_dropna.append(masked_col)
        df["region_group_code"] = _GROUP_CODES.get(region_group, 2)

    # Group-conditional Weibull: one Recharge_Weibull column, kernel chosen by group
    use_by_group = (
        weibull_by_group_cfg is not None
        and weibull_by_group_cfg.get("enabled", False)
    )
    if use_by_group:
        # Build dict of named kernels (excludes the "enabled" key)
        kern_cfgs = {k: v for k, v in weibull_by_group_cfg.items() if k != "enabled"}
        # Select this station's kernel; unknown → fall back to first available
        kern_cfg = kern_cfgs.get(region_group, next(iter(kern_cfgs.values())))
        bgk   = float(kern_cfg["k"])
        bglam = float(kern_cfg["lambda"])
        bglag = int(kern_cfg["lag_days"])
        bgkernel = compute_weibull_kernel(bgk, bglam, bglag)
        df["Recharge_Weibull"] = apply_weibull_recharge(df["Rainfall"], bgkernel)
        required_dropna.append("Recharge_Weibull")
        # Region code kept as optional context feature
        df["region_group_code"] = _GROUP_CODES.get(region_group, 2)

    df = df.dropna(subset=required_dropna)
    return df


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_features(config: dict) -> pd.DataFrame:
    raw_root    = Path(__file__).parents[2] / config["download"]["raw_root"]
    links_path  = Path(__file__).parents[2] / config["linking"]["output_path"]
    output_path = Path(__file__).parents[2] / config["features"]["output_path"]
    weibull_cfg          = config["features"].get("weibull")
    weibull_multi_cfg    = config["features"].get("weibull_multi")
    weibull_by_group_cfg = config["features"].get("weibull_by_group")
    cleaning_cfg = config["features"].get("cleaning", {})
    iqr_fence       = float(cleaning_cfg.get("groundwater_iqr_fence", 20.0))
    min_daily_std   = float(cleaning_cfg.get("groundwater_min_daily_std", 0.0))
    lon_split       = float(config["regional"]["lon_split"])

    joined_path = Path(__file__).parents[2] / config["features"]["joined_path"]

    # Build longitude lookup whenever any group-based Weibull mode is active
    _needs_groups = (
        (weibull_multi_cfg    and weibull_multi_cfg.get("enabled"))
        or (weibull_by_group_cfg and weibull_by_group_cfg.get("enabled"))
    )
    gw_lons_map: dict[str, float] = {}
    if _needs_groups:
        cat_path = Path(__file__).parents[2] / config["catalogue"]["output_path"]
        try:
            catalogue_df = pd.read_csv(cat_path)
            gw_cat = (
                catalogue_df[catalogue_df["measure_type"] == "groundwater"][
                    ["station_id", "lon"]
                ]
                .drop_duplicates("station_id")
                .set_index("station_id")["lon"]
            )
            gw_lons_map = gw_cat.to_dict()
            print(f"  Catalogue loaded: {len(gw_lons_map)} GW stations for group assignment")
        except FileNotFoundError:
            print("  WARNING: catalogue not found — group-based Weibull disabled")
            weibull_multi_cfg    = None
            weibull_by_group_cfg = None

    links = pd.read_csv(links_path)
    print(f"Processing {len(links)} GW stations...")
    if weibull_cfg and weibull_cfg.get("enabled"):
        print(f"  Weibull recharge (pooled): k={weibull_cfg['k']}, "
              f"lambda={weibull_cfg['lambda']}, lag_days={weibull_cfg['lag_days']}")
    if weibull_multi_cfg and weibull_multi_cfg.get("enabled"):
        for gname, gcfg in weibull_multi_cfg["kernels"].items():
            print(f"  Multi-kernel {gname}: k={gcfg['k']}, "
                  f"lambda={gcfg['lambda']}, lag_days={gcfg['lag_days']}")
    if weibull_by_group_cfg and weibull_by_group_cfg.get("enabled"):
        kern_cfgs = {k: v for k, v in weibull_by_group_cfg.items() if k != "enabled"}
        for gname, gcfg in kern_cfgs.items():
            print(f"  Weibull by-group [{gname}]: k={gcfg['k']}, "
                  f"lambda={gcfg['lambda']}, lag_days={gcfg['lag_days']}")
    print(f"  Cleaning: IQR fence={iqr_fence}x, min daily std={min_daily_std}m")

    all_frames  = []
    all_joined  = []  # pre-feature-engineering, for Weibull tuning

    for _, row in links.iterrows():
        gw_station_id = str(row["GWStationID"])
        gw_measure_id = str(row["GWMeasureID"])

        # Load GW
        gw_raw = load_timeseries(gw_measure_id, "groundwater", raw_root)
        if gw_raw is None:
            print(f"  [{gw_station_id}] SKIP -- GW data not found")
            continue

        # Clean: remove instrument spikes and physically impossible values
        gw_raw, n_flagged = clean_groundwater_series(gw_raw, iqr_fence)
        if n_flagged:
            print(f"  [{gw_station_id}] cleaning: {n_flagged} outlier readings flagged "
                  f"(IQR fence={iqr_fence}x)")

        gw_daily = resample_to_daily(gw_raw, agg="mean")

        # Skip stations with flat GW signal (no predictive information)
        if min_daily_std > 0.0:
            daily_std = float(gw_daily["value"].std())
            if daily_std < min_daily_std:
                print(f"  [{gw_station_id}] SKIP -- flat GW signal "
                      f"(daily std={daily_std:.4f}m < {min_daily_std}m)")
                continue

        # Load rainfall (up to 3 stations, average)
        rain_series = []
        for k in ["RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"]:
            mid = row.get(k)
            if pd.notna(mid):
                raw = load_timeseries(str(mid), "rainfall", raw_root)
                rain_series.append(resample_to_daily(raw, agg="sum") if raw is not None else None)
            else:
                rain_series.append(None)
        rainfall_daily = average_rainfall(rain_series)

        joined = join_timeseries(gw_daily, rainfall_daily)
        if joined is None or joined.empty:
            print(f"  [{gw_station_id}] SKIP -- insufficient joined data")
            continue

        # Save pre-feature-engineering joined frame for Weibull tuning
        joined_with_id = joined.copy()
        joined_with_id["station_id"] = gw_station_id
        all_joined.append(joined_with_id)

        region_group = get_station_group(gw_station_id, gw_lons_map, lon_split)
        featured = create_features(
            joined,
            weibull_cfg=weibull_cfg,
            weibull_multi_cfg=weibull_multi_cfg,
            weibull_by_group_cfg=weibull_by_group_cfg,
            region_group=region_group,
        )
        if featured.empty:
            print(f"  [{gw_station_id}] SKIP -- no rows after feature creation")
            continue

        featured["station_id"] = gw_station_id
        all_frames.append(featured)
        print(f"  [{gw_station_id}] {len(featured)} rows")

    if not all_frames:
        raise ValueError("No feature rows produced -- check raw data availability.")

    result = pd.concat(all_frames, axis=0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path)
    print(f"\n{len(result)} total rows across {len(all_frames)} stations")
    print(f"Features written to {output_path}")

    # Save joined timeseries (pre-feature-engineering) for Weibull tuning
    if all_joined:
        joined_output = pd.concat(all_joined, axis=0)
        joined_path.parent.mkdir(parents=True, exist_ok=True)
        joined_output.to_csv(joined_path)
        print(f"Joined timeseries written to {joined_path}")

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    force_utf8_stdio()
    config = load_config()
    try:
        build_features(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

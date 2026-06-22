"""v1.5 step 3 — convert dipped raw files into a daily interpolated series
and append to the joined timeseries so the rest of the pipeline picks them up.

Inputs
------
data/raw/groundwater/<measure_id>.csv   (dipped: irregular timestamps, one
                                         reading every few weeks/months)

Outputs
-------
data/features/joined_timeseries.csv             (extended, in-place — backup first)
data/processed/gw_station_coverage.csv          (per-station diagnostic table)

Rule (per the v1.5 plan)
------------------------
* Daily date grid 2018-01-01 → today.
* Place each dipped reading on its calendar date (median if multiple per day).
* Linear-interpolate gaps **≤ 30 days**; longer gaps stay NaN.
* `is_interpolated=1` for any row where the value came from interpolation.
* `data_source = "logged"` for the existing 131 stations,
  `data_source = "dipped_interp"` for the new ones.
"""
import re
import shutil
from datetime import date
from pathlib import Path

import pandas as pd

from src.utils.io_encoding import force_utf8_stdio

RAW = Path("data/raw/groundwater")
JOINED = Path("data/features/joined_timeseries.csv")
COVERAGE = Path("data/processed/gw_station_coverage.csv")

UUID_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
DAILY_GRID_START = pd.Timestamp("2018-01-01", tz="UTC")
DAILY_GRID_END   = pd.Timestamp(date.today(), tz="UTC")
MAX_INTERP_GAP_DAYS = 30


def extract_station_id(filename: str) -> str | None:
    m = UUID_RE.match(filename)
    return m.group(1) if m else None


def build_daily_for_dipped_file(fp: Path) -> tuple[pd.DataFrame, dict]:
    """Read one dipped raw CSV, build a daily-interpolated DataFrame, return
    it + a coverage diagnostics dict."""
    sid = extract_station_id(fp.stem)
    if sid is None:
        return pd.DataFrame(), {}

    try:
        raw = pd.read_csv(fp, usecols=["dateTime", "value", "quality"])
    except (ValueError, KeyError):
        return pd.DataFrame(), {}

    raw["dateTime"] = pd.to_datetime(raw["dateTime"], utc=True, errors="coerce")
    raw = raw.dropna(subset=["dateTime", "value"])
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce")
    raw = raw.dropna(subset=["value"])
    if raw.empty:
        return pd.DataFrame(), {"station_id": sid, "n_dips": 0,
                                "first_dip": None, "last_dip": None}

    # Only "Good"-quality dips (mirrors the existing project convention)
    if "quality" in raw.columns:
        raw = raw[raw["quality"].astype(str).str.lower().isin(
            ["good", "good ", "unchecked"])]

    if raw.empty:
        return pd.DataFrame(), {"station_id": sid, "n_dips": 0,
                                "first_dip": None, "last_dip": None}

    raw["date"] = raw["dateTime"].dt.tz_convert("UTC").dt.normalize()
    daily_dips = raw.groupby("date", as_index=False)["value"].median()
    daily_dips = daily_dips.rename(columns={"value": "GW_Level"}).sort_values("date")

    first_dip, last_dip = daily_dips["date"].min(), daily_dips["date"].max()
    n_dips = len(daily_dips)

    # Build the daily grid bounded by the dip span (no need to extrapolate)
    grid_start = max(DAILY_GRID_START, first_dip)
    grid_end   = min(DAILY_GRID_END,   last_dip)
    if grid_end < grid_start:
        return pd.DataFrame(), {"station_id": sid, "n_dips": n_dips,
                                "first_dip": first_dip, "last_dip": last_dip}

    grid = pd.DataFrame({"date": pd.date_range(grid_start, grid_end, freq="D")})
    df = grid.merge(daily_dips, on="date", how="left")

    # Interpolate gaps ≤ MAX_INTERP_GAP_DAYS; longer gaps stay NaN.
    # Strategy: linear-interpolate everywhere, then mask back any cell whose
    # nearest-real-dip on EITHER side is > MAX_INTERP_GAP_DAYS away.
    df_value = df.set_index("date")["GW_Level"]
    interp = df_value.interpolate(method="linear", limit_direction="both")

    real_dates = daily_dips["date"].sort_values().reset_index(drop=True)
    days_to_prev = pd.Series(
        df["date"].apply(lambda d:
            (d - real_dates[real_dates <= d].max()).days
            if (real_dates <= d).any() else 9999
        ).values, index=df["date"]
    )
    days_to_next = pd.Series(
        df["date"].apply(lambda d:
            (real_dates[real_dates >= d].min() - d).days
            if (real_dates >= d).any() else 9999
        ).values, index=df["date"]
    )
    gap = pd.concat([days_to_prev, days_to_next], axis=1).max(axis=1)
    masked = interp.where(gap <= MAX_INTERP_GAP_DAYS)

    is_interpolated = (
        masked.notna() & df_value.isna()
    ).astype(int).reset_index(drop=True)

    dt = pd.to_datetime(df["date"])
    if dt.dt.tz is None:
        dt = dt.dt.tz_localize("UTC")
    else:
        dt = dt.dt.tz_convert("UTC")
    out = pd.DataFrame({
        "dateTime": dt,
        "GW_Level": masked.values,
        "Rainfall": pd.NA,
        "station_id": sid,
        "is_interpolated": is_interpolated.values,
        "data_source": "dipped_interp",
    })
    out = out.dropna(subset=["GW_Level"])

    diag = {
        "station_id": sid,
        "data_source": "dipped_interp",
        "n_dips": int(n_dips),
        "first_dip": first_dip.date().isoformat(),
        "last_dip":  last_dip.date().isoformat(),
        "n_days_total":     int(len(out)),
        "n_days_real":      int((out["is_interpolated"] == 0).sum()),
        "n_days_interp":    int((out["is_interpolated"] == 1).sum()),
        "median_gap_days":  (
            float(daily_dips["date"].diff().dt.days.median())
            if n_dips >= 2 else None
        ),
        "max_gap_days": (
            float(daily_dips["date"].diff().dt.days.max())
            if n_dips >= 2 else None
        ),
    }
    return out, diag


def main():
    force_utf8_stdio()
    # ---------- 1. Inventory dipped raw files ----------
    cat = pd.read_csv("data/processed/catalogue.csv")
    gw = cat[cat["measure_type"] == "groundwater"]
    logged_ids = set(gw[gw["measure_period"] == 900]["station_id"].unique())

    dipped_files = []
    for fp in sorted(RAW.glob("*-gw-dipped-*.csv")):
        sid = extract_station_id(fp.stem)
        if sid is None or sid in logged_ids:
            continue
        dipped_files.append(fp)

    print(f"Dipped raw files to ingest: {len(dipped_files)}")
    if not dipped_files:
        print("  (none) — was the fetch step run?")

    # ---------- 2. Build daily series + diagnostics ----------
    frames = []
    diags = []
    for fp in dipped_files:
        df, diag = build_daily_for_dipped_file(fp)
        if df.empty:
            continue
        frames.append(df)
        diags.append(diag)

    if not frames:
        print("No usable dipped data — exiting.")
        return

    new_block = pd.concat(frames, ignore_index=True)
    print(f"  new rows: {len(new_block):,} for {new_block['station_id'].nunique()} stations")

    # ---------- 3. Backup + extend joined_timeseries ----------
    backup = JOINED.with_suffix(".csv.pre-v15.bak")
    if not backup.exists():
        shutil.copy(JOINED, backup)
        print(f"  backed up original to {backup}")

    existing = pd.read_csv(JOINED, parse_dates=["dateTime"])
    # Tag existing rows as logged (default)
    if "data_source" not in existing.columns:
        existing["data_source"] = "logged"
    if "is_interpolated" not in existing.columns:
        existing["is_interpolated"] = 0

    # Drop any rows for stations we're about to re-add (defensive — none expected)
    existing = existing[~existing["station_id"].isin(new_block["station_id"].unique())]

    combined = pd.concat([existing, new_block], ignore_index=True)
    combined = combined.sort_values(["station_id", "dateTime"])
    combined.to_csv(JOINED, index=False)
    print(f"  wrote {JOINED} ({len(combined):,} rows, "
          f"{combined['station_id'].nunique()} stations)")

    # ---------- 4. Coverage diagnostics ----------
    cov = pd.DataFrame(diags)
    COVERAGE.parent.mkdir(parents=True, exist_ok=True)
    cov.to_csv(COVERAGE, index=False)
    print(f"  wrote {COVERAGE} ({len(cov)} rows)")
    print()
    print("Coverage summary (dipped stations only):")
    print(f"  median dips per station:       {cov['n_dips'].median():.0f}")
    print(f"  median sampling gap (days):    {cov['median_gap_days'].median():.0f}")
    print(f"  stations with median gap ≤35d: "
          f"{(cov['median_gap_days'] <= 35).sum()}")
    print(f"  stations with ≥60 dips:        {(cov['n_dips'] >= 60).sum()}")


if __name__ == "__main__":
    main()

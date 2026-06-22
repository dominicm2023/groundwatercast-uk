"""
Station linking engine.

For each groundwater station, finds the 3 nearest rainfall stations.
Uses Haversine distance. No geopandas.

Usage:
    python -m src.linking.build
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = Path(__file__).parents[2] / "config" / "config.json"
    with open(config_path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------

_EARTH_RADIUS_KM = 6371.0


def haversine(lat1: float, lon1: float, lat2, lon2) -> np.ndarray:
    """Haversine distance in km from one point to an array of points.

    lat1, lon1: scalars (source point)
    lat2, lon2: scalars or numpy arrays (target points)
    """
    lat1, lon1 = np.radians(lat1), np.radians(lon1)
    lat2, lon2 = np.radians(np.asarray(lat2, dtype=float)), np.radians(np.asarray(lon2, dtype=float))

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return _EARTH_RADIUS_KM * 2 * np.arcsin(np.sqrt(a))


# ---------------------------------------------------------------------------
# Preference-aware station selection
# ---------------------------------------------------------------------------

def select_groundwater(df: pd.DataFrame) -> pd.DataFrame:
    """Return one row per groundwater station using continuous (logged) measures.

    Only rows with a non-null numeric measure_period are kept (continuous
    loggers). Where a station has multiple continuous measures, the one with
    the lowest period (highest resolution) is chosen; ties broken
    alphabetically by measure_id.
    """
    gw = df[df["measure_type"] == "groundwater"].copy()
    gw["measure_period"] = pd.to_numeric(gw["measure_period"], errors="coerce")
    gw = gw.dropna(subset=["measure_period"])

    if gw.empty:
        return gw

    gw = gw.sort_values(["station_id", "measure_period", "measure_id"])
    return gw.groupby("station_id", sort=False).first().reset_index()


def select_predictors_rainfall(df: pd.DataFrame, prefs: dict) -> pd.DataFrame:
    """Return one row per rainfall station with a selection_reason flag.

    Preferred: measure_period == preferred_period (default 86400, daily total)
    Fallback:  lowest available period, any statistic
    Excluded:  stations with no measures at all

    selection_reason is "preferred" or "fallback".
    """
    candidates = df[df["measure_type"] == "rainfall"].copy()
    candidates["measure_period"] = pd.to_numeric(
        candidates["measure_period"], errors="coerce"
    )
    candidates = candidates.dropna(subset=["measure_period"])

    if candidates.empty:
        return pd.DataFrame()

    preferred_period = prefs.get("preferred_period", 86400)
    rows = []
    for _station_id, group in candidates.groupby("station_id", sort=False):
        preferred = group[group["measure_period"] == preferred_period]
        if not preferred.empty:
            rep = preferred.sort_values("measure_id").iloc[0].copy()
            rep["selection_reason"] = "preferred"
        else:
            rep = group.sort_values(["measure_period", "measure_id"]).iloc[0].copy()
            rep["selection_reason"] = "fallback"
        rows.append(rep)

    return pd.DataFrame(rows).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Nearest selection
# ---------------------------------------------------------------------------

def nearest_n(
    gw_row: pd.Series, candidates: pd.DataFrame, n: int
) -> list[tuple[str | None, float | None]]:
    """Return (measure_id, distance_km) tuples for the n nearest candidate stations.

    Candidates are already deduplicated to one row per station, so results
    are guaranteed to be distinct stations. Slots beyond available candidates
    are filled with (None, None).
    """
    if candidates.empty:
        return [(None, None)] * n

    dists = haversine(
        gw_row["lat"], gw_row["lon"],
        candidates["lat"].values, candidates["lon"].values,
    )
    order = np.argsort(dists)
    top = candidates.iloc[order[:n]]
    selected = list(zip(top["measure_id"].tolist(), dists[order[:n]].tolist()))

    while len(selected) < n:
        selected.append((None, None))

    return selected


# ---------------------------------------------------------------------------
# Link builder
# ---------------------------------------------------------------------------

def build_links(
    gw: pd.DataFrame,
    rain_df: pd.DataFrame,
) -> pd.DataFrame:
    """Build one output row per groundwater station with linked predictor IDs."""
    rows = []
    for _, gw_row in gw.iterrows():
        if pd.isna(gw_row.get("station_id")) or str(gw_row["station_id"]).strip() == "":
            continue

        rain = nearest_n(gw_row, rain_df, n=3)

        rows.append({
            "GWStationID":         gw_row["station_id"],
            "GWMeasureID":         gw_row["measure_id"],
            "RainMeasureID_1":     rain[0][0],
            "RainMeasureID_2":     rain[1][0],
            "RainMeasureID_3":     rain[2][0],
            "RainDist_1":          rain[0][1],
            "RainDist_2":          rain[1][1],
            "RainDist_3":          rain[2][1],
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _selection_summary(label: str, df: pd.DataFrame) -> None:
    total = len(df)
    if "selection_reason" in df.columns:
        counts = df["selection_reason"].value_counts()
        preferred = counts.get("preferred", 0)
        fallback  = counts.get("fallback", 0)
        print(f"  {label}: {total} stations  "
              f"(preferred={preferred}, fallback={fallback})")
    else:
        print(f"  {label}: {total} stations")


def build_station_links(config: dict) -> pd.DataFrame:
    catalogue_path = Path(__file__).parents[2] / config["catalogue"]["output_path"]
    output_path    = Path(__file__).parents[2] / config["linking"]["output_path"]

    catalogue = pd.read_csv(catalogue_path)
    rain_prefs  = config["linking"].get("rainfall_preference", {})

    print("Selecting stations...")

    gw   = select_groundwater(catalogue)
    rain = select_predictors_rainfall(catalogue, rain_prefs)

    _selection_summary("groundwater", gw)
    _selection_summary("rainfall",    rain)

    print("\nBuilding links...")
    links = build_links(gw, rain)

    # Validate: GWStationID must be non-null for every row
    null_gw = links["GWStationID"].isna().sum()
    if null_gw:
        raise ValueError(f"{null_gw} rows in links have null GWStationID")

    missing_rain = links[["RainMeasureID_1", "RainMeasureID_2", "RainMeasureID_3"]].isna().any(axis=1).sum()
    if missing_rain:
        print(f"  WARNING: {missing_rain} GW stations have fewer than 3 rainfall links")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    links.to_csv(output_path, index=False)
    print(f"\n{len(links)} GW stations linked")
    print(f"Station links written to {output_path}")

    return links


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    config = load_config()
    try:
        build_station_links(config)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

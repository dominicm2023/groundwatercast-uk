"""Generic cached data loaders shared across dashboard pages:
station catalogue, per-borehole GW series, and GW data freshness.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

# ----------------------------------------------------------------------------
# Colour scheme (re-used across charts)
# ----------------------------------------------------------------------------
BAND_COLORS = {
    "green":  "#2ca02c",
    "amber":  "#ff7f0e",
    "red":    "#d62728",
    "grey":   "#b5b5b5",
}

# ----------------------------------------------------------------------------
# GW data freshness
# ----------------------------------------------------------------------------
FRESHNESS_LABELS = {
    "fresh":      "Fresh (≤7d)",
    "recent":     "Recent (8-30d)",
    "stale":      "Stale (31-90d)",
    "very_stale": "Very stale (>90d)",
    "no_data":    "No data",
}
FRESHNESS_COLOURS = {
    "fresh":      "#2ca02c",  # green
    "recent":     "#d4b106",  # mustard
    "stale":      "#e8761f",  # orange
    "very_stale": "#a83232",  # dark red
    "no_data":    "#9e9e9e",  # grey
}
FRESHNESS_ORDER = ["fresh", "recent", "stale", "very_stale", "no_data"]
# Beyond this, a current-band classification can no longer be defended for
# either logged or dipped stations (15 days past the 30-day dipped
# max-interpolation gap).
NO_BAND_AGE_DAYS = 45


@st.cache_data(show_spinner=False)
def load_freshness() -> pd.DataFrame:
    """Per-BH freshness: last real reading + age in days + label.
    Built by scripts/v15_build_gw_freshness.py."""
    fp = Path("data/processed/gw_freshness.csv")
    if not fp.exists():
        return pd.DataFrame(columns=["station_id", "last_real_reading",
                                     "days_since", "data_source",
                                     "freshness_label"])
    df = pd.read_csv(fp, parse_dates=["last_real_reading"])
    return df


_FRESHNESS_DEFAULT = {"label": "no_data", "days_since": None,
                      "last_real_reading": None, "data_source": None}


@st.cache_data(show_spinner=False)
def _freshness_map() -> dict:
    """Build a {station_id: freshness_dict} once and reuse. O(1) lookup
    avoids per-call DataFrame filtering on list-heavy pages."""
    fr = load_freshness()
    out: dict[str, dict] = {}
    for _, r in fr.iterrows():
        out[r["station_id"]] = {
            "label":             r["freshness_label"],
            "days_since":        int(r["days_since"]) if pd.notna(r["days_since"]) else None,
            "last_real_reading": r["last_real_reading"],
            "data_source":       r["data_source"],
        }
    return out


def freshness_for(bh_id: str) -> dict:
    """Return the freshness dict for a BH. O(1) dict lookup."""
    if not bh_id:
        return _FRESHNESS_DEFAULT
    return _freshness_map().get(bh_id, _FRESHNESS_DEFAULT)


# ----------------------------------------------------------------------------
# Catalogue — memoised once, reused everywhere.
# ----------------------------------------------------------------------------
_CATALOGUE_PATH = Path("data/processed/catalogue.csv")
_CATALOGUE_COLUMNS = [
    "station_id", "station_name", "lat", "lon", "measure_id", "measure_type",
    "measure_period", "measure_value_statistic", "aquifer_name",
    "aquifer_designation",
]


@st.cache_data(show_spinner=False)
def load_catalogue() -> pd.DataFrame:
    """De-duplicated station catalogue. Cached for the life of the
    Streamlit session.

    Returns a typed empty frame when the catalogue hasn't been built yet
    (run ``python -m src.catalogue.build``)."""
    if not _CATALOGUE_PATH.exists():
        import warnings
        warnings.warn(
            f"{_CATALOGUE_PATH} missing — run python -m src.catalogue.build. "
            f"Returning an empty catalogue frame.",
            RuntimeWarning, stacklevel=2,
        )
        return pd.DataFrame(columns=_CATALOGUE_COLUMNS)
    return (pd.read_csv(_CATALOGUE_PATH)
            .drop_duplicates("station_id")
            .reset_index(drop=True))


# ----------------------------------------------------------------------------
# Per-borehole GW series
# ----------------------------------------------------------------------------
_GW_PARQUET_DIR = Path("data/features/gw_by_station")


def load_gw_for_bh(bh_id: str) -> pd.DataFrame:
    """Daily GW level for a single borehole.

    Fast path: per-station Parquet at
        data/features/gw_by_station/<bh_id>.parquet
    Built by scripts/v15_build_per_station_parquet.py — ~20× faster than
    the CSV fallback below.

    Fallback path: scan the full joined_timeseries.csv (slow). Kept so
    the app works in environments where the Parquet step hasn't been
    run; logs a one-line warning on use.
    """
    if not bh_id:
        return pd.DataFrame()

    pq = _GW_PARQUET_DIR / f"{bh_id}.parquet"
    if pq.exists():
        # Parquet stores `date` as pre-normalised tz-naive datetime64 +
        # `GW_Level` ready to use; no per-call conversion needed.
        return pd.read_parquet(pq, columns=["date", "GW_Level"])

    # ---- fallback: full-CSV scan ---------------------------------------
    import warnings
    warnings.warn(
        f"Per-station Parquet missing for {bh_id} — falling back to "
        f"joined_timeseries.csv scan. Run "
        f"scripts/v15_build_per_station_parquet.py to fix.",
        RuntimeWarning, stacklevel=2,
    )
    raw = pd.read_csv(
        "data/features/joined_timeseries.csv",
        parse_dates=["dateTime"],
        usecols=["dateTime", "GW_Level", "station_id"],
    )
    raw = raw[raw["station_id"] == bh_id].copy()
    if raw.empty:
        return raw
    raw["date"] = raw["dateTime"].dt.tz_localize(None).dt.normalize()
    return (raw.dropna(subset=["GW_Level"])
            .groupby("date", as_index=False)["GW_Level"].mean())

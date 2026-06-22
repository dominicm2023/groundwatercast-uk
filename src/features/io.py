"""Feature-table IO shared by the forecast stages.

Relocated from the retired ``src.model.build`` (the RF model left with the
risk index; the feature loader is forecast machinery — the ensemble member
builders and the hindcast read station histories through it).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

TARGET_COL = "GW_Level"


def load_features(config: dict) -> tuple[pd.DataFrame, dict]:
    """Load feature CSV and encode station_id as integer codes.

    Returns (df, station_id_map) where station_id_map maps code to station_id string.
    df has a UTC DatetimeIndex and a numeric station_id_code column.
    """
    path = Path(__file__).parents[2] / config["features"]["output_path"]
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True)

    assert TARGET_COL in df.columns, (
        f"Target column '{TARGET_COL}' not found in features. "
        f"Available columns: {list(df.columns)}"
    )

    # Encode station_id as integer category code
    cat = pd.Categorical(df["station_id"])
    df["station_id_code"] = cat.codes
    station_id_map = dict(enumerate(cat.categories))

    return df, station_id_map

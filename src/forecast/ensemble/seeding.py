"""Freshest observed GW for forecast seeding — shared by the roll and Pastas.

Seeds at the per-station shard ``data/features/gw_by_station/<sid>.parquet``
(archive + live flood-monitoring tail, maintained by ``v16_refresh_live_gw``)
rather than the staler joined feature level. For boreholes matched to the EA
flood-monitoring API this captures recent recession the joined level misses
(a ~5 m staleness error on some boreholes), shrinking the obs→window gap and
the roll-vs-Pastas model-spread.

Pure pandas — importable in either environment.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_SHARD_DIR = Path(__file__).resolve().parents[3] / "data" / "features" / "gw_by_station"


def _naive_daily(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


def freshest_gw(station_id: str, fallback: pd.Series | None = None) -> pd.Series:
    """Freshest observed daily GW for a borehole: the per-station shard (archive
    + live tail) if present, else ``fallback`` (e.g. the joined level). Returns a
    tz-naive, date-sorted, NaN-free Series (empty if neither source has data)."""
    fp = _SHARD_DIR / f"{station_id}.parquet"
    if fp.exists():
        df = pd.read_parquet(fp, columns=["date", "GW_Level"])
        s = pd.Series(df["GW_Level"].to_numpy(float),
                      index=_naive_daily(df["date"]), name="GW_Level").dropna()
        if not s.empty:
            return s.sort_index()
    if fallback is not None:
        f = pd.Series(fallback.to_numpy(float), index=_naive_daily(fallback.index),
                      name="GW_Level").dropna()
        return f.sort_index()
    return pd.Series(dtype="float64", name="GW_Level")

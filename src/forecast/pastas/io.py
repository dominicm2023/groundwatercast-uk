"""Shared IO helpers for the Pastas build scripts (pure pandas — importable in
both the main GW-pipeline env and the minimal pastas venv).

Kept out of ``recharge.py``, which is deliberately IO-free.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
PET_CACHE = _ROOT / "data" / "raw" / "pet"


def load_pet(sid: str) -> pd.Series | None:
    """Load the cached PET (ET0) series for a borehole, or None if not cached."""
    fp = PET_CACHE / f"{sid}.csv"
    if not fp.exists():
        return None
    d = pd.read_csv(fp, parse_dates=["date"])
    return pd.Series(d["et0_mm"].to_numpy(float),
                     index=pd.to_datetime(d["date"]).dt.tz_localize(None).dt.normalize(),
                     name="evap")

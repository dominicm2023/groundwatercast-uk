"""Breach-threshold resolution.

Per borehole, resolve the GW level (mAOD) whose crossing counts as a breach,
in priority order:
    1. user-supplied threshold  (data/thresholds/user_thresholds.yaml) → "user"
    2. per-station GW P90 proxy (transparent fallback)                 → "gw_p90_proxy"
    3. none                     (no breach number produced)            → "none"

Units are mAOD throughout. The source is always reported so the dashboard can
label proxy-based numbers.

User thresholds file schema (one entry per station)::

    thresholds:
      - station_id: "abc123-..."     # EA hydrology station GUID
        mAOD: 41.2                   # breach level, metres AOD
        label: "Cellar flooding"     # optional, shown in the UI
        source: "Parish flood plan"  # optional provenance note

Operational thresholds (flood-onset levels, abstraction/licence levels, asset
flood levels) are site- and operator-specific knowledge — they are user
config here, not shipped data. When a station appears more than once the
highest (most severe) level wins.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).parents[3]
_USER_THRESHOLDS = ROOT / "data" / "thresholds" / "user_thresholds.yaml"


@lru_cache(maxsize=1)
def load_user_thresholds() -> dict[str, float]:
    """station_id → breach mAOD from the user thresholds file.

    Missing file / malformed YAML / empty list all yield {} — thresholds are
    optional config; everything downstream falls back to the P90 proxy."""
    if not _USER_THRESHOLDS.exists():
        return {}
    try:
        d = yaml.safe_load(_USER_THRESHOLDS.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, float] = {}
    for row in (d.get("thresholds") or []):
        sid = row.get("station_id")
        thr = row.get("mAOD")
        if sid and thr is not None:
            out[sid] = max(out.get(sid, float("-inf")), float(thr))
    return out


@lru_cache(maxsize=1)
def user_threshold_station_ids() -> frozenset[str]:
    """All station_ids with a user-supplied threshold.

    These are the boreholes the user has declared operationally meaningful —
    the outlook page badges them accordingly."""
    return frozenset(load_user_thresholds())


def resolve_threshold(station_id: str, *, gw_p90: float | None = None
                      ) -> tuple[float | None, str]:
    """Return (threshold_mAOD, source) for a borehole (see module docstring)."""
    user = load_user_thresholds()
    if station_id in user:
        return float(user[station_id]), "user"
    if gw_p90 is not None and pd.notna(gw_p90):
        return float(gw_p90), "gw_p90_proxy"
    return None, "none"


def reload() -> None:
    load_user_thresholds.cache_clear()
    user_threshold_station_ids.cache_clear()

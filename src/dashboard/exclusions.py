"""Known-bad station register — loader + lookup helpers.

The register lives at ``data/external/known_bad_stations.yaml`` and
lists EA hydrology stations whose recent readings are not safely
comparable against thresholds derived from their historical data
(typically because EA shifted the sensor datum).

Consumers skip these stations when picking a cluster representative
during dedup, drop them from candidate-scoring pools, and annotate
them with the exclusion reason in exports.

See data/external/known_bad_stations.yaml for the schema documentation.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


_REGISTER_PATH = Path("data/external/known_bad_stations.yaml")

# Recognised exclusion-reason tags (documentation/vocabulary, not enforced at load
# so an unknown reason never silently drops an entry). `abstraction_influenced`
# (roadmap H7) marks a heavily-pumped site whose level reflects a pump schedule
# rather than recharge — flagged advisory by scripts/build_abstraction_screen.py,
# confirmed and added here by a human.
KNOWN_REASONS = frozenset({
    "scaling_change", "datum_shift", "sensor_fault", "decommissioned",
    "abstraction_influenced", "other",
})


@lru_cache(maxsize=1)
def _load_register() -> dict[str, dict[str, Any]]:
    """Return ``{station_id: entry_dict}`` (cached for the process)."""
    if not _REGISTER_PATH.exists():
        return {}
    try:
        with _REGISTER_PATH.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for entry in (data.get("stations") or []):
        sid = entry.get("station_id")
        if sid:
            out[sid] = entry
    return out


def is_excluded(station_id: str | None, on_date: date | None = None) -> bool:
    """Return True if `station_id` is excluded as of `on_date` (default today).

    A station with no `excluded_from` is excluded indefinitely (e.g.
    decommissioned). A station with `excluded_from: YYYY-MM-DD` is
    excluded for the band/fallback computation but its pre-shift
    historical data remains valid.

    For the purposes of fresh-fallback selection on the dashboard we
    treat any presence-in-register as excluded — we don't have a
    sub-date selector in the UI, so it's safer to drop the station
    from operational consideration entirely once a shift is flagged.
    """
    if not station_id:
        return False
    return station_id in _load_register()


def exclusion_for(station_id: str | None) -> dict[str, Any] | None:
    """Full register entry for a station, or None if not flagged."""
    if not station_id:
        return None
    return _load_register().get(station_id)


def excluded_station_ids() -> set[str]:
    """Set of all station_ids currently in the register."""
    return set(_load_register().keys())


def reason_for(station_id: str | None) -> str | None:
    """The exclusion `reason` tag for a station, or None if not flagged."""
    entry = exclusion_for(station_id)
    return entry.get("reason") if entry else None


def reload_register() -> None:
    """Force-reload from disk (cache bust)."""
    _load_register.cache_clear()

"""Abstraction capture-zone screen (roadmap H7) — licence-proximity logic.

Joins EA abstraction-licence points (``data/processed/abstraction_points.csv``,
built by ``scripts/build_abstraction_points.py``) to catalogued boreholes via a
volume-banded influence radius. This is a **screen, not a drawdown model**: the
radius is a deliberately generous capture-zone proxy justified by chalk-typical
transmissivity reasoning (see ``docs/abstraction_screen_design.md``), and every
quantity here is **licensed capacity, not actual pumping** — no live abstraction
feed exists.

Invariants inherited from the ingest (honour them, don't re-derive):
  - a multi-point licence repeats its LICENCE-level maxima on every row, so
    quantities are NEVER summed across rows of one licence — everything here
    dedupes to one capacity per ``licence_no`` first (``dedupe_licences``);
  - holder identities are already stripped upstream; this module only ever
    carries ``licence_no`` (the public join key) forward;
  - the extract covers >100 m³/day returns-submitting licences only, so tier
    ``none`` means "no *large, returns-submitting* licence nearby", not "no
    abstraction".

Output tiers (per borehole, report-only — never auto-excludes):
  - ``likely``   — a licence point sits well inside its influence radius
                   (``likely_inner_fraction``), or the summed deduped licensed
                   capacity within radius reaches ``likely_capacity_m3d``.
  - ``possible`` — at least one licence within its banded radius.
  - ``none``     — no groundwater licence within any banded radius.

Pure numpy/pandas (never imports pastas). I/O lives in
``scripts/build_abstraction_influence.py``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CAPACITY_BASIS = "licensed_max_not_actual_pumping"

_TIER_RANK = {"likely": 2, "possible": 1, "none": 0}


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km; lat2/lon2 may be arrays."""
    r = 6371.0
    p1 = np.radians(float(lat1))
    p2 = np.radians(np.asarray(lat2, dtype=float))
    dlat = np.radians(np.asarray(lat2, dtype=float) - float(lat1))
    dlon = np.radians(np.asarray(lon2, dtype=float) - float(lon1))
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def radius_m_for_volume(daily_m3, bands: list[dict]) -> float:
    """Influence radius (m) for a licensed daily volume, from config bands.

    ``bands`` is an ordered list of ``{"max_daily_m3_lt": <float|None>,
    "radius_m": <float>}``; the first band whose upper bound exceeds the volume
    wins, ``None`` = unbounded top band. A non-finite volume falls into the
    FIRST (smallest-radius) band — the extract's floor is >100 m³/day, so an
    unquantified licence is treated as small, not ignored."""
    v = float(daily_m3) if daily_m3 is not None else float("nan")
    if not np.isfinite(v):
        return float(bands[0]["radius_m"])
    for b in bands:
        lt = b.get("max_daily_m3_lt")
        if lt is None or v < float(lt):
            return float(b["radius_m"])
    return float(bands[-1]["radius_m"])


def dedupe_licences(points: pd.DataFrame) -> pd.DataFrame:
    """One capacity per licence: licence-level maxima, never summed across rows.

    Keeps every point row (each row is a distinct abstraction point and each
    must be distance-tested), but attaches a ``daily_m3`` that is the
    licence-level maximum, and a per-licence ``max_annual_m3`` the same way,
    so any later per-licence aggregation is safe by construction."""
    p = points.copy()
    lic_daily = p.groupby("licence_no")["max_daily_m3"].transform("max")
    lic_annual = p.groupby("licence_no")["max_annual_m3"].transform("max")
    p["daily_m3"] = lic_daily
    p["annual_m3"] = lic_annual
    return p


def prepare_points(points: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Filter to the configured source (Groundwater) + precompute radii."""
    src = cfg.get("source_filter", "Groundwater")
    p = points[points["source"] == src].dropna(subset=["lat", "lon"])
    p = dedupe_licences(p)
    bands = cfg["radius_bands_m"]
    p["radius_m"] = [radius_m_for_volume(v, bands) for v in p["daily_m3"]]
    return p.reset_index(drop=True)


def screen_borehole(bh_lat: float, bh_lon: float, lic: pd.DataFrame,
                    cfg: dict) -> dict:
    """Influence metrics for one borehole vs a ``prepare_points`` frame.

    A licence is "within radius" if ANY of its points lies inside that
    licence's banded radius; capacity sums are over distinct licences."""
    inner_frac = float(cfg.get("likely_inner_fraction", 0.5))
    likely_cap = float(cfg.get("likely_capacity_m3d", 5000.0))

    if lic.empty:
        return dict(nearest_licence_no=None, nearest_licence_km=np.nan,
                    licences_within_radius=0, licensed_daily_m3_within=0.0,
                    licensed_annual_m3_within=0.0, influence_tier="none")

    d_km = haversine_km(bh_lat, bh_lon, lic["lat"].to_numpy(),
                        lic["lon"].to_numpy())
    d_m = d_km * 1000.0
    i_near = int(np.argmin(d_km))

    in_radius = d_m <= lic["radius_m"].to_numpy()
    in_inner = d_m <= inner_frac * lic["radius_m"].to_numpy()

    hit = lic.loc[in_radius, ["licence_no", "daily_m3", "annual_m3"]]
    per_lic = hit.drop_duplicates("licence_no")  # never sum across one licence's rows
    n_lic = int(per_lic["licence_no"].nunique())
    sum_daily = float(per_lic["daily_m3"].fillna(0.0).sum())
    sum_annual = float(per_lic["annual_m3"].fillna(0.0).sum())

    if n_lic == 0:
        tier = "none"
    elif bool(in_inner.any()) or sum_daily >= likely_cap:
        tier = "likely"
    else:
        tier = "possible"

    return dict(
        nearest_licence_no=str(lic.loc[i_near, "licence_no"]),
        nearest_licence_km=float(d_km[i_near]),
        licences_within_radius=n_lic,
        licensed_daily_m3_within=sum_daily,
        licensed_annual_m3_within=sum_annual,
        influence_tier=tier,
    )


def tier_at_least(tier: str, floor: str) -> bool:
    """True if ``tier`` meets the ``floor`` tier (none < possible < likely)."""
    return _TIER_RANK.get(tier, 0) >= _TIER_RANK.get(floor, 1)

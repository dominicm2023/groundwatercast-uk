"""Capture-zone screen (roadmap H7): volume banding, licence dedup, tiering, join.

Pins the two load-bearing invariants of the licence join:
  1. a multi-point licence repeats its LICENCE-level maxima on every row, so
     capacity is never summed across rows of one licence;
  2. radii are volume-banded (config-driven) and an unquantified licence falls
     into the smallest band rather than being dropped.
All offline/pure — synthetic licence frames, no CSV I/O.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.diagnostics import abstraction_influence as ai

CFG = dict(
    source_filter="Groundwater",
    radius_bands_m=[
        {"max_daily_m3_lt": 500, "radius_m": 500},
        {"max_daily_m3_lt": 5000, "radius_m": 1500},
        {"max_daily_m3_lt": None, "radius_m": 3000},
    ],
    likely_inner_fraction=0.5,
    likely_capacity_m3d=5000,
)

# ~0.009° latitude ≈ 1 km; keep everything on one meridian so distance ≈ dlat.
LAT_PER_KM = 1.0 / 111.195


def _points(rows):
    return pd.DataFrame(rows, columns=[
        "licence_no", "source", "purpose", "max_annual_m3", "max_daily_m3",
        "lat", "lon"])


def _lic(licence_no, daily, lat, lon=0.0, source="Groundwater", annual=None):
    return dict(licence_no=licence_no, source=source, purpose="Spray Irrigation",
                max_annual_m3=annual if annual is not None else daily * 365,
                max_daily_m3=daily, lat=lat, lon=lon)


# ---------------------------------------------------------------------------
# radius_m_for_volume
# ---------------------------------------------------------------------------

class TestRadiusBanding:
    BANDS = CFG["radius_bands_m"]

    def test_bands_by_volume(self):
        assert ai.radius_m_for_volume(100, self.BANDS) == 500
        assert ai.radius_m_for_volume(499.9, self.BANDS) == 500
        assert ai.radius_m_for_volume(500, self.BANDS) == 1500
        assert ai.radius_m_for_volume(4999, self.BANDS) == 1500
        assert ai.radius_m_for_volume(5000, self.BANDS) == 3000
        assert ai.radius_m_for_volume(2.9e6, self.BANDS) == 3000

    def test_unquantified_licence_gets_smallest_band_not_dropped(self):
        assert ai.radius_m_for_volume(np.nan, self.BANDS) == 500
        assert ai.radius_m_for_volume(None, self.BANDS) == 500


# ---------------------------------------------------------------------------
# dedupe_licences — the never-sum-across-rows invariant
# ---------------------------------------------------------------------------

class TestLicenceDedup:
    def test_multipoint_licence_capacity_counted_once(self):
        # One licence, three abstraction points, licence-level max 900 m³/d
        # repeated per row (the NALD extract shape). The borehole sits within
        # radius of all three points: capacity must read 900, not 2700.
        pts = _points([
            _lic("L1", 900, lat=0.001),
            _lic("L1", 900, lat=0.002),
            _lic("L1", 900, lat=-0.001),
        ])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["licences_within_radius"] == 1
        assert r["licensed_daily_m3_within"] == 900.0

    def test_distinct_licences_do_sum(self):
        pts = _points([
            _lic("L1", 900, lat=0.001),
            _lic("L2", 600, lat=-0.001),
        ])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["licences_within_radius"] == 2
        assert r["licensed_daily_m3_within"] == 1500.0

    def test_surface_water_licences_filtered_out(self):
        pts = _points([
            _lic("L1", 900, lat=0.001, source="Surface water"),
        ])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "none"
        assert r["licences_within_radius"] == 0


# ---------------------------------------------------------------------------
# screen_borehole — distances, tiers
# ---------------------------------------------------------------------------

class TestTiering:
    def test_none_when_no_licence_within_banded_radius(self):
        # 100 m³/d → 500 m radius; licence 2 km away → out of range.
        pts = _points([_lic("L1", 100, lat=2.0 * LAT_PER_KM)])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "none"
        assert r["licences_within_radius"] == 0
        # nearest distance is still reported for review context
        assert abs(r["nearest_licence_km"] - 2.0) < 0.01
        assert r["nearest_licence_no"] == "L1"

    def test_possible_outer_radius_only(self):
        # 900 m³/d → 1500 m radius, inner = 750 m; licence at ~1 km → within
        # radius but outside the inner fraction, capacity < 5000 → possible.
        pts = _points([_lic("L1", 900, lat=1.0 * LAT_PER_KM)])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "possible"

    def test_likely_when_well_inside_radius(self):
        # Same licence at 300 m — inside the 750 m inner fraction → likely.
        pts = _points([_lic("L1", 900, lat=0.3 * LAT_PER_KM)])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "likely"

    def test_likely_by_summed_capacity(self):
        # Two outer-radius licences summing past 5000 m³/d → likely even
        # though neither is inside its inner fraction.
        pts = _points([
            _lic("L1", 3000, lat=1.2 * LAT_PER_KM),
            _lic("L2", 2500, lat=-1.2 * LAT_PER_KM),
        ])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "likely"
        assert r["licensed_daily_m3_within"] == 5500.0

    def test_big_licence_reaches_further(self):
        # 20,000 m³/d → 3000 m radius: in range at 2.5 km where a small
        # licence would not be.
        pts = _points([_lic("BIG", 20000, lat=2.5 * LAT_PER_KM)])
        lic = ai.prepare_points(pts, CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] != "none"

    def test_empty_licence_frame(self):
        lic = ai.prepare_points(_points([]), CFG)
        r = ai.screen_borehole(0.0, 0.0, lic, CFG)
        assert r["influence_tier"] == "none"
        assert not np.isfinite(r["nearest_licence_km"])


# ---------------------------------------------------------------------------
# tier_at_least
# ---------------------------------------------------------------------------

def test_tier_ordering():
    assert ai.tier_at_least("likely", "possible")
    assert ai.tier_at_least("possible", "possible")
    assert not ai.tier_at_least("none", "possible")
    assert not ai.tier_at_least("possible", "likely")
    # unknown tiers rank lowest — fail closed
    assert not ai.tier_at_least("garbage", "possible")

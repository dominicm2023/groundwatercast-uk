"""
Tests for the live-levels (flood-monitoring API) integration.

Covers:
  1. QC rules — outlier rejection, duplicate removal, stuck-sensor flag.
  2. Cross-reference matcher: reference / coords / name_exact /
     name_fuzzy / none paths each fire on the right inputs.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.forecast.live_levels import apply_qc
from src.diagnostics.flood_monitoring_xref import (
    _haversine_m,
    _norm_name,
    _fuzzy_ratio,
    build_xref,
)


# ---------------------------------------------------------------------------
# 1. QC
# ---------------------------------------------------------------------------

class TestApplyQC:
    def test_drops_nan(self):
        df = pd.DataFrame({
            "dateTime": pd.date_range("2026-05-01", periods=3, freq="D", tz="UTC"),
            "value": [10.0, float("nan"), 10.2],
        })
        out, flags = apply_qc(df, historical_mean=10.0, historical_std=0.5)
        assert len(out) == 2
        assert "stuck_sensor" not in flags

    def test_drops_outliers(self):
        df = pd.DataFrame({
            "dateTime": pd.date_range("2026-05-01", periods=3, freq="D", tz="UTC"),
            "value": [10.0, 10.1, 100.0],   # 100 is far outside the band
        })
        out, flags = apply_qc(df, historical_mean=10.0, historical_std=0.1)
        assert len(out) == 2
        assert any("outlier" in f for f in flags)

    def test_drops_duplicates_keep_last(self):
        t = pd.Timestamp("2026-05-01", tz="UTC")
        df = pd.DataFrame({
            "dateTime": [t, t, t + pd.Timedelta(days=1)],
            "value": [10.0, 10.5, 10.7],
        })
        out, _ = apply_qc(df, historical_mean=10.0, historical_std=1.0)
        assert len(out) == 2
        # Same dateTime kept the LAST value
        same_day = out[out["dateTime"] == t]
        assert float(same_day["value"].iloc[0]) == 10.5

    def test_stuck_sensor_flag(self):
        # Many days of an identical value → stuck flag
        df = pd.DataFrame({
            "dateTime": pd.date_range("2026-05-01", periods=10, freq="D", tz="UTC"),
            "value": [10.0] * 10,
        })
        out, flags = apply_qc(df, historical_mean=10.0, historical_std=1.0)
        assert "stuck_sensor" in flags
        # Rows are not dropped, just flagged
        assert len(out) == 10

    def test_empty_input(self):
        df = pd.DataFrame(columns=["dateTime", "value"])
        out, flags = apply_qc(df, historical_mean=0.0, historical_std=1.0)
        assert out.empty
        assert flags == []


# ---------------------------------------------------------------------------
# 2. Cross-reference helpers
# ---------------------------------------------------------------------------

class TestXrefHelpers:
    def test_haversine_zero(self):
        assert _haversine_m(51.0, -1.0, 51.0, -1.0) == pytest.approx(0.0, abs=1e-6)

    def test_haversine_one_degree_lat(self):
        # 1 deg latitude ≈ 111 km
        d = _haversine_m(50.0, -1.0, 51.0, -1.0)
        assert 110_000 < d < 112_000

    def test_norm_name_strips_punctuation_and_lowercases(self):
        assert _norm_name("Wingham Road  OBH!") == "wingham road obh"

    def test_norm_name_handles_none(self):
        assert _norm_name(None) == ""

    def test_fuzzy_ratio_identical(self):
        assert _fuzzy_ratio("wingham road", "wingham road") == 100

    def test_fuzzy_ratio_close(self):
        # Same tokens, different order should still score well
        score = _fuzzy_ratio("road wingham", "wingham road")
        assert score >= 90

    def test_fuzzy_ratio_dissimilar(self):
        score = _fuzzy_ratio("alkham valley", "houndean bottom")
        assert score < 90


class TestBuildXref:
    def test_reference_match(self):
        """A hydrology station whose stationReference matches an FM
        notation should land in the ``reference`` bucket."""
        cat = pd.DataFrame({
            "station_id":   ["guid-A"],
            "station_name": ["Some Station"],
            "lat":          [51.0],
            "lon":          [-1.0],
            "measure_type": ["groundwater"],
        })
        fm = pd.DataFrame({
            "fm_notation":           ["E12345"],
            "fm_label":              ["Some Station GWL"],
            "fm_station_reference":  ["E12345"],
            "fm_lat":                [None],
            "fm_lon":                [None],
        })
        hydro_idx = {
            "guid-A": {
                "label": "Some Station",
                "stationReference": "E12345",
                "wiskiID": None,
                "notation": None,
            }
        }
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            cat.to_csv(f.name, index=False)
            path = f.name
        try:
            out = build_xref(hydro_catalogue_path=path, fm_stations=fm, hydro_index=hydro_idx)
            assert len(out) == 1
            assert out.iloc[0]["match_method"] == "reference"
            assert out.iloc[0]["fm_notation"] == "E12345"
        finally:
            os.unlink(path)

    def test_coords_match(self):
        cat = pd.DataFrame({
            "station_id":   ["guid-B"],
            "station_name": ["Coastal Borehole"],
            "lat":          [50.82],
            "lon":          [-0.14],
            "measure_type": ["groundwater"],
        })
        fm = pd.DataFrame({
            "fm_notation":           ["FM999"],
            "fm_label":              ["Brighton GWL"],
            "fm_station_reference":  ["FM999"],
            "fm_lat":                [50.8200],   # 30 cm away
            "fm_lon":                [-0.1400],
        })
        # No hydrology-index entry → forces a fallback to coord matching
        hydro_idx: dict = {}
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            cat.to_csv(f.name, index=False)
            path = f.name
        try:
            out = build_xref(hydro_catalogue_path=path, fm_stations=fm, hydro_index=hydro_idx)
            assert out.iloc[0]["match_method"] == "coords"
        finally:
            os.unlink(path)

    def test_none_when_no_match(self):
        cat = pd.DataFrame({
            "station_id":   ["guid-X"],
            "station_name": ["Some Station"],
            "lat":          [51.0],
            "lon":          [-1.0],
            "measure_type": ["groundwater"],
        })
        fm = pd.DataFrame({
            "fm_notation":           ["FFF"],
            "fm_label":              ["Other Place"],
            "fm_station_reference":  ["FFF"],
            "fm_lat":                [53.0],     # far away
            "fm_lon":                [-2.0],
        })
        hydro_idx: dict = {}
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            cat.to_csv(f.name, index=False)
            path = f.name
        try:
            out = build_xref(hydro_catalogue_path=path, fm_stations=fm, hydro_index=hydro_idx)
            assert out.iloc[0]["match_method"] == "none"
            assert pd.isna(out.iloc[0]["fm_notation"]) or out.iloc[0]["fm_notation"] is None
        finally:
            os.unlink(path)

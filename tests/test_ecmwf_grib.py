"""Offline tests for the ECMWF Open Data GRIB provider's pure logic
(src/forecast/ensemble/ecmwf_opendata.py).

No GRIB files, no network, no eccodes: synthetic xarray Datasets mimic what
cfgrib decodes (scalar ``time`` coord, ``step`` as timedelta64, descending
latitudes, either longitude convention, pf ``number`` coord / cf without).
Pins the W1 bug-fix behaviours: UTC-midnight step boundaries, window-START
day labelling, m→mm de-accumulation with negative-noise clamp, and
longitude-convention mapping.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

xr = pytest.importorskip("xarray")

from src.forecast.ensemble.ecmwf_opendata import (  # noqa: E402
    ECMWFOpenDataENS,
    _daily_series,
    _extract_point,
    _grid_lon,
    _utc_day_steps,
)


# ---------------------------------------------------------------------------
# Step boundaries
# ---------------------------------------------------------------------------

class TestUtcDaySteps:
    def test_00z_run(self):
        assert _utc_day_steps(0, 3) == [0, 24, 48, 72]

    def test_12z_run_starts_at_first_midnight(self):
        # The 0-12 h stub is not a boundary: increments are full UTC days.
        assert _utc_day_steps(12, 3) == [12, 36, 60, 84]

    def test_capped_at_ens_limit(self):
        steps = _utc_day_steps(0, 20)
        assert steps[-1] <= 360
        assert steps == [24 * k for k in range(16)]   # 0..360 inclusive

    def test_all_on_the_ens_step_grid(self):
        # multiples of 3 h below 144, of 6 h above — 24-hourly always is.
        for h in (0, 6, 12, 18):
            for s in _utc_day_steps(h, 15):
                assert s % (3 if s <= 144 else 6) == 0

    def test_short_cycles_cap_at_144h(self):
        # 06/18Z cycles only disseminate to 144 h.
        from src.forecast.ensemble.ecmwf_opendata import _MAX_STEP_BY_HOUR
        steps = _utc_day_steps(6, 15, _MAX_STEP_BY_HOUR[6])
        assert steps[-1] <= 144
        assert steps == [18, 42, 66, 90, 114, 138]



# ---------------------------------------------------------------------------
# De-accumulation
# ---------------------------------------------------------------------------

class TestDailySeries:
    BASE = pd.Timestamp("2026-06-12 00:00:00")

    def test_m_to_mm(self):
        s = _daily_series([0, 24, 48], [0.0, 0.001, 0.003], self.BASE)
        assert list(s.values) == [1.0, 2.0]

    def test_day_label_is_window_start(self):
        # Regression (W1 bug 1): the increment over hours [0, 24) IS the
        # run date — it must not be labelled run date + 1.
        s = _daily_series([0, 24], [0.0, 0.002], self.BASE)
        assert s.index[0] == pd.Timestamp("2026-06-12")

    def test_12z_boundaries_label_full_utc_days(self):
        base = pd.Timestamp("2026-06-12 12:00:00")
        s = _daily_series([12, 36, 60], [0.004, 0.005, 0.0075], base)
        # increment [12h, 36h) covers UTC day 2026-06-13
        assert list(s.index) == [pd.Timestamp("2026-06-13"),
                                 pd.Timestamp("2026-06-14")]
        assert list(np.round(s.values, 6)) == [1.0, 2.5]

    def test_negative_packing_noise_clamped(self):
        s = _daily_series([0, 24, 48], [0.0, 0.001, 0.000999], self.BASE)
        assert s.iloc[1] == 0.0

    def test_unsorted_steps_sorted(self):
        s = _daily_series([24, 0], [0.001, 0.0], self.BASE)
        assert s.iloc[0] == 1.0


# ---------------------------------------------------------------------------
# Longitude conventions + point extraction
# ---------------------------------------------------------------------------

def _synthetic_ds(lons, members=None, base="2026-06-12T00:00:00",
                  steps_h=(0, 24, 48)):
    """Mimic a cfgrib-decoded ENS tp dataset over a tiny UK-ish grid."""
    lats = np.array([52.0, 51.75, 51.5])          # descending, as cfgrib does
    lons = np.asarray(lons, dtype=float)
    steps = pd.to_timedelta(list(steps_h), unit="h")
    if members is None:                            # cf file: no number dim
        shape = (len(steps), len(lats), len(lons))
        dims = ("step", "latitude", "longitude")
        coords = {}
    else:                                          # pf file
        shape = (len(members), len(steps), len(lats), len(lons))
        dims = ("number", "step", "latitude", "longitude")
        coords = {"number": list(members)}
    # accumulation grows with step; offset by longitude index so the chosen
    # cell is identifiable from the value
    data = np.zeros(shape)
    for si in range(len(steps)):
        data[..., si, :, :] = si * 0.001
        for li in range(len(lons)):
            data[..., si, :, li] += li * 0.0001
    return xr.Dataset(
        {"tp": (dims, data)},
        coords={**coords, "step": ("step", steps),
                "latitude": ("latitude", lats),
                "longitude": ("longitude", lons),
                "time": pd.Timestamp(base)},
    )


class TestGridLon:
    def test_0_360_grid(self):
        lons = np.array([358.5, 358.75, 359.0])
        assert _grid_lon(-1.3, lons) == pytest.approx(358.7)

    def test_pm180_grid(self):
        lons = np.array([-1.5, -1.25, -1.0])
        assert _grid_lon(-1.3, lons) == pytest.approx(-1.3)
        assert _grid_lon(358.7, lons) == pytest.approx(-1.3)


class TestExtractPoint:
    def test_uk_lon_on_0_360_grid_hits_right_cell(self):
        # Regression (W1 bug 4): -1.25 on a 0..360 grid must select 358.75,
        # not snap to the far end of the axis.
        ds = _synthetic_ds([358.5, 358.75, 359.0])
        pt = _extract_point(ds, 51.75, -1.25)
        assert float(pt["longitude"]) == pytest.approx(358.75)

    def test_uk_lon_on_pm180_grid(self):
        ds = _synthetic_ds([-1.5, -1.25, -1.0])
        pt = _extract_point(ds, 51.75, -1.25)
        assert float(pt["longitude"]) == pytest.approx(-1.25)

    def test_cf_expands_to_member_zero(self):
        ds = _synthetic_ds([-1.5, -1.25, -1.0])      # no number dim
        pt = _extract_point(ds, 51.75, -1.25)
        assert list(pt["number"].values) == [0]

    def test_pf_preserves_member_numbers(self):
        ds = _synthetic_ds([-1.5, -1.25, -1.0], members=[1, 2, 3])
        pt = _extract_point(ds, 51.75, -1.25)
        assert list(pt["number"].values) == [1, 2, 3]


# ---------------------------------------------------------------------------
# End-to-end _parse over synthetic cf+pf files
# ---------------------------------------------------------------------------

class TestParseContract:
    def test_synthetic_pair_through_validate(self, tmp_path, monkeypatch):
        """Full _parse over a synthetic cf+pf pair → OUTPUT_COLUMNS, member
        set {0,1,2}, window-start labels — then through _validate.

        open_dataset is monkeypatched, so the cfgrib stack-check is bypassed
        — this test needs only xarray (runs in the main env)."""
        import src.forecast.ensemble.ecmwf_opendata as eod
        cf = _synthetic_ds([-1.5, -1.25, -1.0])
        pf = _synthetic_ds([-1.5, -1.25, -1.0], members=[1, 2])

        def fake_open_dataset(path, **kw):
            return cf if "ctrl" in str(path) else pf

        monkeypatch.setattr(eod, "_require_parse_stack", lambda: None)
        monkeypatch.setattr(xr, "open_dataset", fake_open_dataset)
        df = ECMWFOpenDataENS._parse(
            [tmp_path / "ens_tp_ctrl_0h-48h.grib2",
             tmp_path / "ens_tp_pf_0h-48h.grib2"],
            51.75, -1.25)
        df = ECMWFOpenDataENS._validate(df)
        assert list(df.columns) == ["member", "date", "precip_mm"]
        assert set(df["member"]) == {0, 1, 2}
        # 3 step boundaries -> 2 daily increments per member
        assert len(df) == 6
        assert df["date"].min() == pd.Timestamp("2026-06-12")  # window start
        # acc grid: 1 mm/day everywhere in this synthetic
        assert np.allclose(df["precip_mm"], 1.0)

"""Season view: alignment math, segmentation (incl. water-year boundary),
envelope, and figure composition. All offline/pure."""
from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd
import pytest

from src.dashboard import season_view as sv

# season_figure needs plotly; the alignment/envelope math does not. Skip only
# the figure-composition tests when the plotting stack isn't installed.
_HAS_PLOTLY = importlib.util.find_spec("plotly") is not None
_needs_plotly = pytest.mark.skipif(not _HAS_PLOTLY, reason="plotly not installed")


def _daily(start, end, value=1.0):
    idx = pd.date_range(start, end, freq="D")
    return pd.Series(value, index=idx)


# ---------------------------------------------------------------------------
# Alignment math
# ---------------------------------------------------------------------------

class TestAlignment:
    def test_calendar_year_start_and_label(self):
        ts = pd.Timestamp("2026-06-12")
        assert sv.year_start(ts, sv.CALENDAR) == pd.Timestamp("2026-01-01")
        assert sv.year_label(ts, sv.CALENDAR) == "2026"

    def test_water_year_boundary(self):
        # 30 Sep belongs to the OLD water year; 1 Oct starts the new one.
        assert sv.year_start(pd.Timestamp("2026-09-30"), sv.WATER) == \
            pd.Timestamp("2025-10-01")
        assert sv.year_start(pd.Timestamp("2026-10-01"), sv.WATER) == \
            pd.Timestamp("2026-10-01")
        assert sv.year_label(pd.Timestamp("2026-10-01"), sv.WATER) == "WY2027"
        assert sv.year_label(pd.Timestamp("2026-09-30"), sv.WATER) == "WY2026"

    def test_axis_day_zero_at_year_start(self):
        s = pd.Series([1.0], index=[pd.Timestamp("2025-10-01")])
        a = sv.align_series(s, sv.WATER)
        assert a.iloc[0]["axis_day"] == 0

    def test_empty_series(self):
        assert sv.align_series(pd.Series(dtype=float), sv.CALENDAR).empty

    def test_leap_year_axis_day_aligns_across_years(self):
        # 15 Mar must land on the same axis_day in a leap year (2024) and a
        # non-leap year (2023, 2025) — otherwise the overlay and envelope are
        # smeared by a day after February.
        s = pd.Series(
            [1.0, 1.0, 1.0],
            index=pd.to_datetime(["2023-03-15", "2024-03-15", "2025-03-15"]),
        )
        a = sv.align_series(s, sv.CALENDAR).sort_values("year_key")
        assert a["axis_day"].nunique() == 1
        # 15 Mar is the 74th day on a non-leap calendar (0-based: 31+28+14).
        assert int(a["axis_day"].iloc[0]) == 73

    def test_leap_day_folds_onto_feb_28(self):
        # 29 Feb has no slot on the 365-day axis; it folds onto 28 Feb so it
        # still contributes to that day's envelope instead of shifting March.
        s = pd.Series([1.0, 2.0],
                      index=pd.to_datetime(["2024-02-28", "2024-02-29"]))
        a = sv.align_series(s, sv.CALENDAR)
        assert a["axis_day"].tolist() == [58, 58]
        # and 1 Mar in that same leap year is still 59, not 60.
        m = sv.align_series(
            pd.Series([1.0], index=[pd.Timestamp("2024-03-01")]), sv.CALENDAR)
        assert int(m["axis_day"].iloc[0]) == 59

    def test_leap_year_water_alignment(self):
        # Same calendar date across a leap and non-leap February, under water-
        # year alignment, must still share an axis_day (Feb is mid-water-year).
        s = pd.Series([1.0, 1.0],
                      index=pd.to_datetime(["2024-03-15", "2023-03-15"]))
        a = sv.align_series(s, sv.WATER)
        assert a["axis_day"].nunique() == 1


class TestSegmentation:
    def test_jun_to_dec_forecast_splits_at_water_year_boundary(self):
        fc = _daily("2026-06-13", "2026-12-12")        # Jun→Dec, crosses 1 Oct
        segs = sv.segments(sv.align_series(fc, sv.WATER))
        keys = [k for k, _ in segs]
        assert keys == ["WY2026", "WY2027"]
        # the WY2027 segment starts at the left edge (axis_day 0)
        wy27 = dict(segs)["WY2027"]
        assert wy27["axis_day"].min() == 0

    def test_same_window_is_one_segment_under_calendar(self):
        fc = _daily("2026-06-13", "2026-12-12")
        segs = sv.segments(sv.align_series(fc, sv.CALENDAR))
        assert [k for k, _ in segs] == ["2026"]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

class TestEnvelope:
    def test_quantile_ordering_and_min_years(self):
        # 5 years of data on the same days, values 1..5
        parts = [_daily(f"{y}-01-01", f"{y}-03-31", value=float(v))
                 for v, y in enumerate(range(2020, 2025), start=1)]
        aligned = sv.align_series(pd.concat(parts), sv.CALENDAR)
        env = sv.daily_envelope(aligned)
        assert not env.empty
        assert (env["p10"] <= env["p50"]).all()
        assert (env["p50"] <= env["p90"]).all()

    def test_days_in_fewer_than_three_years_dropped(self):
        aligned = sv.align_series(
            pd.concat([_daily("2023-01-01", "2023-01-10"),
                       _daily("2024-01-01", "2024-01-10")]), sv.CALENDAR)
        assert sv.daily_envelope(aligned).empty


# ---------------------------------------------------------------------------
# Figure composition
# ---------------------------------------------------------------------------

def _shard_series():
    parts = [_daily(f"{y}-01-01", f"{y}-12-31", value=50.0 + y - 2022)
             for y in (2022, 2023, 2024, 2025)]
    parts.append(_daily("2026-01-01", "2026-06-12", value=55.0))
    return pd.concat(parts)


@_needs_plotly
class TestFigure:
    def test_selected_years_drive_past_traces(self):
        shard = _shard_series()
        f2 = sv.season_figure(shard, None, None, alignment=sv.CALENDAR,
                              years=["2024", "2025"])
        f0 = sv.season_figure(shard, None, None, alignment=sv.CALENDAR,
                              years=[])
        assert len(f2.data) == len(f0.data) + 2

    def test_forecast_and_seasonal_traces_added(self):
        shard = _shard_series()
        fan = _daily("2026-06-13", "2026-07-27", value=55.1)
        seas = pd.Series([55.0, 54.8],
                         index=pd.to_datetime(["2026-07-15", "2026-08-15"]))
        fig = sv.season_figure(shard, fan, seas, alignment=sv.CALENDAR,
                               years=[])
        names = [t.name for t in fig.data if t.name]
        assert any(n.startswith("forecast") for n in names)
        assert any(n.startswith("seasonal") for n in names)
        assert any(n == "2026 (observed)" for n in names)

    def test_water_year_forecast_two_segments(self):
        shard = _shard_series()
        fan = _daily("2026-06-13", "2026-12-12", value=55.1)
        fig = sv.season_figure(shard, fan, None, alignment=sv.WATER, years=[])
        fc = [t.name for t in fig.data if t.name and t.name.startswith("forecast")]
        assert sorted(fc) == ["forecast (WY2026)", "forecast (WY2027)"]

    def test_empty_shard_tolerated(self):
        fig = sv.season_figure(pd.Series(dtype=float), None, None,
                               alignment=sv.CALENDAR, years=[],
                               today=pd.Timestamp("2026-06-12"))
        assert len(fig.data) == 0


class TestEnvelopeExcludesCurrentYear:
    def test_current_year_values_do_not_shape_the_band(self):
        # BUGS.md low: the in-progress year's own observations must not
        # contribute to the "historical P10-P90" band it is judged against.
        import pandas as pd
        from src.dashboard.season_view import align_series, daily_envelope, year_label
        idx = pd.date_range("2020-01-01", "2026-06-30", freq="D")
        vals = pd.Series(10.0, index=idx)
        vals[idx.year == 2026] = 99.0                # wild current-year excursion
        aligned = align_series(vals, "calendar")
        current = year_label(pd.Timestamp("2026-06-30"), "calendar")
        env_excl = daily_envelope(aligned, exclude_year=current)
        assert not env_excl.empty
        assert float(env_excl["p90"].max()) == 10.0  # excursion excluded
        env_incl = daily_envelope(aligned)           # legacy behaviour intact
        assert float(env_incl["p90"].max()) > 10.0

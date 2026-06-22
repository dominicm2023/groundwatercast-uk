"""Unit tests for the forward-outlook view (pure parts only)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.dashboard import ensemble_view as ev


def _fan():
    dates = pd.date_range("2026-06-07", periods=14)
    return pd.DataFrame({
        "station_id": "s1", "lead": range(1, 15), "date": dates,
        "gw_p10": np.linspace(50, 51, 14),
        "gw_p50": np.linspace(50.2, 51.4, 14),
        "gw_p90": np.linspace(50.4, 51.8, 14),
    })


class TestFanFigure:
    def test_three_traces_and_threshold_line(self):
        row = pd.Series({"threshold": 51.0, "threshold_source": "user",
                         "horizon_days": 14, "p_breach": 0.3})
        fig = ev._fan_figure(_fan(), row)
        assert len(fig.data) == 3                       # band(2) + median
        assert any(s["type"] == "line" for s in fig.layout.shapes)  # hline

    def test_proxy_label_in_annotation(self):
        row = pd.Series({"threshold": 51.0, "threshold_source": "gw_p90_proxy",
                         "horizon_days": 14, "p_breach": 0.0})
        fig = ev._fan_figure(_fan(), row)
        texts = " ".join(a.text for a in fig.layout.annotations if a.text)
        assert "proxy" in texts

    def test_no_threshold_no_hline(self):
        row = pd.Series({"threshold": np.nan, "threshold_source": "none",
                         "horizon_days": 14, "p_breach": np.nan})
        fig = ev._fan_figure(_fan(), row)
        assert len(fig.layout.shapes) == 0


class TestPastasOverlay:
    def test_roll_overlay_adds_fourth_trace(self):
        row = pd.Series({"threshold": 51.0, "threshold_source": "user",
                         "horizon_days": 14, "p_breach": 0.3})
        roll = np.linspace(49.5, 50.0, 14)
        fig = ev._fan_figure(_fan(), row, roll_p50=pd.Series(roll))
        assert len(fig.data) == 4                       # band(2) + median + roll
        assert any((s.name or "").startswith("roll") for s in fig.data)

    def test_no_roll_overlay_keeps_three(self):
        row = pd.Series({"threshold": np.nan, "threshold_source": "none",
                         "horizon_days": 14, "p_breach": np.nan})
        assert len(ev._fan_figure(_fan(), row, roll_p50=None).data) == 3

    def test_pastas_fan_builds_from_written_csv(self):
        """Smoke: the real pastas fan CSV (if present) renders with the roll
        overlay and the model-spread column the pipeline emits."""
        psum, pfan = ev.load_pastas()
        if psum.empty or pfan.empty:
            pytest.skip("no pastas artefacts on disk")
        sid = psum.iloc[0]["station_id"]
        fsub = pfan[pfan["station_id"] == sid].sort_values("lead")
        assert {"gw_p10", "gw_p50", "gw_p90", "roll_p50"}.issubset(fsub.columns)
        fig = ev._fan_figure(fsub, psum.iloc[0], roll_p50=fsub["roll_p50"])
        assert len(fig.data) == 4
        assert "stale_days" in psum.columns      # honesty flag plumbed through


class TestEdgeHonestFormatting:
    """A few MC samples crossing must not render as the contradiction
    "0% breach ... median first crossing 16 Jul ... 100% never cross"."""

    def test_pct_never_rounds_small_to_zero(self):
        assert ev._pct(0.0004) == "<1%"
        assert ev._pct(0.9996) == ">99%"
        assert ev._pct(0.0) == "0%"
        assert ev._pct(1.0) == "100%"
        assert ev._pct(0.62) == "62%"
        assert ev._pct(float("nan")) == "—"

    def test_first_cross_suppressed_below_noise_floor(self):
        row = pd.Series({"p_breach": 0.0004,
                         "first_cross_median": pd.Timestamp("2026-07-16")})
        assert ev._first_cross_value(row) == "—"
        row["p_breach"] = 0.25
        assert ev._first_cross_value(row) == "16 Jul"

    def test_dual_window_label_uses_edge_format(self):
        row = pd.Series({"horizon_days": 45, "p_breach": 0.0004,
                         "p_breach_14d": 0.0})
        label, value, help_text = ev._breach_label_value(row)
        assert label == "Breach prob (14 d)"     # short → never truncates
        assert value == "0%"
        assert "<1%" in help_text                # full horizon in the tooltip
        assert "Full 45-day horizon: <1%" in ev._horizon_caption(row)


class TestSeasonalExtension:
    def _seasonal(self):
        return pd.DataFrame({
            "month_start": pd.to_datetime(["2026-07-01", "2026-08-01"]),
            "gw_p10": [49.8, 49.6], "gw_p50": [50.0, 49.9],
            "gw_p90": [50.2, 50.1],
        })

    def _row(self):
        return pd.Series({"threshold": np.nan, "threshold_source": "none",
                          "horizon_days": 14, "p_breach": np.nan})

    def test_adds_three_traces_at_month_midpoints(self):
        base = ev.stitched_figure(_fan(), self._row(), None)
        fig = ev.stitched_figure(_fan(), self._row(), None,
                                 seasonal=self._seasonal())
        assert len(fig.data) == len(base.data) + 3
        median = next(t for t in fig.data
                      if t.name == "seasonal median (monthly)")
        assert pd.Timestamp(median.x[0]) == pd.Timestamp("2026-07-15")

    def test_absent_or_empty_seasonal_changes_nothing(self):
        base = ev.stitched_figure(_fan(), self._row(), None)
        for s in (None, pd.DataFrame(),
                  self._seasonal().assign(gw_p50=np.nan)):
            fig = ev.stitched_figure(_fan(), self._row(), None, seasonal=s)
            assert len(fig.data) == len(base.data)

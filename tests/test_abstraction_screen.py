"""Abstraction screen (roadmap H7): amplitude-isolation math, classification, and
a synthetic pumped-vs-natural discrimination check. All offline/pure.

We have no confirmed abstraction site to calibrate against yet, so the end-to-end
test validates the *metric* on controlled inputs: a borehole with an extra summer
drawdown (a pumping-like signal) must read a markedly larger seasonal amplitude
than otherwise-identical natural-recession neighbours, and so be flagged ``excess``;
a borehole that swings like its neighbours must not be.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.diagnostics import abstraction_screen as ab
from src.diagnostics.trend_screen import screen_series

CFG = dict(min_years=3.0, min_obs=730, min_amp_m=0.3,
           amp_ratio_min=2.5, amp_ratio_med=3.0, amp_ratio_high=4.0,
           neighbour=dict(min_neighbours=2, radius_km=25.0,
                          require_same_aquifer_class=True))


# ---------------------------------------------------------------------------
# amplitude_isolation
# ---------------------------------------------------------------------------

class TestAmplitudeIsolation:
    def test_excess_when_swing_far_exceeds_neighbours(self):
        r = ab.amplitude_isolation(1.0, [0.2, 0.25, 0.3], CFG)
        assert r["amplitude_isolation_class"] == "excess"
        assert r["amp_ratio"] == 4.0
        assert r["neighbour_count"] == 3

    def test_regional_when_comparable_to_neighbours(self):
        r = ab.amplitude_isolation(0.30, [0.28, 0.30, 0.32], CFG)
        assert r["amplitude_isolation_class"] == "regional"

    def test_muted_when_smaller_than_neighbours(self):
        r = ab.amplitude_isolation(0.10, [0.30, 0.30, 0.30], CFG)
        assert r["amplitude_isolation_class"] == "muted"

    def test_no_neighbours_below_min(self):
        r = ab.amplitude_isolation(1.0, [0.2], CFG)
        assert r["amplitude_isolation_class"] == "no_neighbours"

    def test_high_ratio_but_tiny_absolute_swing_not_excess(self):
        # ratio is 4× but the swing is sensor noise (< min_amp_m) — don't flag.
        r = ab.amplitude_isolation(0.20, [0.04, 0.05, 0.06], CFG)
        assert r["amplitude_isolation_class"] != "excess"


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

class TestClassify:
    def _m(self, cls, ratio, years=6.0):
        return dict(amplitude_isolation_class=cls, amp_ratio=ratio, record_years=years)

    def test_excess_severity_scales_with_ratio(self):
        assert ab.classify(self._m("excess", 4.5), CFG)["severity"] == "high"
        assert ab.classify(self._m("excess", 3.2), CFG)["severity"] == "medium"
        assert ab.classify(self._m("excess", 2.6), CFG)["severity"] == "low"

    def test_excess_flags_metadata_check_never_exclude(self):
        c = ab.classify(self._m("excess", 4.5), CFG)
        assert c["provenance_class"] == "abstraction_suspect"
        assert c["recommended_action"] == "metadata_check"  # never review_exclude

    def test_short_record_not_flagged(self):
        c = ab.classify(self._m("excess", 4.5, years=1.0), CFG)
        assert c["severity"] == "none"

    def test_regional_not_flagged(self):
        c = ab.classify(self._m("regional", 1.1), CFG)
        assert c["severity"] == "none"
        assert c["recommended_action"] == "none"


# ---------------------------------------------------------------------------
# Synthetic end-to-end discrimination
# ---------------------------------------------------------------------------

def _daily(years, amp, summer_drawdown=0.0, seed=0):
    """Daily GW series: a natural seasonal sinusoid (amplitude `amp`) plus, if
    `summer_drawdown` > 0, an extra Jun–Sep trough emulating heavy abstraction."""
    idx = pd.date_range("2015-01-01", periods=int(365 * years), freq="D")
    doy = idx.dayofyear.to_numpy(float)
    natural = amp * np.sin(2 * np.pi * doy / 365.25)
    pump = np.where((idx.month >= 6) & (idx.month <= 9), -summer_drawdown, 0.0)
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.01, len(idx))
    return pd.Series(50.0 + natural + pump + noise, index=idx)


class TestSyntheticDiscrimination:
    def test_pumped_site_reads_larger_amplitude_and_flags_excess(self):
        natural_amps = [
            screen_series(_daily(6, 0.20, seed=s), None, CFG)["seasonal_amp_m"]
            for s in range(4)
        ]
        pumped_amp = screen_series(
            _daily(6, 0.20, summer_drawdown=1.0, seed=99), None, CFG)["seasonal_amp_m"]

        # the pumping trough clearly inflates the seasonal swing
        assert pumped_amp > 2.5 * np.median(natural_amps)

        # ...and the screen flags it as excess vs its natural-amplitude neighbours
        iso = ab.amplitude_isolation(pumped_amp, natural_amps, CFG)
        assert iso["amplitude_isolation_class"] == "excess"
        assert ab.classify({**iso, "record_years": 6.0}, CFG)["severity"] in {"medium", "high"}

    def test_natural_site_among_natural_neighbours_not_flagged(self):
        amps = [screen_series(_daily(6, 0.20, seed=s), None, CFG)["seasonal_amp_m"]
                for s in range(5)]
        subject, neighbours = amps[0], amps[1:]
        iso = ab.amplitude_isolation(subject, neighbours, CFG)
        assert iso["amplitude_isolation_class"] != "excess"
        assert ab.classify({**iso, "record_years": 6.0}, CFG)["severity"] == "none"

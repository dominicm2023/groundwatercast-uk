"""Extended-range forecast: EC46 provider parsing, the ENS+EC46 splice, and
the dual-window breach stats.

Offline — provider HTTP calls are faked; payload shapes mirror a live probe
of the seasonal API (2026-06-12).
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.forecast.ensemble import aggregate
from src.forecast.ensemble.open_meteo_ec46 import OpenMeteoEC46, _member_index
from src.forecast.ensemble.provider import EnsembleRainfallProvider
from src.forecast.ensemble.splice import SplicedEnsemble


# ---------------------------------------------------------------------------
# EC46 payload parsing
# ---------------------------------------------------------------------------

def _ec46_payload(start: date, days: int, members: int = 3) -> dict:
    times = [d.strftime("%Y-%m-%d")
             for d in pd.date_range(start, periods=days, freq="D")]
    daily = {"time": times, "precipitation_sum": [1.0] * days}
    for m in range(1, members):
        vals = [float(m)] * days
        if m == members - 1:
            vals[-1] = None          # ragged final day, as seen in the live probe
        daily[f"precipitation_sum_member{m:02d}"] = vals
    return {"daily": daily}


def test_member_index_vocabulary():
    assert _member_index("precipitation_sum") == 0
    assert _member_index("precipitation_sum_member07") == 7
    assert _member_index("precipitation_sum_member50") == 50


def test_ec46_parse_shape_and_ragged_tail():
    df = OpenMeteoEC46._parse(_ec46_payload(date(2026, 6, 12), days=46))
    out = EnsembleRainfallProvider._validate(df)
    assert set(out.columns) == {"member", "date", "precip_mm"}
    assert out[out["member"] == 0]["date"].nunique() == 46
    assert out[out["member"] == 1]["date"].nunique() == 46
    # ragged member: NaN final day dropped by _validate
    assert out[out["member"] == 2]["date"].nunique() == 45


def test_ec46_parse_empty_payload():
    df = OpenMeteoEC46._parse({"daily": {}})
    assert df.empty


# ---------------------------------------------------------------------------
# Splice
# ---------------------------------------------------------------------------

class _Fake(EnsembleRainfallProvider):
    """Constant-value provider over `days` days for `members` members."""

    def __init__(self, name, value, days, members=2, fail=False):
        super().__init__(cache_root="unused")
        self.name = name
        self.value, self.days, self.members, self.fail = value, days, members, fail

    def fetch(self, lat, lon, start, horizon_days):
        if self.fail:
            raise RuntimeError("boom")
        days = min(self.days, int(horizon_days))
        dates = pd.date_range(start, periods=days, freq="D")
        rows = [{"member": m, "date": d, "precip_mm": self.value}
                for m in range(self.members) for d in dates]
        return self._validate(pd.DataFrame(rows))


def test_splice_day_counts_and_values():
    sp = SplicedEnsemble(_Fake("ens", 1.0, days=15),
                         _Fake("ec46", 9.0, days=46), splice_day=15)
    out = sp.fetch(51.0, -1.3, date(2026, 6, 12), 46)
    per_member = out[out["member"] == 0].sort_values("date")
    assert len(per_member) == 46                       # 15 + 31
    assert (per_member["precip_mm"].iloc[:15] == 1.0).all()    # primary head
    assert (per_member["precip_mm"].iloc[15:] == 9.0).all()    # extension tail
    assert out["member"].nunique() == 2


def test_splice_short_horizon_skips_extension():
    ext = _Fake("ec46", 9.0, days=46, fail=True)       # would raise if called
    sp = SplicedEnsemble(_Fake("ens", 1.0, days=15), ext, splice_day=15)
    out = sp.fetch(51.0, -1.3, date(2026, 6, 12), 14)
    assert out["date"].nunique() == 14
    assert (out["precip_mm"] == 1.0).all()


def test_splice_degrades_loudly_on_extension_failure(capsys):
    sp = SplicedEnsemble(_Fake("ens", 1.0, days=15),
                         _Fake("ec46", 9.0, days=46, fail=True), splice_day=15)
    out = sp.fetch(51.0, -1.3, date(2026, 6, 12), 46)
    assert out["date"].nunique() == 15                 # primary only
    assert "WARNING" in capsys.readouterr().out


class _Empty(EnsembleRainfallProvider):
    """Primary that returns no data (e.g. stale run / API hiccup) without raising."""

    def __init__(self, name="ens"):
        super().__init__(cache_root="unused")
        self.name = name

    def fetch(self, lat, lon, start, horizon_days):
        return self._validate(
            pd.DataFrame(columns=["member", "date", "precip_mm"]))


def test_splice_empty_primary_degrades_loudly(capsys):
    # Primary empty + extension OK must WARN (not silently serve EC46 as ENS).
    sp = SplicedEnsemble(_Empty("ens"),
                         _Fake("ec46", 9.0, days=46), splice_day=15)
    out = sp.fetch(51.0, -1.3, date(2026, 6, 12), 46)
    assert "WARNING" in capsys.readouterr().out
    assert not out.empty                               # a forecast still returned


def test_splice_drops_extension_members_without_a_head():
    sp = SplicedEnsemble(_Fake("ens", 1.0, days=15, members=2),
                         _Fake("ec46", 9.0, days=46, members=5), splice_day=15)
    out = sp.fetch(51.0, -1.3, date(2026, 6, 12), 46)
    assert set(out["member"].unique()) == {0, 1}


# ---------------------------------------------------------------------------
# Dual-window breach stats
# ---------------------------------------------------------------------------

def _traj(member_paths: dict[int, list[float]], start="2026-06-12"):
    dates = pd.date_range(start, periods=len(next(iter(member_paths.values()))))
    rows = [{"station_id": "s1", "member": m, "date": d, "gw_pred": gw}
            for m, path in member_paths.items() for d, gw in zip(dates, path)]
    return pd.DataFrame(rows)


def test_breach_46d_counts_late_cross_but_14d_does_not():
    # member 0 crosses on day 20; member 1 never crosses (46-day paths)
    p0 = [5.0] * 19 + [11.0] + [5.0] * 26
    p1 = [5.0] * 46
    s = aggregate.breach_stats(_traj({0: p0, 1: p1}), threshold=10.0)
    assert s["horizon_days"] == 46
    assert s["p_breach"] == pytest.approx(0.5)
    assert s["p_breach_14d"] == pytest.approx(0.0)
    assert s["first_cross_median_lead"] == pytest.approx(20.0)


def test_breach_windows_identical_at_14d_horizon():
    p0 = [5.0] * 5 + [11.0] + [5.0] * 8                 # crosses day 6 of 14
    s = aggregate.breach_stats(_traj({0: p0, 1: [5.0] * 14}), threshold=10.0)
    assert s["p_breach"] == s["p_breach_14d"] == pytest.approx(0.5)


def test_triage_tiers_on_operational_window():
    """A late (day-20) crossing inflates p_breach but must not raise the tier."""
    from src.dashboard.forecast_outlook import build_pastas_triage
    cat = pd.DataFrame({"station_id": ["a"], "station_name": ["A"],
                        "lat": [51.0], "lon": [-1.0], "aquifer_name": ["Chalk"]})
    summary = pd.DataFrame([{
        "station_id": "a", "p_breach": 0.60, "p_breach_14d": 0.05,
        "p_risk_high": 0.0, "stale_days": 1.0,
        "threshold_source": "user", "horizon_days": 46,
        "first_cross_median_lead": 20.0,
    }])
    out = build_pastas_triage(summary, cat, pinned_ids=set())
    # 60% over 46 d would read BREACH_LIKELY; 5% over 14 d is WATCH.
    assert out.iloc[0]["tier"] == "WATCH"

    legacy = summary.drop(columns=["p_breach_14d"])     # pre-extension artifact
    out2 = build_pastas_triage(legacy, cat, pinned_ids=set())
    assert out2.iloc[0]["tier"] == "BREACH_LIKELY"      # tolerant fallback

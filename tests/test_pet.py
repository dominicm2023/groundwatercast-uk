"""Offline unit tests for PET (ET0) ingestion (src/data/pet.py).

No network: the Open-Meteo archive HTTP call is monkeypatched with a synthetic
payload. The live end-to-end check is run manually against the archive API.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

pytest.importorskip("requests")            # main-env test (PET fetch uses requests)

from src.data import pet


def _payload(start: date, vals: list[float]) -> dict:
    """Open-Meteo archive-shaped daily payload for et0_fao_evapotranspiration."""
    days = pd.date_range(start, periods=len(vals), freq="D")
    return {"daily": {
        "time": [d.strftime("%Y-%m-%d") for d in days],
        "et0_fao_evapotranspiration": vals,
    }}


class _FakeResp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


# ---------------------------------------------------------------------------
# et0_archive_daily — payload parsing
# ---------------------------------------------------------------------------

def test_archive_daily_parses_series(monkeypatch):
    monkeypatch.setattr(pet.requests, "get",
                        lambda *a, **k: _FakeResp(_payload(date(2023, 1, 1),
                                                           [0.5, 0.7, 1.1])))
    s = pet.et0_archive_daily(51.3, -1.5, date(2023, 1, 1), date(2023, 1, 3))
    assert s.name == "et0_mm"
    assert len(s) == 3
    assert s.iloc[0] == 0.5 and s.iloc[-1] == 1.1


# ---------------------------------------------------------------------------
# fetch_station_pet — caching / idempotency
# ---------------------------------------------------------------------------

def test_fetch_writes_cache_then_reuses(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _FakeResp(_payload(date(2023, 1, 1), [1.0, 2.0, 3.0]))

    monkeypatch.setattr(pet.requests, "get", fake_get)

    s1 = pet.fetch_station_pet("BH1", 51.3, -1.5, date(2023, 1, 1),
                               date(2023, 1, 3), cache_root=tmp_path)
    assert len(s1) == 3 and calls["n"] == 1
    assert (tmp_path / "BH1.csv").exists()

    # Second identical call: fully cached → no further network call.
    s2 = pet.fetch_station_pet("BH1", 51.3, -1.5, date(2023, 1, 1),
                               date(2023, 1, 3), cache_root=tmp_path)
    assert calls["n"] == 1
    pd.testing.assert_series_equal(s1, s2)


def test_fetch_only_requests_missing_dates(monkeypatch, tmp_path):
    """A second call extending the range fetches only the new tail, then the
    cache holds the union."""
    seen = {"start": None, "end": None}

    def fake_get(url, params, timeout):
        seen["start"], seen["end"] = params["start_date"], params["end_date"]
        # Return a value per requested day so the merge is exercised.
        days = pd.date_range(params["start_date"], params["end_date"], freq="D")
        return _FakeResp(_payload(days[0].date(), [9.0] * len(days)))

    monkeypatch.setattr(pet.requests, "get", fake_get)

    pet.fetch_station_pet("BH2", 51.3, -1.5, date(2023, 1, 1),
                          date(2023, 1, 2), cache_root=tmp_path)
    pet.fetch_station_pet("BH2", 51.3, -1.5, date(2023, 1, 1),
                          date(2023, 1, 4), cache_root=tmp_path)
    # Only the missing tail (Jan 3–4) should have been requested the 2nd time.
    assert seen["start"] == "2023-01-03" and seen["end"] == "2023-01-04"
    cached = pet.load_station_pet("BH2", cache_root=tmp_path)
    assert len(cached) == 4                      # union persisted


# ---------------------------------------------------------------------------
# effective_rainfall
# ---------------------------------------------------------------------------

def test_effective_rainfall_subtracts_and_floors():
    idx = pd.to_datetime(["2023-06-01", "2023-06-02", "2023-06-03"])
    rain = pd.Series([0.0, 5.0, 1.0], index=idx)
    et0 = pd.Series([2.0, 4.0, 3.0], index=idx)
    eff = pet.effective_rainfall(rain, et0)
    assert eff.name == "Rainfall_eff"
    assert eff.tolist() == [0.0, 1.0, 0.0]       # floored at 0, 5−4=1, 1−3→0


def test_effective_rainfall_treats_missing_et0_as_zero():
    """Rain dates with no ET0 are not dropped — ET0 defaults to 0 (no
    reduction), so the effective series keeps full length."""
    rain = pd.Series([3.0, 4.0],
                     index=pd.to_datetime(["2023-06-01", "2023-06-02"]))
    et0 = pd.Series([2.0], index=pd.to_datetime(["2023-06-01"]))
    eff = pet.effective_rainfall(rain, et0)
    assert len(eff) == 2
    assert eff.tolist() == [1.0, 4.0]            # day 2: no ET0 → unchanged


def test_archive_daily_retries_on_429(monkeypatch):
    """Free-tier rate limits (429) are retried with backoff, not fatal."""
    monkeypatch.setattr(pet.time, "sleep", lambda s: None)   # fast test
    calls = {"n": 0}

    class _Resp429(_FakeResp):
        status_code = 429
        def raise_for_status(self):
            raise AssertionError("should not raise on a retried 429")

    def fake_get(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return _Resp429({})
        return _FakeResp(_payload(date(2023, 1, 1), [0.5]))

    monkeypatch.setattr(pet.requests, "get", fake_get)
    s = pet.et0_archive_daily(51.3, -1.5, date(2023, 1, 1), date(2023, 1, 1))
    assert calls["n"] == 3
    assert len(s) == 1


# ---------------------------------------------------------------------------
# resilience — the 2026-07-17 outage class (timeouts must not kill the chain)
# ---------------------------------------------------------------------------

def test_archive_daily_retries_transient_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky_get(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise pet.requests.exceptions.ReadTimeout("read timed out")
        return _FakeResp(_payload(date(2023, 1, 1), [0.4, 0.6]))

    monkeypatch.setattr(pet.requests, "get", flaky_get)
    monkeypatch.setattr(pet.time, "sleep", lambda s: None)
    s = pet.et0_archive_daily(51.3, -1.5, date(2023, 1, 1), date(2023, 1, 2))
    assert calls["n"] == 2 and len(s) == 2


def test_archive_daily_raises_after_exhausted_retries(monkeypatch):
    def dead_get(*a, **k):
        raise pet.requests.exceptions.ConnectTimeout("handshake timed out")

    monkeypatch.setattr(pet.requests, "get", dead_get)
    monkeypatch.setattr(pet.time, "sleep", lambda s: None)
    with pytest.raises(pet.requests.exceptions.ConnectTimeout):
        pet.et0_archive_daily(51.3, -1.5, date(2023, 1, 1), date(2023, 1, 2))


def test_fetch_serves_cached_tail_when_archive_down(monkeypatch, tmp_path):
    # Seed the cache with 3 days, then ask for 5 with the archive dead: the
    # cached tail comes back (2 days short), no exception — the flow chain's
    # daily stage must degrade, not die.
    ok_payload = _payload(date(2023, 1, 1), [0.5, 0.6, 0.7])
    monkeypatch.setattr(pet.requests, "get", lambda *a, **k: _FakeResp(ok_payload))
    monkeypatch.setattr(pet.time, "sleep", lambda s: None)
    seeded = pet.fetch_station_pet("bh1", 51.3, -1.5,
                                   date(2023, 1, 1), date(2023, 1, 3),
                                   cache_root=tmp_path)
    assert len(seeded) == 3

    def dead_get(*a, **k):
        raise pet.requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(pet.requests, "get", dead_get)
    s = pet.fetch_station_pet("bh1", 51.3, -1.5,
                              date(2023, 1, 1), date(2023, 1, 5),
                              cache_root=tmp_path)
    assert len(s) == 3                        # the cached days, tail short
    assert s.index.max() == pd.Timestamp("2023-01-03")


def test_fetch_raises_when_archive_down_and_no_cache(monkeypatch, tmp_path):
    def dead_get(*a, **k):
        raise pet.requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr(pet.requests, "get", dead_get)
    monkeypatch.setattr(pet.time, "sleep", lambda s: None)
    with pytest.raises(pet.requests.exceptions.ReadTimeout):
        pet.fetch_station_pet("fresh", 51.3, -1.5,
                              date(2023, 1, 1), date(2023, 1, 5),
                              cache_root=tmp_path)

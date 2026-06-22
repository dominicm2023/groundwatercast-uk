"""Offline unit tests for the ensemble provider layer (Phase 1).

No network: the Open-Meteo HTTP call is monkeypatched with a synthetic payload.
The live end-to-end check lives in scripts/smoke_test_ensemble.py.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from src.forecast.ensemble import get_provider, OUTPUT_COLUMNS
from src.forecast.ensemble.provider import EnsembleRainfallProvider
from src.forecast.ensemble.open_meteo import OpenMeteoEnsemble, _member_index


def _synthetic_payload(n_members=3, n_days=2):
    """Open-Meteo-shaped hourly payload: member m rains (m+1) mm/h on day 0,
    0 mm/h on day 1 — so day-0 daily total = 24*(m+1), day-1 total = 0."""
    hours = pd.date_range("2026-06-07T00:00", periods=24 * n_days, freq="h")
    hourly = {"time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours]}
    for m in range(n_members):
        field = "precipitation" if m == 0 else f"precipitation_member{m:02d}"
        per_hour = [(m + 1) if h.normalize() == hours[0].normalize() else 0.0
                    for h in hours]
        hourly[field] = per_hour
    return {"hourly": hourly}


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


# ---------------------------------------------------------------------------
# _validate contract
# ---------------------------------------------------------------------------

class TestValidate:
    def test_coerces_and_sorts(self):
        raw = pd.DataFrame({
            "member": ["2", "0", "1"],
            "date": ["2026-06-08", "2026-06-07", "2026-06-07"],
            "precip_mm": [3.0, 1.0, 2.0],
            "extra": ["x", "y", "z"],
        })
        out = EnsembleRainfallProvider._validate(raw)
        assert list(out.columns) == OUTPUT_COLUMNS          # extra dropped
        assert out["member"].tolist() == [0, 1, 2]          # sorted, int
        assert str(out["date"].dtype).startswith("datetime64")

    def test_clamps_negative_precip(self):
        raw = pd.DataFrame({"member": [0], "date": ["2026-06-07"],
                            "precip_mm": [-0.3]})
        out = EnsembleRainfallProvider._validate(raw)
        assert (out["precip_mm"] >= 0).all()

    def test_missing_column_raises(self):
        raw = pd.DataFrame({"member": [0], "date": ["2026-06-07"]})
        with pytest.raises(ValueError):
            EnsembleRainfallProvider._validate(raw)


# ---------------------------------------------------------------------------
# Open-Meteo parsing
# ---------------------------------------------------------------------------

class TestOpenMeteoParse:
    def test_member_index_mapping(self):
        assert _member_index("precipitation") == 0
        assert _member_index("precipitation_member01") == 1
        assert _member_index("precipitation_member50") == 50

    def test_hourly_summed_to_daily(self):
        df = OpenMeteoEnsemble._parse(_synthetic_payload(n_members=3, n_days=2))
        # member m -> day0 total = 24*(m+1), day1 total = 0
        d0 = df[df["date"] == pd.Timestamp("2026-06-07")].set_index("member")["precip_mm"]
        assert d0[0] == pytest.approx(24.0)
        assert d0[1] == pytest.approx(48.0)
        assert d0[2] == pytest.approx(72.0)
        d1 = df[df["date"] == pd.Timestamp("2026-06-08")]["precip_mm"]
        assert (d1 == 0).all()

    def test_empty_payload(self):
        df = OpenMeteoEnsemble._parse({"hourly": {}})
        assert df.empty


# ---------------------------------------------------------------------------
# fetch() — caching + contract (monkeypatched network)
# ---------------------------------------------------------------------------

class TestFetchCaching:
    def test_fetch_caches_and_validates(self, tmp_path, monkeypatch):
        payload = _synthetic_payload(n_members=3, n_days=2)
        monkeypatch.setattr("src.forecast.ensemble.open_meteo.requests.get",
                            lambda *a, **k: _FakeResp(payload))
        prov = OpenMeteoEnsemble(cache_root=tmp_path)
        out = prov.fetch(lat=51.0, lon=-1.3, start=date(2026, 6, 7), horizon_days=2)

        assert list(out.columns) == OUTPUT_COLUMNS
        assert out["member"].nunique() == 3
        assert out["date"].nunique() == 2
        # raw payload cached for audit
        cached = list(tmp_path.joinpath("open_meteo").rglob("*.json"))
        assert len(cached) == 1

    def test_fetch_filters_to_start(self, tmp_path, monkeypatch):
        payload = _synthetic_payload(n_members=2, n_days=2)
        monkeypatch.setattr("src.forecast.ensemble.open_meteo.requests.get",
                            lambda *a, **k: _FakeResp(payload))
        prov = OpenMeteoEnsemble(cache_root=tmp_path)
        out = prov.fetch(lat=51.0, lon=-1.3, start=date(2026, 6, 8), horizon_days=2)
        assert out["date"].min() == pd.Timestamp("2026-06-08")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class TestFactory:
    def test_open_meteo(self, tmp_path):
        p = get_provider("open_meteo", cache_root=tmp_path)
        assert p.name == "open_meteo"

    def test_ecmwf_opendata(self, tmp_path):
        p = get_provider("ecmwf_opendata", cache_root=tmp_path)
        assert p.name == "ecmwf_opendata"

    def test_mogreps_not_implemented(self, tmp_path):
        with pytest.raises(NotImplementedError):
            get_provider("mogreps", cache_root=tmp_path)

    def test_unknown_raises(self, tmp_path):
        with pytest.raises(ValueError):
            get_provider("nope", cache_root=tmp_path)


# ---------------------------------------------------------------------------
# ECMWF provider degrades clearly without the GRIB stack
# ---------------------------------------------------------------------------

class TestEcmwfGuarded:
    def test_fetch_without_grib_stack_raises_import_error(self, tmp_path):
        pytest.importorskip  # noqa
        try:
            import ecmwf.opendata  # noqa: F401
            has_stack = True
        except ImportError:
            has_stack = False
        if has_stack:
            pytest.skip("ecmwf-opendata installed; guard path not exercised")
        prov = get_provider("ecmwf_opendata", cache_root=tmp_path)
        with pytest.raises(ImportError) as exc:
            prov.fetch(lat=51.0, lon=-1.3, start=date(2026, 6, 7), horizon_days=2)
        assert "open_meteo" in str(exc.value)  # points to the dev fallback

"""Offline unit tests for the ensemble provider layer (Phase 1).

No network: the Open-Meteo HTTP call is monkeypatched with a synthetic payload.
The live end-to-end check lives in scripts/smoke_test_ensemble.py.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Cycle-cache pruning (retention) — src/forecast/ensemble/provider.py
# ---------------------------------------------------------------------------

def _make_cycle_dir(cache_root, provider_name, dirname, *, payload_bytes=1000):
    d = cache_root / provider_name / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "payload.bin").write_bytes(b"x" * payload_bytes)
    return d


def _cycle_name(days_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y%m%d%H")


class TestPruneOldCycles:
    def test_prunes_old_keeps_new_and_malformed(self, tmp_path):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        old_dir = _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(10),
                                  payload_bytes=2_000_000)
        new_dir = _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(0))
        malformed_dir = _make_cycle_dir(tmp_path, "open_meteo", "notacycle")
        # matches the 10-digit shape but not a real date/time (month 99)
        bad_date_dir = _make_cycle_dir(tmp_path, "open_meteo", "9999999999")
        stray_file = tmp_path / "open_meteo" / "stray.txt"
        stray_file.write_text("not a dir")

        pruned, freed = prov.prune_old_cycles(retention_days=7)

        assert pruned == 1
        assert freed == pytest.approx(2_000_000, rel=0.01)
        assert not old_dir.exists()
        assert new_dir.exists()
        assert malformed_dir.exists()
        assert bad_date_dir.exists()
        assert stray_file.exists()

    def test_retention_zero_disables_pruning(self, tmp_path):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        old_dir = _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(100))

        pruned, freed = prov.prune_old_cycles(retention_days=0)

        assert (pruned, freed) == (0, 0)
        assert old_dir.exists()

    def test_retention_negative_disables_pruning(self, tmp_path):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        old_dir = _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(100))

        pruned, freed = prov.prune_old_cycles(retention_days=-1)

        assert (pruned, freed) == (0, 0)
        assert old_dir.exists()

    def test_boundary_exactly_retention_days_old_is_pruned(self, tmp_path):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        boundary_dir = _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(7))

        pruned, _ = prov.prune_old_cycles(retention_days=7)

        assert pruned == 1
        assert not boundary_dir.exists()

    def test_no_cache_dir_yet_is_a_noop(self, tmp_path):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        pruned, freed = prov.prune_old_cycles(retention_days=7)
        assert (pruned, freed) == (0, 0)

    def test_safe_wrapper_swallows_prune_errors(self, tmp_path, monkeypatch, capsys):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(30))

        def _boom(_path):
            raise OSError("disk gremlins")
        monkeypatch.setattr("src.forecast.ensemble.provider.shutil.rmtree", _boom)

        pruned, freed = prov.prune_old_cycles_safe(retention_days=7)

        assert (pruned, freed) == (0, 0)          # never raises
        out = capsys.readouterr().out
        assert "ensemble cache prune failed" in out

    def test_safe_wrapper_logs_pruned_summary(self, tmp_path, capsys):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(10), payload_bytes=500_000)
        _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(0))

        pruned, freed = prov.prune_old_cycles_safe(retention_days=7)

        assert pruned == 1
        out = capsys.readouterr().out
        assert "pruned 1 ensemble cycle dir(s) older than 7 days" in out
        assert "freed ~" in out and "MB" in out

    def test_safe_wrapper_silent_when_disabled(self, tmp_path, capsys):
        prov = get_provider("open_meteo", cache_root=tmp_path)
        _make_cycle_dir(tmp_path, "open_meteo", _cycle_name(100))

        pruned, freed = prov.prune_old_cycles_safe(retention_days=0)

        assert (pruned, freed) == (0, 0)
        assert capsys.readouterr().out == ""

    def test_ecmwf_download_prunes_after_fresh_fetch(self, tmp_path, monkeypatch):
        """ECMWFOpenDataENS._download prunes once per fresh download (not on
        a cache hit) using its own cache_retention_days."""
        pytest.importorskip("ecmwf.opendata")
        from src.forecast.ensemble.ecmwf_opendata import ECMWFOpenDataENS

        prov = ECMWFOpenDataENS(cache_root=tmp_path, cache_retention_days=7)
        old_run = _cycle_name(30)
        _make_cycle_dir(tmp_path, "ecmwf_opendata", old_run)

        run_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        class _FakeClient:
            def retrieve(self, **kw):
                Path(kw["target"]).write_bytes(b"grib")

        monkeypatch.setattr(prov, "_client", lambda: _FakeClient())
        prov._download(run_dt, [0, 24])

        assert not (tmp_path / "ecmwf_opendata" / old_run).exists()

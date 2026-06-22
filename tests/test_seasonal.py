"""Seasonal outlook (months 1-6): ESP trace mechanics, tercile math, SEAS5
parsing/weighting, monthly normals, ERA5 precip cache. All offline/pure."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.forecast.seasonal import esp, normals, seas5


# ---------------------------------------------------------------------------
# esp: trace windows + anchors
# ---------------------------------------------------------------------------

class TestTraceWindows:
    def test_window_shape_and_start(self):
        w = esp.trace_windows(pd.Timestamp("2026-06-12"), [1995, 2012])
        assert set(w) == {1995, 2012}
        assert w[1995][0] == pd.Timestamp("1995-06-12")
        assert len(w[1995]) == esp.TRACE_DAYS

    def test_leap_origin_maps_to_feb28(self):
        w = esp.trace_windows(pd.Timestamp("2024-02-29"), [2023])
        assert w[2023][0] == pd.Timestamp("2023-02-28")

    def test_autumn_window_crosses_year_boundary(self):
        w = esp.trace_windows(pd.Timestamp("2026-10-01"), [2000])
        assert w[2000][-1].year == 2001


class TestMonthlyAnchors:
    def test_starts_next_calendar_month(self):
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=6)
        assert str(ps[0]) == "2026-07"
        assert str(ps[-1]) == "2026-12"
        assert len(ps) == 6

    def test_monthly_means_align(self):
        idx = pd.date_range("2026-07-01", "2026-08-31", freq="D")
        s = pd.Series([1.0] * 31 + [3.0] * 31, index=idx)
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=3)
        out = esp.monthly_means(s, ps)
        assert out[0] == pytest.approx(1.0)
        assert out[1] == pytest.approx(3.0)
        assert np.isnan(out[2])                       # September: no data


# ---------------------------------------------------------------------------
# esp: weights + mixture terciles + weighted quantiles
# ---------------------------------------------------------------------------

class TestTraceWeights:
    BOUNDS = np.array([[40.0, 60.0]] * 3)             # t1, t2 per month

    def test_uniform_without_seas5(self):
        w = esp.trace_weights({1995: np.array([10.0] * 3),
                               1996: np.array([90.0] * 3)}, None, self.BOUNDS)
        assert w == {1995: 0.5, 1996: 0.5}

    def test_wet_tilt_upweights_wet_traces(self):
        probs = np.array([[0.1, 0.2, 0.7]] * 3)       # SEAS5 says: wet likely
        w = esp.trace_weights({1995: np.array([10.0] * 3),     # dry trace
                               1996: np.array([90.0] * 3)},    # wet trace
                              probs, self.BOUNDS)
        assert w[1996] > w[1995]
        assert sum(w.values()) == pytest.approx(1.0)
        assert w[1996] / w[1995] == pytest.approx((0.7 / 0.1) ** 3)

    def test_flat_seas5_gives_uniform(self):
        probs = np.full((3, 3), 1 / 3)
        w = esp.trace_weights({1995: np.array([10.0] * 3),
                               1996: np.array([90.0] * 3)}, probs, self.BOUNDS)
        assert w[1995] == pytest.approx(w[1996])

    def test_zero_product_falls_back_to_uniform(self):
        probs = np.array([[0.0, 0.5, 0.5]] * 3)       # below impossible per SEAS5
        w = esp.trace_weights({1995: np.array([10.0] * 3)},   # the only trace: below
                              probs, self.BOUNDS)
        assert w == {1995: 1.0}


class TestMixtureTerciles:
    def test_probs_sum_to_one(self):
        pb, pn, pa = esp.weighted_tercile_probs(
            np.array([50.0, 55.0]), np.array([2.0, 2.0]),
            np.array([0.5, 0.5]), t1=48.0, t2=56.0)
        assert pb + pn + pa == pytest.approx(1.0, abs=1e-9)

    def test_high_mu_gives_above(self):
        pb, pn, pa = esp.weighted_tercile_probs(
            np.array([70.0]), np.array([1.0]), np.array([1.0]),
            t1=40.0, t2=60.0)
        assert pa > 0.99 and pb < 0.01

    def test_nan_traces_dropped(self):
        pb, pn, pa = esp.weighted_tercile_probs(
            np.array([np.nan, 70.0]), np.array([1.0, 1.0]),
            np.array([0.5, 0.5]), t1=40.0, t2=60.0)
        assert pa > 0.99

    def test_all_nan_gives_nan(self):
        out = esp.weighted_tercile_probs(np.array([np.nan]), np.array([1.0]),
                                         np.array([1.0]), 40.0, 60.0)
        assert all(np.isnan(x) for x in out)


def test_weighted_quantiles_median_of_skewed_weights():
    q10, q50, q90 = esp.weighted_quantiles(
        np.array([1.0, 2.0, 3.0]), np.array([0.05, 0.05, 0.90]))
    assert q50 == pytest.approx(3.0, abs=0.3)
    assert q10 < q50 <= q90


# ---------------------------------------------------------------------------
# seas5: payload parse + monthly totals + tercile probs
# ---------------------------------------------------------------------------

def _seas5_payload(start: date, days: int, members: int = 12) -> dict:
    times = [d.strftime("%Y-%m-%d")
             for d in pd.date_range(start, periods=days, freq="D")]
    daily = {"time": times, "precipitation_sum": [2.0] * days}
    for m in range(1, members):
        daily[f"precipitation_sum_member{m:02d}"] = [float(m % 5)] * days
    return {"daily": daily}


class TestSeas5:
    def test_member_daily_frame(self):
        df = seas5.member_daily_frame(_seas5_payload(date(2026, 7, 1), 62))
        assert set(df.columns) == {"member", "date", "precip_mm"}
        assert df["member"].nunique() == 12

    def test_monthly_totals_full_months_only(self):
        # 62 days from 1 Jul = full Jul + full Aug; Sep absent
        df = seas5.member_daily_frame(_seas5_payload(date(2026, 7, 1), 62))
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=3)
        totals = seas5.monthly_member_totals(df, ps)
        assert totals.loc[0, ps[0]] == pytest.approx(2.0 * 31)
        assert np.isnan(totals.loc[0, ps[2]])         # September not covered

    def test_tercile_probs_with_too_few_members_is_uniform(self):
        df = seas5.member_daily_frame(_seas5_payload(date(2026, 7, 1), 31,
                                                     members=3))
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=1)
        totals = seas5.monthly_member_totals(df, ps)
        probs = seas5.tercile_probs(totals, np.array([[10.0, 50.0]]))
        assert probs[0] == pytest.approx((1 / 3, 1 / 3, 1 / 3))

    def test_tercile_probs_classifies(self):
        df = seas5.member_daily_frame(_seas5_payload(date(2026, 7, 1), 31))
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=1)
        totals = seas5.monthly_member_totals(df, ps)
        # member totals are 31*{0..4 or 2}; bounds put everything below t1=200
        probs = seas5.tercile_probs(totals, np.array([[200.0, 400.0]]))
        assert probs[0][0] > 0.8                       # mostly "below"


class TestSeas5Cds:
    """CDS monthly SEAS5 source (W3) — same monthly_member_totals contract."""

    def test_tprate_to_monthly_total(self):
        # 1e-8 m/s over a 30-day month -> 1e-8 * 30*86400 * 1000 mm
        june = pd.Period("2026-06", freq="M")
        assert seas5.tprate_to_monthly_totals(1e-8, june) == pytest.approx(
            1e-8 * 30 * 86400 * 1000)

    def _ds(self, members=(0, 1, 2), fmonths=(1, 2, 3), ref="2026-06-01",
            rate=2e-8):
        xr = pytest.importorskip("xarray")
        lats = np.array([52.0, 51.5])
        lons = np.array([-1.5, -1.0])
        data = np.full((len(members), len(fmonths), len(lats), len(lons)), rate)
        return xr.Dataset(
            {"tprate": (("number", "forecastMonth", "latitude", "longitude"),
                        data)},
            coords={"number": list(members), "forecastMonth": list(fmonths),
                    "latitude": lats, "longitude": lons,
                    "forecast_reference_time": pd.Timestamp(ref)})

    def test_cds_member_period_totals_maps_leadtime_to_calendar(self):
        ds = self._ds(ref="2026-06-01", fmonths=(1, 2, 3))
        totals = seas5.cds_member_period_totals(ds, 51.6, -1.2)
        # forecastMonth 1 = the init month (June); 3 = August
        assert list(totals.columns) == [pd.Period("2026-06", freq="M"),
                                        pd.Period("2026-07", freq="M"),
                                        pd.Period("2026-08", freq="M")]
        assert list(totals.index) == [0, 1, 2]
        # June: 2e-8 * 30 days
        assert totals.iloc[0, 0] == pytest.approx(2e-8 * 30 * 86400 * 1000)

    def test_cds_cache_round_trip_and_tercile(self, tmp_path):
        ds = self._ds(members=tuple(range(20)), fmonths=(1, 2, 3),
                      ref="2026-06-01")
        points = {"BH1": (51.6, -1.2)}

        class _FakeClient:
            def retrieve(self, dataset, req, target):
                ds.to_netcdf(target)

        n = seas5.fetch_seas5_cds(date(2026, 6, 1), points, months=3,
                                  cache_root=tmp_path, client=_FakeClient())
        assert n == 1
        ps = [pd.Period("2026-06", freq="M"), pd.Period("2026-07", freq="M"),
              pd.Period("2026-08", freq="M")]
        totals = seas5.load_cds_totals("BH1", ps, cache_root=tmp_path)
        assert totals is not None
        assert list(totals.columns) == ps
        assert totals.shape == (20, 3)
        # all members identical rate -> degenerate, but tercile_probs runs
        probs = seas5.tercile_probs(totals, np.array([[10.0, 100.0],
                                                      [10.0, 100.0],
                                                      [10.0, 100.0]]))
        assert probs.shape == (3, 3)

    def test_load_cds_totals_missing_returns_none(self, tmp_path):
        ps = [pd.Period("2026-06", freq="M")]
        assert seas5.load_cds_totals("nope", ps, cache_root=tmp_path) is None


# ---------------------------------------------------------------------------
# normals
# ---------------------------------------------------------------------------

class TestNormals:
    def test_gw_monthly_normals_terciles(self):
        rows = []
        for year in range(2015, 2026):                # 11 years
            for d in pd.date_range(f"{year}-01-01", f"{year}-01-31"):
                rows.append({"dateTime": d, "station_id": "a",
                             "GW_Level": float(year - 2015)})
        out = normals.gw_monthly_normals(pd.DataFrame(rows))
        assert list(out.columns) == list(normals.NORMALS_COLS)
        jan = out[(out.station_id == "a") & (out.month == 1)].iloc[0]
        assert jan["n_years"] == 11
        # full quantile ladder (drives both terciles and the status layer)
        assert (jan["p10"] < jan["t1"] < jan["median"]
                < jan["t2"] < jan["p90"])

    def test_too_few_years_dropped(self):
        rows = [{"dateTime": pd.Timestamp(f"{y}-03-15"), "station_id": "a",
                 "GW_Level": 1.0} for y in (2024, 2025)]
        out = normals.gw_monthly_normals(pd.DataFrame(rows))
        assert out.empty

    def test_precip_clim_bounds(self):
        idx = pd.date_range("1991-01-01", "2025-12-31", freq="D")
        s = pd.Series(2.0, index=idx)
        ps = esp.monthly_anchors(pd.Timestamp("2026-06-12"), months=2)
        b = normals.precip_monthly_clim_bounds(s, ps)
        # constant rain: July total = 62, t1 == t2 == 62
        assert b[0][0] == pytest.approx(2.0 * 31)
        assert b.shape == (2, 2)


# ---------------------------------------------------------------------------
# era5_precip cache (offline — requests faked)
# ---------------------------------------------------------------------------

class TestEra5PrecipCache:
    def test_fetch_writes_then_reuses_cache(self, monkeypatch, tmp_path):
        import requests
        from src.data import era5_precip as ep
        calls = {"n": 0}

        class _Resp:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                days = pd.date_range("2023-01-01", periods=3, freq="D")
                return {"daily": {
                    "time": [d.strftime("%Y-%m-%d") for d in days],
                    "precipitation_sum": [1.0, 2.0, 3.0]}}

        def fake_get(*a, **k):
            calls["n"] += 1
            return _Resp()

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(ep.time, "sleep", lambda s: None)
        s1 = ep.fetch_station_precip("BH1", 51.0, -1.3, date(2023, 1, 1),
                                     date(2023, 1, 3), cache_root=tmp_path)
        assert len(s1) == 3 and calls["n"] == 1
        s2 = ep.fetch_station_precip("BH1", 51.0, -1.3, date(2023, 1, 1),
                                     date(2023, 1, 3), cache_root=tmp_path)
        assert calls["n"] == 1                        # fully cached
        pd.testing.assert_series_equal(s1, s2)
        assert len(ep.load_station_precip("BH1", cache_root=tmp_path)) == 3

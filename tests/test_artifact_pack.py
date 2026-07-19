"""Tests for the artifact-pack builder (src/publish/pack.py) and its
contract pins (src/publish/contract.py ↔ SUMMARY_COLS ↔ docs/artifact_contract.md).

Synthetic-input pack: station A in-scope (fan + seasonal + user threshold),
station B status-only, station C excluded — built into tmp_path with an
injected ``now`` so reruns are byte-identical.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.publish import contract as C
from src.publish.pack import PackInputs, build_pack, jround, iso_date, iso_utc

FIXED_NOW = "2026-06-12T18:00:00Z"
RUN = pd.Timestamp("2026-06-12T17:00:00+00:00")


def _shard(dirpath: Path, sid: str, days: int, end="2026-06-10", level=50.0):
    idx = pd.date_range(end=end, periods=days, freq="D")
    df = pd.DataFrame({"date": idx,
                       "GW_Level": level + np.linspace(0, 0.5, days)})
    df.to_parquet(dirpath / f"{sid}.parquet", index=False)


def _catalogue():
    return pd.DataFrame({
        "station_id": ["A", "B", "C"],
        "station_name": ["Alpha BH", "Beta BH", "Gamma BH"],
        "lat": [51.0, 51.1, 51.2],
        "lon": [-1.0, -1.1, -1.2],
        "measure_type": ["groundwater"] * 3,
        "aquifer_name": ["Chalk", "Greensand", "Chalk"],
        "aquifer_designation": ["Principal", "Secondary A", "Principal"],
    })


def _normals(sids=("A", "B", "C")):
    rows = []
    for sid in sids:
        for m in range(1, 13):
            rows.append({"station_id": sid, "month": m, "p10": 48.0,
                         "t1": 49.0, "median": 50.0, "t2": 51.0,
                         "p90": 52.0, "n_years": 10})
    return pd.DataFrame(rows)


def _summary():
    return pd.DataFrame([{
        "station_id": "A", "run": RUN, "scope": "live",
        "origin_date": "2026-06-10", "stale_days": 2, "horizon_days": 46,
        "threshold": 51.2345, "threshold_source": "user",
        "p_breach": 0.31234, "p_breach_14d": 0.12345,
        "first_cross_median": "2026-07-01", "first_cross_p25": np.nan,
        "first_cross_p75": "2026-07-10", "first_cross_median_lead": 19.0,
        "censored_frac": 0.1, "gw_p50_end": 50.45678,
        "p_above_p90_14d": 0.05678, "model_spread_mean": -0.3699,
        "n_members": 51, "n_samples": 4000,
        "headline": "Rising; breach possible within 3 weeks",
    }])


def _fan():
    rows = []
    for lead in range(1, 4):
        rows.append({"station_id": "A", "run": RUN, "lead": lead,
                     "date": pd.Timestamp("2026-06-10") + pd.Timedelta(days=lead),
                     "gw_p10": 50.1111, "gw_p50": 50.2222, "gw_p90": 50.3333,
                     "roll_p50": 50.2, "model_spread": 0.0222})
    return pd.DataFrame(rows)


def _seasonal():
    rows = []
    for m in range(1, 7):
        rows.append({"station_id": "A", "run": RUN,
                     "origin_date": "2026-06-12", "month_ahead": m,
                     "month_start": f"2026-{6 + m:02d}-01" if m <= 6 else None,
                     "p_below": 0.03093, "p_near": 0.89891, "p_above": 0.07016,
                     "gw_p10": 49.5, "gw_p50": 50.0, "gw_p90": 50.5,
                     "n_traces": 35, "seas5_weighted": True})
    return pd.DataFrame(rows)


def _trend_flags():
    # Station A flagged (step-shift artefact); B and C unflagged (absent).
    return pd.DataFrame([{
        "station_id": "A", "station_name": "Alpha BH", "severity": "high",
        "provenance_class": "artifact_like", "recommended_action": "review_exclude",
        "slope_sen_m_yr": 0.7012, "trend_change_m": 3.4567, "rain_corr": 0.1234,
        "isolation_class": "isolated", "neighbour_count": 2,
        "already_in_register": False,
    }])


def _fan_archive():
    """Three archived runs for A: an old closed one, the NEWEST closed one
    (2026-05-20, window ends 06-02 < FIXED_NOW), and an unclosed one whose
    window (06-08 → 06-21) straddles FIXED_NOW. Bands bracket the shard's
    ~50.49 level so every scored day lands in-band."""
    rows = []

    def _run(run, start):
        for i, d in enumerate(pd.date_range(start, periods=14, freq="D"), 1):
            rows.append({"station_id": "A", "run": pd.Timestamp(run),
                         "lead": i, "date": d,
                         "gw_p10": 50.0, "gw_p50": 50.5, "gw_p90": 51.0,
                         "roll_p50": np.nan, "model_spread": np.nan,
                         "segment": "forecast"})

    _run("2026-05-10T07:00:00Z", "2026-05-10")
    _run("2026-05-20T07:00:00Z", "2026-05-20")
    _run("2026-06-08T07:00:00Z", "2026-06-08")
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# RiverCast (flow) fixtures — Stage 7. "F1" is a winterbourne (dry every
# August) fed, per station_links.csv, by GW borehole "A" (the linked_boreholes
# inversion target). Seven years of daily record so gw_monthly_normals' "drop
# the in-progress calendar month" guard (real wall-clock, not FIXED_NOW) can
# never starve a month below MIN_YEARS=5.
# ---------------------------------------------------------------------------
FLOW_RUN = pd.Timestamp("2026-06-12T22:00:00+00:00")


def _flow_shard(dirpath, gid: str, end="2026-06-10", years: int = 7):
    idx = pd.date_range(end=end, periods=365 * years + 2, freq="D")
    doy = idx.dayofyear.to_numpy()
    base = 0.35 + 0.25 * np.cos(2 * np.pi * (doy - 30) / 365.0)
    vals = np.clip(base, 0.02, None)
    dry = (idx.month == 8) & (idx.day <= 15)             # dry every August
    vals = np.where(dry, 0.0, vals)
    df = pd.DataFrame({"date": idx, "Flow_m3s": vals,
                       "data_source": "flow_logged"})
    df.to_parquet(dirpath / f"{gid}.parquet", index=False)


def _flow_catalogue():
    return pd.DataFrame({
        "station_id": ["F1"], "station_name": ["Test Winterbourne"],
        "lat": [51.05], "lon": [-1.05], "river_name": ["Test Brook"],
        "catchment_name": ["Test"], "flow_measure_id": ["F1-flow-m-86400-m3s-qualified"],
        "record_start": ["2019-06-11"],
    })


def _flow_summary():
    return pd.DataFrame([{
        "gauge_id": "F1", "run": FLOW_RUN, "origin_date": "2026-06-10",
        "stale_days": 2, "horizon_days": 14, "q95_m3s": 0.05,
        "threshold_source": "q95_proxy",
        "p_below_q95": 0.234, "p_below_q95_14d": 0.234,
        "first_cross_median": "2026-06-20", "first_cross_p25": "2026-06-17",
        "first_cross_p75": "2026-06-25", "first_cross_median_lead": 8.0,
        "censored_frac": 0.55, "q_p50_end_m3s": 0.312,
        "n_members": 51, "n_samples": 4000,
        "headline": "23% chance of falling below the Q95 low-flow proxy.",
    }])


def _flow_fan():
    rows = []
    for lead in range(1, 4):
        rows.append({"gauge_id": "F1", "run": FLOW_RUN, "lead": lead,
                     "date": pd.Timestamp("2026-06-10") + pd.Timedelta(days=lead),
                     "q_p10_m3s": 0.111, "q_p50_m3s": 0.222, "q_p90_m3s": 0.333,
                     "segment": "forecast"})
    return pd.DataFrame(rows)


def _flow_gate():
    return pd.DataFrame([{
        "gauge_id": "F1", "station_name": "Test Winterbourne",
        "gate_pass": True, "tier": "tier1", "rain_dependent": False,
    }])


def _station_links_flow():
    # RiverFlowMeasureID uses the EA *instantaneous* suffix; the leading GUID
    # ("F1") is shared with the flow catalogue's *daily-mean* measure id.
    return pd.DataFrame([{
        "GWStationID": "A", "GWMeasureID": "A-gw-logged-i-subdaily-mAOD-qualified",
        "RiverFlowMeasureID": "F1-flow-i-900-m3s-qualified", "RiverFlowDist": 1.2,
    }])


@pytest.fixture
def inputs(tmp_path):
    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    _shard(shard_dir, "A", days=1500)
    _shard(shard_dir, "B", days=400)
    _shard(shard_dir, "C", days=400)
    fresh = pd.DataFrame({
        "station_id": ["A", "B"],
        "last_real_reading": ["2026-06-10", "2026-06-08"],
        "data_source": ["logged", "logged"],
        "days_since": [2, 4],
        "freshness_label": ["fresh", "fresh"],
    })
    return PackInputs(
        catalogue=_catalogue(), shard_dir=shard_dir, freshness=fresh,
        normals=_normals(), pastas_summary=_summary(), pastas_fan=_fan(),
        pastas_fan_archive=_fan_archive(),
        seasonal=_seasonal(), trend_flags=_trend_flags(),
        excluded_ids=frozenset({"C"}),
        pinned_ids=frozenset({"A"}), region_name="Testshire",
        source_meta={"catalogue": {"path": "x", "mtime_utc": None, "status": "ok"}})


def _build(inputs, tmp_path, **kw):
    out = tmp_path / "pack"
    meta = build_pack(inputs, out, now=FIXED_NOW, **kw)
    return out, meta


@pytest.fixture
def flow_inputs(inputs, tmp_path):
    """``inputs`` (GW fixture, unchanged) plus a full RiverCast input set:
    one gauge (F1, a winterbourne) with a fan, linked to GW borehole "A"."""
    flow_shard_dir = tmp_path / "flow_shards"
    flow_shard_dir.mkdir()
    _flow_shard(flow_shard_dir, "F1")
    return dataclasses.replace(
        inputs, flow_catalogue=_flow_catalogue(), flow_shard_dir=flow_shard_dir,
        flow_summary=_flow_summary(), flow_fan=_flow_fan(),
        flow_gate=_flow_gate(), station_links=_station_links_flow())


# ---------------------------------------------------------------------------

def test_writes_expected_files(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    assert (out / "meta.json").exists()
    assert (out / "manifest.json").exists()
    assert (out / "stations.geojson").exists()
    assert (out / "stations" / "A.json").exists()
    assert (out / "stations" / "B.json").exists()
    assert not (out / "stations" / "C.json").exists()      # excluded
    assert not (out.parent / "pack.building").exists()     # swap completed


def test_meta_schema(inputs, tmp_path):
    out, meta = _build(inputs, tmp_path)
    on_disk = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert on_disk == meta
    assert meta["schema_version"] == C.SCHEMA_VERSION
    assert meta["generated_at"] == FIXED_NOW
    assert meta["region"] == "Testshire"
    assert meta["counts"] == {"stations": 2, "with_forecast": 1,
                              "with_seasonal": 1, "excluded": 1, "no_data": 0,
                              "flow_gauges": 0, "flow_with_forecast": 0}
    # coverage audit block (additive; live_capable is None when not supplied)
    assert meta["coverage"] == {"catalogued": 3, "observed": 2, "with_forecast": 1,
                                "no_data": 0, "excluded": 1, "live_capable": None}
    assert meta["runs"]["forecast"] == "2026-06-12T17:00:00Z"
    assert meta["runs"]["seasonal"]["origin_date"] == "2026-06-12"
    assert meta["history_days"] == 1100
    assert "disclaimer" in meta and "attribution" in meta


def test_manifest_integrity(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    listed = set(manifest["files"])
    on_disk = {fp.relative_to(out).as_posix() for fp in out.rglob("*")
               if fp.is_file() and fp.name != "manifest.json"}
    assert listed == on_disk
    for rel, entry in manifest["files"].items():
        data = (out / rel).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert len(data) == entry["bytes"]


def test_geojson_validity(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    gj = json.loads((out / "stations.geojson").read_text(encoding="utf-8"))
    assert gj["type"] == "FeatureCollection"
    by_id = {f["properties"]["station_id"]: f for f in gj["features"]}
    assert set(by_id) == {"A", "B"}
    # [lon, lat] order
    assert by_id["A"]["geometry"]["coordinates"] == [-1.0, 51.0]
    # full property set
    expected = (set(C.GEOJSON_IDENTITY_PROPS) | set(C.GEOJSON_STATUS_PROPS)
                | set(C.GEOJSON_FRESHNESS_PROPS) | set(C.GEOJSON_FORECAST_PROPS)
                | set(C.GEOJSON_FLAG_PROPS) | set(C.GEOJSON_TREND_PROPS)
                | set(C.GEOJSON_TIMELINE_PROPS))
    for f in gj["features"]:
        assert set(f["properties"]) == expected
    # B: out of forecast scope -> nulls + flags false
    b = by_id["B"]["properties"]
    assert b["has_forecast"] is False and b["has_seasonal"] is False
    assert all(b[k] in (None, False) for k in C.GEOJSON_FORECAST_PROPS)
    assert b["has_trend_flag"] is False and b["trend_severity"] is None
    # A: in scope
    a = by_id["A"]["properties"]
    assert a["has_forecast"] is True and a["tier"] in (
        "BREACH_LIKELY", "BREACH_POSSIBLE", "WATCH", "STABLE")
    assert a["is_pinned"] is True
    assert a["short_record"] is False                      # full-record fixture
    assert a["has_trend_flag"] is True and a["trend_severity"] == "high"
    # status computed against the normals ladder
    assert a["status"] in ("below", "near", "above")
    assert a["obs_date"] == "2026-06-10"
    # SGI: ladder-based, finite (level within normals), saturates at ~±2.05
    assert a["sgi"] is not None and abs(a["sgi"]) < 2.06


def test_detail_in_scope_schema(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    assert d["schema_version"] == C.SCHEMA_VERSION
    assert d["station"]["name"] == "Alpha BH"
    assert len(d["normals"]) == 12
    assert set(d["normals"][0]) == set(C.NORMALS_ROW_KEYS)
    assert d["observed"]["unit"] == "mAOD"
    fc = d["forecast"]
    assert set(fc) == set(C.DETAIL_FORECAST_KEYS)
    assert fc["is_pinned"] is True and fc["threshold_source"] == "user"
    assert fc["short_record"] is False
    assert fc["run"] == "2026-06-12T17:00:00Z"
    assert fc["first_cross_p25"] is None                   # NaN -> null
    # fan rows: renamed keys per FAN_KEY_MAP + lead/date
    assert len(fc["fan"]) == 3
    assert set(fc["fan"][0]) == {"lead", "date", *C.FAN_KEY_MAP.values(), *C.FAN_EXTRA_KEYS}
    assert fc["fan"][0]["p50"] == 50.222                   # 3 dp
    seas = d["seasonal"]
    assert seas["seas5_weighted"] is True and seas["n_traces"] == 35
    assert len(seas["months"]) == 6
    assert set(seas["months"][0]) == set(C.SEASONAL_MONTH_KEYS)
    assert seas["months"][0]["p_near"] == 0.8989            # 4 dp
    tf = d["trend_flag"]                                     # roadmap 1.1
    assert set(tf) == set(C.TREND_FLAG_KEYS)
    assert tf["severity"] == "high" and tf["provenance_class"] == "artifact_like"
    assert tf["slope_sen_m_yr"] == 0.701 and tf["rain_corr"] == 0.12  # 3dp / 2dp


def test_seasonal_guard_nulls_stale_anchor(inputs, tmp_path):
    """The 2026-07-09 bug: outlooks seeded at months-old observations were
    served under a current run stamp. A stale anchor now publishes as null."""
    stale = _seasonal()
    stale["origin_date"] = "2026-02-01"            # >60 d before FIXED_NOW
    out, _ = _build(dataclasses.replace(inputs, seasonal=stale), tmp_path)
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    assert d["seasonal"] is None
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["counts"]["with_seasonal"] == 0    # the count is honest too


def test_meta_seasonal_origin_uses_dominant_cohort(inputs, tmp_path):
    """A mixed-origin archive (stale per-borehole seeds alongside the fresh
    fleet cohort) must stamp meta.runs.seasonal.origin_date with the DOMINANT
    origin, not whatever row sorts first (the 2026-07 mixed-run bug: iloc[0]
    published 2026-03-05 for a July run)."""
    stale = _seasonal().iloc[:1].copy()
    stale["station_id"] = "Z"
    stale["origin_date"] = "2026-03-05"
    mixed = pd.concat([stale, _seasonal()], ignore_index=True)  # stale first
    _, meta = _build(dataclasses.replace(inputs, seasonal=mixed), tmp_path)
    assert meta["runs"]["seasonal"]["origin_date"] == "2026-06-12"


def test_seasonal_guard_drops_past_months_keeps_current(inputs, tmp_path):
    """Months already over are dropped (an outlook for the past is not an
    outlook); the month in progress and future months stay."""
    se = _seasonal()
    # relative to FIXED_NOW (2026-06-12): two past months, current, three future
    se["month_start"] = ["2026-04-01", "2026-05-01", "2026-06-01",
                         "2026-07-01", "2026-08-01", "2026-09-01"]
    out, _ = _build(dataclasses.replace(inputs, seasonal=se), tmp_path)
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    got = [m["month_start"] for m in d["seasonal"]["months"]]
    assert got == ["2026-06-01", "2026-07-01", "2026-08-01", "2026-09-01"]


def test_verification_picks_newest_closed_run(inputs, tmp_path):
    """The verification block scores the NEWEST archived run whose window has
    fully closed — never the still-open one — against the observed series."""
    out, _ = _build(inputs, tmp_path)
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    v = d["verification"]
    assert v is not None
    assert set(v) == set(C.VERIFICATION_KEYS)
    assert v["run"].startswith("2026-05-20")          # newest CLOSED, not 06-08
    assert v["horizon_days"] == 14
    assert v["n_obs"] == 14                            # shard covers the window
    assert v["n_in_band"] == 14                        # 50.0–51.0 brackets ~50.49
    assert 0.0 <= v["mae_p50"] <= 0.02                 # P50 50.5 vs obs ~50.49
    assert len(v["fan"]) == 14
    assert set(v["fan"][0]) == set(C.VERIFY_FAN_KEYS)
    assert v["origin_date"] == "2026-05-19"


def test_verification_null_without_archive_or_obs(inputs, tmp_path):
    """No archive → null everywhere; a station absent from the archive (B) →
    null even when the archive input exists."""
    out, _ = _build(inputs, tmp_path)
    b = json.loads((out / "stations" / "B.json").read_text(encoding="utf-8"))
    assert b["verification"] is None                   # B has no archived runs
    bare = dataclasses.replace(inputs, pastas_fan_archive=None)
    out2 = tmp_path / "pack_noarch"
    build_pack(bare, out2, now=FIXED_NOW)
    a = json.loads((out2 / "stations" / "A.json").read_text(encoding="utf-8"))
    assert a["verification"] is None


def test_forecast_block_short_record_flag():
    """_forecast_block lifts the summary's short_record flag into the JSON block
    (drives the borehole-page 'provisional' badge); absent → False."""
    from src.publish.pack import _forecast_block
    on = _forecast_block(pd.Series({"tier": "STABLE", "short_record": True}), None)
    off = _forecast_block(pd.Series({"tier": "STABLE"}), None)
    assert on["short_record"] is True
    assert off["short_record"] is False


def test_detail_status_only(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    d = json.loads((out / "stations" / "B.json").read_text(encoding="utf-8"))
    assert d["forecast"] is None
    assert d["seasonal"] is None
    assert d["trend_flag"] is None                          # unflagged
    assert d["status"]["status"] in ("below", "near", "above")
    assert d["status"]["sgi"] is not None                   # finite within normals
    assert len(d["observed"]["series"]) == 400


def test_missing_optional_inputs_degrade(inputs, tmp_path, capsys):
    bare = PackInputs(
        catalogue=inputs.catalogue, shard_dir=inputs.shard_dir,
        excluded_ids=inputs.excluded_ids, region_name="Testshire",
        source_meta={"pastas_summary": {"path": "x", "mtime_utc": None,
                                        "status": "missing"}})
    out, meta = _build(bare, tmp_path)
    assert meta["counts"]["with_forecast"] == 0
    assert meta["runs"]["forecast"] is None
    assert meta["inputs"]["pastas_summary"]["status"] == "missing"
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    assert d["forecast"] is None and d["seasonal"] is None
    assert d["normals"] == []
    assert d["status"]["status"] is None                   # no normals ladder
    assert d["status"]["sgi"] is None                       # no percentile -> no SGI


def test_empty_catalogue_raises(inputs, tmp_path):
    empty = PackInputs(catalogue=inputs.catalogue.iloc[0:0],
                       shard_dir=inputs.shard_dir)
    with pytest.raises(ValueError):
        build_pack(empty, tmp_path / "pack", now=FIXED_NOW)


def test_rounding_and_no_nan(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    d = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    assert d["forecast"]["threshold"] == 51.234            # levels 3 dp
    assert d["forecast"]["p_breach_14d"] == 0.1235         # probabilities 4 dp
    assert d["status"]["percentile"] == round(d["status"]["percentile"], 1)
    for fp in out.rglob("*"):
        if fp.is_file():
            text = fp.read_text(encoding="utf-8")
            assert "NaN" not in text and "Infinity" not in text, fp


def test_idempotent_bytes(inputs, tmp_path):
    out1, _ = _build(inputs, tmp_path)
    m1 = (out1 / "manifest.json").read_bytes()
    out2 = tmp_path / "pack2"
    build_pack(inputs, out2, now=FIXED_NOW)
    assert (out2 / "manifest.json").read_bytes() == m1


def test_rebuild_drops_stale_files(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    stale = out / "stations" / "GONE.json"
    stale.write_text("{}")
    build_pack(inputs, out, now=FIXED_NOW)
    assert not stale.exists()                               # wipe-and-rebuild


def test_history_depth_and_scope_filter(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path, history_days=30,
                    include_history_for="scope")
    a = json.loads((out / "stations" / "A.json").read_text(encoding="utf-8"))
    assert 28 <= len(a["observed"]["series"]) <= 31         # tail sliced
    b = json.loads((out / "stations" / "B.json").read_text(encoding="utf-8"))
    assert b["observed"]["series"] == []                    # out of scope


def test_geology_copied_when_present(inputs, tmp_path):
    geo = tmp_path / "aquifer.geojson"
    geo.write_text('{"type":"FeatureCollection","features":['
                   '{"type":"Feature","properties":{"aquifer_designation":"Principal"},'
                   '"geometry":{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}}]}',
                   encoding="utf-8")
    with_geo = PackInputs(
        catalogue=inputs.catalogue, shard_dir=inputs.shard_dir,
        normals=inputs.normals, excluded_ids=inputs.excluded_ids,
        region_name="Testshire", geology_path=geo)
    out, _ = _build(with_geo, tmp_path)
    assert (out / "geology.geojson").exists()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "geology.geojson" in manifest["files"]


def test_geology_absent_no_file(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)        # fixture has no geology_path
    assert not (out / "geology.geojson").exists()


def test_no_data_station_skipped(inputs, tmp_path):
    cat = pd.concat([inputs.catalogue, pd.DataFrame([{
        "station_id": "D", "station_name": "Delta BH", "lat": 51.3,
        "lon": -1.3, "measure_type": "groundwater",
        "aquifer_name": "Chalk", "aquifer_designation": "Principal"}])],
        ignore_index=True)
    with_d = PackInputs(catalogue=cat, shard_dir=inputs.shard_dir,
                        normals=inputs.normals,
                        excluded_ids=inputs.excluded_ids,
                        region_name="Testshire")
    out, meta = _build(with_d, tmp_path)
    assert meta["counts"]["no_data"] == 1
    assert not (out / "stations" / "D.json").exists()


# ---------------------------------------------------------------------------
# Contract pins
# ---------------------------------------------------------------------------

def test_summary_cols_pinned():
    from src.forecast.pastas.summary import SUMMARY_COLS
    missing = [c for c in C.SUMMARY_COL_SOURCES.values()
               if c not in SUMMARY_COLS]
    assert not missing, f"contract references absent SUMMARY_COLS: {missing}"


def test_helpers():
    assert jround(float("nan"), 3) is None
    assert jround(1.23456, 3) == 1.235
    assert iso_date("2026-06-12T17:00:00+00:00") == "2026-06-12"
    assert iso_date(float("nan")) is None
    assert iso_utc("2026-06-12 17:00:00") == "2026-06-12T17:00:00Z"


def test_public_tiering_hides_non_artifact_provenance(inputs, tmp_path):
    # The trend screen is an operator review queue; only data-quality artefacts
    # reach the public map. Reclassify A's flag as a (non-public) step_shift —
    # it must drop off the published features though it stays in trend_flags.csv.
    inputs.trend_flags.loc[inputs.trend_flags["station_id"] == "A",
                           "provenance_class"] = "step_shift"
    out, _ = _build(inputs, tmp_path)
    geo = json.loads((out / "stations.geojson").read_text())
    a = next(f["properties"] for f in geo["features"]
             if f["properties"]["station_id"] == "A")
    assert a["has_trend_flag"] is False
    assert a["trend_severity"] is None
    assert json.loads((out / "stations" / "A.json").read_text())["trend_flag"] is None


def test_forecast_timeline_sequences(inputs, tmp_path):
    out, meta = _build(inputs, tmp_path)
    frames = meta["forecast_frames"]
    assert frames[0] == "Today" and len(frames) == 9     # Today, +1wk, +2wk, M1..6
    days = meta["forecast_frame_days"]                   # real day-offsets, for slider spacing
    assert len(days) == len(frames) and days[0] == 0 and days == sorted(days)
    gj = json.loads((out / "stations.geojson").read_text())
    for f in gj["features"]:
        p = f["properties"]
        assert len(p["st_seq"]) == len(frames)
        assert len(p["op_seq"]) == len(frames)
        assert p["st_seq"][0] in ("below", "near", "above", "none")
        if p["status"]:
            assert p["st_seq"][0] == p["status"]          # fresh obs -> frame 0 mirrors it
        assert all(0.0 <= o <= 1.0 for o in p["op_seq"])
    by_id = {f["properties"]["station_id"]: f["properties"] for f in gj["features"]}
    # A has a seasonal block -> its Month 1..6 frames carry a tercile category
    assert all(s != "none" for s in by_id["A"]["st_seq"][3:])
    # B has neither forecast nor seasonal -> all forward frames are 'none'
    assert all(s == "none" for s in by_id["B"]["st_seq"][1:])


def test_status_timeline_estimates_today_when_obs_stale():
    # No fresh observed status, but a forecast exists: frame 0 should fall back to
    # the nowcast (modelled-today) category, rendered faint = "estimated".
    from src.publish.pack import status_timeline
    fan = pd.DataFrame({"lead": [0, 7, 14], "date": ["2026-06-21"] * 3,
                        "gw_p10": [10.0] * 3, "gw_p50": [10.0] * 3, "gw_p90": [10.0] * 3})
    qrow = pd.Series({"t1": 20.0, "t2": 30.0})            # month-6 normals; 10 < t1 -> below
    st, op = status_timeline(None, fan, None, {6: qrow})
    assert st[0] == "below"                                # estimated nowcast, not "none"
    assert 0.18 < op[0] < 0.92                             # faint: between no-data and observed


def test_fan_frame_lead0_tie_prefers_nowcast_row():
    # Real fans have no lead-0 row: forecast rows (leads 1..) are appended first,
    # nowcast rows (leads ..-1) after. The frame-0 lookup ties on |lead| between
    # +1 and -1 and must resolve to the NOWCAST -1 row (today), not the forecast
    # +1 row (tomorrow) that argmin's first-occurrence order used to pick.
    from src.publish.pack import _fan_frame
    fan = pd.DataFrame({
        "lead": [1, 2, -2, -1],                            # forecast first, nowcast after
        "date": ["2026-06-21"] * 4,
        "gw_p10": [25.0, 25.0, 10.0, 10.0],                # forecast=above, nowcast=below
        "gw_p50": [25.0, 25.0, 10.0, 10.0],
        "gw_p90": [25.0, 25.0, 10.0, 10.0],
    })
    qrow = pd.Series({"t1": 20.0, "t2": 24.0})             # <20 below, >24 above
    cat, _conf = _fan_frame(fan, 0, qrow.pipe(lambda q: {6: q}))
    assert cat == "below"                                  # the nowcast -1 row won the tie
    # exact-match leads are unaffected by the tie-break
    assert _fan_frame(fan, 2, {6: qrow})[0] == "above"


def test_contract_doc_in_sync():
    doc = Path(__file__).parents[1] / "docs" / "artifact_contract.md"
    assert doc.exists(), "docs/artifact_contract.md missing"
    text = doc.read_text(encoding="utf-8")
    assert f'`"{C.SCHEMA_VERSION}"`' in text or f"`{C.SCHEMA_VERSION}`" in text
    all_keys = (set(C.GEOJSON_IDENTITY_PROPS) | set(C.GEOJSON_STATUS_PROPS)
                | set(C.GEOJSON_FRESHNESS_PROPS) | set(C.GEOJSON_FORECAST_PROPS)
                | set(C.GEOJSON_FLAG_PROPS) | set(C.GEOJSON_TREND_PROPS)
                | set(C.GEOJSON_TIMELINE_PROPS)
                | set(C.SUMMARY_COL_SOURCES)
                | set(C.FAN_KEY_MAP.values()) | set(C.FAN_EXTRA_KEYS)
                | set(C.SEASONAL_MONTH_KEYS)
                | set(C.NORMALS_ROW_KEYS) | set(C.TREND_FLAG_KEYS)
                # RiverCast (Stage 7)
                | set(C.GEOJSON_TYPE_PROPS) | set(C.GEOJSON_FLOW_PROPS)
                | set(C.FLOW_STATION_KEYS) | set(C.FLOW_SUMMARY_COL_SOURCES)
                | set(C.FLOW_FAN_KEY_MAP.values()))
    undocumented = sorted(k for k in all_keys if f"`{k}`" not in text)
    assert not undocumented, f"keys missing from artifact_contract.md: {undocumented}"


def test_read_shard_excludes_stuck_rows(tmp_path):
    # A frozen sensor (logged_live_stuck) carries the last value forward with
    # is_interpolated=0. _read_shard must drop those rows so the status/observed
    # series ends at the last GENUINE reading, not the frozen tail.
    from src.publish.pack import _read_shard
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-06-06", "2026-06-07", "2026-06-08",
                                "2026-06-09", "2026-06-10"]),
        "GW_Level": [50.0, 50.1, 50.2, 50.2, 50.2],
        "is_interpolated": [0, 0, 0, 0, 0],
        "data_source": ["logged", "logged", "logged_live",
                        "logged_live_stuck", "logged_live_stuck"],
    })
    df.to_parquet(tmp_path / "S.parquet", index=False)
    s = _read_shard(tmp_path, "S")
    assert len(s) == 3                                  # two stuck rows dropped
    assert s.index.max() == pd.Timestamp("2026-06-08")  # last real reading
    assert float(s.iloc[-1]) == 50.2


def test_read_shard_without_data_source_column(tmp_path):
    # Older / full-rebuild shards may lack data_source — must still read fine.
    from src.publish.pack import _read_shard
    df = pd.DataFrame({"date": pd.to_datetime(["2026-06-07", "2026-06-08"]),
                       "GW_Level": [50.0, 50.1]})
    df.to_parquet(tmp_path / "T.parquet", index=False)
    s = _read_shard(tmp_path, "T")
    assert len(s) == 2 and s.index.max() == pd.Timestamp("2026-06-08")


# ---------------------------------------------------------------------------
# 2026-07 additive files: stations/index.json + national_history.json,
# and the real-days seasonal frame offsets.
# ---------------------------------------------------------------------------

def test_stations_index_catalogue(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    idx = json.loads((out / "stations" / "index.json").read_text(encoding="utf-8"))
    assert {e["station_id"] for e in idx} == {"A", "B"}
    a = next(e for e in idx if e["station_id"] == "A")
    assert set(a) == {"station_id", "slug", "name", "lat", "lon",
                      "aquifer_designation", "has_forecast", "has_seasonal"}
    assert a["slug"] and a["has_forecast"] is True


def test_national_history_appends_and_dedupes(inputs, tmp_path):
    out, _ = _build(inputs, tmp_path)
    hist = json.loads((out / "national_history.json").read_text(encoding="utf-8"))
    assert len(hist) == 1
    row = hist[0]
    assert row["date"] == FIXED_NOW[:10]
    assert row["stations"] == 2 and row["with_forecast"] == 1
    assert row["below"] + row["near"] + row["above"] <= row["stations"]
    # rebuild on the same day: replaced, not duplicated (store lives NEXT TO the
    # pack dir and survives the atomic swap)
    _build(inputs, tmp_path)
    hist2 = json.loads((out / "national_history.json").read_text(encoding="utf-8"))
    assert len(hist2) == 1
    # UKHO 5-band counts (additive 2026-07-18): partition the SAME population
    # as below/near/above — status-carrying stations with a percentile.
    bands = ["band_low", "band_below", "band_normal", "band_above", "band_high"]
    assert all(b in row for b in bands)
    assert sum(row[b] for b in bands) <= row["below"] + row["near"] + row["above"]


def test_frame_days_use_real_seasonal_month_starts(inputs, tmp_path):
    out, meta = _build(inputs, tmp_path)
    days = meta["forecast_frame_days"]
    frames = meta["forecast_frames"]
    assert days[0] == 0 and days[frames.index("+2 wk")] == 14
    # the fixture's seasonal months start 2026-07-01 (FIXED_NOW = 2026-06-12):
    # Month 1 sits at its month MID-point in real days-ahead (~19 + 14 = 33),
    # NOT the old 30*mi approximation locked to day 30.
    m1 = days[frames.index("Month 1")]
    assert m1 == 33
    assert all(b > a for a, b in zip(days, days[1:]))   # strictly increasing


# ---------------------------------------------------------------------------
# RiverCast (flow gauges) — Stage 7.
# ---------------------------------------------------------------------------

def test_flow_summary_cols_pinned():
    from src.forecast.pastas.flow_summary import SUMMARY_COLS as FLOW_SUMMARY_COLS
    missing = [c for c in C.FLOW_SUMMARY_COL_SOURCES.values()
               if c not in FLOW_SUMMARY_COLS]
    assert not missing, f"contract references absent flow SUMMARY_COLS: {missing}"


def test_flow_fan_cols_pinned():
    from src.forecast.pastas.flow_summary import FAN_COLS as FLOW_FAN_COLS
    missing = [c for c in C.FLOW_FAN_KEY_MAP if c not in FLOW_FAN_COLS]
    assert not missing, f"contract references absent flow FAN_COLS: {missing}"


def test_pack_without_flow_inputs_unchanged(inputs, tmp_path):
    """The GW-only fixture (no flow_* fields set) must build BYTE-IDENTICAL
    meta/geojson content to before RiverCast existed — the graceful-degrade
    contract (docs §6): zero flow stations, no station_type anywhere."""
    out, meta = _build(inputs, tmp_path)
    assert meta["counts"]["flow_gauges"] == 0
    assert meta["counts"]["flow_with_forecast"] == 0
    assert meta["counts"]["stations"] == 2               # unchanged: A, B
    assert "river_disclaimer" in meta                     # always present (static line)
    gj = json.loads((out / "stations.geojson").read_text(encoding="utf-8"))
    assert len(gj["features"]) == 2
    for f in gj["features"]:
        assert "station_type" not in f["properties"]
    idx = json.loads((out / "stations" / "index.json").read_text(encoding="utf-8"))
    for row in idx:
        assert "station_type" not in row


def test_flow_gauge_publishes_feature_and_detail(flow_inputs, tmp_path):
    out, meta = _build(flow_inputs, tmp_path)
    assert meta["counts"]["flow_gauges"] == 1
    assert meta["counts"]["flow_with_forecast"] == 1
    # GW-only counters are untouched by the flow gauge joining the pack
    assert meta["counts"]["stations"] == 2
    assert meta["coverage"]["observed"] == 2

    gj = json.loads((out / "stations.geojson").read_text(encoding="utf-8"))
    by_id = {f["properties"]["station_id"]: f for f in gj["features"]}
    assert set(by_id) == {"A", "B", "F1"}
    flow_props = by_id["F1"]["properties"]
    assert flow_props["station_type"] == "flow"
    assert flow_props["river_name"] == "Test Brook"
    assert flow_props["rain_dependent"] is False
    assert flow_props["has_forecast"] is True
    assert flow_props["winterbourne"] is True     # additive 2026-07-19: lifted from detail
    assert flow_props["status"] in ("below", "near", "above", None)
    # GW features are BYTE-shape unchanged: still no station_type key at all
    assert "station_type" not in by_id["A"]["properties"]
    assert "station_type" not in by_id["B"]["properties"]

    idx = json.loads((out / "stations" / "index.json").read_text(encoding="utf-8"))
    f1_row = next(r for r in idx if r["station_id"] == "F1")
    assert f1_row["station_type"] == "flow"
    a_row = next(r for r in idx if r["station_id"] == "A")
    assert "station_type" not in a_row

    d = json.loads((out / "stations" / "F1.json").read_text(encoding="utf-8"))
    assert d["schema_version"] == C.SCHEMA_VERSION
    assert d["station"]["station_type"] == "flow"
    assert d["station"]["river_name"] == "Test Brook"
    assert d["station"]["linked_boreholes"] == ["A"]        # inverted from station_links
    assert d["station"]["winterbourne"] is True
    assert d["station"]["dry_months"] == [8]
    assert d["observed"]["unit"] == "m3/s"
    assert len(d["observed"]["series"]) > 0
    assert d["seasonal"] is None and d["trend_flag"] is None and d["verification"] is None

    fc = d["forecast"]
    assert fc is not None
    assert set(fc) == set(C.DETAIL_FLOW_FORECAST_KEYS)
    assert fc["threshold"] == 0.05                          # q95_m3s, 3dp
    assert fc["threshold_source"] == "q95_proxy"
    assert fc["p_below_q95_14d"] == 0.234
    assert fc["rain_dependent"] is False
    assert len(fc["fan"]) == 3
    assert set(fc["fan"][0]) == {"lead", "date", "segment", *C.FLOW_FAN_KEY_MAP.values()}
    assert fc["fan"][0]["p50"] == 0.222


def test_flow_gauge_without_fan_does_not_publish(flow_inputs, tmp_path):
    """Launch scope: a flow gauge with a summary row but NO fan rows must not
    appear at all (no status-only tier for rivers in v1)."""
    empty_fan = dataclasses.replace(flow_inputs, flow_fan=_flow_fan().iloc[0:0])
    out, meta = _build(empty_fan, tmp_path)
    assert meta["counts"]["flow_gauges"] == 0
    assert not (out / "stations" / "F1.json").exists()


def test_flow_rain_dependent_true_when_gated(flow_inputs, tmp_path):
    dep = _flow_gate()
    dep.loc[0, "rain_dependent"] = True
    out, _ = _build(dataclasses.replace(flow_inputs, flow_gate=dep), tmp_path)
    d = json.loads((out / "stations" / "F1.json").read_text(encoding="utf-8"))
    assert d["forecast"]["rain_dependent"] is True
    gj = json.loads((out / "stations.geojson").read_text(encoding="utf-8"))
    f1 = next(f["properties"] for f in gj["features"] if f["properties"]["station_id"] == "F1")
    assert f1["rain_dependent"] is True


def test_flow_linked_boreholes_empty_when_no_link(flow_inputs, tmp_path):
    out, _ = _build(dataclasses.replace(flow_inputs, station_links=None), tmp_path)
    d = json.loads((out / "stations" / "F1.json").read_text(encoding="utf-8"))
    assert d["station"]["linked_boreholes"] == []


def test_flow_gauge_no_catalogue_row_skipped(flow_inputs, tmp_path):
    bad = dataclasses.replace(flow_inputs, flow_catalogue=_flow_catalogue().iloc[0:0])
    out, meta = _build(bad, tmp_path)
    assert meta["counts"]["flow_gauges"] == 0


def test_river_disclaimer_wording(flow_inputs, tmp_path):
    out, meta = _build(flow_inputs, tmp_path)
    rd = meta["river_disclaimer"]
    assert "gauged flow, including any abstraction and discharge effects" in rd


def test_national_history_excludes_flow(flow_inputs, tmp_path):
    out, _ = _build(flow_inputs, tmp_path)
    hist = json.loads((out / "national_history.json").read_text(encoding="utf-8"))
    row = hist[-1]
    assert row["stations"] == 2                            # F1 not counted


def test_flow_national_history_accrues_and_dedupes(flow_inputs, tmp_path):
    """flow_national_history.json (additive 2026-07-19): one row per build
    day over the published flow gauges — below/near/above vs each gauge's own
    climatology, Q95-now count, 5-band counts. Same dedupe-on-rebuild and
    survives-the-swap semantics as the GW file."""
    out, _ = _build(flow_inputs, tmp_path)
    hist = json.loads((out / "flow_national_history.json").read_text(encoding="utf-8"))
    assert len(hist) == 1
    row = hist[0]
    assert row["date"] == FIXED_NOW[:10]
    assert row["n_gauges"] == 1 and row["n_with_forecast"] == 1
    assert row["below"] + row["near"] + row["above"] <= row["n_gauges"]
    # fixture: latest observed ~0.19 m3/s, q95 fixture value 0.05 -> not below
    assert row["n_below_q95_now"] == 0
    bands = ["band_low", "band_below", "band_normal", "band_above", "band_high"]
    assert all(b in row for b in bands)
    assert sum(row[b] for b in bands) <= row["n_gauges"]
    # rebuild same day: replaced, not duplicated
    _build(flow_inputs, tmp_path)
    hist2 = json.loads((out / "flow_national_history.json").read_text(encoding="utf-8"))
    assert len(hist2) == 1


def test_flow_national_history_absent_without_flow(inputs, tmp_path):
    """A GW-only pack must NOT ship the flow history file at all (no
    misleading all-zero accrual; pack contents unchanged pre-RiverCast)."""
    out, _ = _build(inputs, tmp_path)
    assert not (out / "flow_national_history.json").exists()


def test_frame_days_ignore_stale_origin_cohort():
    # An archive predating fleet-uniform anchoring mixes per-borehole origins;
    # the dominant (freshest) cohort must win, and offsets inside the fan
    # window fall back to the approximation instead of misleading.
    from src.publish.pack import _frame_days, _seasonal_month_starts
    rows = []
    for m in range(1, 7):
        # 5 fresh-cohort boreholes (dominant) + 1 months-stale straggler
        for _ in range(5):
            rows.append({"month_ahead": m, "origin_date": "2026-07-03",
                         "month_start": f"2026-{7 + m:02d}-01" if 7 + m <= 12
                         else f"2027-{7 + m - 12:02d}-01"})
        rows.append({"month_ahead": m, "origin_date": "2026-03-05",
                     "month_start": f"2026-{3 + m:02d}-01"})
    df = pd.DataFrame(rows)
    starts = _seasonal_month_starts(df)
    assert starts[0] == "2026-08-01"                # dominant cohort, not March's
    days = _frame_days(starts, now="2026-07-04T18:00:00Z")
    assert days[3] == 42                            # Month 1: Aug mid ~= +42 d
    assert all(b > a for a, b in zip(days, days[1:]))

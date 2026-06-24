"""Tests for the artifact-pack builder (src/publish/pack.py) and its
contract pins (src/publish/contract.py ↔ SUMMARY_COLS ↔ docs/artifact_contract.md).

Synthetic-input pack: station A in-scope (fan + seasonal + user threshold),
station B status-only, station C excluded — built into tmp_path with an
injected ``now`` so reruns are byte-identical.
"""
from __future__ import annotations

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
        seasonal=_seasonal(), trend_flags=_trend_flags(),
        excluded_ids=frozenset({"C"}),
        pinned_ids=frozenset({"A"}), region_name="Testshire",
        source_meta={"catalogue": {"path": "x", "mtime_utc": None, "status": "ok"}})


def _build(inputs, tmp_path, **kw):
    out = tmp_path / "pack"
    meta = build_pack(inputs, out, now=FIXED_NOW, **kw)
    return out, meta


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
                              "with_seasonal": 1, "excluded": 1, "no_data": 0}
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
                | set(C.NORMALS_ROW_KEYS) | set(C.TREND_FLAG_KEYS))
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

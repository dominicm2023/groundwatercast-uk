"""The MapLibre explorer (web/) reads only DOCUMENTED pack fields.

web/contract_fields.js declares the geojson properties and detail-file keys
the explorer consumes. This test asserts each is part of the published
artifact-pack contract (src/publish/contract.py) — so a pack-schema change
that would silently break the explorer fails here, with no JS test runner.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.publish import contract as C

WEB = Path(__file__).resolve().parents[1] / "web"


def _js_array(name: str, text: str) -> list[str]:
    """Pull a `name: [ "a", "b", ... ]` array literal out of the JS file."""
    m = re.search(name + r"\s*:\s*\[(.*?)\]", text, re.S)
    assert m, f"{name} not found in contract_fields.js"
    return re.findall(r'"([^"]+)"', m.group(1))


def _load_fields():
    text = (WEB / "contract_fields.js").read_text(encoding="utf-8")
    return _js_array("GEOJSON_FIELDS", text), _js_array("DETAIL_FIELDS", text)


def test_contract_fields_file_present():
    assert (WEB / "contract_fields.js").exists()
    geo, det = _load_fields()
    assert geo and det


def test_geojson_fields_are_documented():
    geo, _ = _load_fields()
    documented = (set(C.GEOJSON_IDENTITY_PROPS) | set(C.GEOJSON_STATUS_PROPS)
                  | set(C.GEOJSON_FRESHNESS_PROPS) | set(C.GEOJSON_FORECAST_PROPS)
                  | set(C.GEOJSON_FLAG_PROPS) | set(C.GEOJSON_TREND_PROPS)
                  | set(C.GEOJSON_TIMELINE_PROPS)
                  | set(C.GEOJSON_TYPE_PROPS) | set(C.GEOJSON_FLOW_PROPS))
    undocumented = sorted(f for f in geo if f not in documented)
    assert not undocumented, (
        f"explorer reads geojson props absent from the pack contract: {undocumented}")


def test_geojson_fields_cover_timeline_and_identity():
    # Regression: the JS list must include the props app.js/home.js actually
    # read (st_seq/op_seq drive the forecast timeline; slug drives /b/ links) —
    # otherwise a pack-side rename passes the suite while breaking the UI.
    geo, det = _load_fields()
    for f in ("st_seq", "op_seq", "slug", "aquifer_designation"):
        assert f in geo, f"GEOJSON_FIELDS missing {f}"
    assert "station.slug" in det


def test_detail_fields_are_documented():
    _, det = _load_fields()
    # Allowed leaf keys per nested group of stations/<id>.json. RiverCast
    # (Stage 7) additions are unioned in, not swapped: a GW detail file's
    # "forecast"/"fan"/"station" shapes are exactly as before.
    station = ({"station_id", "slug", "name", "lat", "lon", "aquifer",
               "aquifer_designation"} | set(C.FLOW_STATION_KEYS))
    status = set(C.GEOJSON_STATUS_PROPS) | {"month"}
    freshness = {"label", "days_since", "last_real_reading", "data_source"}
    normals = set(C.NORMALS_ROW_KEYS)
    observed = {"unit", "series"}
    forecast = set(C.DETAIL_FORECAST_KEYS) | set(C.DETAIL_FLOW_FORECAST_KEYS)
    fan = (set(C.FAN_KEY_MAP.values()) | {"lead", "date"} | set(C.FAN_EXTRA_KEYS)
          | set(C.FLOW_FAN_KEY_MAP.values()))
    seasonal = {"run", "origin_date", "seas5_weighted", "n_traces", "months"}
    months = set(C.SEASONAL_MONTH_KEYS)
    groups = {
        "station": station, "status": status, "freshness": freshness,
        "normals": normals, "observed": observed, "forecast": forecast,
        "fan": fan, "seasonal": seasonal, "months": months,
    }
    bad = []
    for path in det:
        grp, _, leaf = path.partition(".")
        if grp not in groups or leaf not in groups[grp]:
            bad.append(path)
    assert not bad, f"explorer reads detail keys absent from the pack contract: {bad}"


def test_palette_matches_status_module():
    """The explorer's status palette mirrors src/dashboard/status.py."""
    from src.dashboard.status import STATUS_COLOR
    cfg = (WEB / "config.js").read_text(encoding="utf-8")
    for key in ("below", "near", "above"):
        assert STATUS_COLOR[key].lower() in cfg.lower(), (
            f"status colour for {key} ({STATUS_COLOR[key]}) missing from web/config.js")


def test_web_shell_wires_every_script():
    # The explorer moved to web/explorer/ in the multi-page split; / is a landing.
    html = (WEB / "explorer" / "index.html").read_text(encoding="utf-8")
    for src in ("vendor/maplibre-gl.js", "config.js", "contract_fields.js",
                "charts.js", "detail.js", "app.js"):
        assert src in html, f"explorer/index.html does not load {src}"


def test_landing_shell_wires_home_scripts():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for src in ("vendor/maplibre-gl.js", "config.js", "home.js"):
        assert src in html, f"index.html (landing) does not load {src}"

"""OG share-card builder — SVG content, monthly-stable sparkline cut, manifest,
content-hash reuse, and the stub builder's og:image wiring."""
import json

import pytest

from scripts import build_og_cards as C
from scripts import build_seo_stubs as B

SAMPLE = {
    "station": {"station_id": "01e2532a-1f3d-45db-a9a3-f6667b446ab0",
                "slug": "wilgate-green", "name": "Wilgate Green",
                "lat": 51.276283, "lon": 0.866011,
                "aquifer": "Principal", "aquifer_designation": "Principal"},
    "status": {"status": "near", "percentile": 50.6, "level": 40.6,
               "obs_date": "2026-05-09", "obs_age_days": 43},
    "observed": {"unit": "mAOD", "series": [["2025-01-05", 24.5], ["2025-06-01", 25.1],
                                            ["2026-05-09", 40.6]]},
    "forecast": {"p_breach_14d": 0.0},
}


def test_card_svg_is_status_neutral_with_caveat():
    svg = C.card_svg("Wilgate Green", "Kent", "Principal",
                     SAMPLE["observed"]["series"])
    assert "Wilgate Green" in svg and "Kent" in svg
    assert C.CAVEAT in svg
    # status-NEUTRAL by design: scrapers cache cards for weeks, so today's
    # status must never be baked into the image
    for word in ("below normal", "near normal", "above normal"):
        assert word not in svg
    assert "&" not in svg.replace("&amp;", "").replace("&lt;", "").replace(
        "&gt;", "").replace("&quot;", "")   # everything interpolated is escaped


def test_sparkline_cut_at_month_start_is_stable():
    # points on/after the current month start must NOT enter the sparkline —
    # that's what keeps the content hash stable between month boundaries
    from datetime import date
    today = date.today().isoformat()
    series = [["2025-01-01", 1.0], ["2025-06-01", 2.0], [today, 99.0]]
    pts = C._spark_points(series, 0, 0, 100, 50)
    assert pts is not None
    svg_now = C.card_svg("X", None, None, series)
    svg_wo = C.card_svg("X", None, None, series[:-1])
    assert svg_now == svg_wo               # today's reading doesn't change the card


def test_long_names_shrink_not_overflow():
    svg = C.card_svg("An Extremely Long Borehole Station Name Indeed", None, None, [])
    assert 'font-size="48"' in svg


def test_build_writes_cards_manifest_and_reuses_hash(tmp_path):
    try:
        import resvg_py  # noqa: F401
    except ImportError:
        pytest.skip("resvg_py not installed")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    out = tmp_path / "web" / "b"
    manifest = tmp_path / "og_cards.json"
    r1 = C.build(pack, out, manifest)
    assert r1["cards"] == 1 and r1["rendered"] == 1
    m = json.loads(manifest.read_text(encoding="utf-8"))
    fname = m["cards"]["wilgate-green"]
    assert fname.startswith("share.") and fname.endswith(".png")
    png = out / "wilgate-green" / fname
    assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # second build: content unchanged -> reused, not re-rendered
    r2 = C.build(pack, out, manifest)
    assert r2["reused"] == 1 and r2["rendered"] == 0


def test_stub_embeds_og_image_only_when_card_exists(tmp_path):
    try:
        import resvg_py  # noqa: F401
    except ImportError:
        pytest.skip("resvg_py not installed")
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    web = tmp_path / "web"
    out = web / "b"
    manifest = tmp_path / "og_cards.json"
    C.build(pack, out, manifest)
    B.build(pack, out, today="2026-07-04", lastmod_store=tmp_path / "lm.json",
            og_manifest=manifest)
    html = (out / "wilgate-green" / "index.html").read_text(encoding="utf-8")
    fname = json.loads(manifest.read_text())["cards"]["wilgate-green"]
    assert f'property="og:image" content="{B.SITE}/b/wilgate-green/{fname}"' in html
    assert 'og:image:width" content="1200"' in html
    # and without a manifest the stub omits og:image entirely (never a 404 URL)
    B.build(pack, out, today="2026-07-04", lastmod_store=tmp_path / "lm2.json",
            og_manifest=tmp_path / "absent.json")
    html2 = (out / "wilgate-green" / "index.html").read_text(encoding="utf-8")
    assert "og:image" not in html2

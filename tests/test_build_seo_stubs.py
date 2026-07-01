"""Per-borehole SEO stub generation — head/JSON-LD correctness, honesty + region
degradation, noindex on zero-observation stubs, and a build() smoke test."""
import json
import re

from scripts import build_seo_stubs as B

SAMPLE = {
    "station": {"station_id": "01e2532a-1f3d-45db-a9a3-f6667b446ab0", "name": "Wilgate Green",
                "lat": 51.276283, "lon": 0.866011, "aquifer": "Principal"},
    "status": {"status": "near", "percentile": 50.6, "level": 40.6, "obs_date": "2026-05-09"},
    "freshness": {"last_real_reading": "2026-05-09", "label": "stale"},
    "observed": {"unit": "mAOD", "series": [["2023-05-05", 24.495], ["2026-05-09", 40.6]]},
    "forecast": {"p_breach_14d": 0.0},
}


def _jsonld(html):
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_canonical_equals_og_url_and_title():
    html = B._page(SAMPLE, "wilgate-green", "Kent", True)
    assert f'<link rel="canonical" href="{B.SITE}/b/wilgate-green/">' in html
    assert f'<meta property="og:url" content="{B.SITE}/b/wilgate-green/">' in html
    assert "<title>Groundwater at Wilgate Green, Kent — indicative forecast | GroundwaterCast</title>" in html
    assert "not a flood or drought warning" in html  # caveat in the description


def test_jsonld_graph_valid_and_ogl():
    jl = _jsonld(B._page(SAMPLE, "wilgate-green", "Kent", True))
    types = {n["@type"] for n in jl["@graph"]}
    assert {"WebSite", "WebPage", "Dataset", "Place"} <= types
    ds = next(n for n in jl["@graph"] if n["@type"] == "Dataset")
    assert ds["license"] == B.OGL
    assert "creativecommons.org" not in json.dumps(jl)
    assert ds["identifier"] == SAMPLE["station"]["station_id"]
    assert ds["temporalCoverage"] == "2023-05-05/2026-05-09"
    assert ds["dateModified"] == "2026-05-09"
    assert ds["variableMeasured"][0]["unitText"] == "mAOD"
    place = next(n for n in jl["@graph"] if n["@type"] == "Place")
    assert place["address"]["addressRegion"] == "Kent"
    assert place["geo"]["latitude"] == 51.276283


def test_region_less_degrades_cleanly():
    html = B._page(SAMPLE, "wilgate-green", None, True)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "None" not in title and "null" not in title
    assert "Wilgate Green — indicative forecast" in title  # no ", " dangling
    jl = _jsonld(html)
    place = next(n for n in jl["@graph"] if n["@type"] == "Place")
    assert "addressRegion" not in place["address"]
    assert "Kent" not in json.dumps(jl)


def test_noindex_zero_observations():
    head_idx = B._head(SAMPLE, "x", "Kent", True)
    assert 'content="index,follow,max-image-preview:large"' in head_idx
    head_noidx = B._head(SAMPLE, "x", "Kent", False)
    assert 'content="noindex,follow"' in head_noidx


def test_recent_obs_block_present():
    html = B._page(SAMPLE, "wilgate-green", "Kent", True)
    assert "Most recent observed groundwater levels" in html
    assert "40.60 mAOD" in html                     # real number, crawler-visible
    assert 'id="detail-body" data-station=' in html  # JS enrichment hook


def test_build_smoke(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    bare = {"station": {"station_id": "deadbeef-0000", "name": "Empty BH",
                        "lat": 52.0, "lon": -1.0}, "observed": {"series": []}}
    (pack / "b.json").write_text(json.dumps(bare), encoding="utf-8")
    web = tmp_path / "web"
    out = web / "b"
    store = tmp_path / "lastmod.json"
    stats = B.build(pack, out, today="2026-06-30", lastmod_store=store)   # raises on self-check failure
    assert stats["stubs"] == 2
    assert stats["noindex"] == 1          # the empty borehole
    assert (out / "wilgate-green" / "index.html").exists()
    assert (out / "empty-bh" / "index.html").exists()
    # browse + sitemap + robots
    browse = (web / "browse" / "index.html").read_text(encoding="utf-8")
    assert "Browse boreholes" in browse and "/b/wilgate-green/" in browse
    sm = (web / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://groundwatercast.com/b/wilgate-green/</loc>" in sm
    assert "/b/empty-bh/" not in sm       # noindex page excluded from sitemap
    assert stats["sitemap_urls"] == 5     # home + about + explorer + browse + wilgate-green
    assert "<loc>https://groundwatercast.com/about/</loc>" in sm
    assert "<loc>https://groundwatercast.com/explorer/</loc>" in sm
    assert "Sitemap: https://groundwatercast.com/sitemap.xml" in (web / "robots.txt").read_text()
    # lastmod anti-churn: a second build with unchanged content keeps the same lastmod
    B.build(pack, out, today="2026-07-05", lastmod_store=store)
    sm2 = (web / "sitemap.xml").read_text(encoding="utf-8")
    assert "<lastmod>2026-06-30</lastmod>" in sm2   # carried forward, not bumped to 07-05

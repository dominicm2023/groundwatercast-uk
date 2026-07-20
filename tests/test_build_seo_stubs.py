"""Per-borehole SEO stub generation — head/JSON-LD correctness, honesty + region
degradation, noindex on zero-observation stubs, and a build() smoke test."""
import json
import re

from scripts import build_seo_stubs as B

SAMPLE = {
    "station": {"station_id": "01e2532a-1f3d-45db-a9a3-f6667b446ab0", "name": "Wilgate Green",
                "lat": 51.276283, "lon": 0.866011, "aquifer": "Principal"},
    "status": {"status": "near", "percentile": 50.6, "level": 40.6,
               "obs_date": "2026-05-09", "obs_age_days": 43},
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


def test_right_now_stat_bar_present():
    html = B._page(SAMPLE, "wilgate-green", "Kent", True)
    assert "Right now" in html                       # the summary card replaces the obs table
    assert "Latest level" in html and "40.60 mAOD" in html   # real number, crawler-visible
    assert "Breach (14 d)" in html                   # stat tile from the forecast
    assert 'id="detail-body" data-station=' in html  # JS enrichment hook


def test_build_smoke(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "a.json").write_text(json.dumps(SAMPLE), encoding="utf-8")
    # the stations/index.json catalogue (a LIST) lives in the same dir — both
    # builders must skip it, not crash on d.get() (regression: 2026-07-05 VPS)
    (pack / "index.json").write_text(json.dumps([{"station_id": "x"}]), encoding="utf-8")
    bare = {"station": {"station_id": "deadbeef-0000", "name": "Empty BH",
                        "lat": 52.0, "lon": -1.0}, "observed": {"series": []}}
    (pack / "b.json").write_text(json.dumps(bare), encoding="utf-8")
    # RiverCast expansion (2026-07-19): a flow detail file gets its OWN
    # flow-vocabulary stub at /r/<slug>/, a browse entry in the rivers
    # section, and a sitemap loc — via the flow template, never the GW one.
    flow = {"station": {"station_id": "aaaa-flow-1", "station_type": "flow",
                        "name": "Chalkbourne Gauge", "slug": "chalkbourne-gauge",
                        "river_name": "River Chalkbourne",
                        "winterbourne": True, "dry_months": [8, 9],
                        "lat": 51.0, "lon": -1.5},
            "status": {"status": "below", "percentile": 4.0, "level": 0.042,
                       "obs_date": "2026-06-29", "obs_age_days": 1},
            "observed": {"unit": "m3/s", "series": [["2026-06-29", 0.42]]},
            "forecast": {"threshold": 0.05, "threshold_source": "q95_proxy",
                         "p_below_q95_14d": 0.234}}
    (pack / "c.json").write_text(json.dumps(flow), encoding="utf-8")
    web = tmp_path / "web"
    out = web / "b"
    store = tmp_path / "lastmod.json"
    stats = B.build(pack, out, today="2026-06-30", lastmod_store=store,
                    og_manifest=tmp_path / "no-cards.json")   # raises on self-check failure
    assert stats["stubs"] == 2            # boreholes only in the GW count
    assert stats["flow_stubs"] == 1
    assert stats["river_hubs"] == 1       # one river (River Chalkbourne) → one hub
    assert stats["noindex"] == 1          # the empty borehole
    assert (out / "wilgate-green" / "index.html").exists()
    assert (out / "empty-bh" / "index.html").exists()
    assert not (out / "chalkbourne-gauge").exists()   # never through the GW template
    flow_html = (web / "r" / "chalkbourne-gauge" / "index.html").read_text(encoding="utf-8")
    assert "River Chalkbourne at Chalkbourne Gauge" in flow_html
    assert "river flow forecast" in flow_html
    assert "Gauged flow" in flow_html                  # honesty caveat is static
    assert "Hands-off-Flow" in flow_html               # Q95-proxy caveat is static
    assert "Winterbourne" in flow_html and "Aug/Sep" in flow_html
    # GW-template vocabulary never leaks into the prose (the winterbourne
    # note may legitimately mention the aquifer — physics, not template —
    # and /borehole.css is a stylesheet path, not copy)
    assert "mAOD" not in flow_html
    assert "Groundwater at" not in flow_html
    assert "this borehole" not in flow_html.lower()
    # browse + sitemap + robots
    browse = (web / "browse" / "index.html").read_text(encoding="utf-8")
    assert "Browse boreholes" in browse and "/b/wilgate-green/" in browse
    assert "Rivers &amp; flow gauges" in browse
    assert "/r/chalkbourne-gauge/" in browse
    sm = (web / "sitemap.xml").read_text(encoding="utf-8")
    assert "<loc>https://groundwatercast.com/b/wilgate-green/</loc>" in sm
    assert "/b/empty-bh/" not in sm       # noindex page excluded from sitemap
    assert "<loc>https://groundwatercast.com/r/chalkbourne-gauge/</loc>" in sm
    assert "<loc>https://groundwatercast.com/rivers/</loc>" in sm
    assert "<loc>https://groundwatercast.com/rivers/river-chalkbourne/</loc>" in sm
    # home + rivers + about + methods + contact + explorer + browse + valley
    # + wilgate-green + chalkbourne-gauge + river-chalkbourne hub
    assert stats["sitemap_urls"] == 11

    # the river hub: river-level page linking down to its gauge, ItemList JSON-LD,
    # and the gauge stub's breadcrumb climbs back up to it (crawlable pair).
    hub = (web / "rivers" / "river-chalkbourne" / "index.html").read_text(encoding="utf-8")
    assert "River Chalkbourne" in hub and "/r/chalkbourne-gauge/" in hub
    hub_jl = _jsonld(hub)
    assert {"WebSite", "WebPage", "ItemList", "Place"} <= {n["@type"] for n in hub_jl["@graph"]}
    gstub = (web / "r" / "chalkbourne-gauge" / "index.html").read_text(encoding="utf-8")
    assert '<a href="/rivers/river-chalkbourne/">River Chalkbourne</a>' in gstub
    assert "<loc>https://groundwatercast.com/methods/</loc>" in sm
    assert "<loc>https://groundwatercast.com/about/</loc>" in sm
    assert "<loc>https://groundwatercast.com/contact/</loc>" in sm
    assert "<loc>https://groundwatercast.com/explorer/</loc>" in sm
    assert "Sitemap: https://groundwatercast.com/sitemap.xml" in (web / "robots.txt").read_text()
    # lastmod anti-churn: a second build with unchanged DATA keeps the same
    # lastmod even though the daily pack rebuild bumped the derived observation
    # age (the age text must be excluded from the content hash — hashing it
    # would bump lastmod on every page every day).
    aged = json.loads(json.dumps(SAMPLE))
    aged["status"]["obs_age_days"] = 48        # five days later, no new reading
    (pack / "a.json").write_text(json.dumps(aged), encoding="utf-8")
    B.build(pack, out, today="2026-07-05", lastmod_store=store,
            og_manifest=tmp_path / "no-cards.json")
    sm2 = (web / "sitemap.xml").read_text(encoding="utf-8")
    assert "<lastmod>2026-06-30</lastmod>" in sm2   # carried forward, not bumped to 07-05


def test_pack_slug_is_authoritative(tmp_path):
    # A pack that carries the canonical slug (pack.py) wins over re-derivation.
    pack = tmp_path / "pack"
    pack.mkdir()
    d = json.loads(json.dumps(SAMPLE))
    d["station"]["slug"] = "wilgate-green-7b1f7f"    # collision-suffixed upstream
    (pack / "a.json").write_text(json.dumps(d), encoding="utf-8")
    out = tmp_path / "web" / "b"
    B.build(pack, out, today="2026-06-30", lastmod_store=tmp_path / "lm.json",
            og_manifest=tmp_path / "no-cards.json")
    assert (out / "wilgate-green-7b1f7f" / "index.html").exists()
    assert not (out / "wilgate-green").exists()


FLOW_SAMPLE = {
    "station": {"station_id": "aaaa-flow-1", "station_type": "flow",
                "name": "Chilbolton Main", "slug": "chilbolton-main",
                "river_name": "River Test", "winterbourne": False, "dry_months": [],
                "lat": 51.145, "lon": -1.437},
    "status": {"status": "below", "percentile": 8.2, "level": 1.234,
               "obs_date": "2026-07-17", "obs_age_days": 2},
    "observed": {"unit": "m3/s", "series": [["2019-01-01", 2.5], ["2026-07-17", 1.234]]},
    "forecast": {"threshold": 0.851, "threshold_source": "q95_proxy",
                 "p_below_q95_14d": 0.4123},
}


def test_flow_page_head_and_canonical():
    html = B._flow_page(FLOW_SAMPLE, "chilbolton-main", "Hampshire", True)
    assert f'<link rel="canonical" href="{B.SITE}/r/chilbolton-main/">' in html
    assert f'<meta property="og:url" content="{B.SITE}/r/chilbolton-main/">' in html
    assert ("<title>River Test at Chilbolton Main — river flow forecast, Hampshire | "
            "GroundwaterCast</title>") in html
    assert "not a drought warning" in html
    assert "Gauged flow" in html and "Hands-off-Flow" in html


def test_flow_jsonld_graph():
    jl = _jsonld(B._flow_page(FLOW_SAMPLE, "chilbolton-main", "Hampshire", True))
    types = {n["@type"] for n in jl["@graph"]}
    assert {"WebSite", "WebPage", "Dataset", "Place"} <= types
    ds = next(n for n in jl["@graph"] if n["@type"] == "Dataset")
    assert ds["license"] == B.OGL
    assert ds["variableMeasured"][0]["unitText"] == "m3/s"
    assert "River flow time series — River Test at Chilbolton Main" in ds["name"]
    assert "abstraction" in ds["description"]          # gauged-flow caveat travels
    assert ds["temporalCoverage"] == "2019-01-01/2026-07-17"


def test_flow_stat_bar_and_sentence():
    html = B._flow_page(FLOW_SAMPLE, "chilbolton-main", "Hampshire", True)
    assert "Latest flow" in html and "1.234 m³/s" in html
    assert "Q95 proxy" in html and "0.851 m³/s" in html
    assert "P(below Q95, 14 d)" in html and "41%" in html
    assert "below normal flow for the time of year" in html
    assert 'id="detail-body" data-station="aaaa-flow-1"' in html


def test_flow_title_name_variants():
    assert B._flow_title_name({"name": "Chilbolton", "river_name": "River Test"}) \
        == "River Test at Chilbolton"
    # river name embedded in the station name -> no silly "X at X"
    assert B._flow_title_name({"name": "River Bain Tattershall",
                               "river_name": "River Bain"}) == "River Bain Tattershall"
    assert B._flow_title_name({"name": "Newbourne", "river_name": None}) == "Newbourne"
    # pipe-separated candidates use the first
    assert B._flow_title_name({"name": "Amesbury", "river_name": "Hampshire Avon|River Avon"}) \
        == "Hampshire Avon at Amesbury"


def test_valley_teaser_gated_by_name_and_bbox():
    # in the valley river set AND inside the Test-valley bbox → link shown
    assert "/valley/test/" in B._valley_teaser("River Test", 51.15, -1.44)
    # right name, wrong place (a different "River Dun" outside the bbox) → nothing
    assert B._valley_teaser("River Dun", 54.0, -2.0) == ""
    # not a valley river → nothing, even inside the bbox
    assert B._valley_teaser("River Itchen", 51.05, -1.35) == ""


def test_hub_slugs_disambiguate_same_name_across_regions():
    slugs = B._assign_hub_slugs([("River Wey", "Dorset"), ("River Wey", "Surrey"),
                                 ("River Test", "Hampshire")])
    assert slugs[("River Test", "Hampshire")] == "river-test"          # unique → bare
    assert slugs[("River Wey", "Dorset")] != slugs[("River Wey", "Surrey")]  # split
    assert "dorset" in slugs[("River Wey", "Dorset")]


def test_river_hub_page_multi_gauge_and_valley_and_boreholes():
    gauges = [
        {"slug": "chilbolton-main", "name": "Chilbolton Main",
         "title": "River Test at Chilbolton Main",
         "status": {"status": "below", "percentile": 8.0}, "winterbourne": False,
         "lat": 51.15, "lon": -1.44},
        {"slug": "chilbolton-total", "name": "Chilbolton Total",
         "title": "River Test at Chilbolton Total",
         "status": {"status": "near", "percentile": 45.0}, "winterbourne": False,
         "lat": 51.15, "lon": -1.44},
    ]
    html = B._river_hub_page("River Test", "Hampshire", "river-test", gauges,
                             [("test-bh", "Test Borehole")])
    assert f'<link rel="canonical" href="{B.SITE}/rivers/river-test/">' in html
    assert "/r/chilbolton-main/" in html and "/r/chilbolton-total/" in html
    assert "2 gauges" in html
    assert "/valley/test/" in html                       # Test is a valley river, in bbox
    assert "/b/test-bh/" in html                         # feeding-borehole link is crawlable
    assert "<b>1</b> of the 2 gauges" in html            # today-summary (1 below)
    from scripts.seo_common import pct_str
    assert pct_str(0.995) == ">99%"     # honesty ceiling — never "100%"
    assert pct_str(0.003) == "<1%"      # honesty floor — never "0%"
    assert pct_str(0.125) == "13%"      # half-up like JS Math.round, not banker's
    assert pct_str(None) is None
    html = B._page(SAMPLE, "wilgate-green", "Kent", True)
    assert "&lt;1%" in html             # SAMPLE p_breach_14d=0.0 → floored

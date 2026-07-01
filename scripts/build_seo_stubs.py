"""Emit per-borehole static SEO stubs (web/b/<slug>/index.html) from the artifact
pack — the Phase-2 multi-page rollout (see docs PHASE2-SEO-SPEC).

Each stub is a HYBRID page:
  * Python bakes the crawler-facing parts into static HTML — the full <head>
    (title / meta description / canonical / Open Graph / Twitter / JSON-LD
    @graph), a persistent honesty banner, and a "recent observations" block
    (real per-borehole numbers, so the page is unique content and matches the
    Dataset markup). Scrapers don't run JS, so these MUST be static.
  * The rich interactive forecast (fan chart, folds, trigger levels, ☆) is
    rendered client-side by the explorer's own detail.js into #detail-body
    (web/bore.js), so the renderer is REUSED, never forked.

Stdlib-only; pure read of outputs/pack/stations/*.json (no network). Stubs are
build artifacts (git-ignored, regenerated each run) — like outputs/pack. Not yet
wired into run_chain; run directly:  python scripts/build_seo_stubs.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.seo_common import (STATUS_LABEL, esc, last_data_date, pct_ordinal, slug)  # noqa: E402
from scripts.geo_region import region_for  # noqa: E402

SITE = "https://groundwatercast.com"
OGL = "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
PACK_DIR = _ROOT / "outputs" / "pack" / "stations"
OUT_DIR = _ROOT / "web" / "b"
BROWSE_DIR = _ROOT / "web" / "browse"
SITEMAP_PATH = _ROOT / "web" / "sitemap.xml"
ROBOTS_PATH = _ROOT / "web" / "robots.txt"
LASTMOD_STORE = _ROOT / "outputs" / "seo_lastmod.json"   # {slug: {hash, lastmod}} — anti-churn
CAVEAT = "Indicative, experimental — not a flood or drought warning. England only."
_REQUIRED_TYPES = {"WebSite", "WebPage", "Dataset", "Place"}


def _fmt(v, dp=2):
    try:
        return f"{float(v):.{dp}f}"
    except (TypeError, ValueError):
        return None


def _status_sentence(d):
    st = d.get("status") or {}
    unit = esc((d.get("observed") or {}).get("unit") or "mAOD")
    s = st.get("status")
    if not s:
        return ("No current status — the latest reading is too old to place "
                "against the seasonal normal.")
    pc = pct_ordinal(st.get("percentile"))
    pctxt = f" (around the {pc} percentile)" if pc else ""
    lvl = _fmt(st.get("level"))
    od = st.get("obs_date")
    tail = f" Latest reading {lvl} {unit} on {esc(od)}." if (lvl and od) else ""
    return f"Currently {esc(STATUS_LABEL.get(s, s))} for the time of year{pctxt}.{tail}"


_TREND_ARROW = {"rising": "↑", "falling": "↓", "stable": "→"}


def _status_chip(d):
    """Static status chip mirroring web/detail.js statusChip — crawler-visible,
    reuses the .chip / .chip-pct styles from style.css."""
    st = d.get("status") or {}
    s = st.get("status")
    if not s:
        return '<span class="chip none">no current status</span>'
    arrow = (" " + _TREND_ARROW[st["trend"]]) if st.get("trend") in _TREND_ARROW else ""
    pc = pct_ordinal(st.get("percentile"))
    p = f' <span class="chip-pct">{pc} pct</span>' if pc else ""
    return f'<span class="chip {esc(s)}">{esc(STATUS_LABEL.get(s, s))}{arrow}{p}</span>'


def _obs_note(d):
    st = d.get("status") or {}
    od = st.get("obs_date")
    if not od:
        return ""
    age = st.get("obs_age_days")
    agetxt = f" · {age} d old" if age is not None else ""
    return f'<p class="bore-mast-obs">observed {esc(od)}{agetxt}</p>'


def _recent_obs_html(d, n=8):
    obs = d.get("observed") or {}
    series = obs.get("series") or []
    unit = esc(obs.get("unit") or "mAOD")
    if not series:
        return '<p class="caption">No observations available for this borehole yet.</p>'
    rows = "".join(
        f'<tr><th scope="row">{esc(date)}</th><td>{esc(_fmt(lvl) or "–")} {unit}</td></tr>'
        for date, lvl in reversed(series[-n:]))
    return ('<table class="bore-obs-table"><caption>Most recent observed groundwater levels</caption>'
            '<thead><tr><th scope="col">Date</th><th scope="col">Level</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>')


def _jsonld(d, sl, region, last_date):
    stn = d.get("station") or {}
    sid = stn.get("station_id")
    name = stn.get("name") or sid
    lat, lon = stn.get("lat"), stn.get("lon")
    aquifer = stn.get("aquifer")
    obs = d.get("observed") or {}
    unit = obs.get("unit") or "mAOD"
    series = obs.get("series") or []
    base = f"{SITE}/b/{sl}/"
    region_phrase = f" in {region}" if region else ""
    kw = ["groundwater", "England", "open data"]
    if aquifer:
        kw.append(aquifer)
    if region:
        kw.append(region)
    dataset = {
        "@type": "Dataset", "@id": base + "#dataset",
        "name": f"Groundwater level time series — {name}" + (f" ({sid})" if sid else ""),
        "description": (
            f"Daily groundwater-level observations and an indicative, uncalibrated, experimental "
            f"probabilistic forecast for the {name} borehole"
            + (f" ({aquifer})" if aquifer else "") + f"{region_phrase}, England. "
            "Derived from open Environment Agency hydrology data under the Open Government Licence "
            "v3.0. This is NOT an authoritative flood or drought warning, and is England-only. "
            "Provided for information and research; do not use for operational flood or drought "
            "decisions."),
        "creativeWorkStatus": ("Experimental — indicative/uncalibrated; not an official flood or "
                               "drought warning"),
        "url": base, "isAccessibleForFree": True, "license": OGL,
        "creator": {"@type": "Organization", "name": "GroundwaterCast", "url": SITE + "/"},
        "keywords": kw,
        "variableMeasured": [{"@type": "PropertyValue", "name": "Groundwater level", "unitText": unit}],
        "spatialCoverage": {"@id": base + "#place"},
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                          "contentUrl": f"{SITE}/pack/stations/{sid}.json"}],
    }
    if sid:
        dataset["identifier"] = sid
        ea = f"https://environment.data.gov.uk/hydrology/station/{sid}"
        dataset["isBasedOn"] = ea
        dataset["sameAs"] = ea
    if series and last_date:
        dataset["temporalCoverage"] = f"{series[0][0]}/{last_date}"
    if last_date:
        dataset["dateModified"] = last_date

    addr = {"@type": "PostalAddress", "addressCountry": "GB"}
    if region:
        addr["addressRegion"] = region
    place = {"@type": "Place", "@id": base + "#place", "name": f"{name} borehole",
             "address": addr, "containedInPlace": {"@type": "Country", "name": "England"}}
    if lat is not None and lon is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}

    webpage = {"@type": "WebPage", "@id": base + "#webpage", "name": f"Groundwater at {name}",
               "url": base, "inLanguage": "en-GB", "isPartOf": {"@id": SITE + "/#website"},
               "about": {"@id": base + "#dataset"}, "mainEntity": {"@id": base + "#dataset"}}
    if last_date:
        webpage["dateModified"] = last_date

    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": SITE + "/#website", "name": "GroundwaterCast UK",
         "url": SITE + "/", "inLanguage": "en-GB"},
        webpage, dataset, place]}
    # Embed-safe: prevent an in-string </script> from closing the tag.
    return json.dumps(graph, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


def _head(d, sl, region, indexable):
    stn = d.get("station") or {}
    name = stn.get("name") or stn.get("station_id") or "borehole"
    status_label = STATUS_LABEL.get((d.get("status") or {}).get("status"), "no current status")
    rtitle = f", {region}" if region else ""        # title / og:description
    rparen = f" ({region})" if region else ""        # description / og:title
    jl = _jsonld(d, sl, region, last_data_date(d))
    robots = "index,follow,max-image-preview:large" if indexable else "noindex,follow"
    # NB: og:image / twitter:image are added by the card builder (build_og_cards),
    # a later step — omitted here so the build-time self-check never points at a
    # missing PNG.
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Groundwater at {esc(name)}{esc(rtitle)} — indicative forecast | GroundwaterCast</title>'
        f'<meta name="description" content="Indicative groundwater level for {esc(name)}{esc(rparen)}: '
        f'currently {esc(status_label)} for the time of year. Experimental 14-day open-data outlook — '
        f'not a flood or drought warning. England only.">'
        f'<link rel="canonical" href="{SITE}/b/{esc(sl)}/">'
        f'<meta name="robots" content="{robots}">'
        '<meta name="theme-color" content="#1a3a5c">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="GroundwaterCast UK">'
        '<meta property="og:locale" content="en_GB">'
        f'<meta property="og:title" content="Groundwater at {esc(name)}{esc(rparen)} (indicative)">'
        f'<meta property="og:description" content="Experimental 14-day open-data groundwater outlook '
        f'for {esc(name)}{esc(rtitle)}. Not a flood or drought warning. England only.">'
        f'<meta property="og:url" content="{SITE}/b/{esc(sl)}/">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="Groundwater at {esc(name)}{esc(rparen)} (indicative)">'
        '<meta name="twitter:description" content="Experimental 14-day open-data outlook — not a '
        'flood/drought warning. England only.">'
        f'<script type="application/ld+json">{jl}</script>'
    )


def _page(d, sl, region, indexable):
    stn = d.get("station") or {}
    sid = stn.get("station_id")
    name = stn.get("name") or sid or "Borehole"
    lat, lon = stn.get("lat"), stn.get("lon")
    sub = " · ".join(b for b in [
        esc(stn.get("aquifer")) if stn.get("aquifer") else None,
        esc(region) if region else None,
        (f"{_fmt(lat, 4)}°N {_fmt(lon, 4)}°E" if lat is not None and lon is not None else None),
        (f"EA {esc(str(sid)[:8])}" if sid else None),
    ] if b)
    crumb = ('<a href="/">Home</a> / <a href="/explorer/">Map</a> / <a href="/browse/">Browse</a> / '
             + (f"{esc(region)} / " if region else "") + esc(name))
    return (
        '<!DOCTYPE html><html lang="en-GB"><head>' + _head(d, sl, region, indexable) + "</head><body>"
        '<header class="bore-top"><a class="bore-brand" href="/"><span class="bore-logo">💧</span> '
        'GroundwaterCast&nbsp;UK</a><nav><a href="/explorer/">Explorer</a> <a href="/browse/">Browse</a> '
        '<a href="/about/">About</a></nav></header>'
        '<div class="bore-wrap">'
        f'<nav class="bore-crumb">{crumb}</nav>'
        '<div class="bore-masthead"><div class="bore-mast-id">'
        f'<h1 class="bore-h1">{esc(name)}</h1>'
        f'<p class="bore-sub">{sub}</p></div>'
        f'<div class="bore-mast-status">{_status_chip(d)}{_obs_note(d)}</div></div>'
        f'<p class="bore-caveat">⚠ {esc(CAVEAT)} <a href="/about/">How this works</a>.</p>'
        '<section class="bore-obs"><h2>Recent observations</h2>'
        f'<p>{_status_sentence(d)}</p>{_recent_obs_html(d)}'
        '<p class="caption">Source: Environment Agency hydrology (Open Government Licence v3.0). '
        f'Full data: <a href="/pack/stations/{esc(sid)}.json">JSON</a>'
        + (f' · <a href="https://environment.data.gov.uk/hydrology/station/{esc(sid)}" '
           'rel="noopener">EA record ↗</a>' if sid else "")
        + '</p></section>'
        '<section class="bore-detail"><h2>Forecast &amp; full detail</h2>'
        f'<div id="detail-body" data-station="{esc(sid)}"><p class="caption">Loading the interactive '
        'forecast…</p></div>'
        '<noscript><p class="caption">The interactive forecast needs JavaScript; the observed levels '
        'above are static.</p></noscript></section></div>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, uncalibrated research '
        'forecast.</b> Not a flood or drought warning; not for safety-critical use. England-only. '
        'Independent open-source project — not affiliated with or endorsed by any employer, the '
        'Environment Agency, ECMWF, or any water company.</p>'
        '<p class="caption">Contains EA data (OGL v3) · ECMWF Open Data (CC-BY-4.0) · Copernicus '
        'ERA5/SEAS5 · Free &amp; open source (MIT).</p></footer>'
        '<script src="/config.js"></script><script src="/contract_fields.js"></script>'
        '<script src="/charts.js"></script><script src="/detail.js"></script>'
        '<script src="/watchlist.js"></script><script src="/ladders.js"></script>'
        '<script src="/bore.js"></script></body></html>'
    )


_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _check(html, sl, problems):
    head = html.split("</head>", 1)[0]
    if f'canonical" href="{SITE}/b/{sl}/"' not in head:
        problems.append(f"{sl}: canonical")
    if f'og:url" content="{SITE}/b/{sl}/"' not in head:
        problems.append(f"{sl}: og:url != canonical")
    title = re.search(r"<title>(.*?)</title>", head)
    if title and ("None" in title.group(1) or "null" in title.group(1)):
        problems.append(f"{sl}: leaked None/null in title")
    if "creativecommons.org" in head:
        problems.append(f"{sl}: CC licence regression (must be OGL)")
    m = _JSONLD_RE.search(head)
    if not m:
        problems.append(f"{sl}: no JSON-LD")
        return
    try:
        jl = json.loads(m.group(1).replace("<\\/", "</"))
    except Exception as exc:
        problems.append(f"{sl}: JSON-LD parse error: {exc}")
        return
    types = {n.get("@type") for n in jl.get("@graph", [])}
    if not _REQUIRED_TYPES <= types:
        problems.append(f"{sl}: JSON-LD missing types {_REQUIRED_TYPES - types}")


def _mini_shell(title, canonical, body):
    return (
        '<!DOCTYPE html><html lang="en-GB"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        f'<link rel="canonical" href="{canonical}">'
        '<meta name="robots" content="index,follow"><meta name="theme-color" content="#1a3a5c">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        '</head><body>'
        '<header class="bore-top"><a class="bore-brand" href="/"><span class="bore-logo">💧</span> '
        'GroundwaterCast&nbsp;UK</a><nav><a href="/explorer/">Explorer</a> <a href="/browse/">Browse</a> '
        '<a href="/about/">About</a></nav></header>'
        f'<div class="bore-wrap">{body}</div>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, uncalibrated research '
        'forecast.</b> Not a flood or drought warning. England-only. Independent open-source '
        'project — not affiliated with or endorsed by any employer.</p></footer></body></html>'
    )


def _browse_html(entries):
    """entries: list of (slug, name, region). A crawlable, county-grouped directory."""
    by_region: dict[str, list] = {}
    for sl, name, region in entries:
        by_region.setdefault(region or "Other", []).append((sl, name))
    parts = ['<h1 class="bore-h1">Browse boreholes</h1>',
             f'<p class="bore-sub">All {len(entries)} monitored boreholes with a forecast page, '
             'by ceremonial county.</p>',
             f'<p class="bore-caveat">⚠ {esc(CAVEAT)}</p>']
    for region in sorted(by_region):
        parts.append(f'<h2 class="bore-browse-h">{esc(region)} '
                     f'<span class="caption">({len(by_region[region])})</span></h2>'
                     '<ul class="bore-browse-list">')
        for sl, name in sorted(by_region[region], key=lambda x: (x[1] or "").lower()):
            parts.append(f'<li><a href="/b/{esc(sl)}/">{esc(name)}</a></li>')
        parts.append("</ul>")
    return _mini_shell("Browse boreholes — GroundwaterCast UK", f"{SITE}/browse/", "".join(parts))


def _sitemap_xml(urls):
    rows = "".join(f"<url><loc>{loc}</loc><lastmod>{lm}</lastmod></url>" for loc, lm in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + rows + "</urlset>")


def build(pack_dir: Path = PACK_DIR, out_dir: Path = OUT_DIR, today: str | None = None,
          lastmod_store: Path = LASTMOD_STORE) -> dict:
    today = today or date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    web_dir = out_dir.parent
    browse_dir = web_dir / "browse"
    sitemap_path = web_dir / "sitemap.xml"
    robots_path = web_dir / "robots.txt"
    store = {}
    if lastmod_store.exists():
        try:
            store = json.loads(lastmod_store.read_text(encoding="utf-8"))
        except Exception:
            store = {}
    new_store: dict[str, dict] = {}
    seen: dict[str, str] = {}
    entries: list[tuple] = []                                  # (slug, name, region)
    # home + top-level pages + directory (all editorial, always fresh-dated)
    urls = [(f"{SITE}/", today), (f"{SITE}/about/", today),
            (f"{SITE}/explorer/", today), (f"{SITE}/browse/", today)]
    n = noindex = noregion = 0
    problems: list[str] = []
    for fp in sorted(pack_dir.glob("*.json")):   # sorted → deterministic slug collisions
        d = json.loads(fp.read_text(encoding="utf-8"))
        stn = d.get("station") or {}
        sid = stn.get("station_id")
        name = stn.get("name") or sid
        sl = slug(name)
        if sl in seen and seen[sl] != sid:
            sl = f"{sl}-{str(sid)[:6]}"
        seen[sl] = sid
        region = region_for(stn.get("lat"), stn.get("lon"))
        if not region:
            noregion += 1
        indexable = bool((d.get("observed") or {}).get("series"))   # noindex zero-observation stubs
        if not indexable:
            noindex += 1
        html = _page(d, sl, region, indexable)
        _check(html, sl, problems)
        dst = out_dir / sl
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "index.html").write_text(html, encoding="utf-8")
        n += 1
        entries.append((sl, name, region))
        # lastmod anti-churn: bump only when the page's indexable content changed
        h = hashlib.sha256(html.encode("utf-8")).hexdigest()
        prev = store.get(sl)
        lm = prev["lastmod"] if (prev and prev.get("hash") == h) else today
        new_store[sl] = {"hash": h, "lastmod": lm}
        if indexable:                                            # don't sitemap noindex pages
            urls.append((f"{SITE}/b/{sl}/", lm))

    browse_dir.mkdir(parents=True, exist_ok=True)
    (browse_dir / "index.html").write_text(_browse_html(entries), encoding="utf-8")
    sitemap_path.write_text(_sitemap_xml(urls), encoding="utf-8")
    robots_path.write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    lastmod_store.parent.mkdir(parents=True, exist_ok=True)
    lastmod_store.write_text(json.dumps(new_store), encoding="utf-8")

    print(f"wrote {n} stubs + /browse + sitemap ({len(urls)} urls) + robots  "
          f"(noindex {noindex}, no-region {noregion})")
    if problems:
        for p in problems[:25]:
            print("  FAIL:", p)
        raise SystemExit(f"{len(problems)} stub self-check failure(s)")
    return {"stubs": n, "noindex": noindex, "noregion": noregion, "sitemap_urls": len(urls)}


if __name__ == "__main__":
    build()

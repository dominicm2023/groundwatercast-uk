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

import calendar
import hashlib
import json
import math
import re
import shutil
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.seo_common import (STATUS_LABEL, esc, last_data_date, pct_ordinal,
                                pct_str, slug)  # noqa: E402
from scripts.geo_region import region_for  # noqa: E402

SITE = "https://groundwatercast.com"
OGL = "http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
PACK_DIR = _ROOT / "outputs" / "pack" / "stations"
OUT_DIR = _ROOT / "web" / "b"
BROWSE_DIR = _ROOT / "web" / "browse"
SITEMAP_PATH = _ROOT / "web" / "sitemap.xml"
ROBOTS_PATH = _ROOT / "web" / "robots.txt"
LASTMOD_STORE = _ROOT / "outputs" / "seo_lastmod.json"   # {slug: {hash, lastmod}} — anti-churn
OG_MANIFEST = _ROOT / "outputs" / "og_cards.json"        # {slug: share.<hash>.png} from build_og_cards
CAVEAT = "Indicative, experimental — not a flood or drought warning. England only."
FLOW_CAVEAT = ("Indicative, experimental — not a drought warning. Gauged flow, "
               "including abstraction and discharge effects. England only.")
FLOW_STATUS_LABEL = {"below": "below normal flow", "near": "near normal flow",
                     "above": "above normal flow"}
_REQUIRED_TYPES = {"WebSite", "WebPage", "Dataset", "Place"}
# River hub pages (/rivers/<river>/) are collection pages, not Datasets.
_REQUIRED_HUB_TYPES = {"WebSite", "WebPage", "ItemList", "Place"}

# Rivers modelled in the /valley/test/ 3-D visualisation (the COURSES in
# scripts/build_valley_rivers.py). A river page links to the valley only when
# its river is in this set AND the gauge/hub sits inside the valley's bbox —
# the bbox guard defends against name collisions (there are several "River
# Dun"s nationally; only the Hampshire one is in the Test-valley model).
VALLEY_RIVERS = {"River Test", "River Anton", "River Dever", "River Dun",
                 "Bourne Rivulet", "Wallop Brook", "Pillhill Brook"}
VALLEY_BBOX = (-1.66, 50.87, -1.20, 51.30)   # matches build_valley_rivers.DEFAULT_BBOX
VALLEY_URL = "/valley/test/"



# THE canonical site nav for every non-map shell page (borehole stubs, /browse,
# and — kept in sync by hand — the static /about and /methods shells). One
# builder, one link set: nav drift across shells is how the mobile-overflow
# bug shipped five separate times.
def _topnav(current: str | None = None) -> str:
    links = (("explorer", "/explorer/", "Explorer"), ("rivers", "/rivers/", "Rivers"),
             ("browse", "/browse/", "Browse"),
             ("about", "/about/", "About"), ("methods", "/methods/", "Methods"))
    nav = " ".join(
        f'<a href="{href}"{" aria-current=\"page\"" if key == current else ""}>{label}</a>'
        for key, href, label in links)
    return ('<header class="bore-top"><a class="bore-brand" href="/">'
            '<img class="bore-logo" src="/favicon.svg" width="24" height="24" alt=""> '
            f'GroundwaterCast&nbsp;UK</a><nav>{nav}</nav></header>')


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


def _stat_bar(d):
    """A crawler-visible 'Right now' strip of key numbers (replaces the raw
    observation table). Only tiles with a real value are emitted."""
    st = d.get("status") or {}
    fc = d.get("forecast") or {}
    unit = esc((d.get("observed") or {}).get("unit") or "mAOD")
    tiles = []
    lvl = _fmt(st.get("level"))
    if lvl:
        tiles.append(("Latest level", f"{lvl} {unit}"))
    pc = pct_ordinal(st.get("percentile"))
    if pc:
        tiles.append(("Percentile (month)", pc))
    try:
        if st.get("sgi") is not None:
            tiles.append(("SGI", f"{float(st['sgi']):+.1f}"))
    except (TypeError, ValueError):
        pass
    tr = st.get("trend")
    if tr in _TREND_ARROW:
        tiles.append(("7-day trend", f"{_TREND_ARROW[tr]} {esc(tr)}"))
    od = st.get("obs_date")
    if od:
        age = st.get("obs_age_days")
        tiles.append(("Observed", esc(od) + (f" · {age} d" if age is not None else "")))
    # Same formatter as detail.js pct() (via seo_common.pct_str): half-up
    # rounding + the <1% / >99% honesty floor & ceiling, so the static tile and
    # the interactive panel on the SAME page can never disagree.
    breach = pct_str(fc.get("p_breach_14d"))
    if breach is not None:
        tiles.append(("Breach (14 d)", esc(breach)))
    if not tiles:
        return ""
    cells = "".join(f'<div class="bore-stat"><span class="bs-k">{esc(k)}</span>'
                    f'<span class="bs-v">{v}</span></div>' for k, v in tiles)
    return f'<div class="bore-stats">{cells}</div>'


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
            "v3.0. This is NOT an authoritative flood or drought warning, and is England only. "
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


def _head(d, sl, region, indexable, card: str | None = None, title_extra: str = ""):
    stn = d.get("station") or {}
    name = stn.get("name") or stn.get("station_id") or "borehole"
    status_label = STATUS_LABEL.get((d.get("status") or {}).get("status"), "no current status")
    rtitle = f", {region}" if region else ""        # title / og:description
    rparen = f" ({region})" if region else ""        # description / og:title
    jl = _jsonld(d, sl, region, last_data_date(d))
    robots = "index,follow,max-image-preview:large" if indexable else "noindex,follow"
    # Only claim a "14-day outlook" when this borehole actually carries a
    # forecast (most don't — forecast is None); otherwise the meta description
    # oversells an abandoned-looking page. Append the last observed reading so
    # every description is genuinely unique (level+date differ per borehole),
    # not 686 byte-identical boilerplate.
    st = d.get("status") or {}
    has_fc = bool(d.get("forecast"))
    unit_b = esc((d.get("observed") or {}).get("unit") or "mAOD")
    lvl_b, od_b = _fmt(st.get("level")), st.get("obs_date")
    reading = f" Last reading {lvl_b} {unit_b} on {esc(od_b)}." if (lvl_b and od_b) else ""
    tail = ("Experimental 14-day open-data outlook — not a flood or drought warning. England only."
            if has_fc else
            "Daily open-data groundwater monitoring — not a flood or drought warning. England only.")
    desc = (f"Groundwater level for {esc(name)}{esc(rparen)}: currently {esc(status_label)} "
            f"for the time of year.{reading} {tail}")
    og_desc = ((f"Experimental 14-day open-data groundwater outlook for {esc(name)}{esc(rtitle)}."
                if has_fc else
                f"Daily open-data groundwater level for {esc(name)}{esc(rtitle)}.{reading}")
               + " Not a flood or drought warning. England only.")
    tw_desc = ("Experimental 14-day open-data outlook — not a flood/drought warning. England only."
               if has_fc else
               "Daily open-data groundwater monitoring — not a flood/drought warning. England only.")
    # og:image: the status-NEUTRAL share card rendered by build_og_cards (the
    # stage before this one), addressed by content-hash filename so scraper
    # caches bust exactly when the card really changed. Omitted when the card
    # builder didn't run (no resvg) — never a broken URL.
    og_image = (
        f'<meta property="og:image" content="{SITE}/b/{esc(sl)}/{esc(card)}">'
        '<meta property="og:image:width" content="1200">'
        '<meta property="og:image:height" content="630">'
        f'<meta property="og:image:alt" content="GroundwaterCast share card for '
        f'{esc(name)}: observed groundwater sparkline — indicative, not a flood or '
        'drought warning">'
        f'<meta name="twitter:image" content="{SITE}/b/{esc(sl)}/{esc(card)}">'
    ) if card else ""
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Groundwater at {esc(name)}{esc(rtitle)}{esc(title_extra)} — indicative forecast | GroundwaterCast</title>'
        f'<meta name="description" content="{desc}">'
        f'<link rel="canonical" href="{SITE}/b/{esc(sl)}/">'
        f'<meta name="robots" content="{robots}">'
        '<meta name="theme-color" content="#1a3a5c">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="GroundwaterCast UK">'
        '<meta property="og:locale" content="en_GB">'
        f'<meta property="og:title" content="Groundwater at {esc(name)}{esc(rparen)} (indicative)">'
        f'<meta property="og:description" content="{og_desc}">'
        f'<meta property="og:url" content="{SITE}/b/{esc(sl)}/">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="Groundwater at {esc(name)}{esc(rparen)} (indicative)">'
        f'<meta name="twitter:description" content="{tw_desc}">'
        + og_image +
        f'<script type="application/ld+json">{jl}</script>'
    )


def _page(d, sl, region, indexable, card: str | None = None, feeds=None, title_extra: str = ""):
    stn = d.get("station") or {}
    sid = stn.get("station_id")
    name = stn.get("name") or sid or "Borehole"
    # Reciprocal link: the river hub links DOWN to its feeding boreholes, so the
    # borehole should link back UP to the river(s) it feeds (crawlable, static).
    feeds_html = ""
    if feeds:
        seen_h, links = set(), []
        for f in feeds:
            if f["hub_url"] not in seen_h:
                seen_h.add(f["hub_url"])
                links.append(f'<a href="{f["hub_url"]}">{esc(f["river"])}</a>')
        if links:
            feeds_html = ('<p class="bore-feeds">Feeds ' + " · ".join(links)
                          + ' — see the river’s gauges and low-flow forecast.</p>')
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
        '<!DOCTYPE html><html lang="en-GB"><head>'
        + _head(d, sl, region, indexable, card, title_extra) + "</head><body>"
        + _topnav("explorer") +
        '<main><div class="bore-wrap">'
        f'<nav class="bore-crumb">{crumb}</nav>'
        '<div class="bore-masthead"><div class="bore-mast-id">'
        f'<h1 class="bore-h1">{esc(name)}</h1>'
        f'<p class="bore-sub">{sub}</p>'
        '<div class="bore-actions" id="bore-actions"></div></div>'
        f'<div class="bore-mast-status">{_status_chip(d)}{_obs_note(d)}</div></div>'
        f'<p class="bore-caveat">⚠ {esc(CAVEAT)} <a href="/about/">How this works</a>.</p>'
        '<section class="bore-summary"><h2>Right now</h2>'
        f'<p class="bore-status-line">{_status_sentence(d)}</p>{feeds_html}{_stat_bar(d)}'
        '<p class="caption">Source: Environment Agency hydrology (Open Government Licence v3.0). '
        f'Full data: <a href="/pack/stations/{esc(sid)}.json">JSON</a>'
        + (f' · <a href="https://environment.data.gov.uk/hydrology/station/{esc(sid)}" '
           'rel="noopener">EA record ↗</a>' if sid else "")
        + '</p></section>'
        '<section class="bore-detail">'
        f'<div id="detail-body" data-station="{esc(sid)}"><p class="caption">Loading the interactive '
        'forecast…</p></div>'
        '<noscript><p class="caption">The interactive forecast needs JavaScript; the observed levels '
        'above are static.</p></noscript></section></div></main>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, uncalibrated research '
        'forecast.</b> Not a flood or drought warning; not for safety-critical use. England only. '
        'Independent open-source project — not affiliated with or endorsed by any employer, the '
        'Environment Agency, ECMWF, or any water company.</p>'
        '<p class="caption">Contains EA data (OGL v3) · ECMWF Open Data (CC-BY-4.0) · Copernicus '
        'ERA5/SEAS5 · Free &amp; open source (MIT) · <a href="/contact/">Contact</a>.</p></footer>'
        '<script src="/config.js"></script><script src="/contract_fields.js"></script>'
        '<script src="/charts.js"></script><script src="/detail.js"></script>'
        '<script src="/watchlist.js"></script><script src="/ladders.js"></script>'
        '<script src="/bore.js"></script></body></html>'
    )


# ---------------------------------------------------------------------------
# RiverCast (flow gauge) stub template — /r/<slug>/. Its own template, NOT the
# GW one: every sentence above speaks groundwater ("borehole", "aquifer",
# mAOD). The flow template speaks gauged flow, Q95, winterbournes — and
# carries the flow honesty caveats on every crawlable page. The interactive
# body is the same reused detail.js (isFlow branch) via bore.js.
# ---------------------------------------------------------------------------

def _flow_title_name(stn) -> str:
    """"River Test at Chilbolton" when the river name is known, else the
    station name alone — the brief's "River <name> at <site>" pattern."""
    name = stn.get("name") or stn.get("station_id") or "gauge"
    river = (stn.get("river_name") or "").split("|")[0].strip()
    if not river or river.lower() in name.lower():
        return name                     # river unknown, or already in the name
    return f"{river} at {name}"


def _flow_status_sentence(d):
    st = d.get("status") or {}
    s = st.get("status")
    if not s:
        return ("No current status — the latest reading is too old to place "
                "against the seasonal flow normal.")
    pc = pct_ordinal(st.get("percentile"))
    pctxt = f" (around the {pc} percentile)" if pc else ""
    lvl = _fmt(st.get("level"), 3)
    od = st.get("obs_date")
    tail = f" Latest gauged flow {lvl} m³/s on {esc(od)}." if (lvl and od) else ""
    label = FLOW_STATUS_LABEL.get(s, s)
    return f"Currently {esc(label)} for the time of year{pctxt}.{tail}"


def _flow_stat_bar(d):
    st = d.get("status") or {}
    fc = d.get("forecast") or {}
    tiles = []
    lvl = _fmt(st.get("level"), 3)
    if lvl:
        tiles.append(("Latest flow", f"{lvl} m³/s"))
    pc = pct_ordinal(st.get("percentile"))
    if pc:
        tiles.append(("Percentile (month)", pc))
    tr = st.get("trend")
    if tr in _TREND_ARROW:
        tiles.append(("7-day trend", f"{_TREND_ARROW[tr]} {esc(tr)}"))
    od = st.get("obs_date")
    if od:
        age = st.get("obs_age_days")
        tiles.append(("Observed", esc(od) + (f" · {age} d" if age is not None else "")))
    q95 = _fmt(fc.get("threshold"), 3)
    if q95:
        tiles.append(("Q95 proxy", f"{q95} m³/s"))
    below = pct_str(fc.get("p_below_q95_14d"))
    if below is not None:
        tiles.append(("P(below Q95, 14 d)", esc(below)))
    if not tiles:
        return ""
    cells = "".join(f'<div class="bore-stat"><span class="bs-k">{esc(k)}</span>'
                    f'<span class="bs-v">{v}</span></div>' for k, v in tiles)
    return f'<div class="bore-stats">{cells}</div>'


def _flow_winterbourne_note(stn):
    # Same strictness as the geojson feature flag (pack.py): the crawlable
    # winterbourne claim requires a RECURRING dry season (dry_months
    # non-empty), never the detail's literal any-zero-day flag — one datum
    # artifact must not put a permanent "dries by design" line on an indexed
    # page that the landing/explorer then contradict.
    months = [int(m) for m in (stn.get("dry_months") or []) if 1 <= int(m) <= 12]
    if not (stn.get("winterbourne") and months):
        return ""
    when = " — typically dry around " + "/".join(
        calendar.month_abbr[m] for m in months)
    return (f'<p class="bore-mast-obs">Winterbourne: this chalk stream dries '
            f'by design when the aquifer is low{esc(when)}.</p>')


def _flow_jsonld(d, sl, region, last_date):
    stn = d.get("station") or {}
    sid = stn.get("station_id")
    tname = _flow_title_name(stn)
    lat, lon = stn.get("lat"), stn.get("lon")
    series = (d.get("observed") or {}).get("series") or []
    base = f"{SITE}/r/{sl}/"
    region_phrase = f" in {region}" if region else ""
    kw = ["river flow", "low flow", "chalk stream", "England", "open data", "Q95"]
    if region:
        kw.append(region)
    if stn.get("winterbourne"):
        kw.append("winterbourne")
    dataset = {
        "@type": "Dataset", "@id": base + "#dataset",
        "name": f"River flow time series — {tname}" + (f" ({sid})" if sid else ""),
        "description": (
            f"Daily mean gauged river flow observations and an indicative, experimental "
            f"14-day low-flow forecast for {tname}{region_phrase}, England, including the "
            "probability of falling below the gauge's Q95 low-flow threshold (a "
            "climatological proxy, not a licence Hands-off-Flow value). Gauged flow — as "
            "measured, including abstraction and discharge effects; rating curves are "
            "least accurate at low flows. Derived from open Environment Agency hydrology "
            "data under the Open Government Licence v3.0. NOT a drought warning; England "
            "only. Provided for information and research."),
        "creativeWorkStatus": ("Experimental — indicative; not an official drought warning"),
        "url": base, "isAccessibleForFree": True, "license": OGL,
        "creator": {"@type": "Organization", "name": "GroundwaterCast", "url": SITE + "/"},
        "keywords": kw,
        "variableMeasured": [{"@type": "PropertyValue", "name": "River flow",
                              "unitText": "m3/s"}],
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
    place = {"@type": "Place", "@id": base + "#place", "name": f"{tname} flow gauge",
             "address": addr, "containedInPlace": {"@type": "Country", "name": "England"}}
    if lat is not None and lon is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}

    webpage = {"@type": "WebPage", "@id": base + "#webpage",
               "name": f"River flow at {tname}",
               "url": base, "inLanguage": "en-GB", "isPartOf": {"@id": SITE + "/#website"},
               "about": {"@id": base + "#dataset"}, "mainEntity": {"@id": base + "#dataset"}}
    if last_date:
        webpage["dateModified"] = last_date

    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": SITE + "/#website", "name": "GroundwaterCast UK",
         "url": SITE + "/", "inLanguage": "en-GB"},
        webpage, dataset, place]}
    return json.dumps(graph, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


def _flow_head(d, sl, region, indexable):
    stn = d.get("station") or {}
    tname = _flow_title_name(stn)
    status_label = FLOW_STATUS_LABEL.get(
        (d.get("status") or {}).get("status"), "no current status")
    rtitle = f", {region}" if region else ""
    rparen = f" ({region})" if region else ""
    jl = _flow_jsonld(d, sl, region, last_data_date(d))
    robots = "index,follow,max-image-preview:large" if indexable else "noindex,follow"
    return (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(tname)} — river flow forecast{esc(rtitle)} | GroundwaterCast</title>'
        f'<meta name="description" content="Daily low-flow forecast for {esc(tname)}{esc(rparen)}: '
        f'currently {esc(status_label)} for the season. 14-day gauged-flow outlook with the chance '
        f'of falling below the Q95 low-flow threshold. Open data; indicative, not a drought warning.">'
        f'<link rel="canonical" href="{SITE}/r/{esc(sl)}/">'
        f'<meta name="robots" content="{robots}">'
        '<meta name="theme-color" content="#1a3a5c">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="GroundwaterCast UK">'
        '<meta property="og:locale" content="en_GB">'
        f'<meta property="og:title" content="{esc(tname)} — river flow forecast (indicative)">'
        f'<meta property="og:description" content="Experimental 14-day low-flow outlook for '
        f'{esc(tname)}{esc(rtitle)}. Gauged flow, open data. Not a drought warning.">'
        f'<meta property="og:url" content="{SITE}/r/{esc(sl)}/">'
        # No per-gauge share card in v1 — fall back to the site default card so
        # shares on X/Slack/LinkedIn render an image (was blank on all /r/ pages).
        f'<meta property="og:image" content="{SITE}/og/default.png">'
        '<meta property="og:image:width" content="1200">'
        '<meta property="og:image:height" content="630">'
        '<meta name="twitter:card" content="summary_large_image">'
        f'<meta name="twitter:title" content="{esc(tname)} — river flow forecast (indicative)">'
        '<meta name="twitter:description" content="Experimental 14-day low-flow outlook — '
        'gauged flow, open data. Not a drought warning. England only.">'
        f'<meta name="twitter:image" content="{SITE}/og/default.png">'
        f'<script type="application/ld+json">{jl}</script>'
    )


def _flow_page(d, sl, region, indexable, hub_url=None):
    stn = d.get("station") or {}
    sid = stn.get("station_id")
    name = stn.get("name") or sid or "Gauge"
    river = (stn.get("river_name") or "").split("|")[0].strip()
    lat, lon = stn.get("lat"), stn.get("lon")
    sub = " · ".join(b for b in [
        esc(river) if river else None,
        # same strict seasonal read as the geojson flag / winterbourne note
        "winterbourne" if (stn.get("winterbourne") and stn.get("dry_months")) else None,
        esc(region) if region else None,
        (f"{_fmt(lat, 4)}°N {_fmt(lon, 4)}°E" if lat is not None and lon is not None else None),
        (f"EA {esc(str(sid)[:8])}" if sid else None),
    ] if b)
    # Breadcrumb climbs up to the river hub (/rivers/<river>/) when one exists,
    # so the gauge stub and its river page form a crawlable category→item pair.
    crumb = ('<a href="/">Home</a> / <a href="/rivers/">Rivers</a> / '
             + (f'<a href="{hub_url}">{esc(river)}</a> / ' if hub_url and river else '')
             + '<a href="/explorer/#rivers=1">Map</a> / '
             + (f"{esc(region)} / " if region else "") + esc(name))
    return (
        '<!DOCTYPE html><html lang="en-GB"><head>' + _flow_head(d, sl, region, indexable)
        + "</head><body>"
        + _topnav("rivers") +
        '<main><div class="bore-wrap">'
        f'<nav class="bore-crumb">{crumb}</nav>'
        '<div class="bore-masthead"><div class="bore-mast-id">'
        f'<h1 class="bore-h1">{esc(_flow_title_name(stn))}</h1>'
        f'<p class="bore-sub">{sub}</p>'
        '<div class="bore-actions" id="bore-actions"></div></div>'
        f'<div class="bore-mast-status">{_status_chip(d)}{_obs_note(d)}'
        f'{_flow_winterbourne_note(stn)}</div></div>'
        f'<p class="bore-caveat">⚠ {esc(FLOW_CAVEAT)} <a href="/methods/">How this works</a>.</p>'
        '<section class="bore-summary"><h2>Right now</h2>'
        f'<p class="bore-status-line">{_flow_status_sentence(d)}</p>{_flow_stat_bar(d)}'
        '<p class="caption">Q95 is computed from this gauge\'s own record — a climatological '
        'low-flow proxy, not the Hands-off-Flow condition on any abstraction licence. Rating '
        'curves (the stage-to-flow conversion) are least accurate at low flows.</p>'
        '<p class="caption">Source: Environment Agency hydrology (Open Government Licence v3.0).'
        + (f' Full data: <a href="/pack/stations/{esc(sid)}.json">JSON</a>'
           f' · <a href="https://environment.data.gov.uk/hydrology/station/{esc(sid)}" '
           'rel="noopener">EA record ↗</a>' if sid else "")
        + '</p></section>'
        + _valley_teaser(river, lat, lon) +
        '<section class="bore-detail">'
        f'<div id="detail-body" data-station="{esc(sid)}"><p class="caption">Loading the interactive '
        'forecast…</p></div>'
        '<noscript><p class="caption">The interactive forecast needs JavaScript; the numbers '
        'above are static.</p></noscript></section></div></main>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, experimental research '
        'forecast.</b> Not a drought warning; not for safety-critical or operational abstraction '
        'decisions. RiverCast forecasts gauged flow — as measured, including abstraction and '
        'discharge effects. England only. Independent open-source project — not affiliated with '
        'or endorsed by any employer, the Environment Agency, ECMWF, or any water company.</p>'
        '<p class="caption">Contains EA data (OGL v3) · ECMWF Open Data (CC-BY-4.0) · Copernicus '
        'ERA5/SEAS5 · Free &amp; open source (MIT) · <a href="/contact/">Contact</a>.</p></footer>'
        '<script src="/config.js"></script><script src="/contract_fields.js"></script>'
        '<script src="/charts.js"></script><script src="/detail.js"></script>'
        '<script src="/watchlist.js"></script><script src="/ladders.js"></script>'
        '<script src="/bore.js"></script></body></html>'
    )


_JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _check(html, sl, problems, base="b"):
    head = html.split("</head>", 1)[0]
    if f'canonical" href="{SITE}/{base}/{sl}/"' not in head:
        problems.append(f"{sl}: canonical")
    if f'og:url" content="{SITE}/{base}/{sl}/"' not in head:
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


def _mini_shell(title, canonical, body, description: str = "", jsonld: str = ""):
    desc_meta = f'<meta name="description" content="{esc(description)}">' if description else ""
    og_desc = f'<meta property="og:description" content="{esc(description)}">' if description else ""
    jl_meta = f'<script type="application/ld+json">{jsonld}</script>' if jsonld else ""
    return (
        '<!DOCTYPE html><html lang="en-GB"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        + desc_meta +
        f'<link rel="canonical" href="{canonical}">'
        '<meta name="robots" content="index,follow"><meta name="theme-color" content="#1a3a5c">'
        f'<meta property="og:title" content="{esc(title)}">'
        + og_desc +
        f'<meta property="og:url" content="{canonical}">'
        f'<meta property="og:image" content="{SITE}/og/default.png">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        + jl_meta +
        '</head><body>'
        + _topnav("browse") +
        f'<main><div class="bore-wrap">{body}</div></main>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, uncalibrated research '
        'forecast.</b> Not a flood or drought warning. England only. Independent open-source '
        'project — not affiliated with or endorsed by any employer.</p>'
        '<p class="caption"><a href="/contact/">Contact</a>.</p></footer></body></html>'
    )


def _browse_html(entries, flow_entries=(), river_hubs=(), n_forecast=0, county_slug_of=None):
    """entries: list of (slug, name, region) boreholes; flow_entries the same
    for RiverCast gauges (linked under /r/); river_hubs: (slug, river, region)
    for the /rivers/<river>/ hubs. A crawlable, county-grouped directory —
    rivers get their own section (rivers first, then their gauges).
    n_forecast: how many boreholes carry a live 14-day forecast today (most
    don't — the subhead must not overclaim 'a forecast page' for all of them)."""
    by_region: dict[str, list] = {}
    for sl, name, region in entries:
        by_region.setdefault(region or "Other", []).append((sl, name))
    fc_note = (f' {n_forecast} carry a live 14-day forecast today' if n_forecast else "")
    parts = ['<nav class="bore-crumb"><a href="/">Home</a> / Browse</nav>',
             '<h1 class="bore-h1">Browse boreholes</h1>',
             f'<p class="bore-sub">All {len(entries)} monitored boreholes, by ceremonial county'
             + (f', plus {len(flow_entries)} RiverCast flow gauges below' if flow_entries else "")
             + '.' + fc_note + (' — each with its own daily page.' if fc_note else '') + '</p>',
             f'<p class="bore-caveat">⚠ {esc(CAVEAT)}</p>']
    for region in sorted(by_region):
        # Link each county heading to its own /browse/<county>/ landing page.
        rlabel = esc(region)
        if county_slug_of and region in county_slug_of:
            rlabel = f'<a href="/browse/{county_slug_of[region]}/">{esc(region)}</a>'
        parts.append(f'<h2 class="bore-browse-h">{rlabel} '
                     f'<span class="caption">({len(by_region[region])})</span></h2>'
                     '<ul class="bore-browse-list">')
        for sl, name in sorted(by_region[region], key=lambda x: (x[1] or "").lower()):
            parts.append(f'<li><a href="/b/{esc(sl)}/">{esc(name)}</a></li>')
        parts.append("</ul>")
    if flow_entries:
        parts.append('<h1 class="bore-h1" id="rivers">Rivers &amp; flow gauges</h1>'
                     f'<p class="bore-sub">{len(flow_entries)} RiverCast gauges with a daily '
                     'low-flow forecast — <a href="/rivers/">the rivers front page</a> has the '
                     'national picture.</p>'
                     f'<p class="bore-caveat">⚠ {esc(FLOW_CAVEAT)}</p>')
        if river_hubs:
            by_region_h: dict[str, list] = {}
            for hs, river, region in river_hubs:
                by_region_h.setdefault(region or "Other", []).append((hs, river))
            parts.append('<h2 class="bore-browse-h">Rivers</h2><ul class="bore-browse-list">')
            for region in sorted(by_region_h):
                for hs, river in sorted(by_region_h[region], key=lambda x: (x[1] or "").lower()):
                    reg = f' <span class="caption">({esc(region)})</span>' if region else ""
                    parts.append(f'<li><a href="/rivers/{esc(hs)}/">{esc(river)}</a>{reg}</li>')
            parts.append("</ul>")
        by_region_f: dict[str, list] = {}
        for sl, name, region in flow_entries:
            by_region_f.setdefault(region or "Other", []).append((sl, name))
        parts.append('<h2 class="bore-browse-h">Gauges</h2>')
        for region in sorted(by_region_f):
            parts.append(f'<h3 class="bore-browse-h">{esc(region)} '
                         f'<span class="caption">({len(by_region_f[region])})</span></h3>'
                         '<ul class="bore-browse-list">')
            for sl, name in sorted(by_region_f[region], key=lambda x: (x[1] or "").lower()):
                parts.append(f'<li><a href="/r/{esc(sl)}/">{esc(name)}</a></li>')
            parts.append("</ul>")
    browse_desc = (f"All {len(entries)} monitored boreholes"
                   + (f" and {len(flow_entries)} RiverCast river gauges" if flow_entries else "")
                   + " across England, grouped by county — each with its own daily page.")
    browse_jl = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": f"{SITE}/#website", "url": f"{SITE}/",
         "name": "GroundwaterCast UK"},
        {"@type": "CollectionPage", "@id": f"{SITE}/browse/#webpage",
         "url": f"{SITE}/browse/", "name": "Browse boreholes & rivers — GroundwaterCast UK",
         "isPartOf": {"@id": f"{SITE}/#website"}, "inLanguage": "en-GB",
         "description": browse_desc}]}, separators=(",", ":")).replace("</", "<\\/")
    return _mini_shell("Browse boreholes & rivers — GroundwaterCast UK",
                       f"{SITE}/browse/", "".join(parts),
                       description=browse_desc, jsonld=browse_jl)


def _county_page(region, bores, flows, hubs):
    """A per-county directory at /browse/<county>/ — a focused, crawlable landing
    that ranks for '<county> groundwater forecast' and distributes link equity to
    that county's stubs, without splitting/removing the single /browse/ index."""
    cslug = slug(region)
    canonical = f"{SITE}/browse/{cslug}/"
    parts = ['<nav class="bore-crumb"><a href="/">Home</a> / <a href="/browse/">Browse</a> / '
             f'{esc(region)}</nav>',
             f'<h1 class="bore-h1">{esc(region)} — groundwater &amp; river forecasts</h1>',
             f'<p class="bore-sub">{len(bores)} monitored boreholes'
             + (f', {len(flows)} river gauges' if flows else '')
             + (f' and {len(hubs)} rivers' if hubs else '') + f' in {esc(region)}.</p>',
             f'<p class="bore-caveat">⚠ {esc(CAVEAT)}</p>']
    if hubs:
        parts.append('<h2 class="bore-browse-h">Rivers</h2><ul class="bore-browse-list">')
        for hs, river in sorted(hubs, key=lambda x: (x[1] or "").lower()):
            parts.append(f'<li><a href="/rivers/{esc(hs)}/">{esc(river)}</a></li>')
        parts.append("</ul>")
    if flows:
        parts.append('<h2 class="bore-browse-h">River gauges</h2><ul class="bore-browse-list">')
        for sl, name in sorted(flows, key=lambda x: (x[1] or "").lower()):
            parts.append(f'<li><a href="/r/{esc(sl)}/">{esc(name)}</a></li>')
        parts.append("</ul>")
    parts.append('<h2 class="bore-browse-h">Boreholes</h2><ul class="bore-browse-list">')
    for sl, name in sorted(bores, key=lambda x: (x[1] or "").lower()):
        parts.append(f'<li><a href="/b/{esc(sl)}/">{esc(name)}</a></li>')
    parts.append('</ul><p class="caption"><a href="/browse/">← All counties</a></p>')
    desc = (f"Daily groundwater and river low-flow forecasts for {esc(region)}, England — "
            f"{len(bores)} boreholes"
            + (f" and {len(flows)} river gauges" if flows else "")
            + ", each with its own page. Open data; indicative, not a warning.")
    jl = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": f"{SITE}/#website", "url": f"{SITE}/",
         "name": "GroundwaterCast UK"},
        {"@type": "CollectionPage", "@id": f"{canonical}#webpage", "url": canonical,
         "name": f"{region} — groundwater & river forecasts",
         "isPartOf": {"@id": f"{SITE}/#website"}, "inLanguage": "en-GB",
         "description": desc}]}, separators=(",", ":")).replace("</", "<\\/")
    return _mini_shell(f"{region} groundwater & river forecasts — GroundwaterCast UK",
                       canonical, "".join(parts), description=desc, jsonld=jl)


def _sitemap_xml(urls):
    rows = "".join(f"<url><loc>{loc}</loc><lastmod>{lm}</lastmod></url>" for loc, lm in urls)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + rows + "</urlset>")


# ---------------------------------------------------------------------------
# River hub pages — /rivers/<river>/. One per river with ≥1 published gauge,
# grouped by (river_name, region) so two same-named rivers in different
# counties (e.g. the Dorset vs Surrey River Wey) don't merge onto one page.
# Deliberately a RIVER-level page, distinct from the gauge stub: river
# character + all its gauges + the crawlable boreholes that feed it + the
# valley teaser. That "category page" intent is what keeps a single-gauge
# hub from reading as a thin duplicate of its one gauge stub.
# ---------------------------------------------------------------------------

def _valley_teaser(river, lat, lon):
    """A 3-D valley cross-link, shown only when the river is actually in the
    /valley/test/ model AND the point is inside the valley bbox (the bbox is
    the collision guard — name alone isn't enough for 'River Dun')."""
    if river not in VALLEY_RIVERS:
        return ""
    try:
        if not (VALLEY_BBOX[0] <= float(lon) <= VALLEY_BBOX[2]
                and VALLEY_BBOX[1] <= float(lat) <= VALLEY_BBOX[3]):
            return ""
    except (TypeError, ValueError):
        return ""
    return ('<section class="bore-summary"><h2>See this river in 3-D</h2>'
            f'<p>The {esc(river)} runs through the '
            f'<a href="{VALLEY_URL}">River Test valley 3-D model</a> — replay three years of '
            'measured rain, groundwater and river flow, then walk the forecast six months '
            'ahead.</p></section>')


def _assign_hub_slugs(keys):
    """(river, region) keys → collision-safe slugs. A river name unique across
    regions keeps its bare slug (river-test); a name shared by two regions is
    disambiguated with the region (river-wey-dorset / river-wey-surrey)."""
    from collections import defaultdict
    by_base: dict[str, list] = defaultdict(list)
    for k in keys:
        by_base[slug(k[0])].append(k)
    out: dict = {}
    for base, ks in by_base.items():
        if len(ks) == 1:
            out[ks[0]] = base
            continue
        used: dict[str, tuple] = {}
        for k in sorted(ks, key=lambda x: (x[1] or "")):
            s = slug(f"{k[0]} {k[1]}") if k[1] else base
            while s in used:                       # last-resort de-dup
                s = f"{s}-{len(used)}"
            used[s] = k
            out[k] = s
    return out


def _river_gauge_chip(st):
    s = (st or {}).get("status")
    if not s:
        return '<span class="chip none">no current status</span>'
    pc = pct_ordinal((st or {}).get("percentile"))
    p = f' <span class="chip-pct">{pc}</span>' if pc else ""
    return f'<span class="chip {esc(s)}">{esc(FLOW_STATUS_LABEL.get(s, s))}{p}</span>'


# ---------------------------------------------------------------------------
# Server-baked river visuals for the hub — all STATIC SVG (crawlable, no JS);
# interactivity is native <a> links + <title> tooltips, theming via CSS vars.
# ---------------------------------------------------------------------------
_GW_LABEL = {"below": "below normal", "near": "near normal", "above": "above normal"}
_RV_COL = {"below": "var(--rv-below)", "near": "var(--rv-near)", "above": "var(--rv-above)"}


def _rv_col(status):
    return _RV_COL.get(status, "var(--rv-none)")


def _project_geo(segs, points, box_w=440, box_h=560, pad=22):
    """Equirectangular (cos-lat) projection of river line segments + marker
    points into an SVG box, aspect preserved. Returns (W, H, proj) or None."""
    lons = [p[0] for s in segs for p in s] + [q["lon"] for q in points]
    lats = [p[1] for s in segs for p in s] + [q["lat"] for q in points]
    lons = [x for x in lons if isinstance(x, (int, float))]
    lats = [y for y in lats if isinstance(y, (int, float))]
    if len(lons) < 2:
        return None
    minlon, maxlon, minlat, maxlat = min(lons), max(lons), min(lats), max(lats)
    if maxlon - minlon < 1e-6:
        minlon -= 5e-4; maxlon += 5e-4
    if maxlat - minlat < 1e-6:
        minlat -= 5e-4; maxlat += 5e-4
    dm = 0.04 * (maxlat - minlat)
    minlat -= dm; maxlat += dm; minlon -= dm; maxlon += dm
    latc = math.radians((minlat + maxlat) / 2)
    gw = (maxlon - minlon) * math.cos(latc)
    gh = (maxlat - minlat)
    scale = min((box_w - 2 * pad) / gw, (box_h - 2 * pad) / gh)
    W = round(gw * scale + 2 * pad, 1)
    H = round(gh * scale + 2 * pad, 1)

    def proj(lon, lat):
        return (round((lon - minlon) * math.cos(latc) * scale + pad, 1),
                round((maxlat - lat) * scale + pad, 1))
    return W, H, proj


def _river_map_svg(river, river_segs, gauges, boreholes):
    """Static SVG: the river's own course with its gauges (diamonds, linked +
    labelled) and feeding boreholes (rings, linked, tooltip). Empty string
    when there's no usable geometry (falls back to no map)."""
    pts = ([{"lon": g["lon"], "lat": g["lat"]} for g in gauges]
           + [{"lon": b["lon"], "lat": b["lat"]} for b in boreholes])
    pr = _project_geo(river_segs, pts)
    if not pr:
        return ""
    W, H, proj = pr
    out = [f'<svg viewBox="0 0 {W} {H}" style="aspect-ratio:{W}/{H}" role="img" '
           f'aria-label="Map of the {esc(river)} with its gauges and feeding boreholes">']
    for cls, w in (("rv-river-case", 4.2), ("rv-river-line", 1.8), ("rv-river-sheen", 0.9)):
        for s in river_segs:
            pstr = " ".join(f"{x},{y}" for x, y in (proj(lo, la) for lo, la in s))
            out.append(f'<polyline class="{cls}" points="{pstr}" stroke-width="{w}"/>')
    for b in boreholes:
        x, y = proj(b["lon"], b["lat"])
        out.append(f'<a href="/b/{esc(b["slug"])}/"><title>{esc(b["name"])} borehole — '
                   f'{esc(_GW_LABEL.get(b.get("status"), "no current status"))}</title>'
                   f'<circle class="rv-bm" cx="{x}" cy="{y}" r="3.4" fill="var(--rv-chalk)" '
                   f'stroke="{_rv_col(b.get("status"))}" stroke-width="1.9"/></a>')
    for g in gauges:
        x, y = proj(g["lon"], g["lat"])
        st = (g["status"] or {}).get("status")
        out.append(f'<a href="/r/{esc(g["slug"])}/"><title>{esc(g["name"])} gauge — '
                   f'{esc(FLOW_STATUS_LABEL.get(st, "no current status"))}</title>'
                   f'<path class="rv-gm" d="M{x} {y-6.2}L{x+6.2} {y}L{x} {y+6.2}L{x-6.2} {y}Z" '
                   f'fill="{_rv_col(st)}" stroke="var(--rv-card)" stroke-width="1.5"/>'
                   f'<text class="rv-mlabel" x="{x+9}" y="{y+3}">{esc(g["name"])}</text></a>')
    out.append('</svg>')
    return "".join(out)


def _river_hub_blob(river_segs, gauges, boreholes):
    """Compact JSON geometry for the interactive hub mini-map (rivers-hub.js):
    the river's line segments, its gauges and feeding boreholes, and a padded
    fit bbox. Returns None when there's no usable geometry (map falls back to
    the baked SVG). Coordinates rounded to 5 dp to keep the inline blob small."""
    def rnd(v):
        return round(float(v), 5)

    segs = [[[rnd(lo), rnd(la)] for lo, la in s] for s in river_segs if len(s) >= 2]
    g = [{"lon": rnd(x["lon"]), "lat": rnd(x["lat"]), "slug": x["slug"],
          "name": x["name"], "status": (x["status"] or {}).get("status")}
         for x in gauges if isinstance(x.get("lon"), (int, float))]
    b = [{"lon": rnd(x["lon"]), "lat": rnd(x["lat"]), "slug": x["slug"],
          "name": x["name"], "status": x.get("status")}
         for x in boreholes if isinstance(x.get("lon"), (int, float))]
    lons = [p[0] for s in segs for p in s] + [q["lon"] for q in g] + [q["lon"] for q in b]
    lats = [p[1] for s in segs for p in s] + [q["lat"] for q in g] + [q["lat"] for q in b]
    if len(lons) < 2:
        return None
    pad_lon = max((max(lons) - min(lons)) * 0.12, 0.01)
    pad_lat = max((max(lats) - min(lats)) * 0.12, 0.01)
    bounds = [[round(min(lons) - pad_lon, 5), round(min(lats) - pad_lat, 5)],
              [round(max(lons) + pad_lon, 5), round(max(lats) + pad_lat, 5)]]
    return json.dumps({"segs": segs, "gauges": g, "boreholes": b, "bounds": bounds},
                      separators=(",", ":"))


def _river_ribbon_svg(gauges, boreholes):
    """Source→sea strip: markers by latitude (high=source left, low=sea right).
    Gauges labelled; boreholes are dots with a tooltip (avoids label pile-up)."""
    marks = ([{"y": g["lat"], "name": g["name"], "slug": g["slug"], "kind": "g",
               "s": (g["status"] or {}).get("status")} for g in gauges]
             + [{"y": b["lat"], "name": b["name"], "slug": b["slug"], "kind": "b",
                 "s": b.get("status")} for b in boreholes])
    ys = [m["y"] for m in marks if isinstance(m["y"], (int, float))]
    if len(ys) < 2:
        return ""
    ymin, ymax = min(ys), max(ys)
    if ymax - ymin < 1e-6:
        ymax += 1e-3; ymin -= 1e-3
    W, mid = 900, 54
    out = [f'<svg viewBox="0 0 {W} 96" preserveAspectRatio="none" role="img" '
           'aria-label="The river\'s gauges from source to sea">',
           f'<path d="M0 {mid-1.4} Q{W*0.5} {mid-3} {W} {mid-4.6} L{W} {mid+4.6} '
           f'Q{W*0.5} {mid+3} 0 {mid+1.4} Z" fill="var(--rv-river)" fill-opacity="0.42"/>']
    for m in sorted(marks, key=lambda z: -(z["y"] or 0)):
        if not isinstance(m["y"], (int, float)):
            continue
        x = round(((ymax - m["y"]) / (ymax - ymin)) * (W - 40) + 20, 1)
        c = _rv_col(m["s"])
        if m["kind"] == "g":
            out.append(f'<a href="/r/{esc(m["slug"])}/"><title>{esc(m["name"])} gauge</title>'
                       f'<path d="M{x} {mid-7}L{x+7} {mid}L{x} {mid+7}L{x-7} {mid}Z" fill="{c}" '
                       f'stroke="var(--rv-card)" stroke-width="2"/>'
                       f'<text class="rv-mlabel" x="{x}" y="{mid-15}" text-anchor="middle" '
                       f'font-size="10">{esc(m["name"])}</text></a>')
        else:
            out.append(f'<a href="/b/{esc(m["slug"])}/"><title>{esc(m["name"])} borehole</title>'
                       f'<circle cx="{x}" cy="{mid}" r="5" fill="var(--rv-card)" stroke="{c}" '
                       f'stroke-width="2.4"/></a>')
    out.append('</svg>')
    return "".join(out)


def _flow_sparkline(series):
    """Small SVG sparkline from a gauge's recent observed flow (last ~16 days)."""
    vals = [v for _, v in (series or [])[-16:] if isinstance(v, (int, float))]
    if len(vals) < 3:
        return ""
    W, H, p = 240, 44, 4
    mn, mx = min(vals), max(vals)
    if mx - mn < 1e-9:
        mx += 1; mn -= 1

    def X(i):
        return round(p + i / (len(vals) - 1) * (W - 2 * p), 1)

    def Y(v):
        return round(H - p - ((v - mn) / (mx - mn)) * (H - 2 * p), 1)
    d = "M" + " L".join(f"{X(i)} {Y(v)}" for i, v in enumerate(vals))
    return (f'<svg class="rv-spark" viewBox="0 0 {W} {H}" width="100%" height="{H}" aria-hidden="true">'
            f'<path d="{d} L{X(len(vals)-1)} {H} L{X(0)} {H} Z" fill="var(--rv-river)" fill-opacity="0.1"/>'
            f'<path d="{d}" fill="none" stroke="var(--rv-river-deep)" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{X(len(vals)-1)}" cy="{Y(vals[-1])}" r="2.6" fill="var(--rv-river-deep)"/></svg>')


def _rv_valley_band(river, lat, lon):
    """The 3-D valley cross-link as a styled band — same VALLEY_RIVERS + bbox
    guard as _valley_teaser, restyled for the redesigned hub."""
    if river not in VALLEY_RIVERS:
        return ""
    try:
        if not (VALLEY_BBOX[0] <= float(lon) <= VALLEY_BBOX[2]
                and VALLEY_BBOX[1] <= float(lat) <= VALLEY_BBOX[3]):
            return ""
    except (TypeError, ValueError):
        return ""
    return ('<section class="rv-band"><div class="rv-valley"><div>'
            '<h3 class="rv-h3">See the Test valley in 3-D</h3>'
            f'<p>The {esc(river)} runs through a museum-style model of the whole valley — replay '
            'three years of measured rain, groundwater and river flow, then walk the forecast six '
            'months ahead.</p></div>'
            f'<a class="rv-go" href="{VALLEY_URL}">Open the 3-D valley →</a></div></section>')


def _load_river_geometry(*paths):
    """river name → list of line segments ([[lon,lat],...]) from a rivers /
    river_polylines geojson (first readable path wins). Empty dict if none."""
    for p in paths:
        try:
            if not p.exists():
                continue
            geo = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out: dict = {}
        for f in geo.get("features", []):
            nm = (f.get("properties") or {}).get("name")
            g = f.get("geometry") or {}
            if not nm:
                continue
            if g.get("type") == "MultiLineString":
                out.setdefault(nm, []).extend(g.get("coordinates") or [])
            elif g.get("type") == "LineString":
                out.setdefault(nm, []).append(g.get("coordinates") or [])
        return out
    return {}


def _segs_for(river, river_geo):
    """Resolve a hub river name to its polyline segments — the geojson key is
    the OS watercourse name, which may add/drop the 'River ' prefix vs the
    catalogue name, so try the obvious variants (case-insensitively)."""
    if river in river_geo:
        return river_geo[river]
    cands = [river,
             river[6:] if river.lower().startswith("river ") else "River " + river]
    low = {k.lower(): v for k, v in river_geo.items()}
    for c in cands:
        if c.lower() in low:
            return low[c.lower()]
    return []


def _river_hub_jsonld(river, region, hub_slug, gauges):
    base = f"{SITE}/rivers/{hub_slug}/"
    items = [{"@type": "ListItem", "position": i, "url": f"{SITE}/r/{g['slug']}/",
              "name": g["title"]} for i, g in enumerate(gauges, 1)]
    addr = {"@type": "PostalAddress", "addressCountry": "GB"}
    if region:
        addr["addressRegion"] = region
    place = {"@type": "Place", "@id": base + "#place", "name": river, "address": addr,
             "containedInPlace": {"@type": "Country", "name": "England"}}
    webpage = {"@type": "WebPage", "@id": base + "#webpage",
               "name": f"{river} — river flow forecast", "url": base, "inLanguage": "en-GB",
               "isPartOf": {"@id": SITE + "/#website"}, "about": {"@id": base + "#place"}}
    itemlist = {"@type": "ItemList", "@id": base + "#gauges",
                "name": f"Flow gauges on the {river}", "numberOfItems": len(gauges),
                "itemListElement": items}
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "@id": SITE + "/#website", "name": "GroundwaterCast UK",
         "url": SITE + "/", "inLanguage": "en-GB"},
        webpage, place, itemlist]}
    return json.dumps(graph, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")


def _rv_chip(st):
    s = (st or {}).get("status")
    if not s:
        return '<span class="rv-chip rv-nostat">no current status</span>'
    pc = pct_ordinal((st or {}).get("percentile"))
    p = f' <span class="rv-pct">{pc}</span>' if pc else ""
    return f'<span class="rv-chip rv-{esc(s)}">{esc(FLOW_STATUS_LABEL.get(s, s))}{p}</span>'


def _rv_gcard(g):
    st = g["status"] or {}
    stats = []
    if g.get("latest") is not None:
        stats.append((_fmt(g["latest"], 3), "latest m³/s"))
    if g.get("q95") is not None:
        stats.append((_fmt(g["q95"], 3), "Q95 proxy"))
    pb = pct_str(g.get("p_below"))
    if pb is not None:
        stats.append((esc(pb), "below Q95, 14d"))
    row = "".join(f'<span><b class="rv-num">{v}</b>{esc(l)}</span>' for v, l in stats if v)
    return (f'<a class="rv-gcard" href="/r/{esc(g["slug"])}/">'
            f'<div class="rv-r1"><div><div class="rv-gname">{esc(g["name"])}</div>'
            f'<div class="rv-gsite">{esc(g["title"])}</div></div>{_rv_chip(st)}</div>'
            f'{_flow_sparkline(g.get("series"))}'
            f'<div class="rv-gstats">{row}</div></a>')


def _river_hub_page(river, region, hub_slug, gauges, bore_links, river_segs=()):
    n = len(gauges)
    below = sum(1 for g in gauges if (g["status"] or {}).get("status") == "below")
    withstatus = sum(1 for g in gauges if (g["status"] or {}).get("status"))
    any_wb = any(g["winterbourne"] for g in gauges)
    map_svg = _river_map_svg(river, river_segs, gauges, bore_links)
    hub_blob = _river_hub_blob(river_segs, gauges, bore_links) if map_svg else None
    base = f"{SITE}/rivers/{hub_slug}/"
    rtitle = f", {region}" if region else ""
    rphrase = f" in {region}" if region else ""
    desc = (f"Daily low-flow forecasts for the {river}{rphrase}, England — {n} "
            f"gauge{'s' if n != 1 else ''} with current flow vs normal, a 14-day fan, and the "
            "chance of dropping below Q95. Open data; indicative, not a drought warning.")
    jl = _river_hub_jsonld(river, region, hub_slug, gauges)
    head = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(river)} — river flow forecast{esc(rtitle)} | GroundwaterCast</title>'
        f'<meta name="description" content="{esc(desc)}">'
        f'<link rel="canonical" href="{base}">'
        '<meta name="robots" content="index,follow,max-image-preview:large">'
        '<meta name="theme-color" content="#1a3a5c">'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
        '<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/borehole.css">'
        '<link rel="stylesheet" href="/rivers.css">'
        + ('<link rel="stylesheet" href="/vendor/maplibre-gl.css">' if hub_blob else '')
        +
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="GroundwaterCast UK">'
        '<meta property="og:locale" content="en_GB">'
        f'<meta property="og:title" content="{esc(river)} — river flow forecast (indicative)">'
        f'<meta property="og:description" content="{esc(desc)}">'
        f'<meta property="og:url" content="{base}">'
        f'<meta property="og:image" content="{SITE}/og/default.png">'
        f'<script type="application/ld+json">{jl}</script>'
    )
    eyebrow = (f"{region} · RiverCast" if region else "RiverCast")
    crumb = ('<a href="/">Home</a> / <a href="/rivers/">Rivers</a> / '
             + (f"{esc(region)} / " if region else "") + f"<b>{esc(river)}</b>")
    lede = f"Daily low-flow forecasts for the {esc(river)}"
    if withstatus:
        verb, gword = ("sits", "gauge") if withstatus == 1 else ("sit", "gauges")
        lede += (f" — today {below} of {withstatus} {gword} with a fresh reading "
                 f"{verb} below normal flow for the season.")
    else:
        lede += "."
    # headline status pill
    if n == 1 and withstatus:
        pst = (gauges[0]["status"] or {}).get("status")
        pill = (f'<span class="rv-pill rv-{esc(pst)}"><span class="rv-dot"></span>'
                f'<span><b>{esc(FLOW_STATUS_LABEL.get(pst, pst).capitalize())}</b> today</span></span>')
    elif withstatus:
        pill = (f'<span class="rv-pill {"rv-below" if below else "rv-near"}">'
                f'<span class="rv-dot"></span><span><b>{below} of {withstatus}</b> '
                'gauges below normal today</span></span>')
    else:
        pill = ('<span class="rv-pill rv-nostat"><span class="rv-dot"></span>'
                '<span>No fresh reading today</span></span>')
    facts = [(str(n), "gauge" + ("s" if n != 1 else ""))]
    if bore_links:
        facts.append((str(len(bore_links)), "feeding borehole" + ("s" if len(bore_links) != 1 else "")))
    facts_html = "".join(f'<li><b>{v}</b><span>{esc(l)}</span></li>' for v, l in facts)

    if map_svg:
        gl = '<div id="rv-hubmap" class="rv-glmap"></div>' if hub_blob else ""
        hero_map = (f'<figure class="rv-map">{gl}'
                    f'<div id="rv-mapfallback">{map_svg}</div>'
                    '<figcaption class="rv-mapcap">Gauge ◆ &nbsp; borehole ○ · '
                    'OS Open Rivers (OGL)</figcaption></figure>')
    else:
        hero_map = ""
    hero = (f'<section class="rv-hero{"" if map_svg else " rv-hero-solo"}"><div>'
            f'<span class="rv-eyebrow">{esc(eyebrow)}</span>'
            f'<h1 class="rv-h1">{esc(river)}</h1>'
            f'<p class="rv-lede">{lede}</p>'
            f'<ul class="rv-facts">{facts_html}</ul>{pill}'
            '<p class="rv-subline">Gauged flow · updated daily · indicative, not a warning</p>'
            f'</div>{hero_map}</section>')

    ribbon = _river_ribbon_svg(gauges, bore_links)
    ribbon_band = (f'<section class="rv-band"><div class="rv-sechead">'
                   '<span class="rv-eyebrow">Source to sea</span>'
                   '<h2 class="rv-h2">The gauges along it</h2>'
                   '<p>Where each monitoring point sits along the river, and how it reads today '
                   'against its own seasonal normal.</p></div>'
                   f'<div class="rv-ribbon">{ribbon}<div class="rv-ends">'
                   '<span>Headwaters (source)</span><span>Downstream (sea)</span></div></div>'
                   '</section>') if ribbon else ""

    cards = "".join(_rv_gcard(g) for g in gauges)
    gauge_band = ('<section class="rv-band"><div class="rv-sechead">'
                  '<span class="rv-eyebrow">Flow forecast</span>'
                  f'<h2 class="rv-h2">Gauge{"s" if n != 1 else ""} on this river</h2></div>'
                  f'<div class="rv-cards">{cards}</div></section>')

    aq_band = ""
    if bore_links:
        rows = "".join(
            f'<a class="rv-brow rv-{esc(b.get("status") or "nostat")}" href="/b/{esc(b["slug"])}/">'
            f'<span class="rv-bdot"></span><span class="rv-bn">{esc(b["name"])}</span>'
            f'<span class="rv-bo">{esc(_GW_LABEL.get(b.get("status"), "—"))}</span></a>'
            for b in bore_links)
        aq_band = ('<section class="rv-band"><div class="rv-sechead">'
                   '<span class="rv-eyebrow">The moat · groundwater ↔ river</span>'
                   '<h2 class="rv-h2">The aquifer that feeds it</h2>'
                   '<p>In a chalk stream the summer flow <em>is</em> groundwater draining. These '
                   'boreholes sit in the catchment and each carries its own forecast — the read no '
                   'river-only service can give you.</p></div>'
                   f'<div class="rv-aq">{rows}</div></section>')

    wb_band = ('<section class="rv-band"><div class="rv-wbb"><span class="rv-ic">🌾</span><div>'
               '<h3 class="rv-h3">Its winterbourne reaches</h3>'
               '<p>Parts of this river run as winterbournes — chalk streams that dry through late '
               'summer and return when the aquifer refills. Each gauge page flags whether the '
               'bourne is flowing today and the months it typically runs dry.</p></div></div>'
               '</section>') if any_wb else ""

    valley_band = _rv_valley_band(river, gauges[0]["lat"], gauges[0]["lon"])

    body = (
        _topnav("rivers") +
        '<div class="rv-wrap">'
        f'<nav class="rv-crumb">{crumb}</nav>'
        + hero + ribbon_band + gauge_band + aq_band + wb_band + valley_band +
        '<section class="rv-band"><p class="rv-caveat"><b>Indicative, experimental — not a '
        'drought warning.</b> RiverCast forecasts gauged flow — as measured, including abstraction '
        'and discharge effects; Q95 is a climatological low-flow proxy computed from the gauge\'s '
        'own record, not a licence Hands-off-Flow value; rating curves are least accurate at low '
        'flows. <a href="/methods/">How this works</a>.</p></section>'
        '</div>'
        '<footer class="bore-foot"><p class="disclaimer"><b>Indicative, experimental research '
        'forecast.</b> Not a drought warning; not for safety-critical or operational abstraction '
        'decisions. RiverCast forecasts gauged flow — as measured, including abstraction and '
        'discharge effects. England only. Independent open-source project — not affiliated with '
        'or endorsed by any employer, the Environment Agency, ECMWF, or any water company.</p>'
        '<p class="caption">Contains EA data (OGL v3) · OS Open Rivers (OGL) · ECMWF Open Data '
        '(CC-BY-4.0) · Copernicus ERA5/SEAS5 · Free &amp; open source (MIT) · '
        '<a href="/contact/">Contact</a>.</p></footer>')
    scripts = (('<script src="/vendor/maplibre-gl.js"></script>'
                '<script src="/config.js"></script>'
                f'<script>window.RV_HUB={hub_blob}</script>'
                '<script src="/rivers-hub.js"></script>') if hub_blob else "")
    return ("<!DOCTYPE html><html lang=\"en-GB\"><head>" + head + "</head><body>"
            + body + scripts + "</body></html>")


def _check_hub(html, sl, problems):
    head = html.split("</head>", 1)[0]
    if f'canonical" href="{SITE}/rivers/{sl}/"' not in head:
        problems.append(f"rivers/{sl}: canonical")
    if f'og:url" content="{SITE}/rivers/{sl}/"' not in head:
        problems.append(f"rivers/{sl}: og:url != canonical")
    title = re.search(r"<title>(.*?)</title>", head)
    if title and ("None" in title.group(1) or "null" in title.group(1)):
        problems.append(f"rivers/{sl}: leaked None/null in title")
    m = _JSONLD_RE.search(head)
    if not m:
        problems.append(f"rivers/{sl}: no JSON-LD")
        return
    try:
        jl = json.loads(m.group(1).replace("<\\/", "</"))
    except Exception as exc:
        problems.append(f"rivers/{sl}: JSON-LD parse error: {exc}")
        return
    types = {n.get("@type") for n in jl.get("@graph", [])}
    if not _REQUIRED_HUB_TYPES <= types:
        problems.append(f"rivers/{sl}: JSON-LD missing types {_REQUIRED_HUB_TYPES - types}")


def build(pack_dir: Path = PACK_DIR, out_dir: Path = OUT_DIR, today: str | None = None,
          lastmod_store: Path = LASTMOD_STORE,
          og_manifest: Path = OG_MANIFEST) -> dict:
    today = today or date.today().isoformat()
    # Share-card manifest from build_og_cards (the stage before this one) —
    # {slug: share.<hash>.png}. Absent (no resvg on the box) => no og:image.
    cards: dict[str, str] = {}
    if og_manifest.exists():
        try:
            cards = json.loads(og_manifest.read_text(encoding="utf-8")).get("cards", {})
        except Exception:
            cards = {}
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
    flow_entries: list[tuple] = []                             # RiverCast gauges (/r/)
    flow_out_dir = web_dir / "r"
    rivers_out_dir = web_dir / "rivers"
    # Top-level editorial pages: anti-churn their <lastmod> like the stubs — hash
    # the actual file so a daily rebuild doesn't re-stamp "today" when the page
    # didn't change. /browse/ is regenerated every build, so it stays date=today.
    _toplevel = [("", "index.html"), ("rivers/", "rivers/index.html"),
                 ("about/", "about/index.html"), ("methods/", "methods/index.html"),
                 ("contact/", "contact/index.html"), ("explorer/", "explorer/index.html"),
                 ("valley/test/", "valley/test/index.html")]
    urls = []
    for urlrel, filerel in _toplevel:
        fp = web_dir / filerel
        lm = today
        if fp.exists():
            hh = hashlib.sha256(
                re.sub(r" · \d+ d(?: old)?", "", fp.read_text(encoding="utf-8")).encode("utf-8")
            ).hexdigest()
            sk = f"top/{urlrel}"
            prev = store.get(sk)
            lm = prev["lastmod"] if (prev and prev.get("hash") == hh) else today
            new_store[sk] = {"hash": hh, "lastmod": lm}
        urls.append((f"{SITE}/{urlrel}", lm))
    urls.append((f"{SITE}/browse/", today))
    n = n_flow = n_hub = noindex = noregion = 0
    problems: list[str] = []

    # Load every station detail once — the stub loop below AND the river-hub
    # pre-scan both need it, and the file set is small.
    docs = []
    for fp in sorted(pack_dir.glob("*.json")):   # sorted → deterministic slug collisions
        if fp.name == "index.json":
            continue                     # the stations/index.json catalogue, not a station
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict):          # defensive: only station detail dicts
            docs.append(d)

    # Pre-scan: group published flow gauges by (river, region) for the
    # /rivers/<river>/ hubs, and map borehole sid → (slug, name) so a hub can
    # link the boreholes that feed the river as CRAWLABLE anchors (the on-page
    # panel builds that list in JS, which crawlers never see).
    bore_by_sid: dict = {}
    river_groups: dict = {}                      # (river, region) -> [gauge record, ...]
    for d in docs:
        stn = d.get("station") or {}
        sid = stn.get("station_id")
        sl = stn.get("slug") or slug(stn.get("name") or sid or "")
        if stn.get("station_type") == "flow":
            river = (stn.get("river_name") or "").split("|")[0].strip()
            if not river:
                continue                 # no river name (e.g. Nene Valley) → no hub
            key = (river, region_for(stn.get("lat"), stn.get("lon")))
            fc = d.get("forecast") or {}
            obs = d.get("observed") or {}
            river_groups.setdefault(key, []).append({
                "sid": sid, "slug": sl, "name": stn.get("name") or sid,
                "title": _flow_title_name(stn), "status": d.get("status") or {},
                "winterbourne": bool(stn.get("winterbourne") and stn.get("dry_months")),
                "lat": stn.get("lat"), "lon": stn.get("lon"),
                "linked": stn.get("linked_boreholes") or [],
                "series": obs.get("series") or [],
                "q95": fc.get("threshold"), "p_below": fc.get("p_below_q95_14d"),
                "latest": (d.get("status") or {}).get("level"),
            })
        elif sid:
            bore_by_sid[sid] = {"slug": sl, "name": stn.get("name") or sid,
                                "lat": stn.get("lat"), "lon": stn.get("lon"),
                                "status": (d.get("status") or {}).get("status")}
    hub_slug_of = _assign_hub_slugs(list(river_groups))       # (river, region) -> slug
    hub_url_by_sid: dict = {}
    for key, recs in river_groups.items():
        for r in recs:
            if r["sid"]:
                hub_url_by_sid[r["sid"]] = f"/rivers/{hub_slug_of[key]}/"
    # Inverse of the hub→borehole links: borehole sid → [{river, hub_url}], so a
    # borehole page can link back UP to the river(s) it feeds (the hub already
    # links down; this makes the relationship reciprocal and crawlable).
    feeds_by_sid: dict = {}
    for key, recs in river_groups.items():
        hub = f"/rivers/{hub_slug_of[key]}/"
        for r in recs:
            for bsid in r["linked"]:
                feeds_by_sid.setdefault(bsid, [])
                if not any(x["hub_url"] == hub for x in feeds_by_sid[bsid]):
                    feeds_by_sid[bsid].append({"river": key[0], "hub_url": hub})
    # Borehole (name, region) collisions → disambiguate the <title> so two
    # distinct EA stations never ship byte-identical titles + descriptions.
    bore_nr: dict = {}
    for _d in docs:
        _stn = _d.get("station") or {}
        if _stn.get("station_type") != "flow":
            _k = ((_stn.get("name") or "").strip().lower(),
                  region_for(_stn.get("lat"), _stn.get("lon")))
            bore_nr[_k] = bore_nr.get(_k, 0) + 1
    n_forecast = sum(1 for _d in docs
                     if (_d.get("station") or {}).get("station_type") != "flow"
                     and _d.get("forecast"))

    for d in docs:
        stn = d.get("station") or {}
        is_flow = stn.get("station_type") == "flow"
        sid = stn.get("station_id")
        name = stn.get("name") or sid
        # Prefer the pack's canonical slug (assigned once in pack.py and shared
        # with the client link generators). The legacy re-derivation below only
        # covers packs built before the slug field existed — same rule, same
        # station_id order, so the URLs are identical either way.
        sl = stn.get("slug")
        if not sl:
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
        if is_flow:
            # RiverCast gauges get their OWN template (flow vocabulary — the
            # GW template speaks "borehole"/"aquifer"/mAOD and must never be
            # reused for a gauge) at /r/<slug>/. No og:image card in v1.
            html = _flow_page(d, sl, region, indexable,
                              hub_url=hub_url_by_sid.get(sid))
            _check(html, sl, problems, base="r")
            dst = flow_out_dir / sl
            store_key = f"r/{sl}"
            page_url = f"{SITE}/r/{sl}/"
            n_flow += 1
            flow_entries.append((sl, name, region))
        else:
            card = cards.get(sl)
            if card and not (out_dir / sl / card).exists():
                # never publish an og:image that would 404 (spec self-check)
                problems.append(f"{sl}: og:image {card} missing on disk")
                card = None
            title_extra = ""
            if sid and bore_nr.get((str(name or "").strip().lower(), region), 0) > 1:
                title_extra = f" (EA {str(sid)[:8]})"
            html = _page(d, sl, region, indexable, card,
                         feeds=feeds_by_sid.get(sid), title_extra=title_extra)
            _check(html, sl, problems)
            dst = out_dir / sl
            store_key = sl
            page_url = f"{SITE}/b/{sl}/"
            n += 1
            entries.append((sl, name, region))
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "index.html").write_text(html, encoding="utf-8")
        # lastmod anti-churn: bump only when the page's indexable content changed.
        # Hash with the derived observation-AGE fragments stripped ("· N d old" /
        # "· N d") — the age increments every day by definition, so hashing it
        # would bump lastmod daily on every stale page and defeat the store.
        stable = re.sub(r" · \d+ d(?: old)?", "", html)
        h = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        prev = store.get(store_key)
        lm = prev["lastmod"] if (prev and prev.get("hash") == h) else today
        new_store[store_key] = {"hash": h, "lastmod": lm}
        if indexable:                                            # don't sitemap noindex pages
            urls.append((page_url, lm))

    # River hubs (/rivers/<river>/) — one per river with ≥1 published gauge.
    # Written after the stub loop so bore_by_sid is fully populated.
    river_hubs: list[tuple] = []                  # (hub_slug, river, region) for /browse
    if river_groups:
        rivers_out_dir.mkdir(parents=True, exist_ok=True)
    # River polylines for the baked hub maps — prefer the published pack copy.
    river_geo = _load_river_geometry(pack_dir.parent / "rivers.geojson",
                                     _ROOT / "data" / "processed" / "river_polylines.geojson")
    rivers_index: list = []                       # for the /rivers/ landing directory + map
    for key in sorted(river_groups):
        river, region = key
        hs = hub_slug_of[key]
        recs = sorted(river_groups[key], key=lambda r: (r["name"] or "").lower())
        seen_b: set = set()
        bore_links: list = []
        for r in recs:
            for bsid in r["linked"]:
                if bsid in bore_by_sid and bsid not in seen_b:
                    seen_b.add(bsid)
                    bore_links.append(bore_by_sid[bsid])
        html = _river_hub_page(river, region, hs, recs, bore_links,
                               _segs_for(river, river_geo))
        _check_hub(html, hs, problems)
        dst = rivers_out_dir / hs
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "index.html").write_text(html, encoding="utf-8")
        n_hub += 1
        river_hubs.append((hs, river, region))
        rivers_index.append({
            "name": river, "slug": hs, "region": region or "", "gauges": len(recs),
            "below": sum(1 for r in recs if (r["status"] or {}).get("status") == "below"),
            "total": sum(1 for r in recs if (r["status"] or {}).get("status")),
            "lat": recs[0].get("lat"), "lon": recs[0].get("lon"),
        })
        store_key = f"rivers/{hs}"
        stable = re.sub(r" · \d+ d(?: old)?", "", html)
        hh = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        prev = store.get(store_key)
        lm = prev["lastmod"] if (prev and prev.get("hash") == hh) else today
        new_store[store_key] = {"hash": hh, "lastmod": lm}
        urls.append((f"{SITE}/rivers/{hs}/", lm))

    # Machine-readable rivers index — the /rivers/ landing fetches this to draw
    # the clickable river directory + route map-clicks to the right hub slug
    # (collision-aware, unlike a client-side slugify of the river name).
    (web_dir / "rivers_index.json").write_text(
        json.dumps(sorted(rivers_index, key=lambda x: x["name"]), separators=(",", ":")),
        encoding="utf-8")

    browse_dir.mkdir(parents=True, exist_ok=True)
    # Per-county landing pages under /browse/<county>/ (additive — the single
    # /browse/ index stays; each county heading there links to its page).
    by_county_b, by_county_f, by_county_h = {}, {}, {}
    for sl, name, region in entries:
        if region:
            by_county_b.setdefault(region, []).append((sl, name))
    for sl, name, region in flow_entries:
        if region:
            by_county_f.setdefault(region, []).append((sl, name))
    for hs, river, region in river_hubs:
        if region:
            by_county_h.setdefault(region, []).append((hs, river))
    county_slug_of = {region: slug(region)
                      for region in (set(by_county_b) | set(by_county_f) | set(by_county_h))}
    for region, cslug in sorted(county_slug_of.items()):
        chtml = _county_page(region, by_county_b.get(region, []),
                             by_county_f.get(region, []), by_county_h.get(region, []))
        cdst = browse_dir / cslug
        cdst.mkdir(parents=True, exist_ok=True)
        (cdst / "index.html").write_text(chtml, encoding="utf-8")
        urls.append((f"{SITE}/browse/{cslug}/", today))
    # prune stale county dirs the current run no longer produces
    keep_counties = set(county_slug_of.values())
    for sub in browse_dir.iterdir():
        if sub.is_dir() and sub.name not in keep_counties:
            shutil.rmtree(sub, ignore_errors=True)
    (browse_dir / "index.html").write_text(
        _browse_html(entries, flow_entries, river_hubs, n_forecast=n_forecast,
                     county_slug_of=county_slug_of), encoding="utf-8")
    sitemap_path.write_text(_sitemap_xml(urls), encoding="utf-8")
    robots_path.write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")
    lastmod_store.parent.mkdir(parents=True, exist_ok=True)
    lastmod_store.write_text(json.dumps(new_store), encoding="utf-8")

    # Prune stale stub directories the current pack no longer produces — the
    # generator only ever overwrites, so a renamed/retired/reclassified station
    # (e.g. a borehole promoted to a flow gauge) used to leave its old stub live,
    # indexable, unlinked and duplicate-content forever. Only remove DIRS whose
    # slug isn't in this run's set, and only when the set is non-empty (guards
    # against a partial/failed pack read nuking the whole tree). Files (e.g. the
    # hand-built web/rivers/index.html landing) are never touched.
    pruned = 0
    for dirpath, keep in ((out_dir, {e[0] for e in entries}),
                          (flow_out_dir, {e[0] for e in flow_entries}),
                          (rivers_out_dir, {h[0] for h in river_hubs})):
        if not keep or not dirpath.exists():
            continue
        for sub in dirpath.iterdir():
            if sub.is_dir() and sub.name not in keep:
                shutil.rmtree(sub, ignore_errors=True)
                pruned += 1

    print(f"wrote {n} borehole + {n_flow} river stubs + {n_hub} river hubs + /browse + "
          f"sitemap ({len(urls)} urls) + robots  (noindex {noindex}, no-region {noregion}, "
          f"pruned {pruned})")
    if problems:
        for p in problems[:25]:
            print("  FAIL:", p)
        raise SystemExit(f"{len(problems)} stub self-check failure(s)")
    return {"stubs": n, "flow_stubs": n_flow, "river_hubs": n_hub, "noindex": noindex,
            "noregion": noregion, "sitemap_urls": len(urls)}


if __name__ == "__main__":
    build()

"""Shared SEO / structured-data helpers — the SINGLE source of truth for the
per-borehole stub builder (build_seo_stubs) AND the share-card builder
(build_og_cards), so a borehole's slug / percentile / status wording can never
differ between its page and its card (a mismatch reads as cloaking to Google).

Ports the canonical helpers from web/detail.js (slug, esc, STATUS_LABEL, ordinal)
verbatim, so the static pages and cards match the live explorer exactly.
Stdlib-only; safe to import anywhere (no pandas / requests / network).
"""
from __future__ import annotations

import math
import re

# Mirror of web/detail.js STATUS_LABEL (line 6) — keep in lockstep.
STATUS_LABEL = {"below": "below normal", "near": "near normal", "above": "above normal"}


def slug(s) -> str:
    """Port of web/detail.js slug() (line 31): lowercase, runs of non-alphanumerics
    → '-', trim leading/trailing '-', empty → 'station'. FROZEN forever —
    re-slugging breaks every shared URL and loses link equity."""
    s = re.sub(r"[^a-z0-9]+", "-", (str(s) if s else "station").lower()).strip("-")
    return s or "station"


def esc(s) -> str:
    """Port of web/detail.js esc() (line 36): HTML/XML-escape & < > " (ampersand
    FIRST). Applied to every interpolated value in the stub <head> and card SVG."""
    return (("" if s is None else str(s))
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _round_half_up(x) -> int:
    """JS Math.round semantics (round half UP), NOT Python's banker's rounding — so
    a percentile rounds identically on the page (detail.js) and in the card/stub."""
    return int(math.floor(float(x) + 0.5))


def ordinal(n) -> str:
    """Port of web/detail.js ordinal(): 1→'1st', 2→'2nd', 3→'3rd', 11→'11th',
    21→'21st'. Rounds half-up to match the JS."""
    n = _round_half_up(n)
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def pct_ordinal(percentile):
    """'51st' for a finite percentile (e.g. 50.6 → '51st'), else None. The ' pct'
    suffix is added by the caller. ONE rounding rule for both the page and the
    card, so they can never disagree."""
    if percentile is None:
        return None
    try:
        p = float(percentile)
    except (TypeError, ValueError):
        return None
    return ordinal(p) if math.isfinite(p) else None


def last_data_date(detail: dict):
    """The real last-data date for JSON-LD dateModified / temporalCoverage end —
    NEVER today. Fallback order: freshness.last_real_reading → status.obs_date →
    observed.series[-1][0]. Returns an ISO date string or None."""
    fr = (detail.get("freshness") or {}) if isinstance(detail, dict) else {}
    if fr.get("last_real_reading"):
        return fr["last_real_reading"]
    st = detail.get("status") or {}
    if st.get("obs_date"):
        return st["obs_date"]
    series = (detail.get("observed") or {}).get("series") or []
    if series:
        return series[-1][0]
    return None

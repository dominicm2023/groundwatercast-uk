"""Shared SEO helpers — ports of the web/detail.js helpers must match the JS so
the static pages/cards never diverge from the live explorer."""
from scripts.seo_common import (
    STATUS_LABEL, esc, last_data_date, ordinal, pct_ordinal, slug,
)


def test_slug():
    assert slug("Wilgate Green") == "wilgate-green"
    assert slug("St. Mary's & Co") == "st-mary-s-co"
    assert slug("  A -- B  ") == "a-b"
    assert slug("") == "station"
    assert slug(None) == "station"
    assert slug("---") == "station"


def test_esc():
    assert esc('a & b < c > d "e"') == "a &amp; b &lt; c &gt; d &quot;e&quot;"
    assert esc(None) == ""
    assert esc("plain") == "plain"
    # ampersand must be escaped first (no double-escaping of &lt;)
    assert esc("<&>") == "&lt;&amp;&gt;"


def test_ordinal_half_up():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(11) == "11th"
    assert ordinal(12) == "12th"
    assert ordinal(13) == "13th"
    assert ordinal(21) == "21st"
    assert ordinal(50.6) == "51st"
    # half-UP (JS Math.round), NOT Python banker's rounding (which gives 50)
    assert ordinal(50.5) == "51st"
    assert ordinal(0.5) == "1st"


def test_pct_ordinal():
    assert pct_ordinal(50.6) == "51st"
    assert pct_ordinal(None) is None
    assert pct_ordinal(float("nan")) is None
    assert pct_ordinal("not a number") is None


def test_status_label_matches_js():
    assert STATUS_LABEL == {"below": "below normal", "near": "near normal",
                            "above": "above normal"}


def test_last_data_date_fallbacks():
    assert last_data_date({"freshness": {"last_real_reading": "2026-05-09"}}) == "2026-05-09"
    assert last_data_date({"freshness": {}, "status": {"obs_date": "2026-04-01"}}) == "2026-04-01"
    assert last_data_date(
        {"observed": {"series": [["2023-05-05", 24.5], ["2026-03-01", 25.1]]}}) == "2026-03-01"
    assert last_data_date({}) is None

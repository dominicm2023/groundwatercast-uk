"""Home page — minimalist landing with two equal-weight tiles.

Cross-section navigation: each tile sets a session-state hint that the
target page can read on load (e.g. to highlight that you've just arrived
from Home).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

# Page-local CSS
st.markdown(
    """
    <style>
      .gwc-hero {
          padding: 24px 0 8px 0;
      }
      .gwc-hero h1 {
          font-size: 1.85rem;
          font-weight: 700;
          color: #1a3a5c;
          margin: 0 0 4px 0;
      }
      .gwc-hero p.lead {
          color: #555;
          margin: 0;
          font-size: 1.0rem;
      }
      .gwc-tile {
          background: #ffffff;
          border: 1px solid #e6e6ea;
          border-left: 5px solid #1a3a5c;
          border-radius: 6px;
          padding: 22px 24px;
          height: 100%;
          min-height: 220px;
          transition: box-shadow 120ms ease, transform 120ms ease;
      }
      .gwc-tile:hover {
          box-shadow: 0 4px 16px rgba(26, 58, 92, 0.10);
          transform: translateY(-1px);
      }
      .gwc-tile h3 {
          margin: 0 0 6px 0;
          font-size: 1.20rem;
          color: #1a3a5c;
      }
      .gwc-tile p.sub {
          color: #555;
          margin: 0 0 14px 0;
          font-size: 0.92rem;
          line-height: 1.45;
      }
      .gwc-footer {
          color: #888;
          font-size: 0.82rem;
          padding-top: 28px;
          border-top: 1px solid #f0f0f3;
          margin-top: 32px;
      }
      .gwc-footer a { color: #1a3a5c; text-decoration: none; }
      .gwc-footer a:hover { text-decoration: underline; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="gwc-hero">
      <h1>💧 GroundwaterCast UK</h1>
      <p class="lead">Daily probabilistic groundwater forecasts to 14 days,
        built entirely on open data.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

st.write("")

st.markdown(
    """
    <div class="gwc-tile">
      <h3>Forecast outlook</h3>
      <p class="sub">One view per borehole, three horizons, one vocabulary:
        <b>current level vs normal</b> (live-seeded percentile for the
        month) → <b>14-day probabilistic fan</b> (daily ECMWF ensemble to
        day 15) with breach probabilities and
        first-crossing dates → <b>months 1–6 seasonal terciles</b>
        (below / near / above normal).</p>
    </div>
    """,
    unsafe_allow_html=True,
)
if st.button("Open Forecast outlook →", key="go_outlook",
             width="stretch", type="primary"):
    st.switch_page("pages_app/gw_outlook.py")

# ---------------------------------------------------------------------------
# Data freshness widget — per-source last-refresh + lag colour-coding.
# Reads file mtimes; no network calls.
# ---------------------------------------------------------------------------
st.markdown("")

st.markdown("### Data freshness")
st.caption(
    "Last refresh of each upstream data source. See `docs/data_sources.md` "
    "for refresh commands."
)


def _age_hours(p: Path) -> float | None:
    if not p.exists():
        return None
    return (datetime.now().timestamp() - p.stat().st_mtime) / 3600.0


def _colour_for(age_h: float | None, fresh_h: float, recent_h: float) -> str:
    """Green if ≤ fresh, amber if ≤ recent, red beyond, grey if missing."""
    if age_h is None:
        return "#888"
    if age_h <= fresh_h:
        return "#2ca02c"
    if age_h <= recent_h:
        return "#d4b106"
    return "#d62728"


def _fmt_age(age_h: float | None) -> str:
    if age_h is None:
        return "missing"
    if age_h < 1:
        return f"{int(age_h*60)} min ago"
    if age_h < 24:
        return f"{age_h:.1f} h ago"
    return f"{age_h/24:.1f} d ago"


# The 14-day forecast tile tracks the same marker the auto-refresh net
# maintains (Pastas summary, or the roll summary when the pastas venv is
# absent) — single source of truth, so the tile can't go permanently
# amber tracking a file the net never rebuilds.
from src.dashboard.auto_refresh import build_jobs as _build_refresh_jobs

_fc_marker = next(j.marker for j in _build_refresh_jobs() if j.name == "forecast")

# (label, path, fresh_h, recent_h, expected_cadence_human)
sources = [
    # Live data: auto-refreshed hourly while the app is in use
    ("EA live GW (flood-monitoring)",
        Path("data/features/gw_by_station/_MANIFEST.json"),
        2, 24, "hourly"),
    # 14-day probabilistic forecast: auto-refreshed daily
    ("14-day forecast (ensemble + Pastas)",
        _fc_marker,
        26, 24*3, "daily"),
    # Archive: refreshed when pipeline runs (manual but tracked)
    ("EA Hydrology archive",
        Path("data/features/joined_timeseries.csv"),
        24*7, 24*30, "weekly"),
    # Static-ish geology
    ("Indicative aquifer (BGS 625k, OGL)",
        Path("data/geology/bedrock_625k.geojson"),
        24*365*10, 24*365*20, "static"),
]

# Render as a grid of small cards
cols = st.columns(3)
for i, (label, path, fresh_h, recent_h, cadence) in enumerate(sources):
    col = cols[i % 3]
    age_h = _age_hours(path)
    colour = _colour_for(age_h, fresh_h, recent_h)
    age_str = _fmt_age(age_h)
    with col:
        st.markdown(
            f"""
            <div style='border:1px solid #e6e6ea;border-left:4px solid {colour};
                        border-radius:4px;padding:8px 12px;margin-bottom:8px;
                        background:#fafafb;font-size:12px;line-height:1.5'>
              <div style='font-weight:600;color:#1a3a5c;font-size:13px'>{label}</div>
              <div style='color:#555'>{age_str}
                <span style='color:#999'>· expected: {cadence}</span></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.markdown(
    """
    <div class="gwc-footer">
      Daily groundwater forecasts from Environment Agency open data.
      Independent open-source project — not affiliated with or endorsed by
      the Environment Agency or any water company.
      &nbsp;·&nbsp;
      <a href="/about" target="_self">About & methodology</a>
    </div>
    """,
    unsafe_allow_html=True,
)

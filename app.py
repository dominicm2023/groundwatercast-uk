"""
GroundwaterCast UK (entry point).

Daily 15-day probabilistic groundwater forecasts for the UK, built entirely
on open data. Multi-page Streamlit app:

  Home (default)
  ─ Groundwater
    └ Forecast outlook         (15-day probabilistic fan + breach probability)
  About                        (methodology, data sources, roadmap)

Page sources live in ./pages_app/. We deliberately avoid Streamlit's
implicit /pages folder so navigation is driven by st.navigation() here
rather than by file ordering.

Run with:
    streamlit run app.py
"""
from __future__ import annotations

import importlib
import sys

# Force re-import of dashboard modules on Streamlit hot-reload.
for _k in list(sys.modules.keys()):
    if _k.startswith("src.dashboard"):
        del sys.modules[_k]
importlib.invalidate_caches()

import streamlit as st

from src.utils.io_encoding import force_utf8_stdio, silence_known_console_noise

force_utf8_stdio()
silence_known_console_noise()


# Set page config once, here. Pages must not call st.set_page_config().
st.set_page_config(
    page_title="GroundwaterCast UK",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Pages --------------------------------------------------------------
# Page specs live in src/dashboard/nav.py (pure, testable); regional packs
# can add flag-gated pages via config/config.json -> "modules".
# Imported here rather than at the top because the hot-reload shim above
# clears src.dashboard.* from sys.modules on every rerun.
from src.dashboard.nav import build_nav_spec, load_modules_config

pg = st.navigation(
    {
        section: [
            st.Page(p.path, title=p.title, icon=p.icon,
                    url_path=p.url_path, default=p.default)
            for p in pages
        ]
        for section, pages in build_nav_spec(load_modules_config()).items()
    },
)

# ---------------------------------------------------------------------------
# In-app refresh safety nets (no cron on a local deployment). While the
# app is in use, src/dashboard/auto_refresh.py keeps the data fresh:
#   live chain hourly · forecast builds daily.
# The dashboard always renders immediately on the existing artefacts;
# refreshed files swap in atomically and the mtime-keyed caches pick them
# up on the next interaction. On a hosted deployment with a real scheduler
# (docs/deploy.md), disable with GWC_APP_START_REFRESH=0.
# Imported here rather than at the top because the hot-reload shim above
# clears src.dashboard.* from sys.modules on every rerun.
# ---------------------------------------------------------------------------
from src.dashboard.auto_refresh import maybe_kick_refreshes

maybe_kick_refreshes()
pg.run()

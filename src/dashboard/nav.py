"""Navigation spec for the dashboard, gated by ``config.modules``.

The page list lives here — NOT in ``app.py`` — as a pure data structure so
it can be unit-tested without a Streamlit session. ``app.py`` maps the spec
onto ``st.Page`` / ``st.navigation``.

The core product has no optional modules: Home, the forecast outlook and
About are always present. The ``modules`` config section and
the gating hook are kept so a regional pack (e.g. an operator-specific
overlay) can register extra pages behind a flag without touching
the core spec — flags default to **false**: a pack's pages appear only when
its config says so AND its page files exist.

This module must stay streamlit-free (json + dataclasses only).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]
_CONFIG_PATH = ROOT / "config" / "config.json"

# No optional modules ship with the core product; regional packs add theirs.
DEFAULT_MODULES: dict[str, bool] = {}


@dataclass(frozen=True)
class PageSpec:
    """Streamlit-free description of one ``st.Page``."""
    path: str            # page source, relative to repo root
    title: str
    icon: str
    url_path: str
    default: bool = False


def load_modules_config(config_path: Path = _CONFIG_PATH) -> dict[str, bool]:
    """``modules`` section of config.json, merged over ``DEFAULT_MODULES``.

    Missing file / malformed JSON / absent section all yield the defaults —
    optional modules are opt-in config, never required.
    """
    section = {}
    try:
        with open(config_path) as f:
            section = json.load(f).get("modules", {})
    except (OSError, ValueError):
        pass
    out = dict(DEFAULT_MODULES)
    if isinstance(section, dict):
        out.update({k: bool(v) for k, v in section.items()})
    return out


def build_nav_spec(modules_cfg: dict | None = None
                   ) -> dict[str, list[PageSpec]]:
    """Sidebar navigation spec: {section title: [PageSpec, ...]}.

    The core pages (Home, Forecast outlook, About) are always present.
    ``modules_cfg`` is accepted for regional packs; the core spec
    currently defines no flag-gated pages.
    """
    modules = dict(DEFAULT_MODULES)
    if modules_cfg:
        modules.update(modules_cfg)

    home = PageSpec("pages_app/home.py", "Home", "🏠", "home", default=True)
    gw_outlook = PageSpec("pages_app/gw_outlook.py",
                          "Forecast outlook", "🔭", "gw-outlook")
    about = PageSpec("pages_app/about.py", "About", "ℹ️", "about")

    return {
        "": [home],
        "Groundwater": [gw_outlook],
        "Info": [about],
    }

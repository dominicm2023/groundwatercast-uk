"""Navigation spec (src/dashboard/nav.py) + generic loader guards
(src/dashboard/loaders.py).

The nav spec is a pure data structure so the page set is testable without a
Streamlit session. The core product ships no optional modules; the
``modules`` config hook is kept for regional packs and must be accepted
(and currently ignored) without error.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from src.dashboard.nav import (DEFAULT_MODULES, PageSpec, build_nav_spec,
                               load_modules_config)


def _all_pages(spec: dict[str, list[PageSpec]]) -> list[PageSpec]:
    return [p for pages in spec.values() for p in pages]


CORE_PATHS = {
    "pages_app/home.py",
    "pages_app/gw_outlook.py",
    "pages_app/about.py",
}


class TestBuildNavSpec:
    def test_core_pages_always_present(self):
        spec = build_nav_spec()
        assert {p.path for p in _all_pages(spec)} == CORE_PATHS

    def test_sections_and_order(self):
        spec = build_nav_spec()
        assert list(spec) == ["", "Groundwater", "Info"]
        assert [p.title for p in spec["Groundwater"]] == ["Forecast outlook"]

    def test_none_and_empty_args_equal_default(self):
        assert build_nav_spec(None) == build_nav_spec() == build_nav_spec({})

    def test_unknown_module_flags_are_tolerated(self):
        # Regional packs may set flags the core doesn't know about.
        spec = build_nav_spec({"regional_overlay": True, "some_future_pack": False})
        assert {p.path for p in _all_pages(spec)} == CORE_PATHS

    def test_home_is_the_only_default_page(self):
        spec = build_nav_spec()
        defaults = [p for p in _all_pages(spec) if p.default]
        assert len(defaults) == 1 and defaults[0].path == "pages_app/home.py"


class TestLoadModulesConfig:
    def test_repo_config_has_no_modules_on(self):
        # The shipped config declares no optional modules.
        assert load_modules_config() == DEFAULT_MODULES == {}

    def test_missing_file_falls_back_to_defaults(self, tmp_path):
        assert load_modules_config(tmp_path / "nope.json") == DEFAULT_MODULES

    def test_absent_section_falls_back_to_defaults(self, tmp_path):
        fp = tmp_path / "config.json"
        fp.write_text(json.dumps({"other": 1}))
        assert load_modules_config(fp) == DEFAULT_MODULES

    def test_malformed_json_falls_back_to_defaults(self, tmp_path):
        fp = tmp_path / "config.json"
        fp.write_text("{not json")
        assert load_modules_config(fp) == DEFAULT_MODULES

    def test_pack_flags_pass_through(self, tmp_path):
        fp = tmp_path / "config.json"
        fp.write_text(json.dumps({"modules": {"regional_pack_x": True}}))
        assert load_modules_config(fp) == {"regional_pack_x": True}


# ---------------------------------------------------------------------------
# Generic loader guards — a fresh clone (no built artefacts) must get typed
# empty frames, never an exception.
# ---------------------------------------------------------------------------

@pytest.fixture
def loaders_mod():
    from src.dashboard import loaders
    loaders.load_catalogue.clear()
    loaders.load_freshness.clear()
    yield loaders
    loaders.load_catalogue.clear()
    loaders.load_freshness.clear()


class TestLoaderGuards:
    def test_load_catalogue_missing_returns_typed_empty(
            self, loaders_mod, monkeypatch, tmp_path):
        monkeypatch.setattr(loaders_mod, "_CATALOGUE_PATH",
                            tmp_path / "absent.csv")
        with pytest.warns(RuntimeWarning):
            df = loaders_mod.load_catalogue()
        assert df.empty
        assert list(df.columns) == loaders_mod._CATALOGUE_COLUMNS

    def test_load_catalogue_dedupes_when_present(
            self, loaders_mod, monkeypatch, tmp_path):
        fp = tmp_path / "catalogue.csv"
        pd.DataFrame({
            "station_id": ["a", "a", "b"],
            "station_name": ["A", "A", "B"],
            "lat": [51.0] * 3, "lon": [-1.0] * 3,
        }).to_csv(fp, index=False)
        monkeypatch.setattr(loaders_mod, "_CATALOGUE_PATH", fp)
        df = loaders_mod.load_catalogue()
        assert list(df["station_id"]) == ["a", "b"]

    def test_load_freshness_missing_returns_typed_empty(
            self, loaders_mod, monkeypatch, tmp_path):
        # load_freshness reads a repo-relative path — run from an empty cwd.
        monkeypatch.chdir(tmp_path)
        df = loaders_mod.load_freshness()
        assert df.empty
        assert "station_id" in df.columns

    def test_load_gw_for_bh_empty_id(self, loaders_mod):
        assert loaders_mod.load_gw_for_bh("").empty

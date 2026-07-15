"""Pins Finding 3 of the 2026-07-15 verify-gated sweep: the four low-flow
pilot consumers — ``scripts/build_ensemble_members.py``'s
``build_flow_ens_bridge`` call site, ``scripts/build_flow_models.py``'s
``--pilot`` default, ``scripts/build_flow_seasonal_shadow.py``, and
``scripts/refresh_seasonal_inputs.py`` — must resolve
``forecast.ensemble.flow.pilot_path`` identically. Before the fix, the first
two hardcoded the pilot CSV path while the latter two read it from config;
a host that overrode ``pilot_path`` got a bridge/calibration pass silently
pointed at the wrong (or default) pilot set while the seasonal shadow used
the configured one.

All four now import ``src.download.flow.resolve_flow_pilot_path`` and call
it with their own module's repo-root constant — this test drives that exact
call through each module's own import binding (not a shared indirection) so
a future accidental re-hardcoding in any one of the four fails here.
"""
from __future__ import annotations

from pathlib import Path

import scripts.build_ensemble_members as build_ensemble_members
import scripts.build_flow_models as build_flow_models
import scripts.build_flow_seasonal_shadow as build_flow_seasonal_shadow
import scripts.refresh_seasonal_inputs as refresh_seasonal_inputs

_CONSUMERS = {
    "build_ensemble_members": (build_ensemble_members, "_PROJECT_ROOT"),
    "build_flow_models": (build_flow_models, "ROOT"),
    "build_flow_seasonal_shadow": (build_flow_seasonal_shadow, "ROOT"),
    "refresh_seasonal_inputs": (refresh_seasonal_inputs, "ROOT"),
}


def _resolve_all(cfg: dict) -> dict[str, Path]:
    return {
        name: mod.resolve_flow_pilot_path(cfg, getattr(mod, root_attr))
        for name, (mod, root_attr) in _CONSUMERS.items()
    }


def test_all_four_consumers_import_the_same_function_object():
    fns = {mod.resolve_flow_pilot_path for mod, _ in _CONSUMERS.values()}
    assert len(fns) == 1, (
        "one or more consumers no longer import "
        "src.download.flow.resolve_flow_pilot_path — they have drifted "
        "apart on how the pilot path resolves")


def test_all_four_consumers_share_the_same_repo_root():
    roots = {getattr(mod, attr) for mod, attr in _CONSUMERS.values()}
    assert len(roots) == 1, f"consumers disagree on repo root: {roots}"


def test_all_four_resolve_identically_with_default_config():
    cfg = {"forecast": {"ensemble": {"flow": {"enabled": True}}}}
    resolved = _resolve_all(cfg)
    values = set(resolved.values())
    assert len(values) == 1, f"pilot paths diverged: {resolved}"
    assert next(iter(values)).name == "flow_pilot.csv"


def test_all_four_resolve_identically_with_custom_relative_pilot_path():
    cfg = {"forecast": {"ensemble": {"flow": {
        "pilot_path": "data/processed/custom_pilot_set.csv",
    }}}}
    resolved = _resolve_all(cfg)
    values = set(resolved.values())
    assert len(values) == 1, f"pilot paths diverged: {resolved}"
    assert next(iter(values)).name == "custom_pilot_set.csv"


def test_all_four_resolve_identically_with_custom_absolute_pilot_path(tmp_path):
    custom = tmp_path / "elsewhere" / "pilot.csv"
    cfg = {"forecast": {"ensemble": {"flow": {"pilot_path": str(custom)}}}}
    resolved = _resolve_all(cfg)
    values = set(resolved.values())
    assert len(values) == 1, f"pilot paths diverged: {resolved}"
    assert next(iter(values)) == custom

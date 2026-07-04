"""Tests for scripts.run_chain — the declarative refresh-chain runner.

These tests pin the *documented* order from CLAUDE.md "How to run":
if a stage moves, the prose spec and the runner must move together.
Pure-helper tests only — no subprocess execution.
"""

import pytest

from scripts.run_chain import (
    GROUPS,
    MAIN_ENV,
    PASTAS_ENV,
    STAGES,
    select_stages,
    stage_command,
)

ALL_NAMES = [s.name for s in STAGES]


def _pos(name):
    return ALL_NAMES.index(name)


# ---------------------------------------------------------------------------
# (a) stage names unique + documented order preserved
# ---------------------------------------------------------------------------

def test_stage_names_unique():
    assert len(ALL_NAMES) == len(set(ALL_NAMES))


def test_every_stage_in_a_known_group_and_env():
    for s in STAGES:
        assert s.group in GROUPS, s.name
        assert s.env in (MAIN_ENV, PASTAS_ENV), s.name


def test_dipped_rebuild_runs_before_parquet_and_freshness():
    # The v1.6 landmine: dipped re-merge MUST precede the shard rebuild,
    # which MUST precede freshness (freshness reads the shards).
    assert (_pos("v15_build_dipped_daily_series")
            < _pos("v15_build_per_station_parquet")
            < _pos("v15_build_gw_freshness"))


def test_core_chain_order():
    core = [s.name for s in STAGES if s.group == "core"]
    assert core == [
        "v15_build_dipped_daily_series",
        "v15_build_per_station_parquet",
        "v15_build_gw_freshness",
        "build_gw_normals",
    ]


def test_ensemble_then_pastas_order():
    # 8d -> 8e -> 8g -> 8h: pastas members need roll members; summaries follow.
    assert (_pos("build_ensemble_members")
            < _pos("build_ensemble_summary")
            < _pos("build_pastas_members")
            < _pos("build_pastas_summary"))


def test_pet_refresh_before_pastas_calibration():
    # 8e-pre caches PET; 8f calibration reads that cache.
    assert _pos("refresh_pet") < _pos("build_pastas_models")


def test_live_rainfall_before_ensemble():
    # 8d needs fresh rainfall from 8b.
    assert _pos("v19_refresh_live_rainfall") < _pos("build_ensemble_members")


def test_v16_uses_documented_path_form_invocation():
    (stage,) = [s for s in STAGES if s.name == "v16_refresh_live_gw"]
    assert stage.argv == ("scripts/v16_refresh_live_gw.py",)


# ---------------------------------------------------------------------------
# (b) --from / --to selection slices correctly
# ---------------------------------------------------------------------------

def test_default_core_selection():
    plan = select_stages(["core"])
    assert [s.name for s in plan] == [s.name for s in STAGES if s.group == "core"]


def test_all_groups_selection_preserves_global_order():
    plan = select_stages(GROUPS)
    assert [s.name for s in plan] == ALL_NAMES


def test_from_to_slices_inclusively():
    plan = select_stages(
        ["core"],
        from_name="v15_build_per_station_parquet",
        to_name="v15_build_gw_freshness",
    )
    assert [s.name for s in plan] == [
        "v15_build_per_station_parquet",
        "v15_build_gw_freshness",
    ]


def test_from_only_runs_to_end_of_selection():
    plan = select_stages(["core"], from_name="v15_build_per_station_parquet")
    assert [s.name for s in plan] == [
        "v15_build_per_station_parquet",
        "v15_build_gw_freshness",
        "build_gw_normals",
    ]


def test_from_outside_selected_groups_rejected():
    # build_ensemble_members exists but is not in core: slicing must refuse.
    with pytest.raises(ValueError):
        select_stages(["core"], from_name="build_ensemble_members")


def test_inverted_from_to_rejected():
    with pytest.raises(ValueError):
        select_stages(["core"],
                      from_name="v15_build_gw_freshness",
                      to_name="v15_build_dipped_daily_series")


def test_unknown_group_rejected():
    with pytest.raises(ValueError):
        select_stages(["corre"])


# ---------------------------------------------------------------------------
# (c) pastas stages carry the venv-interpreter marker
# ---------------------------------------------------------------------------

def test_pastas_stages_use_pastas_env():
    pastas_env_stages = {s.name for s in STAGES if s.env == PASTAS_ENV}
    assert pastas_env_stages == {
        "build_pastas_models",      # 8f
        "build_pastas_members",     # 8g
        "build_pastas_summary",     # 8h
        "build_seasonal_outlook",   # 9b (seasonal, monthly)
    }


def test_seasonal_group_order_and_envs():
    # 9 (fetch, main env) must precede 9b (compute, pastas env).
    seasonal = [s for s in STAGES if s.group == "seasonal"]
    assert [s.name for s in seasonal] == ["refresh_seasonal_inputs",
                                          "build_seasonal_outlook"]
    assert seasonal[0].env == MAIN_ENV and seasonal[1].env == PASTAS_ENV


def test_refresh_pet_runs_in_main_env():
    # 8e-pre is explicitly a MAIN-env step per CLAUDE.md.
    (stage,) = [s for s in STAGES if s.name == "refresh_pet"]
    assert stage.env == MAIN_ENV


def test_stage_command_uses_placeholder_when_pastas_python_missing():
    (stage,) = [s for s in STAGES if s.name == "build_pastas_models"]
    cmd = stage_command(stage, pastas_python=None)
    assert cmd[0] == "<pastas-py>"
    assert cmd[1:] == list(stage.argv)


def test_stage_command_resolves_pastas_interpreter():
    (stage,) = [s for s in STAGES if s.name == "build_pastas_summary"]
    cmd = stage_command(stage, pastas_python="X:/venv/python.exe")
    assert cmd[0] == "X:/venv/python.exe"


def test_publish_is_last_stage():
    # The pack packages this run's outputs; the SEO stubs derive FROM the pack,
    # so the order must be ...build_artifact_pack -> build_seo_stubs (last, so a
    # stub failure can't undo the published pack).
    assert ALL_NAMES[-1] == "build_seo_stubs"
    assert ALL_NAMES[-2] == "build_og_cards"      # cards render BEFORE the stubs embed them
    assert ALL_NAMES[-3] == "build_artifact_pack"
    plan = [s.name for s in select_stages(["forecast", "publish"])]
    assert plan[-1] == "build_seo_stubs"
    assert plan.index("build_pastas_summary") < plan.index("build_artifact_pack")
    assert plan.index("build_artifact_pack") < plan.index("build_og_cards")
    assert plan.index("build_og_cards") < plan.index("build_seo_stubs")


def test_publish_runs_in_main_env():
    (stage,) = [s for s in STAGES if s.name == "build_artifact_pack"]
    assert stage.env == MAIN_ENV and stage.group == "publish"


# ---------------------------------------------------------------------------
# (d) src.pipeline.run stops at features — the forecast chain (and the
#     retired risk/RF stages) must NOT creep back in
# ---------------------------------------------------------------------------

def test_pipeline_run_stages_end_at_features():
    from src.pipeline.run import STAGES as PIPELINE_STAGES
    assert [s.name for s in PIPELINE_STAGES] == [
        "catalogue", "linking", "download", "features"]


import scripts.run_chain as run_chain


class TestForecastVirtualGroup:
    """--forecast = the daily forecast refresh: ensemble + daily pastas
    stages, EXCLUDING the 8f recalibration (that's a retrain)."""

    def test_members(self):
        plan = [s.name for s in run_chain.select_stages(["forecast"])]
        assert plan == ["build_ensemble_members", "build_ensemble_summary",
                        "refresh_pet", "build_pastas_members",
                        "build_pastas_summary"]
        assert "build_pastas_models" not in plan

    def test_combines_with_physical_groups_without_duplicates(self):
        plan = [s.name for s in run_chain.select_stages(["forecast", "ensemble"])]
        assert plan.count("build_ensemble_members") == 1

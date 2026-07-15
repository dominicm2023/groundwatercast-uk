"""Tests for scripts.run_chain — the declarative refresh-chain runner.

These tests pin the *documented* order from CLAUDE.md "How to run":
if a stage moves, the prose spec and the runner must move together.
Pure-helper tests only — no subprocess execution.
"""

import pytest

import scripts.run_chain as RC
from scripts.run_chain import (
    GROUPS,
    MAIN_ENV,
    PASTAS_ENV,
    STAGES,
    _acquire_lock_or_wait,
    _parse_args,
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


def test_freshness_group_is_topup_shard_append_then_rainfall():
    # Top up the raw GW tail, append it surgically into the shards (which also
    # rebuilds gw_freshness), then top up the flow gauges' own shards
    # (build_flow_shards — low-flow build_plan.md Stage 2, added 2026-07-14;
    # no xref dependency, so it sits with the other ingest-group stages),
    # then extend the live rainfall tail (v19 — moved here from the hourly
    # --live after the 2026-07-09 lockout: the forecast needs rainfall fresh
    # at 06:30, not on the hour every hour). Deliberately NO wholesale
    # joined/shard rebuild: that wipes v16's live overlay (the 2026-07-08
    # 288→188 regression), and v15 only re-merges dipped data.
    plan = [s.name for s in select_stages(["freshness"])]
    assert plan == ["refresh_gw_tail", "refresh_gw_shard_tail",
                    "build_flow_shards", "v19_refresh_live_rainfall"]
    for banned in ("v15_build_dipped_daily_series",
                   "v15_build_per_station_parquet"):
        assert banned not in plan


def test_build_flow_shards_is_ingest_group_main_env():
    # low-flow build_plan.md Stage 2: flow shard top-up runs in the main env
    # (stdlib + pandas only, no Pastas needed for ingest).
    (stage,) = [s for s in STAGES if s.name == "build_flow_shards"]
    assert stage.group == "ingest" and stage.env == MAIN_ENV


def test_hourly_live_group_is_gw_only():
    # The hourly --live must stay MINUTES-fast (it shares the run_chain lock
    # with everything else): v16 GW readings only, never the ~30-min v19
    # rainfall sweep (the 2026-07-09 forecast-cron lockout).
    live = [s.name for s in STAGES if s.group == "live"]
    assert live == ["v16_refresh_live_gw"]


def test_ensemble_then_pastas_order():
    # 8d -> 8e -> 8g -> 8h: pastas members need roll members; summaries follow.
    assert (_pos("build_ensemble_members")
            < _pos("build_ensemble_summary")
            < _pos("build_pastas_members")
            < _pos("build_pastas_summary"))


def test_flow_models_sits_with_pastas_recalibration():
    # low-flow build_plan.md Stage 6: build_flow_models (8f-flow) is
    # build_pastas_models' (8f) monthly-recalibration sibling — declared
    # right after it, before the daily 8g/8h stages.
    assert (_pos("build_pastas_models")
            < _pos("build_flow_models")
            < _pos("build_pastas_members"))


def test_flow_members_sits_after_gw_forecast_stages():
    # low-flow build_plan.md Stage 6: build_flow_members (8h-flow) runs AFTER
    # the GW forecast stages so it can reuse the day's already-cached ENS
    # GRIB cycle, and before the (monthly) seasonal group.
    assert _pos("build_pastas_summary") < _pos("build_flow_members") < _pos("refresh_seasonal_inputs")


def test_flow_stages_are_pastas_group_pastas_env():
    for name in ("build_flow_models", "build_flow_members"):
        (stage,) = [s for s in STAGES if s.name == name]
        assert stage.group == "pastas" and stage.env == PASTAS_ENV


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
        "build_flow_models",        # 8f-flow (low-flow build_plan.md Stage 6)
        "build_pastas_members",     # 8g
        "build_pastas_summary",     # 8h
        "build_flow_members",       # 8h-flow (low-flow build_plan.md Stage 6)
        "build_seasonal_outlook",   # 9b (seasonal, monthly)
        "build_flow_seasonal_shadow",  # 9c (low-flow build_plan.md Stage 6b)
    }


def test_seasonal_group_order_and_envs():
    # 9 (fetch, main env) must precede 9b (GW compute) and 9c (flow shadow
    # compute, low-flow build_plan.md Stage 6b) — both pastas env.
    seasonal = [s for s in STAGES if s.group == "seasonal"]
    assert [s.name for s in seasonal] == ["refresh_seasonal_inputs",
                                          "build_seasonal_outlook",
                                          "build_flow_seasonal_shadow"]
    assert seasonal[0].env == MAIN_ENV
    assert seasonal[1].env == PASTAS_ENV
    assert seasonal[2].env == PASTAS_ENV


def test_flow_seasonal_shadow_runs_after_gw_seasonal_and_flow_models():
    # low-flow build_plan.md Stage 6b: the shadow archive needs the flow
    # pilot's calibrated models (8f-flow) and runs "alongside" (i.e. right
    # after) the GW seasonal outlook (9b), not before it.
    assert _pos("build_flow_models") < _pos("build_flow_seasonal_shadow")
    assert _pos("build_seasonal_outlook") < _pos("build_flow_seasonal_shadow")


def test_flow_seasonal_shadow_not_in_forecast_group():
    # Monthly cadence, like 9/9b — must NOT be pulled into the daily
    # --forecast virtual group.
    plan = [s.name for s in select_stages(["forecast"])]
    assert "build_flow_seasonal_shadow" not in plan


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
                        "build_pastas_summary", "build_flow_members"]
        assert "build_pastas_models" not in plan
        assert "build_flow_models" not in plan   # recalibration, not a refresh

    def test_combines_with_physical_groups_without_duplicates(self):
        plan = [s.name for s in run_chain.select_stages(["forecast", "ensemble"])]
        assert plan.count("build_ensemble_members") == 1


class TestLockWait:
    """--lock-wait-min lets a colliding cron wait out the hourly --live instead
    of skipping a whole day (the 2026-07-09 lockout: --live grew to ~45 min and
    the 06:30 forecast fired mid-run)."""

    def test_default_is_zero(self):
        assert _parse_args(["--forecast"]).lock_wait_min == 0.0
        assert _parse_args(["--forecast", "--lock-wait-min", "45"]).lock_wait_min == 45.0

    def test_acquires_immediately_when_free(self, monkeypatch):
        monkeypatch.setattr(RC, "_acquire_lock", lambda: True)
        slept = []
        assert _acquire_lock_or_wait(30, sleep=slept.append, clock=lambda: 0.0) is True
        assert slept == []                          # no waiting when the lock is free

    def test_waits_then_acquires(self, monkeypatch):
        calls = {"n": 0}
        def fake_acquire():
            calls["n"] += 1
            return calls["n"] >= 3                  # busy twice, then free
        monkeypatch.setattr(RC, "_acquire_lock", fake_acquire)
        t = {"v": 0.0}
        slept = []
        def clock():
            return t["v"]
        def sleep(s):
            slept.append(s); t["v"] += s
        assert _acquire_lock_or_wait(30, sleep=sleep, clock=clock) is True
        assert slept == [30, 30]                    # polled twice before acquiring

    def test_times_out(self, monkeypatch):
        monkeypatch.setattr(RC, "_acquire_lock", lambda: False)   # never free
        t = {"v": 0.0}
        def clock():
            return t["v"]
        def sleep(s):
            t["v"] += s
        assert _acquire_lock_or_wait(2, sleep=sleep, clock=clock) is False
        assert t["v"] >= 120                         # waited ~the full 2 min

    def test_zero_wait_is_single_attempt(self, monkeypatch):
        monkeypatch.setattr(RC, "_acquire_lock", lambda: False)
        slept = []
        assert _acquire_lock_or_wait(0, sleep=slept.append, clock=lambda: 0.0) is False
        assert slept == []                          # exits immediately, old behaviour

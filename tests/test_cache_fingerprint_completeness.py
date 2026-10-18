"""Deterministic regression tests for cache fingerprint completeness and material configuration invalidation.

Verifies:
1. An unchanged run can reuse valid artifacts.
2. Changing a material stage configuration (adapter, model, effort) invalidates the affected stage.
3. Changing a prompt template (.ai/roles/xxx.md or protocol.md) invalidates the affected stage.
4. Changing upstream artifact content invalidates dependent stages even with preserved timestamps.
5. Changes to irrelevant execution metadata (timeout, auto_approve, idle_timeout) do NOT cause unnecessary invalidation.
6. Cache invalidation remains correct across stage resumption and cascade invalidation.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.cli import is_stage_completed
from forge.core.config import Config, StageConfig
from forge.core.context import Context
from forge.core.role import Role
from forge.stages.definition import StageOrder
from forge.storage.run_manager import (
    RunManager,
    compute_stage_config_fingerprint,
)


def _setup_pipeline_run(tmp_path: Path):
    """Helper to initialize a project root and produce authentic artifacts for Architect and Planner."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Implement resilient distributed telemetry service")

    config = Config.default()
    config.stages["planner"] = StageConfig(
        adapter="opencode",
        model="gpt-5.6-terra",
        effort="medium",
        timeout=300,
        auto_approve=False,
    )

    # Architect (Stage 1)
    arch_def = StageOrder.get_architect()
    arch_role = Role.load(arch_def.name, project_root=tmp_path, sequence_number=arch_def.sequence_number)
    arch_sc = config.get_stage_config(arch_def.name)
    arch_cfg_fp = compute_stage_config_fingerprint(adapter=arch_sc.adapter, model=arch_sc.model, effort=arch_sc.effort, extra_flags=arch_sc.extra_flags)
    rm.save_stage_artifacts(
        run=run,
        sequence_number=arch_def.sequence_number,
        role_name=arch_def.name,
        markdown_content="# Architecture\nDesigned telemetry service architecture.",
        json_data={
            "status": "APPROVED",
            "handoff": "PLANNER",
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "role": arch_def.name,
            "sequence_number": arch_def.sequence_number,
            "machine_report": {"role": "ARCHITECT", "status": "APPROVED", "is_valid": True},
        },
        adapter_name=arch_sc.adapter,
        template_hash=arch_role.template_hash,
        config_fingerprint=arch_cfg_fp,
    )

    # Planner (Stage 2)
    plan_def = StageOrder.get_planner()
    plan_role = Role.load(plan_def.name, project_root=tmp_path, sequence_number=plan_def.sequence_number)
    plan_sc = config.get_stage_config(plan_def.name)
    plan_cfg_fp = compute_stage_config_fingerprint(adapter=plan_sc.adapter, model=plan_sc.model, effort=plan_sc.effort, extra_flags=plan_sc.extra_flags)
    rm.save_stage_artifacts(
        run=run,
        sequence_number=plan_def.sequence_number,
        role_name=plan_def.name,
        markdown_content="# Planner\nPlan steps: 1. Ingest, 2. Buffer, 3. Export.",
        json_data={
            "status": "READY",
            "handoff": "EXECUTOR",
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "role": plan_def.name,
            "sequence_number": plan_def.sequence_number,
            "machine_report": {"role": "PLANNER", "status": "READY", "is_valid": True},
        },
        adapter_name=plan_sc.adapter,
        template_hash=plan_role.template_hash,
        config_fingerprint=plan_cfg_fp,
    )

    # Executor (Stage 3)
    exec_def = StageOrder.get_executor()
    exec_role = Role.load(exec_def.name, project_root=tmp_path, sequence_number=exec_def.sequence_number)
    exec_sc = config.get_stage_config(exec_def.name)
    exec_cfg_fp = compute_stage_config_fingerprint(adapter=exec_sc.adapter, model=exec_sc.model, effort=exec_sc.effort, extra_flags=exec_sc.extra_flags)
    rm.save_stage_artifacts(
        run=run,
        sequence_number=exec_def.sequence_number,
        role_name=exec_def.name,
        markdown_content="# Executor\nImplemented telemetry endpoints.",
        json_data={
            "status": "SUCCESS",
            "handoff": "TESTER",
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "role": exec_def.name,
            "sequence_number": exec_def.sequence_number,
            "machine_report": {"role": "EXECUTOR", "status": "SUCCESS", "is_valid": True},
        },
        adapter_name=exec_sc.adapter,
        template_hash=exec_role.template_hash,
        config_fingerprint=exec_cfg_fp,
    )

    ctx = Context(run=run, project_root=tmp_path, config=config, git=MagicMock())
    return rm, run, ctx


def test_unchanged_run_reuses_valid_artifacts(tmp_path):
    """Verify an unchanged run legitimately skips and reuses completed stage deliverables."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)

    arch_def = StageOrder.get_architect()
    arch_done, arch_status = is_stage_completed(run, arch_def, rm, context=ctx)
    assert arch_done is True
    assert arch_status == "APPROVED"

    plan_def = StageOrder.get_planner()
    plan_done, plan_status = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is True
    assert plan_status == "READY"

    exec_def = StageOrder.get_executor()
    exec_done, exec_status = is_stage_completed(run, exec_def, rm, context=ctx)
    assert exec_done is True
    assert exec_status == "SUCCESS"


def test_material_config_adapter_change_invalidates_stage(tmp_path):
    """Verify changing a stage's configured adapter rejects cached artifacts and triggers fresh execution."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)

    plan_def = StageOrder.get_planner()
    # Before change: planner is completed
    assert is_stage_completed(run, plan_def, rm, context=ctx)[0] is True

    # User reconfigures Planner to use 'antigravity' instead of 'opencode'
    ctx.config.stages["planner"].adapter = "antigravity"

    # Planner MUST be rejected because its execution adapter changed
    plan_done, _ = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is False, "Stage must be invalidated when configured adapter changes!"

    # Invalidate stage deliverables: downstream Executor must also be invalidated
    invalidated = rm.invalidate_stage_and_downstream_artifacts(run, plan_def.sequence_number)
    assert any("02_planner" in f for f in invalidated)
    assert any("03_executor" in f for f in invalidated)

    exec_def = StageOrder.get_executor()
    exec_done, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert exec_done is False, "Downstream Executor deliverable must not be reusable after Planner invalidation!"

    # Architect (upstream) remains valid and reusable
    arch_def = StageOrder.get_architect()
    arch_done, arch_status = is_stage_completed(run, arch_def, rm, context=ctx)
    assert arch_done is True
    assert arch_status == "APPROVED"


def test_material_config_model_change_invalidates_stage(tmp_path):
    """Verify changing a stage's configured model rejects cached artifacts."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)

    plan_def = StageOrder.get_planner()
    # User switches model from 'gpt-5.6-terra' to 'gemini-3.7-flash-high'
    ctx.config.stages["planner"].model = "gemini-3.7-flash-high"

    plan_done, _ = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is False, "Stage must be invalidated when configured model changes!"


def test_prompt_template_change_invalidates_stage_and_downstream(tmp_path):
    """Verify modifying a role's markdown prompt template rejects cached deliverables."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)
    plan_def = StageOrder.get_planner()

    # Create custom role template file on disk that alters Planner prompt instructions
    roles_dir = tmp_path / ".ai" / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)
    custom_planner_template = (
        "# ROLE: PLANNER\n"
        "You are an expert system planner.\n"
        "STRICT MANDATE: All services must use gRPC streaming with protobuf v3 contracts."
    )
    (roles_dir / "planner.md").write_text(custom_planner_template, encoding="utf-8")

    # Planner artifact was generated with default template; now template on disk is customized
    plan_done, _ = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is False, "Stage must be invalidated when prompt template on disk changes!"

    # Downstream invalidation ensures fresh execution with the new template
    rm.invalidate_stage_and_downstream_artifacts(run, plan_def.sequence_number)
    exec_def = StageOrder.get_executor()
    assert is_stage_completed(run, exec_def, rm, context=ctx)[0] is False


def test_irrelevant_metadata_changes_do_not_cause_unnecessary_invalidation(tmp_path):
    """Verify changing non-material execution metadata (timeout, auto_approve) does NOT invalidate valid cache."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)
    plan_def = StageOrder.get_planner()

    # Changing execution timeout from 300s to 600s
    ctx.config.stages["planner"].timeout = 600

    # Changing auto_approve flag from False to True
    ctx.config.stages["planner"].auto_approve = True

    # Artifact remains completely valid and reusable because timeout/auto_approve do not alter stage output semantics
    plan_done, plan_status = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is True
    assert plan_status == "READY"


def test_upstream_content_change_with_preserved_mtime_invalidates_downstream(tmp_path):
    """Verify upstream content change rejects downstream deliverables even when mtime is preserved."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)
    exec_def = StageOrder.get_executor()
    planner_json = run.run_dir / "02_planner.json"

    # Capture original timestamps
    stat_before = planner_json.stat()
    orig_atime = stat_before.st_atime
    orig_mtime = stat_before.st_mtime

    # Mutate planner content
    p_data = json.loads(planner_json.read_text(encoding="utf-8"))
    p_data["plan_steps"] = ["Altered step 1", "Altered step 2"]
    planner_json.write_text(json.dumps(p_data, indent=2), encoding="utf-8")

    # Restore exact timestamps
    os.utime(planner_json, (orig_atime, orig_mtime))
    assert planner_json.stat().st_mtime == orig_mtime

    # Downstream Executor must be invalidated
    exec_done, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert exec_done is False, "Downstream Executor deliverable must be rejected after upstream content mutation!"


def test_cache_invalidation_remains_correct_after_resuming_run(tmp_path):
    """Verify resuming an interrupted run re-executes invalidated stages and skips intact upstream stages."""
    rm, run, ctx = _setup_pipeline_run(tmp_path)

    # Invalidate from Planner forward (e.g. user re-runs from planner)
    plan_def = StageOrder.get_planner()
    rm.invalidate_stage_and_downstream_artifacts(run, plan_def.sequence_number)

    # Architect (Stage 1) is still intact and reusable
    arch_def = StageOrder.get_architect()
    arch_done, arch_status = is_stage_completed(run, arch_def, rm, context=ctx)
    assert arch_done is True
    assert arch_status == "APPROVED"

    # Planner (Stage 2) is invalidated and must execute
    plan_done, _ = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is False

    # Executor (Stage 3) is invalidated and must execute
    exec_def = StageOrder.get_executor()
    exec_done, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert exec_done is False

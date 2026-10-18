"""Deterministic regression tests for Forge artifact freshness and integrity.

Adversarial test cases verifying that cached stage artifacts cannot be reused
when upstream inputs have changed, when timestamps are manipulated or preserved,
when artifacts are corrupted, tampered with, missing, or cross-contaminated from another run.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from forge.cli import is_stage_completed
from forge.core.config import Config
from forge.core.context import Context
from forge.stages.definition import StageOrder
from forge.storage.run_manager import RunManager


def _setup_valid_run_through_executor(tmp_path: Path):
    """Helper to initialize a project and produce authentic stage artifacts up to Executor."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Build adversarial telemetry monitor")

    stages_data = [
        (StageOrder.get_architect(), "APPROVED", "PLANNER", "Architecture for telemetry monitor"),
        (StageOrder.get_planner(), "READY", "EXECUTOR", "Plan steps 1..3 for telemetry monitor"),
        (StageOrder.get_executor(), "SUCCESS", "TESTER", "Executed code for telemetry monitor"),
    ]

    for s_def, s_status, s_handoff, s_doc in stages_data:
        data = {
            "status": s_status,
            "handoff": s_handoff,
            "exit_code": 0,
            "task_fingerprint": run.task_fingerprint,
            "run_id": run.run_id,
            "role": s_def.name,
            "sequence_number": s_def.sequence_number,
            "machine_report": {
                "role": s_def.name.upper(),
                "status": s_status,
                "handoff": s_handoff,
                "exit_code": 0,
                "is_valid": True,
            },
        }
        rm.save_stage_artifacts(
            run=run,
            sequence_number=s_def.sequence_number,
            role_name=s_def.name,
            markdown_content=f"# {s_def.name}\n{s_doc}",
            json_data=data,
        )

    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=MagicMock())
    return rm, run, ctx


def test_adversarial_case_a_content_changes_with_preserved_timestamps(tmp_path):
    """Case A: Modify upstream contents while preserving original mtime.
    Downstream stage must detect changed input and reject cached artifact.
    """
    rm, run, ctx = _setup_valid_run_through_executor(tmp_path)

    # Sanity check: Executor initially valid
    exec_def = StageOrder.get_executor()
    done, status = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done is True
    assert status == "SUCCESS"

    # Capture original mtime and atime of upstream Planner
    planner_json = run.run_dir / "02_planner.json"
    stat_before = planner_json.stat()
    orig_atime = stat_before.st_atime
    orig_mtime = stat_before.st_mtime

    # Mutate planner content (e.g. altered plan steps)
    p_data = json.loads(planner_json.read_text(encoding="utf-8"))
    p_data["plan_steps"] = ["Completely new malicious or modified plan"]
    planner_json.write_text(json.dumps(p_data, indent=2), encoding="utf-8")

    # Restore exact modification and access times
    os.utime(planner_json, (orig_atime, orig_mtime))
    stat_after = planner_json.stat()
    assert stat_after.st_mtime == orig_mtime

    # Verification: Executor MUST NOT be reused!
    done_after, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done_after is False, "Downstream Executor deliverable must be rejected when upstream Planner content changed despite preserved timestamp!"


def test_adversarial_case_b_same_timestamp_content_replacement(tmp_path):
    """Case B: Replace upstream artifact with different content while keeping timestamp unchanged.
    Verify cached downstream results are rejected.
    """
    rm, run, ctx = _setup_valid_run_through_executor(tmp_path)
    exec_def = StageOrder.get_executor()

    planner_json = run.run_dir / "02_planner.json"
    orig_mtime = planner_json.stat().st_mtime
    orig_atime = planner_json.stat().st_atime

    # Replace 02_planner.json completely with a different dictionary
    replacement_data = {
        "status": "READY",
        "handoff": "EXECUTOR",
        "exit_code": 0,
        "task_fingerprint": run.task_fingerprint,
        "run_id": run.run_id,
        "role": "planner",
        "sequence_number": 2,
        "tasks": ["Arbitrary replaced task list"],
    }
    planner_json.write_text(json.dumps(replacement_data, indent=2), encoding="utf-8")
    os.utime(planner_json, (orig_atime, orig_mtime))

    done_after, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done_after is False, "Cached downstream deliverable must be rejected after upstream content replacement!"


def test_adversarial_case_c_timestamp_manipulation(tmp_path):
    """Case C: Older, newer, identical, and deliberately restored modification times.
    Verify timestamps cannot independently establish artifact validity when content evidence differs.
    """
    rm, run, ctx = _setup_valid_run_through_executor(tmp_path)
    exec_def = StageOrder.get_executor()
    planner_json = run.run_dir / "02_planner.json"
    candidate_json = run.run_dir / "03_executor.json"

    # Mutate planner content
    p_data = json.loads(planner_json.read_text(encoding="utf-8"))
    p_data["revision"] = "rogue_v2"
    planner_json.write_text(json.dumps(p_data, indent=2), encoding="utf-8")

    # Sub-case C1: Backdate upstream mtime to be 100 seconds OLDER than candidate
    cand_mtime = candidate_json.stat().st_mtime
    os.utime(planner_json, (cand_mtime - 100, cand_mtime - 100))
    done_c1, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done_c1 is False, "Backdated upstream artifact with changed content must be rejected!"

    # Sub-case C2: Set upstream mtime to be identical to candidate
    os.utime(planner_json, (cand_mtime, cand_mtime))
    done_c2, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done_c2 is False, "Identical-timestamp upstream artifact with changed content must be rejected!"


def test_adversarial_case_d_missing_and_replaced_upstream_artifacts(tmp_path):
    """Case D: Missing, malformed, or foreign-run upstream artifacts.
    Verify downstream cached results are not reused without valid upstream provenance.
    """
    # D1: Delete required upstream artifact
    rm1, run1, ctx1 = _setup_valid_run_through_executor(tmp_path / "d1")
    exec_def = StageOrder.get_executor()
    planner_json1 = run1.run_dir / "02_planner.json"
    planner_json1.unlink()

    done_d1, _ = is_stage_completed(run1, exec_def, rm1, context=ctx1)
    assert done_d1 is False, "Executor cannot be completed if prerequisite Planner artifact was deleted!"

    # D2: Replace upstream artifact with malformed non-JSON
    rm2, run2, ctx2 = _setup_valid_run_through_executor(tmp_path / "d2")
    planner_json2 = run2.run_dir / "02_planner.json"
    planner_json2.write_text("MALFORMED { CORRUPT JSON", encoding="utf-8")

    done_d2, _ = is_stage_completed(run2, exec_def, rm2, context=ctx2)
    assert done_d2 is False, "Executor cannot be completed if upstream artifact is malformed!"

    # D3: Replace upstream artifact with an artifact from another run
    rm3, run3, ctx3 = _setup_valid_run_through_executor(tmp_path / "d3")
    planner_json3 = run3.run_dir / "02_planner.json"
    foreign_data = json.loads(planner_json3.read_text(encoding="utf-8"))
    foreign_data["run_id"] = "run-999-foreign"
    planner_json3.write_text(json.dumps(foreign_data, indent=2), encoding="utf-8")

    done_d3, _ = is_stage_completed(run3, exec_def, rm3, context=ctx3)
    assert done_d3 is False, "Executor cannot be completed if upstream artifact belongs to a foreign run!"


def test_adversarial_case_e_downstream_artifact_tampering(tmp_path):
    """Case E: Modify cached downstream artifact without changing its modification time.
    Verify corruption or tampering is detected before the artifact is accepted.
    """
    rm, run, ctx = _setup_valid_run_through_executor(tmp_path)
    exec_def = StageOrder.get_executor()
    candidate_json = run.run_dir / "03_executor.json"
    orig_mtime = candidate_json.stat().st_mtime
    orig_atime = candidate_json.stat().st_atime

    # Tamper with the executor deliverable (e.g. inject rogue code touches or modify fields)
    c_data = json.loads(candidate_json.read_text(encoding="utf-8"))
    c_data["tampered_payload"] = "malicious_modification"
    candidate_json.write_text(json.dumps(c_data, indent=2), encoding="utf-8")
    os.utime(candidate_json, (orig_atime, orig_mtime))

    done_e, _ = is_stage_completed(run, exec_def, rm, context=ctx)
    assert done_e is False, "Tampered downstream artifact must be detected and rejected!"


def test_adversarial_case_f_unchanged_inputs_valid_reuse(tmp_path):
    """Case F: Resume valid run with unchanged upstream inputs and intact artifacts.
    Confirm that legitimately reusable stages can still be skipped when all validation succeeds.
    """
    rm, run, ctx = _setup_valid_run_through_executor(tmp_path)

    # Architect (Stage 1)
    arch_def = StageOrder.get_architect()
    arch_done, arch_status = is_stage_completed(run, arch_def, rm, context=ctx)
    assert arch_done is True
    assert arch_status == "APPROVED"

    # Planner (Stage 2)
    plan_def = StageOrder.get_planner()
    plan_done, plan_status = is_stage_completed(run, plan_def, rm, context=ctx)
    assert plan_done is True
    assert plan_status == "READY"

    # Executor (Stage 3)
    exec_def = StageOrder.get_executor()
    exec_done, exec_status = is_stage_completed(run, exec_def, rm, context=ctx)
    assert exec_done is True
    assert exec_status == "SUCCESS"

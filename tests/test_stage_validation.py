"""Tests for Stage pre-flight execution environment validation (Phase 1)."""

import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.stages.stage import Stage, StageValidationError
from forge.storage.run_manager import RunManager


class DummyAdapter(BaseAdapter):
    """Test adapter tracking whether execute was invoked."""

    def __init__(self, auto_approve: bool = False):
        super().__init__(name="dummy", auto_approve=auto_approve)
        self.execute_called = False

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        self.execute_called = True
        return AdapterResponse(
            stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\n```",
        )


def _create_test_context(tmp_path: Path, stage_auto_approve: Optional[bool] = None) -> Context:
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Implement feature")
    config = Config.default()
    if stage_auto_approve is not None:
        config.stages["executor"].auto_approve = stage_auto_approve
        config.stages["executor"]._explicit_fields.add("auto_approve")
    return Context(
        run=run,
        project_root=tmp_path,
        config=config,
        git=GitService(tmp_path),
    )


def test_stage_headless_auto_approve_false_raises_immediately(tmp_path):
    """Verify that running headless (non-TTY) with auto_approve=False raises StageValidationError in < 10ms."""
    context = _create_test_context(tmp_path, stage_auto_approve=False)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="You are executor.",
        protocol_content="Emit machine report.",
    )
    adapter = DummyAdapter(auto_approve=False)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with patch.object(sys.stdin, "isatty", return_value=False):
        start = time.perf_counter()
        with pytest.raises(StageValidationError) as exc_info:
            stage.run(context)
        elapsed_ms = (time.perf_counter() - start) * 1000

    # Must fail fast (acceptance criterion: < 10ms)
    assert elapsed_ms < 10.0, f"Validation took {elapsed_ms:.2f}ms, expected < 10ms"

    # Must be an instance of RuntimeError as well for backward compatibility
    assert isinstance(exc_info.value, RuntimeError)

    # Diagnostic message must identify role and state remediation
    err_msg = str(exc_info.value)
    assert "executor" in err_msg
    assert "auto_approve=false" in err_msg
    assert "auto_approve: true" in err_msg

    # Ensure zero child processes / no adapter execution occurred
    assert adapter.execute_called is False


def test_stage_headless_auto_approve_true_proceeds(tmp_path):
    """Verify that running headless with auto_approve=True passes validation and executes."""
    context = _create_test_context(tmp_path, stage_auto_approve=True)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="You are executor.",
        protocol_content="Emit machine report.",
    )
    adapter = DummyAdapter(auto_approve=True)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with patch.object(sys.stdin, "isatty", return_value=False):
        result = stage.run(context)

    assert result.status == "SUCCESS"
    assert adapter.execute_called is True


def test_stage_interactive_auto_approve_false_proceeds(tmp_path):
    """Verify that running interactively (TTY present) with auto_approve=False passes validation."""
    context = _create_test_context(tmp_path, stage_auto_approve=False)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="You are executor.",
        protocol_content="Emit machine report.",
    )
    adapter = DummyAdapter(auto_approve=False)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with patch.object(sys.stdin, "isatty", return_value=True):
        result = stage.run(context)

    assert result.status == "SUCCESS"
    assert adapter.execute_called is True


def test_stage_validation_config_precedence(tmp_path):
    """Verify that stage config auto_approve=True overrides adapter default False."""
    context = _create_test_context(tmp_path, stage_auto_approve=True)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="You are executor.",
        protocol_content="Emit machine report.",
    )
    # Adapter default is False, but config overrides it to True
    adapter = DummyAdapter(auto_approve=False)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with patch.object(sys.stdin, "isatty", return_value=False):
        result = stage.run(context)

    assert result.status == "SUCCESS"
    assert adapter.execute_called is True


def test_stage_validation_latency_benchmark(tmp_path):
    """Verify that validation check latency averages <= 5ms across 100 evaluations."""
    context = _create_test_context(tmp_path, stage_auto_approve=False)
    role = Role(
        name="executor",
        sequence_number=3,
        template_content="You are executor.",
        protocol_content="Emit machine report.",
    )
    adapter = DummyAdapter(auto_approve=False)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with patch.object(sys.stdin, "isatty", return_value=False):
        start = time.perf_counter()
        iterations = 100
        for _ in range(iterations):
            try:
                stage._validate_execution_environment(context)
            except StageValidationError:
                pass
        total_ms = (time.perf_counter() - start) * 1000
        avg_ms = total_ms / iterations

    assert avg_ms <= 5.0, f"Average latency was {avg_ms:.3f}ms, expected <= 5ms"

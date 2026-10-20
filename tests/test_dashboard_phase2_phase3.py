"""Tests for Dashboard Phase 2 (Live Attach Mode) and Phase 3 (Operational Views)."""

import json
import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from forge.cli import main
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.knowledge import KnowledgeFact, FactStatus, FactSource
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.compare_view import render_compare_view
from forge.dashboard.components.console_view import render_console_view
from forge.dashboard.components.pkb_view import render_pkb_view
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.model import RunModel, StageModel, StageAttemptModel
from forge.dashboard.state import DashboardState
from forge.storage.knowledge import KnowledgeStore
from forge.storage.run_manager import RunManager


@pytest.fixture
def temp_run_dir(tmp_path: Path) -> Path:
    """Create a temporary run directory structure."""
    runs_dir = tmp_path / ".forge" / "runs" / "run-201"
    runs_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": "run-201",
        "task": "Build robust authentication service",
        "created_at": "2026-09-27T12:00:00Z",
        "status": "RUNNING",
    }
    with open(runs_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return runs_dir


# ==============================================================================
# Phase 2: Live Event Processing & RunModel.apply_event
# ==============================================================================

def test_apply_event_lifecycle_stage_start_and_finish(temp_run_dir: Path):
    """Verify stage_start and stage_finish lifecycle events update RunModel."""
    model = RunModel.from_dir(temp_run_dir)
    assert not model.is_active

    # 1. stage_start
    start_ev = AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={
            "lifecycle": "stage_start",
            "stage_name": "architect",
            "role_name": "architect",
            "sequence_number": 1,
            "banner_prefix": "[1/5]",
        },
    )
    model.apply_event(start_ev)
    assert model.is_active
    assert model.active_stage_name in ("architect", "01_architect")
    stage = model.get_stage("architect")
    assert stage is not None
    assert stage.status == "RUNNING"
    assert any("Stage execution started" in line for line in model.console_logs)

    # 2. stage_finish
    finish_ev = AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={
            "lifecycle": "stage_finish",
            "stage_name": "architect",
            "status": "APPROVED",
            "duration_seconds": 12.5,
        },
    )
    model.apply_event(finish_ev)
    assert stage.status == "APPROVED"
    assert stage.duration_seconds == 12.5
    assert any("Finished with status: APPROVED" in line for line in model.console_logs)


def test_apply_event_streaming_chunk_and_tools(temp_run_dir: Path):
    """Verify streaming CHUNK, TOOL_START, and TOOL_FINISH events update logs and stage content."""
    model = RunModel.from_dir(temp_run_dir)

    # Start stage
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "executor", "sequence_number": 3},
    ))

    # Send chunks
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="# Architecture Plan\nImplementing auth service tokens.",
        data={"stage_name": "executor"},
    ))
    stage = model.get_stage("executor")
    assert "Implementing auth service tokens" in stage.raw_content
    assert "Implementing auth service tokens" in stage.human_report
    assert any("Architecture Plan" in line for line in model.console_logs)

    # Send tool start
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="edit_file",
        data={"stage_name": "executor", "tool": "edit_file", "input": {"file": "auth.py"}},
    ))
    assert len(stage.metadata["tool_calls"]) == 1
    assert stage.metadata["tool_calls"][0]["tool"] == "edit_file"
    assert any("TOOL_START: edit_file" in line for line in model.console_logs)

    # Send tool finish
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="edit_file",
        data={"stage_name": "executor", "tool": "edit_file"},
    ))
    assert any("TOOL_FINISH: edit_file" in line for line in model.console_logs)


def test_apply_event_standalone_tool_finish_displays_arguments(temp_run_dir: Path):
    """Verify standalone TOOL_FINISH without prior TOOL_START formats and displays arguments."""
    model = RunModel.from_dir(temp_run_dir)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="shell",
        data={
            "stage_name": "architect",
            "tool": "shell",
            "call_id": "call-sh-1",
            "input": {"command": "git status"},
            "status": "completed",
        },
    ))
    assert any("TOOL_FINISH: shell ({'command': 'git status'})" in line for line in model.console_logs)


def test_apply_event_paired_tool_start_and_finish_does_not_duplicate_arguments(temp_run_dir: Path):
    """Verify paired TOOL_START and TOOL_FINISH displays arguments on start and avoids duplication on finish."""
    model = RunModel.from_dir(temp_run_dir)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="shell",
        data={"stage_name": "architect", "tool": "shell", "call_id": "call-sh-2", "input": {"command": "ls -la"}},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="shell",
        data={"stage_name": "architect", "tool": "shell", "call_id": "call-sh-2", "input": {"command": "ls -la"}, "status": "completed"},
    ))
    assert any("⚡ TOOL_START: shell ({'command': 'ls -la'})" in line for line in model.console_logs)
    assert any(line.endswith("✔ TOOL_FINISH: shell") for line in model.console_logs)
    assert not any("✔ TOOL_FINISH: shell (" in line for line in model.console_logs)


def test_apply_event_tool_error_formatting_and_red_styling(temp_run_dir: Path):
    """Verify tool failure renders failure label and console_view styles line in bold red."""
    from forge.dashboard.state import DashboardState
    from forge.dashboard.components.console_view import render_console_view

    model = RunModel.from_dir(temp_run_dir)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="run_command",
        data={
            "stage_name": "executor",
            "tool": "run_command",
            "call_id": "call-err-1",
            "input": {"CommandLine": "cat missing.txt"},
            "status": "error",
            "error": True,
        },
    ))
    assert any("✗ TOOL_ERROR: run_command ({'CommandLine': 'cat missing.txt'})" in line for line in model.console_logs)

    state = DashboardState(run=model, focused_pane="content", active_tab=1)
    panel = render_console_view(state)
    assert panel is not None
    rendered_text = panel.renderable
    # Verify styled text contains the red error line
    found_error_span = False
    for span in rendered_text.spans:
        if span.style == "bold red":
            found_error_span = True
            break
    assert found_error_span, "Expected bold red style span for TOOL_ERROR in console_view"


def test_apply_event_tool_calls_metadata_updated_on_finish_with_call_id(temp_run_dir: Path):
    """Verify stage metadata tool_calls record updates from running to completed on unambiguous call_id."""
    model = RunModel.from_dir(temp_run_dir)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="edit_file",
        data={"stage_name": "executor", "tool": "edit_file", "call_id": "c1", "input": {"file": "auth.py"}},
    ))
    stage = model.get_stage("executor")
    assert len(stage.metadata["tool_calls"]) == 1
    assert stage.metadata["tool_calls"][0]["status"] == "running"

    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="edit_file",
        data={"stage_name": "executor", "tool": "edit_file", "call_id": "c1", "output": "file updated", "status": "completed"},
    ))
    assert len(stage.metadata["tool_calls"]) == 1
    assert stage.metadata["tool_calls"][0]["status"] == "completed"
    assert stage.metadata["tool_calls"][0]["output"] == "file updated"


def test_apply_event_standalone_tool_finish_empty_input_clean_label(temp_run_dir: Path):
    """Verify standalone TOOL_FINISH with empty input does not append empty parentheses."""
    model = RunModel.from_dir(temp_run_dir)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="read",
        data={"stage_name": "architect", "tool": "read", "input": {}, "status": "completed"},
    ))
    assert any(line.endswith("✔ TOOL_FINISH: read") for line in model.console_logs)
    assert not any("✔ TOOL_FINISH: read (" in line for line in model.console_logs)


def test_stage_boundary_clears_stale_tool_tracking_on_abnormal_termination_and_retry(temp_run_dir: Path):
    """Verify tool tracking state does not leak across abnormal stage termination and retries."""
    model = RunModel.from_dir(temp_run_dir)
    arch_stage = model.get_stage("architect")
    arch_key = arch_stage.stage_name if arch_stage else "architect"

    # Attempt 1 of architect stage starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="",
        data={"lifecycle": "stage_start", "stage_name": "architect", "sequence_number": 1},
    ))
    # In-flight tool call starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        text="read",
        data={"stage_name": "architect", "tool": "read", "call_id": "c_stale_1", "input": {"file": "stale.txt"}},
    ))
    assert (arch_key, "c_stale_1") in model._started_tool_calls

    # Attempt 1 terminates abnormally without TOOL_FINISH
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="",
        data={"lifecycle": "stage_finish", "stage_name": "architect", "status": "FAILED", "duration_seconds": 1.0},
    ))
    # Stale tool tracking for architect should be purged on stage_finish
    assert (arch_key, "c_stale_1") not in model._started_tool_calls

    # Attempt 2 of architect stage starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="",
        data={"lifecycle": "stage_start", "stage_name": "architect", "sequence_number": 1},
    ))

    # Attempt 2 emits standalone TOOL_FINISH with same call_id
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        text="read",
        data={"stage_name": "architect", "tool": "read", "call_id": "c_stale_1", "input": {"file": "fresh.txt"}, "status": "completed"},
    ))
    # Verify Attempt 2 renders tool arguments rather than suppressing them
    assert any("✔ TOOL_FINISH: read ({'file': 'fresh.txt'})" in line for line in model.console_logs)


def test_stage_boundary_clearing_preserves_other_stage_tracking(temp_run_dir: Path):
    """Verify clearing tracking for one stage does not erase in-flight tracking for another stage."""
    model = RunModel.from_dir(temp_run_dir)
    arch_stage = model.get_stage("architect")
    arch_key = arch_stage.stage_name if arch_stage else "architect"
    plan_stage = model.get_stage("planner")
    plan_key = plan_stage.stage_name if plan_stage else "planner"

    # Start architect and initiate a tool call
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "architect", "sequence_number": 1},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        data={"stage_name": "architect", "tool": "shell", "call_id": "c_arch", "input": {"cmd": "ls"}},
    ))

    # Interleaved planner stage initiates a tool call
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        data={"stage_name": "planner", "tool": "shell", "call_id": "c_plan", "input": {"cmd": "pwd"}},
    ))
    assert (arch_key, "c_arch") in model._started_tool_calls
    assert (plan_key, "c_plan") in model._started_tool_calls

    # Finish architect stage
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        data={"lifecycle": "stage_finish", "stage_name": "architect", "status": "APPROVED", "duration_seconds": 2.0},
    ))

    # architect tool tracking is cleared, but planner tool tracking is strictly preserved
    assert (arch_key, "c_arch") not in model._started_tool_calls
    assert (plan_key, "c_plan") in model._started_tool_calls

    # planner tool finish suppresses arguments because it was properly paired with its start
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        data={"stage_name": "planner", "tool": "shell", "call_id": "c_plan", "status": "completed"},
    ))
    assert any(line.endswith("✔ TOOL_FINISH: shell") for line in model.console_logs)


def test_stage_boundary_missing_ids_cleaned_up_on_retry(temp_run_dir: Path):
    """Verify tool calls with empty/missing call_id are purged on boundary and behave correctly on retry."""
    model = RunModel.from_dir(temp_run_dir)
    exec_stage = model.get_stage("executor")
    exec_key = exec_stage.stage_name if exec_stage else "executor"

    # Attempt 1 starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "executor", "sequence_number": 2},
    ))
    # Tool start with missing call_id
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_START,
        timestamp=time.time(),
        data={"stage_name": "executor", "tool": "edit_file", "call_id": "", "input": {"file": "old.py"}},
    ))
    assert model._open_unidentified_tools.get((exec_key, "edit_file"), 0) == 1

    # Stage encounters engine ERROR
    model.apply_event(AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Process killed by SIGKILL",
        data={"stage_name": "executor"},
    ))
    # Unidentified tool tracking for executor must be purged
    assert model._open_unidentified_tools.get((exec_key, "edit_file"), 0) == 0

    # Attempt 2 starts
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "executor", "sequence_number": 2},
    ))
    # Standalone finish with missing call_id in attempt 2
    model.apply_event(AgentEvent(
        event_type=AgentEventType.TOOL_FINISH,
        timestamp=time.time(),
        data={"stage_name": "executor", "tool": "edit_file", "call_id": "", "input": {"file": "new.py"}, "status": "completed"},
    ))
    # Must display arguments rather than assuming it was paired with Attempt 1's dead start
    assert any("✔ TOOL_FINISH: edit_file ({'file': 'new.py'})" in line for line in model.console_logs)


def test_console_view_styling_narrow_error_matching(temp_run_dir: Path):
    """Verify console styling restricts bold red to TOOL_ERROR and stage/run ERROR, preserving plain narration."""
    model = RunModel.from_dir(temp_run_dir)

    model.console_logs = [
        "  ✗ TOOL_ERROR: shell",
        "✗ [Executor] ERROR: command timed out",
        "  ✗ Unit test assertion failure in test_auth.py",
        "  ✔ TOOL_FINISH: shell",
        "▶ [architect] Stage execution started",
        "✓ [architect] Finished with status: APPROVED (1.0s)",
        "  ⚡ TOOL_START: run_command",
    ]

    state = DashboardState(run=model, focused_pane="content", active_tab=1)
    panel = render_console_view(state)
    assert panel is not None
    rendered_text = panel.renderable

    # Map each line in plain text to its applied style (if any)
    line_styles = {}
    for line in model.console_logs:
        line_styles[line] = None

    for span in rendered_text.spans:
        span_slice = rendered_text.plain[span.start:span.end].strip()
        for log_line in model.console_logs:
            if span_slice == log_line.strip():
                line_styles[log_line] = span.style

    # 1. TOOL_ERROR is bold red
    assert line_styles["  ✗ TOOL_ERROR: shell"] == "bold red"
    # 2. ✗ [Executor] ERROR: ... is bold red
    assert line_styles["✗ [Executor] ERROR: command timed out"] == "bold red"
    # 3. Indented agent narration starting with ✗ is NOT bold red (default None)
    assert line_styles["  ✗ Unit test assertion failure in test_auth.py"] is None
    # 4. Normal TOOL_FINISH is green
    assert line_styles["  ✔ TOOL_FINISH: shell"] == "green"
    # 5. Other markers styled correctly
    assert line_styles["▶ [architect] Stage execution started"] == "bold cyan"
    assert line_styles["✓ [architect] Finished with status: APPROVED (1.0s)"] == "bold green"
    assert line_styles["  ⚡ TOOL_START: run_command"] == "bold yellow"


def test_apply_event_complete_and_error(temp_run_dir: Path):
    """Verify COMPLETE and ERROR stream events parse reports and update status."""
    model = RunModel.from_dir(temp_run_dir)

    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "planner", "sequence_number": 2},
    ))

    raw_output = (
        "# Execution Plan\nAll steps outlined.\n\n"
        "## Machine Report\n```yaml\nstatus: APPROVED\nconfidence: 0.95\n```\n"
    )
    res = ExecutionResult(exit_code=0, duration_seconds=5.0, stdout=raw_output, stderr="")
    model.apply_event(AgentEvent(
        event_type=AgentEventType.COMPLETE,
        timestamp=time.time(),
        result=res,
        data={"stage_name": "planner"},
    ))
    stage = model.get_stage("planner")
    assert stage.status == "APPROVED"
    assert stage.exit_code == 0
    assert stage.human_report == "# Execution Plan\nAll steps outlined."
    assert stage.machine_report is not None
    assert stage.machine_report.status == "APPROVED"
    assert str(stage.machine_report.confidence) == "0.95"

    # Test error event
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "reviewer", "sequence_number": 5},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.ERROR,
        timestamp=time.time(),
        text="Subprocess timed out after 120s",
        data={"stage_name": "reviewer"},
    ))
    rev_stage = model.get_stage("reviewer")
    assert rev_stage.status == "FAILED"
    assert rev_stage.has_error
    assert any("ERROR: Subprocess timed out" in line for line in model.console_logs)


def test_repair_loop_attempt_archiving(temp_run_dir: Path):
    """Verify repeated execution of the same stage archives prior attempts into StageAttemptModel."""
    model = RunModel.from_dir(temp_run_dir)

    # Attempt 1: Executor runs and finishes with CHANGES_REQUIRED
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "executor", "sequence_number": 3},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="First attempt implementation with bugs.",
        data={"stage_name": "executor"},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_finish", "stage_name": "executor", "status": "CHANGES_REQUIRED", "duration_seconds": 15.0},
    ))

    stage = model.get_stage("executor")
    assert stage.status == "CHANGES_REQUIRED"
    assert len(stage.attempts) == 0

    # Attempt 2: Executor starts again (repair loop)
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "executor", "sequence_number": 3, "banner_prefix": "[3/5] (Attempt 2/3)"},
    ))
    # Previous attempt should now be archived in stage.attempts
    assert len(stage.attempts) == 1
    assert stage.attempts[0].attempt_number == 1
    assert stage.attempts[0].status == "CHANGES_REQUIRED"
    assert stage.attempts[0].duration_seconds == 15.0
    assert "First attempt implementation" in stage.attempts[0].raw_content
    # Current stage fields reset for new attempt
    assert stage.status == "RUNNING"
    assert stage.duration_seconds == 0.0

    # Attempt 2 completes with APPROVED
    model.apply_event(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Second attempt implementation fixed.",
        data={"stage_name": "executor"},
    ))
    model.apply_event(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_finish", "stage_name": "executor", "status": "APPROVED", "duration_seconds": 18.0},
    ))
    assert stage.status == "APPROVED"
    assert len(stage.attempts) == 1


# ==============================================================================
# Phase 2: Live Queue, Cooperative Cancellation, & Failure Isolation
# ==============================================================================

def test_dashboard_app_live_queue_and_cooperative_exit(temp_run_dir: Path):
    """Verify DashboardApp consumes from live_queue and exits cleanly when stop_event is set."""
    live_q = queue.Queue()
    stop_ev = threading.Event()
    app = DashboardApp(temp_run_dir, live_queue=live_q, stop_event=stop_ev)

    # Initial tab should be 1 (Console) in live attach mode
    assert app.state.active_tab == 1

    # Put events into queue
    live_q.put(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_start", "stage_name": "architect", "sequence_number": 1},
    ))
    live_q.put(AgentEvent(
        event_type=AgentEventType.CHUNK,
        timestamp=time.time(),
        text="Processing architecture...",
        data={"stage_name": "architect"},
    ))
    live_q.put(AgentEvent(
        event_type=AgentEventType.HEARTBEAT,
        timestamp=time.time(),
        data={"lifecycle": "stage_finish", "stage_name": "architect", "status": "APPROVED", "duration_seconds": 4.2},
    ))

    # Signal stop
    stop_ev.set()

    # run() in non-TTY mode drains the queue until stop_ev is set and renders
    exit_code = app.run()
    assert exit_code == 0
    arch_stage = app.run_model.get_stage("architect")
    assert arch_stage is not None
    assert arch_stage.status == "APPROVED"
    assert arch_stage.duration_seconds == 4.2


def test_dashboard_app_detach_vs_abort_keys(temp_run_dir: Path):
    """Verify 'q' detaches without setting abort_event, while 'ctrl+c' sets abort_event."""
    live_q = queue.Queue()
    stop_ev = threading.Event()
    abort_ev = threading.Event()
    app = DashboardApp(temp_run_dir, live_queue=live_q, stop_event=stop_ev, abort_event=abort_ev)

    # User presses 'q' to detach
    app.handle_key("q")
    assert app.state.should_exit is True
    assert not abort_ev.is_set()  # Pipeline execution MUST NOT be cancelled
    assert not stop_ev.is_set()

    # Reset should_exit
    app.state.should_exit = False

    # User presses Ctrl+C to cooperatively cancel
    app.handle_key("\x03")
    assert app.state.should_exit is True
    assert abort_ev.is_set()  # Pipeline execution cancellation requested


def test_failure_isolation_listener_exception(temp_run_dir: Path):
    """Verify that an exception in event_listener never crashes Stage execution."""
    from forge.stages.stage import Stage
    from forge.core.role import Role

    class CrashingListener:
        def __call__(self, event):
            raise RuntimeError("Dashboard UI thread disconnected or crashed")

    role = Role(name="architect", sequence_number=1, template_content="Test prompt")
    adapter = Mock()
    adapter.name = "mock_adapter"
    adapter.capabilities.return_value = role.required_capabilities
    adapter.requires_headless_approval = False
    adapter.max_prompt_bytes = None
    adapter.iter_events.return_value = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Sample text"),
        AgentEvent(event_type=AgentEventType.COMPLETE, timestamp=time.time(), result=ExecutionResult(0, 1.0, "Done")),
    ]

    run_mgr = RunManager(temp_run_dir.parent.parent)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=30, event_listener=CrashingListener())

    # Running stage must not raise RuntimeError even though event_listener crashes
    mock_config = Mock()
    mock_config.get_stage_config.return_value = None
    mock_config.defaults.idle_timeout = None
    real_run = run_mgr.create_run(task="Test task")
    mock_git = Mock()
    mock_git.is_git_repo.return_value = False
    ctx = Context(run=real_run, project_root=temp_run_dir.parent.parent, config=mock_config, git=mock_git)
    res = stage.run(ctx)
    assert res is not None
    assert res.duration_seconds >= 0


# ==============================================================================
# Phase 3: Operational Views (Console, Tester, PKB, Compare)
# ==============================================================================

def test_console_view_rendering(temp_run_dir: Path):
    """Verify render_console_view renders live logs and handles empty state."""
    model = RunModel.from_dir(temp_run_dir)
    state = DashboardState(run=model, active_tab=1)

    # Empty state
    panel_empty = render_console_view(state)
    assert panel_empty is not None

    # Add logs
    model.console_logs.append("▶ [architect] Stage execution started")
    model.console_logs.append("  ⚡ TOOL_START: edit_file")
    model.console_logs.append("  ✔ TOOL_FINISH: edit_file")
    model.console_logs.append("✓ [architect] Finished with status: APPROVED (10.0s)")

    panel_with_logs = render_console_view(state)
    assert panel_with_logs is not None


def test_tester_view_structured_matrix_and_failures(temp_run_dir: Path):
    """Verify render_tester_view extracts test summary, journeys, and failures."""
    model = RunModel.from_dir(temp_run_dir)
    tester_stage = StageModel(
        stage_name="tester",
        role_name="tester",
        sequence_number=4,
        status="CHANGES_REQUIRED",
        duration_seconds=8.5,
        raw_content="### Human Report\n2 tests passed, 1 test failed.\n\n=== FAILURES ===\nAssertionError: expected 200 got 500",
        human_report="2 tests passed, 1 test failed.\n\n=== FAILURES ===\nAssertionError: expected 200 got 500",
        metadata={
            "test_results": {
                "total": 3,
                "passed": 2,
                "failed": 1,
                "tests": [
                    {"name": "test_auth_login", "status": "PASS", "duration": 0.12},
                    {"name": "test_auth_token_refresh", "status": "PASS", "duration": 0.08},
                    {"name": "test_auth_expired_session", "status": "FAIL", "duration": 0.45},
                ],
            }
        },
    )
    model.stages.append(tester_stage)
    state = DashboardState(run=model, active_tab=3)

    panel = render_tester_view(state)
    assert panel is not None


def test_pkb_view_rendering_and_filters(temp_run_dir: Path):
    """Verify render_pkb_view displays facts and supports type and status filtering."""
    root = temp_run_dir.parent.parent
    store = KnowledgeStore(root)

    # Seed facts
    fact1 = KnowledgeFact(
        id="arch-token-svc",
        title="JWT Token Service Architecture",
        type="architecture",
        status=FactStatus.VERIFIED.value,
        summary="Stateless HMAC JWT with 15min expiry",
        provenance={"source": FactSource.INFERRED.value, "confidence": 0.98},
    )
    fact2 = KnowledgeFact(
        id="feat-rbac",
        title="Role-Based Access Control",
        type="feature",
        status=FactStatus.HUMAN_LOCKED.value,
        summary="Admin and developer roles only",
        provenance={"source": FactSource.HUMAN.value, "confidence": 1.0},
    )
    fact3 = KnowledgeFact(
        id="dec-refresh-db",
        title="Refresh Token Database Storage",
        type="decision",
        status=FactStatus.DISPUTED.value,
        summary="Dispute regarding redis vs sqlite",
        provenance={"source": FactSource.OBSERVED.value, "confidence": 0.6},
    )
    store.save_fact(fact1)
    store.save_fact(fact2)
    store.save_fact(fact3)

    model = RunModel.from_dir(temp_run_dir)
    state = DashboardState(run=model, active_tab=4)

    # Render all
    panel_all = render_pkb_view(state, project_root=root)
    assert panel_all is not None

    # Filter by type architecture
    state.pkb_type_filter = "architecture"
    panel_arch = render_pkb_view(state, project_root=root)
    assert panel_arch is not None

    # Filter by status HUMAN_LOCKED
    state.pkb_type_filter = None
    state.pkb_status_filter = "HUMAN_LOCKED"
    panel_locked = render_pkb_view(state, project_root=root)
    assert panel_locked is not None


def test_compare_view_side_by_side(temp_run_dir: Path):
    """Verify render_compare_view computes duration delta and status comparison."""
    # Create second run to compare against
    run_b_dir = temp_run_dir.parent / "run-200"
    run_b_dir.mkdir(parents=True, exist_ok=True)
    with open(run_b_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump({
            "run_id": "run-200",
            "task": "Prior implementation attempt",
            "created_at": "2026-09-27T10:00:00Z",
            "status": "APPROVED",
        }, f)

    model_a = RunModel.from_dir(temp_run_dir)
    model_a.stages.append(StageModel("architect", "architect", 1, "APPROVED", duration_seconds=10.0))
    model_a.stages.append(StageModel("executor", "executor", 3, "APPROVED", duration_seconds=25.0))
    model_a.total_duration_seconds = 35.0

    model_b = RunModel.from_dir(run_b_dir)
    model_b.stages.append(StageModel("architect", "architect", 1, "APPROVED", duration_seconds=12.0))
    model_b.stages.append(StageModel("executor", "executor", 3, "APPROVED", duration_seconds=40.0))
    model_b.total_duration_seconds = 52.0

    state = DashboardState(run=model_a, active_tab=5)
    state.compare_run_model = model_b
    state.compare_run_id = model_b.run_id

    panel = render_compare_view(state)
    assert panel is not None


def test_tab_navigation_keys(temp_run_dir: Path):
    """Verify numeric keys 1..4 switch between operational views in DashboardApp (5 aliases to 4)."""
    # Test in live mode (where 1..4 switch tabs directly)
    app = DashboardApp(temp_run_dir, live_queue=queue.Queue())
    assert app.state.active_tab == 1

    app.handle_key("2")
    assert app.state.active_tab == 2  # Artifact

    app.handle_key("3")
    assert app.state.active_tab == 3  # PKB

    # Test PKB filter cycling with [ and ]
    app.handle_key("]")
    assert app.state.pkb_type_filter == "architecture"
    app.handle_key("]")
    assert app.state.pkb_type_filter == "feature"
    app.handle_key("[")
    assert app.state.pkb_type_filter == "architecture"

    app.handle_key("4")
    assert app.state.active_tab == 4  # Compare

    app.handle_key("5")
    assert app.state.active_tab == 4  # Compare (5 aliases to 4)

    app.handle_key("1")
    assert app.state.active_tab == 1  # Console


def test_tester_results_discoverable_under_artifacts_tab(temp_run_dir: Path):
    """Verify test matrix, summary, and journeys are rendered under Artifacts tab when Tests subtab is selected."""
    from forge.dashboard.components.artifact_view import render_artifact_view
    from rich.console import Console

    run = RunModel(
        run_id="run-tests-artifact",
        task="Test artifact unification",
        status="APPROVED",
        created_at="",
        run_dir=temp_run_dir,
        stages=[
            StageModel(
                stage_name="04_tester",
                role_name="tester",
                sequence_number=4,
                status="PASSED",
                human_report="## Test Report\nAll 5 journeys passed cleanly.",
                raw_content="",
                machine_report=None,
                metadata={
                    "test_results": {
                        "total": 5,
                        "passed": 5,
                        "failed": 0,
                        "tests": [
                            {"name": "test_auth_flow", "status": "PASS", "duration": 0.12},
                            {"name": "test_token_refresh", "status": "PASS", "duration": 0.08},
                        ],
                    }
                },
                exit_code=0,
                duration_seconds=1.5,
            )
        ],
    )
    state = DashboardState(run=run, active_tab=2, active_content_tab="tests")
    panel = render_artifact_view(state)
    assert panel is not None

    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()

    assert "TOTAL: 5" in output
    assert "PASSED: 5" in output
    assert "test_auth_flow" in output
    assert "test_token_refresh" in output


def test_tester_results_failure_inspector_under_artifacts(temp_run_dir: Path):
    """Verify failure inspector is rendered under Artifacts tab when test failures exist."""
    from forge.dashboard.components.artifact_view import render_artifact_view
    from rich.console import Console

    run = RunModel(
        run_id="run-failures-artifact",
        task="Test failure inspector",
        status="FAILED",
        created_at="",
        run_dir=temp_run_dir,
        stages=[
            StageModel(
                stage_name="04_tester",
                role_name="tester",
                sequence_number=4,
                status="FAILED",
                human_report="### Test Failures\nFAILURES\nAssertionError: expected 200 got 500 in test_login",
                raw_content="",
                machine_report=None,
                metadata={},
                exit_code=1,
                duration_seconds=2.0,
            )
        ],
    )
    state = DashboardState(run=run, active_tab=2, active_content_tab="tests")
    panel = render_artifact_view(state)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()

    assert "STATUS: FAILED" in output
    assert "Failure Inspector & Evidence:" in output
    assert "AssertionError: expected 200 got 500" in output


def test_tester_incomplete_or_pending_under_artifacts(temp_run_dir: Path):
    """Verify pending tester stage renders graceful informational message under Artifacts."""
    from forge.dashboard.components.artifact_view import render_artifact_view
    from rich.console import Console

    run = RunModel(
        run_id="run-pending-tester",
        task="Test pending tester",
        status="RUNNING",
        created_at="",
        run_dir=temp_run_dir,
        stages=[
            StageModel(
                stage_name="04_tester",
                role_name="tester",
                sequence_number=4,
                status="PENDING",
                human_report="",
                raw_content="",
                machine_report=None,
                metadata={},
                exit_code=None,
                duration_seconds=0.0,
            )
        ],
    )
    state = DashboardState(run=run, active_tab=2, active_content_tab="tests")
    panel = render_artifact_view(state)
    console = Console(record=True, width=100)
    console.print(panel)
    output = console.export_text()

    assert "No tester evidence available for this stage" in output


def test_shortcut_t_jumps_to_tester_stage_and_subtab(temp_run_dir: Path):
    """Verify pressing 't' jumps selection to tester stage and activates tests subtab under Artifacts."""
    app = DashboardApp(temp_run_dir, live_queue=queue.Queue())
    # Mock multiple stages in the run
    stage_spec = StageModel(stage_name="01_spec", role_name="spec", sequence_number=1, status="PASSED", human_report="", raw_content="", machine_report=None, metadata={}, exit_code=0, duration_seconds=1.0)
    stage_tester = StageModel(stage_name="04_tester", role_name="tester", sequence_number=4, status="PASSED", human_report="", raw_content="", machine_report=None, metadata={"test_results": {"total": 2, "passed": 2, "failed": 0}}, exit_code=0, duration_seconds=1.0)
    app.state.run.stages = [stage_spec, stage_tester]
    app.state.selected_stage_index = 0
    app.state.active_tab = 1

    app.handle_key("t")
    assert app.state.selected_stage_index == 1
    assert app.state.active_tab == 2
    assert app.state.active_content_tab == "tests"


def test_artifact_tab_indicator_shows_tests_subtab_only_when_test_evidence_present():
    """Verify tab navigation displays 'T: Tests' only when current stage contains test evidence."""
    from forge.dashboard.components.navigation import render_tab_navigation

    stage_spec = StageModel(stage_name="01_spec", role_name="spec", sequence_number=1, status="PASSED", human_report="", raw_content="", machine_report=None, metadata={}, exit_code=0, duration_seconds=1.0)
    stage_tester = StageModel(stage_name="04_tester", role_name="tester", sequence_number=4, status="PASSED", human_report="", raw_content="", machine_report=None, metadata={}, exit_code=0, duration_seconds=1.0)

    run = RunModel(run_id="run-test", task="Test", status="RUNNING", created_at="", run_dir=Path("/tmp"), stages=[stage_spec, stage_tester])
    state = DashboardState(run=run, active_tab=2)

    # 1. Spec selected -> only H: Human and M: Machine
    state.selected_stage_index = 0
    nav_spec = render_tab_navigation(state)
    assert "H: Human" in nav_spec
    assert "M: Machine" in nav_spec
    assert "T: Tests" not in nav_spec

    # 2. Tester selected -> includes T: Tests
    state.selected_stage_index = 1
    nav_tester = render_tab_navigation(state)
    assert "H: Human" in nav_tester
    assert "M: Machine" in nav_tester
    assert "T: Tests" in nav_tester


# ==============================================================================
# CLI Verification: forge auto --dashboard
# ==============================================================================

def test_cli_auto_dashboard_flag_recognized():
    """Verify that forge auto accepts the --dashboard / -d flag."""
    runner = CliRunner()
    res = runner.invoke(main, ["auto", "--help"])
    assert res.exit_code == 0
    assert "--dashboard" in res.output or "-d" in res.output

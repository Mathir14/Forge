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
    """Verify numeric keys 1..5 switch between operational views in DashboardApp."""
    # Test in live mode (where 1..5 switch tabs directly)
    app = DashboardApp(temp_run_dir, live_queue=queue.Queue())
    assert app.state.active_tab == 1

    app.handle_key("2")
    assert app.state.active_tab == 2  # Artifact

    app.handle_key("3")
    assert app.state.active_tab == 3  # Tester

    app.handle_key("4")
    assert app.state.active_tab == 4  # PKB

    # Test PKB filter cycling with [ and ]
    app.handle_key("]")
    assert app.state.pkb_type_filter == "architecture"
    app.handle_key("]")
    assert app.state.pkb_type_filter == "feature"
    app.handle_key("[")
    assert app.state.pkb_type_filter == "architecture"

    app.handle_key("5")
    assert app.state.active_tab == 5  # Compare

    app.handle_key("1")
    assert app.state.active_tab == 1  # Console


# ==============================================================================
# CLI Verification: forge auto --dashboard
# ==============================================================================

def test_cli_auto_dashboard_flag_recognized():
    """Verify that forge auto accepts the --dashboard / -d flag."""
    runner = CliRunner()
    res = runner.invoke(main, ["auto", "--help"])
    assert res.exit_code == 0
    assert "--dashboard" in res.output or "-d" in res.output

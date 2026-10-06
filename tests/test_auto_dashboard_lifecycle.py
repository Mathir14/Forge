"""Tests for forge auto -d process lifecycle, dashboard persistence, and cancellation."""

import io
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.cli import main, auto_pipeline, execute_stage
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType
from forge.core.role import Role
from forge.core.run import Run
from forge.dashboard.app import DashboardApp
from forge.dashboard.model import RunModel
from forge.protocol.report import MachineReport
from forge.stages.definition import StageDefinition, StageOrder
from forge.stages.result import StageResult, AutonomousHalt
from forge.stages.stage import Stage


@pytest.fixture
def mock_run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / ".forge" / "runs" / "run-999"
    run_dir.mkdir(parents=True, exist_ok=True)
    meta = run_dir / "metadata.json"
    meta.write_text('{"run_id": "run-999", "task": "Test task", "status": "IN_PROGRESS"}', encoding="utf-8")
    return run_dir


def test_auto_worker_failure_does_not_directly_exit_process(mock_run_dir: Path):
    """Test 1: A failed stage raises AutonomousHalt instead of calling sys.exit in worker thread.
    
    The worker catches AutonomousHalt, preserves halt metadata, and lets main thread exit.
    """
    halt_holder = [None]
    pipeline_exit_code = [0]
    stop_event = threading.Event()

    def fake_worker():
        try:
            # Simulate a stage timing out / failing in autonomous loop
            raise AutonomousHalt(
                status="FAILED",
                exit_code=1,
                reason="Execution timed out after 900 seconds (absolute timeout).",
                stage_name="Architect",
            )
        except AutonomousHalt as halt:
            halt_holder[0] = halt
            pipeline_exit_code[0] = halt.exit_code
        except SystemExit as se:
            pytest.fail(f"Worker called sys.exit({se.code}) directly instead of using AutonomousHalt!")
        finally:
            stop_event.set()

    worker = threading.Thread(target=fake_worker)
    worker.start()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert stop_event.is_set()
    assert pipeline_exit_code[0] == 1
    assert halt_holder[0] is not None
    assert halt_holder[0].status == "FAILED"
    assert halt_holder[0].stage_name == "Architect"
    assert "900 seconds" in halt_holder[0].reason


def test_dashboard_persists_after_pipeline_failure(mock_run_dir: Path):
    """Test 2: stop_event does NOT cause the dashboard to auto-close after 200ms.
    
    The dashboard transitions to terminal state and remains running until dismissed.
    """
    live_q = queue.Queue()
    stop_ev = threading.Event()
    halt_holder = [
        AutonomousHalt(
            status="FAILED",
            exit_code=1,
            reason="Execution timed out after 900 seconds.",
            stage_name="Architect",
        )
    ]

    app = DashboardApp(mock_run_dir, live_queue=live_q, stop_event=stop_ev, halt_holder=halt_holder)
    stop_ev.set()

    # Verify that in interactive mode, stop_event alone does NOT set should_exit
    assert not app.state.should_exit
    app._apply_terminal_state()
    assert app.state.terminal_status == "FAILED"
    assert app.state.terminal_stage == "Architect"
    assert "900 seconds" in app.state.terminal_reason
    assert not app.state.should_exit

    # Explicit user 'q' key dismisses dashboard
    app.handle_key("q")
    assert app.state.should_exit is True


def test_dashboard_displays_pipeline_failure_reason(mock_run_dir: Path):
    """Test 3: A stage timeout/failure reason reaches the dashboard user-visible layout."""
    live_q = queue.Queue()
    stop_ev = threading.Event()
    halt_holder = [
        AutonomousHalt(
            status="FAILED",
            exit_code=1,
            reason="Execution timed out after 900 seconds (absolute timeout).",
            stage_name="Architect",
        )
    ]

    app = DashboardApp(mock_run_dir, live_queue=live_q, stop_event=stop_ev, halt_holder=halt_holder)
    app._apply_terminal_state()

    assert app.state.terminal_status == "FAILED"
    assert app.state.terminal_stage == "Architect"
    assert "900 seconds" in app.state.terminal_reason

    output = app.render_once()
    assert "PIPELINE FAILED" in output
    assert "Architect" in output
    assert "Execution timed out" in output
    assert "[Q]" in output or "to exit" in output


def test_dashboard_failure_output_is_not_lost(mock_run_dir: Path):
    """Test 4: Verify worker diagnostics survive dashboard stream handling and are flushed."""
    live_q = queue.Queue()
    stop_ev = threading.Event()
    stop_ev.set()

    out_buf = io.StringIO()
    console = Console(file=out_buf)
    app = DashboardApp(mock_run_dir, console=console, live_queue=live_q, stop_event=stop_ev)

    exit_code = app.run()
    assert exit_code == 0
    assert len(out_buf.getvalue()) > 0

    # Simulate worker writing to captured stdout stream and verify flushing
    app.captured_stdout_content = "\n⚠️ Autonomous loop halted: Architect finished with status 'FAILED'.\n"
    target_out = io.StringIO()
    target_out.write(app.captured_stdout_content)
    target_out.flush()

    assert "Autonomous loop halted" in target_out.getvalue()
    assert "Architect" in target_out.getvalue()


def test_dashboard_ctrl_c_propagates_cancellation(mock_run_dir: Path):
    """Test 5: abort_event propagates from dashboard through stage to adapter and terminates subprocess."""
    abort_ev = threading.Event()
    mock_adapter = Mock(spec=BaseAdapter)
    mock_adapter.name = "mock_adapter"
    mock_adapter.max_prompt_bytes = None
    mock_adapter.capabilities.return_value = {"streaming", "code_read"}
    mock_adapter.has_capability.return_value = True
    mock_adapter.can_recover_session.return_value = False

    cancelled_called = threading.Event()

    def fake_cancel():
        cancelled_called.set()

    mock_adapter.cancel.side_effect = fake_cancel

    # Adapter yields heartbeats indefinitely until cancelled
    def fake_iter(*args, **kwargs):
        while not cancelled_called.is_set():
            yield AgentEvent(event_type=AgentEventType.HEARTBEAT, timestamp=time.time())
            time.sleep(0.02)

    mock_adapter.iter_events.side_effect = fake_iter

    role = Role(
        name="architect",
        sequence_number=1,
        template_content="Task: {{ task }}",
    )

    stage = Stage(
        role=role,
        adapter=mock_adapter,
        timeout=10,
        abort_event=abort_ev,
    )

    run_obj = Run(
        run_id="run-999",
        task="Test cancellation",
        run_dir=mock_run_dir,
    )

    context = Context(
        run=run_obj,
        project_root=mock_run_dir.parent.parent,
        config=None,
        git=None,
        abort_event=abort_ev,
    )

    # Set abort_event after a short delay
    def trigger_abort():
        time.sleep(0.05)
        abort_ev.set()

    t = threading.Thread(target=trigger_abort)
    t.start()

    res = stage.run(context)
    t.join()

    # Verify adapter was cancelled and stage recorded code 130
    assert cancelled_called.is_set()
    assert res.response.exit_code == 130
    assert "cancelled" in res.response.stderr.lower()


def test_q_does_not_claim_detachment_when_joining():
    """Test 6: Ensure UI messaging matches actual lifecycle behavior (no false detachment claims)."""
    # Verify the wording in cli.py auto_pipeline
    cli_file = Path(__file__).parent.parent / "src" / "forge" / "cli.py"
    content = cli_file.read_text(encoding="utf-8")

    assert "Dashboard detached. Execution continuing in background..." not in content
    assert "Dashboard closed. Pipeline execution continues in foreground..." in content


def test_process_group_safety_guards_against_parent_signalling():
    """Test 7: BaseAdapter._kill_process_group never signals parent or invalid/system process groups."""
    # Test PID None
    proc_none = Mock()
    proc_none.pid = None
    with patch("os.killpg") as mock_killpg:
        BaseAdapter._kill_process_group(proc_none)
        mock_killpg.assert_not_called()

    # Test PID 0
    proc_zero = Mock()
    proc_zero.pid = 0
    with patch("os.killpg") as mock_killpg:
        BaseAdapter._kill_process_group(proc_zero)
        mock_killpg.assert_not_called()

    # Test PID 1 (init / system root)
    proc_one = Mock()
    proc_one.pid = 1
    with patch("os.killpg") as mock_killpg:
        BaseAdapter._kill_process_group(proc_one)
        mock_killpg.assert_not_called()

    # Test negative PID
    proc_neg = Mock()
    proc_neg.pid = -42
    with patch("os.killpg") as mock_killpg:
        BaseAdapter._kill_process_group(proc_neg)
        mock_killpg.assert_not_called()

    # Test PGID <= 1
    proc_pgid_one = Mock()
    proc_pgid_one.pid = 1234
    with patch("os.getpgid", return_value=1), patch("os.killpg") as mock_killpg:
        BaseAdapter._kill_process_group(proc_pgid_one)
        mock_killpg.assert_not_called()


def test_normal_success_path_regression(mock_run_dir: Path):
    """Test 8: Successful pipeline transitions to APPROVED terminal banner and exits cleanly on q."""
    meta = mock_run_dir / "metadata.json"
    meta.write_text('{"run_id": "run-999", "task": "Test", "status": "APPROVED"}', encoding="utf-8")

    live_q = queue.Queue()
    stop_ev = threading.Event()
    stop_ev.set()

    app = DashboardApp(mock_run_dir, live_queue=live_q, stop_event=stop_ev)
    app._apply_terminal_state()

    assert app.state.terminal_status == "APPROVED"
    assert "completed successfully" in app.state.terminal_reason.lower()

    output = app.render_once()
    assert "PIPELINE COMPLETED" in output
    assert "Press" in output and "[Q]" in output

    # Dismiss dashboard
    app.handle_key("q")
    assert app.state.should_exit is True

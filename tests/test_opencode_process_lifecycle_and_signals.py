"""Regression tests for OpenCode process lifecycle, process group signals, and exit handling."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import BaseAdapter
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.dashboard.app import DashboardApp
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX killpg/getpgid specific behavior")
def test_kill_process_group_rejects_caller_pgid_and_pid():
    """Verify _kill_process_group never signals os.getpgrp() or os.getpid()."""
    mock_proc_same_pgid = MagicMock()
    mock_proc_same_pgid.pid = os.getpid()

    with patch("os.killpg") as mock_killpg, patch.object(BaseAdapter, "_safe_kill") as mock_safe_kill:
        BaseAdapter._kill_process_group(mock_proc_same_pgid)
        mock_killpg.assert_not_called()
        mock_safe_kill.assert_called_once_with(mock_proc_same_pgid)

    # Test with simulated child proc whose pgid matches caller's pgid
    caller_pgid = os.getpgrp()
    mock_proc_child = MagicMock()
    mock_proc_child.pid = 99999

    with patch("os.getpgid", return_value=caller_pgid), \
         patch("os.killpg") as mock_killpg, \
         patch.object(BaseAdapter, "_safe_kill") as mock_safe_kill:
        BaseAdapter._kill_process_group(mock_proc_child)
        mock_killpg.assert_not_called()
        mock_safe_kill.assert_called_once_with(mock_proc_child)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX killpg/getpgid specific behavior")
def test_kill_process_group_signals_isolated_child_pgid():
    """Verify _kill_process_group signals genuine isolated child process groups."""
    caller_pgid = os.getpgrp()
    distinct_pgid = caller_pgid + 1000
    mock_proc = MagicMock()
    mock_proc.pid = distinct_pgid

    with patch("os.getpgid", return_value=distinct_pgid), \
         patch("os.killpg") as mock_killpg, \
         patch("time.sleep"):
        BaseAdapter._kill_process_group(mock_proc)
        assert mock_killpg.call_count == 2
        mock_killpg.assert_any_call(distinct_pgid, signal.SIGTERM)
        mock_killpg.assert_any_call(distinct_pgid, signal.SIGKILL)


def test_stage_stream_events_does_not_cancel_adapter_on_non_intentional_exit():
    """Verify _execute_stream_events does not cancel the adapter during normal loop exit."""
    adapter = MagicMock()
    adapter.iter_events.return_value = []
    adapter.max_prompt_bytes = 100000

    role = Role(
        name="architect",
        sequence_number=1,
        template_content="test template",
        protocol_content="test protocol",
    )
    stage = Stage(role=role, adapter=adapter, run_manager=MagicMock())

    stage._execute_stream_events(
        prompt="hello",
        cwd=Path.cwd(),
        timeout=10,
        idle_timeout=None,
    )

    # adapter.cancel() must NOT have been called because there was no timeout or abort_event
    adapter.cancel.assert_not_called()


def test_stage_stream_events_cancels_adapter_on_intentional_abort():
    """Verify _execute_stream_events cancels adapter when abort_event is set."""
    import threading

    abort_ev = threading.Event()
    abort_ev.set()

    adapter = MagicMock()
    adapter.iter_events.return_value = []
    adapter.max_prompt_bytes = 100000

    role = Role(
        name="architect",
        sequence_number=1,
        template_content="test template",
        protocol_content="test protocol",
    )
    stage = Stage(role=role, adapter=adapter, run_manager=MagicMock(), abort_event=abort_ev)

    stage._execute_stream_events(
        prompt="hello",
        cwd=Path.cwd(),
        timeout=10,
        idle_timeout=None,
    )

    # adapter.cancel() MUST be called when abort_event was set
    assert adapter.cancel.call_count >= 1


def test_dashboard_app_reraises_system_exit(tmp_path, monkeypatch):
    """Verify DashboardApp.run does not swallow SystemExit."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run("Test dashboard system exit")

    app = DashboardApp(run.run_dir)

    monkeypatch.setattr(sys.stdin, "fileno", lambda: 0)
    from contextlib import ExitStack
    patches = [
        patch("sys.stdin.isatty", return_value=True),
        patch.object(app, "create_layout", side_effect=SystemExit(143)),
    ]
    if sys.platform != "win32":
        patches.extend([
            patch("termios.tcgetattr", return_value=[]),
            patch("termios.tcsetattr"),
            patch("tty.setcbreak"),
        ])
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        with pytest.raises(SystemExit) as exc_info:
            app.run()
        assert exc_info.value.code == 143

"""Focused test suite for OpenCodeAdapter native event streaming (Phase 4A)."""

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, List
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult


# ---------------------------------------------------------------------------
# 1. Chunk Events
# ---------------------------------------------------------------------------

def test_opencode_chunk_events():
    """Verify text events from OpenCode stream are translated to AgentEventType.CHUNK."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({
            "type": "text",
            "timestamp": 1789970469589,
            "sessionID": "ses_test123",
            "part": {"type": "text", "text": "Hello "},
        }),
        json.dumps({
            "type": "text",
            "timestamp": 1789970469600,
            "sessionID": "ses_test123",
            "part": {"type": "text", "text": "world!"},
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop"},
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Hello "
    assert events[0].data.get("session_id") == "ses_test123"

    assert events[1].event_type == AgentEventType.CHUNK
    assert events[1].text == "world!"
    assert events[1].data.get("session_id") == "ses_test123"

    assert events[2].event_type == AgentEventType.COMPLETE
    assert events[2].result.stdout == "Hello world!"


# ---------------------------------------------------------------------------
# 2. Tool Start and Finish Events
# ---------------------------------------------------------------------------

def test_opencode_tool_start_events():
    """Verify tool_use with running state or tool_start emits AgentEventType.TOOL_START."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({
            "type": "tool_use",
            "timestamp": 1789970487000,
            "sessionID": "ses_tool",
            "part": {
                "type": "tool",
                "tool": "read_file",
                "callID": "call-1",
                "state": {
                    "status": "running",
                    "input": {"path": "/src/code.py"},
                },
            },
        }),
        json.dumps({
            "type": "tool_start",
            "tool": "bash",
            "call_id": "call-2",
            "input": {"command": "ls -la"},
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop"},
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.TOOL_START
    assert events[0].text == "read_file"
    assert events[0].data["tool"] == "read_file"
    assert events[0].data["call_id"] == "call-1"
    assert events[0].data["input"] == {"path": "/src/code.py"}

    assert events[1].event_type == AgentEventType.TOOL_START
    assert events[1].text == "bash"
    assert events[1].data["tool"] == "bash"
    assert events[1].data["call_id"] == "call-2"
    assert events[1].data["input"] == {"command": "ls -la"}


def test_opencode_tool_finish_events():
    """Verify tool_use with completed state or tool_finish emits AgentEventType.TOOL_FINISH."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({
            "type": "tool_use",
            "timestamp": 1789970487397,
            "sessionID": "ses_tool",
            "part": {
                "type": "tool",
                "tool": "read",
                "callID": "call-read-1",
                "state": {
                    "status": "completed",
                    "output": "file contents line 1",
                    "input": {"filePath": "test.txt"},
                },
            },
        }),
        json.dumps({
            "type": "tool_finish",
            "tool": "write",
            "call_id": "call-write-2",
            "status": "success",
            "output": "Wrote 42 bytes",
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop"},
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.TOOL_FINISH
    assert events[0].text == "read"
    assert events[0].data["tool"] == "read"
    assert events[0].data["call_id"] == "call-read-1"
    assert events[0].data["status"] == "completed"
    assert events[0].data["output"] == "file contents line 1"

    assert events[1].event_type == AgentEventType.TOOL_FINISH
    assert events[1].text == "write"
    assert events[1].data["status"] == "success"
    assert events[1].data["output"] == "Wrote 42 bytes"


# ---------------------------------------------------------------------------
# 3. Terminal COMPLETE & Intermediate Steps
# ---------------------------------------------------------------------------

def test_opencode_complete_event_via_step_finish():
    """Verify step_finish with reason='stop' yields COMPLETE with ExecutionResult."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({
            "type": "text",
            "part": {"text": "All tasks completed successfully."},
        }),
        json.dumps({
            "type": "step_finish",
            "sessionID": "ses_abc",
            "part": {
                "reason": "stop",
                "tokens": {"total": 500, "input": 450, "output": 50},
            },
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time() - 2.0))
    assert len(events) == 2

    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.COMPLETE
    result = events[1].result
    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 0
    assert result.stdout == "All tasks completed successfully."
    assert result.duration_seconds >= 2.0
    assert result.token_usage == {"total": 500, "input": 450, "output": 50}
    assert result.metadata.get("session_id") == "ses_abc"


def test_opencode_complete_event_via_explicit_result():
    """Verify type='complete' or type='result' yields COMPLETE."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "complete"}),
    ]
    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 1
    assert events[0].event_type == AgentEventType.COMPLETE
    assert events[0].result.exit_code == 0


def test_opencode_intermediate_step_finish_not_terminal():
    """Verify step_finish with reason='tool-calls' does not emit COMPLETE or stop iteration."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Calling tool..."}}),
        json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}}),
        json.dumps({
            "type": "tool_use",
            "part": {"tool": "read", "state": {"status": "completed", "output": "content"}},
        }),
        json.dumps({"type": "text", "part": {"text": "Done!"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 4
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.TOOL_FINISH
    assert events[2].event_type == AgentEventType.CHUNK
    assert events[3].event_type == AgentEventType.COMPLETE
    assert events[3].result.stdout == "Calling tool...Done!"


# ---------------------------------------------------------------------------
# 4. Error Events
# ---------------------------------------------------------------------------

def test_opencode_error_event_via_step_finish():
    """Verify step_finish with reason='error' emits AgentEventType.ERROR."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Starting..."}}),
        json.dumps({
            "type": "step_finish",
            "part": {
                "reason": "error",
                "error": "Rate limit exceeded",
            },
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.ERROR
    assert events[1].text == "Rate limit exceeded"
    assert events[1].result.exit_code == 1
    assert events[1].result.stdout == "Starting..."


def test_opencode_explicit_error_event():
    """Verify type='error' or type='fatal' emits AgentEventType.ERROR."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "error", "message": "Failed to connect to model server"}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert events[0].text == "Failed to connect to model server"
    assert events[0].result.exit_code == 1


# ---------------------------------------------------------------------------
# 5. Heartbeat & Lifecycle Events
# ---------------------------------------------------------------------------

def test_opencode_heartbeat_event():
    """Verify heartbeat events are emitted as AgentEventType.HEARTBEAT."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "heartbeat", "timestamp": 123456789}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.HEARTBEAT
    assert events[1].event_type == AgentEventType.COMPLETE


def test_opencode_lifecycle_step_start_ignored():
    """Verify step_start and init events are safely ignored without emitting events."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "init", "tools": ["read", "write"]}),
        json.dumps({"type": "step_start", "sessionID": "ses_001"}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 1
    assert events[0].event_type == AgentEventType.COMPLETE


# ---------------------------------------------------------------------------
# 6. Unknown Provider Events & Malformed Payloads
# ---------------------------------------------------------------------------

def test_opencode_unknown_provider_events_logged_and_ignored(caplog):
    """Verify unknown provider events are logged as warnings and ignored."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "experimental_hook", "payload": {"foo": "bar"}}),
        json.dumps({"type": "unknown_metric", "val": 123}),
        json.dumps({"type": "text", "part": {"text": "Recovered text"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Recovered text"
    assert events[1].event_type == AgentEventType.COMPLETE
    assert any("Unrecognized provider event ignored" in r.message for r in caplog.records)


def test_opencode_malformed_json_payloads_ignored(caplog):
    """Verify malformed JSON lines or non-dict payloads are logged and ignored."""
    adapter = OpenCodeAdapter()
    stream = [
        "not json at all",
        "{invalid json",
        json.dumps([1, 2, 3]),  # Not a dict
        json.dumps({"type": "text", "part": {"text": "Valid text"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Valid text"
    assert events[1].event_type == AgentEventType.COMPLETE


# ---------------------------------------------------------------------------
# 7. Unexpected EOF (Protocol Contract Violation)
# ---------------------------------------------------------------------------

def test_opencode_unexpected_eof_without_terminal_event():
    """Verify EOF without terminal event emits ERROR even when process exit code is 0."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Partial work before unexpected exit"}}),
    ]

    # Process returned 0, but stream closed without COMPLETE or ERROR
    events = list(adapter._decode_stream_events(
        stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Partial work before unexpected exit"

    err_event = events[1]
    assert err_event.event_type == AgentEventType.ERROR
    assert err_event.text == "Unexpected EOF before terminal event."
    assert err_event.result is not None
    assert err_event.result.exit_code != 0
    assert err_event.result.stdout == "Partial work before unexpected exit"
    assert "Provider stream terminated prematurely" in err_event.result.metadata.get("diagnostic", "")


def test_opencode_unexpected_eof_with_nonzero_returncode():
    """Verify EOF without terminal event preserves process non-zero exit code."""
    adapter = OpenCodeAdapter()
    stream = []

    events = list(adapter._decode_stream_events(
        stream,
        start_time=time.time(),
        get_returncode=lambda: 137,  # SIGKILL
        get_stderr=lambda: "OOM killed",
    ))

    assert len(events) == 1
    err_event = events[0]
    assert err_event.event_type == AgentEventType.ERROR
    assert err_event.result.exit_code == 137
    assert err_event.result.stderr == "OOM killed"


# ---------------------------------------------------------------------------
# 8. Duplicate Terminal Events
# ---------------------------------------------------------------------------

def test_opencode_duplicate_terminal_events_dropped(caplog):
    """Verify duplicate terminal events after COMPLETE are dropped and logged."""
    adapter = OpenCodeAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Done"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),  # Duplicate
        json.dumps({"type": "complete"}),  # Another duplicate
        json.dumps({"type": "error", "message": "Late error"}),  # Another duplicate
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.COMPLETE
    assert any("Duplicate terminal event received" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 9. Subprocess Command Construction & iter_events Integration
# ---------------------------------------------------------------------------

def test_opencode_iter_events_command_construction():
    """Verify iter_events() appends --format json and forwards prompt to stdin."""
    adapter = OpenCodeAdapter(model="custom-model", effort="high", auto_approve=True)

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"type": "text", "part": {"text": "Stream test"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ])
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 0
    mock_proc.wait.return_value = 0

    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch.object(adapter, "_kill_process_group") as mock_kill_pg:

        events = list(adapter.iter_events(prompt="run this prompt", timeout=45))

        # Check command passed to subprocess.Popen
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == "/bin/opencode"
        assert cmd[1] == "run"
        assert "-m" in cmd
        assert cmd[cmd.index("-m") + 1] == "custom-model"
        assert "--variant" in cmd
        assert cmd[cmd.index("--variant") + 1] == "high"
        assert "--auto" in cmd
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"

        # Check stdin received prompt
        mock_proc.stdin.write.assert_called_once_with("run this prompt")
        mock_proc.stdin.close.assert_called_once()

        assert len(events) == 2
        assert events[0].event_type == AgentEventType.CHUNK
        assert events[1].event_type == AgentEventType.COMPLETE
        mock_kill_pg.assert_called_once_with(mock_proc)


def test_kill_process_group_safely_ignores_mock_and_invalid_pids():
    """Verify _kill_process_group does not invoke os.killpg for mock, non-integer, or <= 1 PIDs/PGIDs."""
    import signal

    with patch("os.killpg") as mock_killpg, \
         patch("os.getpgid") as mock_getpgid, \
         patch("time.sleep"), \
         patch("os.name", "posix"):
        # 1. MagicMock process (pid is MagicMock)
        mock_proc = MagicMock()
        OpenCodeAdapter._kill_process_group(mock_proc)
        mock_killpg.assert_not_called()
        mock_getpgid.assert_not_called()
        mock_proc.kill.assert_called_once()

        # 2. Process with PID 1 (must not call killpg or proc.kill)
        proc_pid_1 = MagicMock()
        proc_pid_1.pid = 1
        OpenCodeAdapter._kill_process_group(proc_pid_1)
        mock_killpg.assert_not_called()
        mock_getpgid.assert_not_called()
        proc_pid_1.kill.assert_not_called()

        # 3. Process with non-int PID (e.g. bool)
        proc_pid_bool = MagicMock()
        proc_pid_bool.pid = True
        OpenCodeAdapter._kill_process_group(proc_pid_bool)
        mock_killpg.assert_not_called()
        mock_getpgid.assert_not_called()
        proc_pid_bool.kill.assert_called_once()

        # 4. Process where os.getpgid returns 1 (systemd/all processes)
        mock_getpgid.return_value = 1
        proc_valid = MagicMock()
        proc_valid.pid = 9999
        OpenCodeAdapter._kill_process_group(proc_valid)
        mock_getpgid.assert_called_once_with(9999)
        mock_killpg.assert_not_called()
        proc_valid.kill.assert_called_once()

        # 5. Valid child process (PGID > 1)
        mock_killpg.reset_mock()
        mock_getpgid.return_value = 7777
        proc_isolated = MagicMock()
        proc_isolated.pid = 7777
        OpenCodeAdapter._kill_process_group(proc_isolated)
        assert mock_killpg.call_count == 2
        mock_killpg.assert_any_call(7777, signal.SIGTERM)
        mock_killpg.assert_any_call(7777, signal.SIGKILL)


def test_opencode_iter_events_missing_binary():
    """Verify iter_events yields ERROR when binary is missing and Popen fails."""
    adapter = OpenCodeAdapter()
    with patch("subprocess.Popen", side_effect=FileNotFoundError("No such file: opencode")):
        events = list(adapter.iter_events(prompt="test"))
        assert len(events) == 1
        assert events[0].event_type == AgentEventType.ERROR
        assert events[0].result.exit_code == 1
        assert "No such file: opencode" in events[0].text


# ---------------------------------------------------------------------------
# 10. Backward Compatibility: execute() Unchanged
# ---------------------------------------------------------------------------

def test_opencode_execute_backward_compatibility():
    """Verify execute() remains 100% unchanged and returns AdapterResponse."""
    adapter = OpenCodeAdapter()
    with patch.object(adapter, "_run_subprocess", return_value=("Standard stdout", "", 0)):
        res = adapter.execute(prompt="test")
        assert isinstance(res, AdapterResponse)
        assert res.stdout == "Standard stdout"
        assert res.stderr == ""
        assert res.exit_code == 0
        assert res.raw_output == "Standard stdout"

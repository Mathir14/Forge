"""Focused test suite for AntigravityAdapter native event streaming (Phase 4B)."""

import json
import logging
import time
from unittest.mock import MagicMock, patch

from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.base import AdapterResponse
from forge.core.events import AgentEventType, ExecutionResult


# ---------------------------------------------------------------------------
# 1. Chunk Events & Partial Output Preservation
# ---------------------------------------------------------------------------

def test_antigravity_chunk_events():
    """Verify text delta events from Antigravity stream are translated to AgentEventType.CHUNK."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({
            "event": "step_update",
            "timestamp": 1789970469589,
            "session_id": "ses_agy123",
            "step_update": {"text_delta": "Hello "},
        }),
        json.dumps({
            "event": "step_update",
            "timestamp": 1789970469600,
            "session_id": "ses_agy123",
            "step_update": {"text_delta": "from Antigravity!"},
        }),
        json.dumps({
            "event": "result",
            "status": "success",
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Hello "
    assert events[0].data.get("session_id") == "ses_agy123"

    assert events[1].event_type == AgentEventType.CHUNK
    assert events[1].text == "from Antigravity!"
    assert events[1].data.get("session_id") == "ses_agy123"

    assert events[2].event_type == AgentEventType.COMPLETE
    assert events[2].result.stdout == "Hello from Antigravity!"


def test_antigravity_partial_output_preservation():
    """Verify stdout and raw_output preserve all partial text prior to stream termination."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Partial output chunk 1\n"}}),
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Partial output chunk 2\n"}}),
        json.dumps({"event": "error", "message": "Backend failure"}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.CHUNK
    assert events[2].event_type == AgentEventType.ERROR

    result = events[2].result
    assert isinstance(result, ExecutionResult)
    assert result.exit_code == 1
    assert "Partial output chunk 1\nPartial output chunk 2\n" == result.stdout
    assert "Backend failure" in result.stderr
    assert "Partial output chunk 1" in result.raw_output


# ---------------------------------------------------------------------------
# 2. Tool Start and Finish Events
# ---------------------------------------------------------------------------

def test_antigravity_tool_start_events():
    """Verify tool_info with running status or tool_start emits AgentEventType.TOOL_START."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({
            "event": "step_update",
            "timestamp": 1789970487000,
            "session_id": "ses_tool",
            "tool_info": {
                "tool_name": "view_file",
                "call_id": "call-agy-1",
                "status": "running",
                "parameters": {"path": "/src/main.py"},
            },
        }),
        json.dumps({
            "event": "tool_start",
            "tool": "bash",
            "call_id": "call-agy-2",
            "input": {"command": "pytest"},
        }),
        json.dumps({
            "event": "result",
            "status": "success",
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.TOOL_START
    assert events[0].text == "view_file"
    assert events[0].data["tool"] == "view_file"
    assert events[0].data["call_id"] == "call-agy-1"
    assert events[0].data["input"] == {"path": "/src/main.py"}

    assert events[1].event_type == AgentEventType.TOOL_START
    assert events[1].text == "bash"
    assert events[1].data["tool"] == "bash"
    assert events[1].data["call_id"] == "call-agy-2"
    assert events[1].data["input"] == {"command": "pytest"}

    assert events[2].event_type == AgentEventType.COMPLETE


def test_antigravity_tool_finish_events():
    """Verify tool_info with completed status or tool_finish emits AgentEventType.TOOL_FINISH."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({
            "event": "step_update",
            "timestamp": 1789970487397,
            "session_id": "ses_tool",
            "tool_info": {
                "tool_name": "view_file",
                "call_id": "call-agy-1",
                "status": "completed",
                "output": "print('hello')",
                "parameters": {"path": "/src/main.py"},
            },
        }),
        json.dumps({
            "event": "tool_finish",
            "tool": "run_command",
            "call_id": "call-agy-3",
            "output": "tests passed",
            "status": "completed",
        }),
        json.dumps({
            "event": "result",
            "status": "success",
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 3

    assert events[0].event_type == AgentEventType.TOOL_FINISH
    assert events[0].text == "view_file"
    assert events[0].data["tool"] == "view_file"
    assert events[0].data["call_id"] == "call-agy-1"
    assert events[0].data["output"] == "print('hello')"
    assert events[0].data["error"] is False

    assert events[1].event_type == AgentEventType.TOOL_FINISH
    assert events[1].text == "run_command"
    assert events[1].data["tool"] == "run_command"
    assert events[1].data["call_id"] == "call-agy-3"
    assert events[1].data["output"] == "tests passed"
    assert events[1].data["error"] is False

    assert events[2].event_type == AgentEventType.COMPLETE


# ---------------------------------------------------------------------------
# 3. Terminal Completion Events
# ---------------------------------------------------------------------------

def test_antigravity_complete_event_via_result():
    """Verify result event translates into AgentEventType.COMPLETE with full ExecutionResult."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({
            "event": "step_update",
            "session_id": "ses_complete",
            "step_update": {"text_delta": "Plan executed successfully."},
        }),
        json.dumps({
            "event": "result",
            "status": "success",
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 450,
                "total_tokens": 1650,
            },
        }),
    ]

    start_time = time.time() - 1.5
    events = list(adapter._decode_stream_events(
        stream,
        start_time=start_time,
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK

    complete_evt = events[1]
    assert complete_evt.event_type == AgentEventType.COMPLETE
    assert complete_evt.text == "Plan executed successfully."
    assert complete_evt.result is not None
    assert complete_evt.result.exit_code == 0
    assert complete_evt.result.stdout == "Plan executed successfully."
    assert complete_evt.result.duration_seconds >= 1.5
    assert complete_evt.result.token_usage == {
        "prompt_tokens": 1200,
        "completion_tokens": 450,
        "total_tokens": 1650,
    }
    assert complete_evt.result.metadata.get("session_id") == "ses_complete"


def test_antigravity_complete_event_via_step_finish():
    """Verify step_finish compatibility event produces AgentEventType.COMPLETE."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"type": "text", "part": {"text": "Done with step"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[1].event_type == AgentEventType.COMPLETE
    assert events[1].result.stdout == "Done with step"


# ---------------------------------------------------------------------------
# 4. Error Events
# ---------------------------------------------------------------------------

def test_antigravity_error_event_via_result_failed():
    """Verify result event with status='failed' produces AgentEventType.ERROR."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Processing..."}}),
        json.dumps({
            "event": "result",
            "status": "failed",
            "error": "Execution budget exceeded",
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK

    err_evt = events[1]
    assert err_evt.event_type == AgentEventType.ERROR
    assert err_evt.text == "Execution budget exceeded"
    assert err_evt.result is not None
    assert err_evt.result.exit_code == 1
    assert err_evt.result.stdout == "Processing..."
    assert err_evt.result.stderr == "Execution budget exceeded"


def test_antigravity_explicit_error_event():
    """Verify explicit error or fatal events produce AgentEventType.ERROR."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "error", "message": "Failed to connect to agent endpoint"}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert events[0].text == "Failed to connect to agent endpoint"
    assert events[0].result.exit_code == 1


# ---------------------------------------------------------------------------
# 5. Heartbeat & Lifecycle Events
# ---------------------------------------------------------------------------

def test_antigravity_heartbeat_event():
    """Verify heartbeat / ping events produce AgentEventType.HEARTBEAT."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "heartbeat", "timestamp": 123456789}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))
    assert len(events) == 2
    assert events[0].event_type == AgentEventType.HEARTBEAT
    assert events[0].data.get("timestamp") == 123456789
    assert events[1].event_type == AgentEventType.COMPLETE


def test_antigravity_lifecycle_events_ignored(caplog):
    """Verify init and step_start lifecycle events are logged at debug and produce no events."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "init", "session_id": "ses_init"}),
        json.dumps({"event": "step_start", "step": 1}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    with caplog.at_level(logging.DEBUG):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 1
    assert events[0].event_type == AgentEventType.COMPLETE
    assert any("Antigravity lifecycle event received: init" in r.message for r in caplog.records)
    assert any("Antigravity lifecycle event received: step_start" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 6. Unknown Events & Malformed Payloads
# ---------------------------------------------------------------------------

def test_antigravity_unknown_provider_events_logged_and_ignored(caplog):
    """Verify unrecognized event types are logged as warning and ignored."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "telemetry_metric", "metric": "cpu_load", "value": 42}),
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Valid output"}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Valid output"
    assert events[1].event_type == AgentEventType.COMPLETE
    assert any("Unrecognized provider event ignored: telemetry_metric" in r.message for r in caplog.records)


def test_antigravity_malformed_json_payloads_ignored(caplog):
    """Verify unparseable JSON lines and non-dict payloads are logged and ignored."""
    adapter = AntigravityAdapter()
    stream = [
        "not a valid json string",
        "{invalid json",
        json.dumps([1, 2, 3]),  # Not a dict
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Text after bad lines"}}),
        json.dumps({"event": "result", "status": "success"}),
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Text after bad lines"
    assert events[1].event_type == AgentEventType.COMPLETE


# ---------------------------------------------------------------------------
# 7. Duplicate Terminal Events & Unexpected EOF
# ---------------------------------------------------------------------------

def test_antigravity_duplicate_terminal_events_dropped(caplog):
    """Verify first terminal event wins and subsequent terminal events are dropped with warning."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "result", "status": "success"}),
        json.dumps({"event": "result", "status": "success"}),
        json.dumps({"event": "error", "message": "Subsequent error"}),
    ]

    with caplog.at_level(logging.WARNING):
        events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    assert len(events) == 1
    assert events[0].event_type == AgentEventType.COMPLETE
    assert any("Duplicate terminal event received from Antigravity stream" in r.message for r in caplog.records)


def test_antigravity_unexpected_eof_without_terminal_event():
    """Verify EOF without terminal event emits ERROR with diagnostic metadata."""
    adapter = AntigravityAdapter()
    stream = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Partial output before crash"}}),
    ]

    events = list(adapter._decode_stream_events(
        stream,
        start_time=time.time(),
        get_returncode=lambda: 0,
        get_stderr=lambda: "",
    ))

    assert len(events) == 2
    assert events[0].event_type == AgentEventType.CHUNK
    assert events[0].text == "Partial output before crash"

    err_evt = events[1]
    assert err_evt.event_type == AgentEventType.ERROR
    assert err_evt.text == "Unexpected EOF before terminal event."
    assert err_evt.result is not None
    assert err_evt.result.exit_code == 1
    assert err_evt.result.stdout == "Partial output before crash"
    assert "Provider stream terminated prematurely" in err_evt.result.metadata.get("diagnostic", "")


def test_antigravity_unexpected_eof_with_nonzero_returncode():
    """Verify EOF without terminal event preserves non-zero process return code."""
    adapter = AntigravityAdapter()
    stream = []

    events = list(adapter._decode_stream_events(
        stream,
        start_time=time.time(),
        get_returncode=lambda: 137,
        get_stderr=lambda: "Killed by OOM",
    ))

    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert events[0].result.exit_code == 137
    assert events[0].result.stderr == "Killed by OOM"


# ---------------------------------------------------------------------------
# 8. Command Construction, Validation & Backward Compatibility
# ---------------------------------------------------------------------------

def test_antigravity_iter_events_command_construction():
    """Verify iter_events constructs agy command with --output-format stream-json and all options."""
    adapter = AntigravityAdapter(
        model="custom-gemini",
        effort="medium",
        auto_approve=True,
        extra_flags={"--custom-flag": "val"},
    )

    mock_proc = MagicMock()
    mock_proc.stdout = iter([
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Antigravity running"}}),
        json.dumps({"event": "result", "status": "success"}),
    ])
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 0
    mock_proc.wait.return_value = 0

    with patch("shutil.which", return_value="/bin/agy"), \
         patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch.object(adapter, "_kill_process_group") as mock_kill_pg:

        events = list(adapter.iter_events(prompt="run this task", timeout=60))

        args, _ = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == "/bin/agy"
        assert cmd[1] == "-p"
        assert cmd[2] == "run this task"
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "custom-gemini"
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == "medium"
        assert "--dangerously-skip-permissions" in cmd
        assert "--print-timeout" in cmd
        assert cmd[cmd.index("--print-timeout") + 1] == "60s"
        assert "--custom-flag" in cmd
        assert cmd[cmd.index("--custom-flag") + 1] == "val"

        assert len(events) == 2
        assert events[0].event_type == AgentEventType.CHUNK
        assert events[1].event_type == AgentEventType.COMPLETE
        mock_kill_pg.assert_called_once_with(mock_proc)


def test_antigravity_iter_events_missing_binary():
    """Verify iter_events yields ERROR immediately if agy binary is not found in PATH."""
    adapter = AntigravityAdapter()

    with patch("shutil.which", return_value=None):
        events = list(adapter.iter_events(prompt="test prompt"))

    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert "Neither 'agy' nor 'antigravity' was found in PATH" in events[0].text
    assert events[0].result.exit_code == 1


def test_antigravity_iter_events_max_prompt_bytes():
    """Verify iter_events yields ERROR immediately if prompt exceeds MAX_PROMPT_BYTES."""
    adapter = AntigravityAdapter()
    oversized_prompt = "x" * (adapter.MAX_PROMPT_BYTES + 10)

    with patch("shutil.which", return_value="/bin/agy"):
        events = list(adapter.iter_events(prompt=oversized_prompt))

    assert len(events) == 1
    assert events[0].event_type == AgentEventType.ERROR
    assert "exceeds Antigravity CLI argv transport limit" in events[0].text
    assert events[0].result.exit_code == 1


def test_antigravity_execute_backward_compatibility():
    """Verify execute() remains completely unchanged and returns AdapterResponse."""
    adapter = AntigravityAdapter()

    with patch("shutil.which", return_value="/bin/agy"), \
         patch.object(
             adapter,
             "_run_subprocess",
             return_value=("Standard execution output", "", 0)
         ) as mock_run_sub:

        resp = adapter.execute("run old execution")
        assert isinstance(resp, AdapterResponse)
        assert resp.stdout == "Standard execution output"
        assert resp.exit_code == 0
        assert resp.stderr == ""

        # Verify command still used --output-format text
        cmd = mock_run_sub.call_args[0][0]
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "text"

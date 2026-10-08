"""Focused test suite for OpenCodeAdapter native event streaming (Phase 4A)."""

import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, List
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import AdapterResponse
from forge.adapters.opencode import OpenCodeAdapter
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult


@pytest.fixture(autouse=True)
def reset_opencode_cache():
    OpenCodeAdapter._clear_variants_cache()
    yield
    OpenCodeAdapter._clear_variants_cache()


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
         patch.object(OpenCodeAdapter, "_fetch_model_variants", return_value={}), \
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
        assert "--variant" not in cmd
        assert "--effort" not in cmd
        assert "--auto" in cmd
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"

        # Check stdin received prompt
        mock_proc.stdin.write.assert_called_once_with("run this prompt")
        mock_proc.stdin.close.assert_called_once()

        assert len(events) == 2
        assert events[0].event_type == AgentEventType.CHUNK
        assert events[1].event_type == AgentEventType.COMPLETE
        mock_kill_pg.assert_not_called()  # Safe cleanup does not kill already-exited process (ADR-017 / F-005)


MOCK_OPENCODE_CATALOG = {
    "data": [
        {
            "id": "fledge-alpha-free",
            "modelID": "fledge-alpha-free",
            "providerID": "opencode",
            "variants": [
                {"id": "low", "settings": {"reasoningEffort": "low"}},
                {"id": "high", "settings": {"reasoningEffort": "high"}},
                {"id": "max", "settings": {"reasoningEffort": "max"}},
            ],
        },
        {
            "id": "space-bunny-free",
            "modelID": "space-bunny-free",
            "providerID": "opencode",
            "variants": [
                {"id": "low", "settings": {"reasoningEffort": "low"}},
                {"id": "medium", "settings": {"reasoningEffort": "medium"}},
                {"id": "high", "settings": {"reasoningEffort": "high"}},
                {"id": "xhigh", "settings": {"reasoningEffort": "xhigh"}},
                {"id": "max", "settings": {"reasoningEffort": "max"}},
            ],
        },
        {
            "id": "big-pickle",
            "modelID": "big-pickle",
            "providerID": "opencode",
            "variants": [],
        },
    ]
}


def test_opencode_dynamic_variant_discovery_supported():
    """Verify dynamic variant discovery translates supported effort levels to model#effort."""
    mock_res = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=0,
        stdout=json.dumps(MOCK_OPENCODE_CATALOG),
        stderr="",
    )
    with patch("shutil.which", return_value="/bin/opencode"), patch("subprocess.run", return_value=mock_res):
        # 1. Provider-qualified ID
        adapter_high = OpenCodeAdapter(model="opencode/fledge-alpha-free", effort="high")
        assert adapter_high._resolve_model_argument() == "opencode/fledge-alpha-free#high"

        # 2. Short model ID
        adapter_low = OpenCodeAdapter(model="fledge-alpha-free", effort="low")
        assert adapter_low._resolve_model_argument() == "fledge-alpha-free#low"

        # 3. Medium effort on space-bunny-free
        adapter_med = OpenCodeAdapter(model="opencode/space-bunny-free", effort="medium")
        assert adapter_med._resolve_model_argument() == "opencode/space-bunny-free#medium"


def test_opencode_effort_unsupported_model_logs_warning_and_uses_base_model(caplog):
    """When a model cannot honor requested effort, OpenCodeAdapter logs a warning and uses base model."""
    mock_res = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=0,
        stdout=json.dumps(MOCK_OPENCODE_CATALOG),
        stderr="",
    )
    adapter = OpenCodeAdapter(model="opencode/big-pickle", effort="high")

    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.run", return_value=mock_res), \
         caplog.at_level(logging.WARNING):
        model_arg = adapter._resolve_model_argument()

    assert model_arg == "opencode/big-pickle"
    assert any("OpenCode model 'opencode/big-pickle' does not expose a variant corresponding to requested effort 'high'" in r.message for r in caplog.records)


def test_opencode_explicit_model_variant_preserved_and_bypasses_metadata_query():
    """Explicit model variant (e.g. #custom) is preserved and bypasses OpenCode metadata query."""
    with patch("subprocess.run") as mock_sub:
        adapter = OpenCodeAdapter(model="custom/model#myvariant", effort="high")
        assert adapter._resolve_model_argument() == "custom/model#myvariant"
        mock_sub.assert_not_called()


def test_opencode_metadata_caching_and_cache_clear():
    """Metadata is cached at class level across instances, and cleared by _clear_variants_cache."""
    mock_res = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=0,
        stdout=json.dumps(MOCK_OPENCODE_CATALOG),
        stderr="",
    )
    with patch("shutil.which", return_value="/bin/opencode"), patch("subprocess.run", return_value=mock_res) as mock_sub:
        ad1 = OpenCodeAdapter(model="opencode/space-bunny-free", effort="high")
        assert ad1._resolve_model_argument() == "opencode/space-bunny-free#high"

        ad2 = OpenCodeAdapter(model="opencode/space-bunny-free", effort="low")
        assert ad2._resolve_model_argument() == "opencode/space-bunny-free#low"

        assert mock_sub.call_count == 1

        # Clear cache and verify subsequent call re-queries metadata
        OpenCodeAdapter._clear_variants_cache()
        ad3 = OpenCodeAdapter(model="opencode/space-bunny-free", effort="high")
        assert ad3._resolve_model_argument() == "opencode/space-bunny-free#high"
        assert mock_sub.call_count == 2


def test_opencode_metadata_query_failure_falls_back_to_base_model(caplog):
    """When metadata query fails (non-zero exit, timeout, invalid JSON, or missing binary),
    fall back to base model, log DEBUG, and do NOT emit warning about missing variant."""
    adapter = OpenCodeAdapter(model="opencode/big-pickle", effort="high")

    # 1. Non-zero exit code
    mock_err_res = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=1,
        stdout="",
        stderr="internal server error",
    )
    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.run", return_value=mock_err_res), \
         caplog.at_level(logging.DEBUG):
        caplog.clear()
        assert adapter._resolve_model_argument() == "opencode/big-pickle"
        assert any("Failed to query OpenCode model metadata:" in r.message for r in caplog.records)
        assert not any("does not expose a variant corresponding to requested effort" in r.message for r in caplog.records)

    # 2. Timeout
    OpenCodeAdapter._clear_variants_cache()
    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=3.0)), \
         caplog.at_level(logging.DEBUG):
        caplog.clear()
        assert adapter._resolve_model_argument() == "opencode/big-pickle"
        assert any("Failed to query OpenCode model metadata:" in r.message for r in caplog.records)
        assert not any("does not expose a variant corresponding to requested effort" in r.message for r in caplog.records)

    # 3. Invalid JSON
    OpenCodeAdapter._clear_variants_cache()
    mock_bad_json = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=0,
        stdout="THIS IS NOT JSON",
        stderr="",
    )
    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.run", return_value=mock_bad_json), \
         caplog.at_level(logging.DEBUG):
        caplog.clear()
        assert adapter._resolve_model_argument() == "opencode/big-pickle"
        assert any("Failed to query OpenCode model metadata:" in r.message for r in caplog.records)
        assert not any("does not expose a variant corresponding to requested effort" in r.message for r in caplog.records)

    # 4. OpenCode binary not found
    OpenCodeAdapter._clear_variants_cache()
    with patch.object(adapter, "_resolve_binary", return_value=(None, None)), caplog.at_level(logging.DEBUG):
        caplog.clear()
        assert adapter._resolve_model_argument() == "opencode/big-pickle"
        assert any("Failed to query OpenCode model metadata:" in r.message for r in caplog.records)
        assert not any("does not expose a variant corresponding to requested effort" in r.message for r in caplog.records)


def test_opencode_never_emits_unsupported_cli_flags():
    """OpenCodeAdapter must never emit --variant or --effort flags for any effort level."""
    mock_res = subprocess.CompletedProcess(
        args=["opencode", "api", "model.list"],
        returncode=0,
        stdout=json.dumps(MOCK_OPENCODE_CATALOG),
        stderr="",
    )
    for effort in ("low", "medium", "high", None):
        adapter = OpenCodeAdapter(model="opencode/space-bunny-free", effort=effort)

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        with patch("shutil.which", return_value="/bin/opencode"), \
             patch("subprocess.run", return_value=mock_res), \
             patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
             patch.object(adapter, "_kill_process_group"):

            list(adapter.iter_events(prompt="test"))
            cmd = mock_popen.call_args[0][0]
            assert "--variant" not in cmd
            assert "--effort" not in cmd
            if effort:
                assert f"opencode/space-bunny-free#{effort}" in cmd

        with patch("shutil.which", return_value="/bin/opencode"), \
             patch("subprocess.run", return_value=mock_res), \
             patch.object(adapter, "_run_subprocess", return_value=("", "", 0)) as mock_run_sub:

            adapter.execute(prompt="test")
            cmd = mock_run_sub.call_args[0][0]
            assert "--variant" not in cmd
            assert "--effort" not in cmd
            if effort:
                assert f"opencode/space-bunny-free#{effort}" in cmd


def test_provider_specific_effort_flags_matrix():
    """Ensure provider-specific CLI flags are only emitted by adapters whose CLI tools support them."""
    from forge.adapters.antigravity import AntigravityAdapter
    from forge.adapters.codex import CodexAdapter

    # 1. Antigravity emits --effort
    agy = AntigravityAdapter(effort="high")
    with patch("shutil.which", return_value="/bin/agy"), \
         patch.object(agy, "_run_subprocess", return_value=("", "", 0)) as mock_agy:
        agy.execute(prompt="test")
        agy_cmd = mock_agy.call_args[0][0]
        assert "--effort" in agy_cmd
        assert agy_cmd[agy_cmd.index("--effort") + 1] == "high"

    # 2. Codex emits -c model_reasoning_effort="..."
    codex = CodexAdapter(effort="high")
    with patch("shutil.which", return_value="/bin/codex"), \
         patch.object(codex, "_run_subprocess", return_value=("", "", 0)) as mock_codex:
        codex.execute(prompt="test")
        codex_cmd = mock_codex.call_args[0][0]
        assert "-c" in codex_cmd
        assert 'model_reasoning_effort="high"' in codex_cmd[codex_cmd.index("-c") + 1]

    # 3. OpenCode does NOT emit --effort or --variant
    oc = OpenCodeAdapter(model="opencode/big-pickle", effort="high")
    with patch("shutil.which", return_value="/bin/opencode"), \
         patch.object(oc, "_fetch_model_variants", return_value={}), \
         patch.object(oc, "_run_subprocess", return_value=("", "", 0)) as mock_oc:
        oc.execute(prompt="test")
        oc_cmd = mock_oc.call_args[0][0]
        assert "--effort" not in oc_cmd
        assert "--variant" not in oc_cmd


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX killpg/getpgid specific behavior")
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


# ---------------------------------------------------------------------------
# 11. Context Compaction & Post-Compaction Continuation Handling
# ---------------------------------------------------------------------------

def test_opencode_compaction_step_finish_does_not_abort_stream():
    """Verify that an intermediate step_finish with reason='stop' (e.g. from context compaction)
    does not cause premature COMPLETE emission or abort stream consumption before process EOF.
    Also verifies that intermediate compaction output is purged when a subsequent step starts.
    """
    adapter = OpenCodeAdapter()
    stream = [
        # Step 1: Tool call
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "tool_use",
            "part": {"tool": "read", "state": {"status": "completed", "output": "file content"}},
        }),
        json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}}),

        # Step 2: Intermediate context compaction step (emits compaction summary and reason='stop')
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "text",
            "part": {
                "text": "## Objective\n- Complete the CRITIC audit\n## Next Move\n1. Write report",
            },
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {"total": 50000, "input": 45000, "output": 5000}},
        }),

        # Step 3: Post-compaction continuation step (emits final report)
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "text",
            "part": {
                "text": (
                    "# Human Report — VerifyHire\n\n"
                    "```yaml\n"
                    "ROLE: CRITIC\n"
                    "STATUS: CRITIQUE_COMPLETE\n"
                    "HANDOFF: NONE\n"
                    "```\n"
                ),
            },
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {"total": 52000, "input": 46000, "output": 6000}},
        }),
    ]

    events = list(adapter._decode_stream_events(stream, start_time=time.time()))

    # Expected: TOOL_FINISH, CHUNK (compaction text), CHUNK (final report), COMPLETE
    event_types = [e.event_type for e in events]
    assert AgentEventType.TOOL_FINISH in event_types
    assert AgentEventType.COMPLETE in event_types
    assert event_types.count(AgentEventType.COMPLETE) == 1
    assert event_types[-1] == AgentEventType.COMPLETE

    complete_event = events[-1]
    assert complete_event.result is not None
    assert complete_event.result.exit_code == 0

    # Assert that stdout contains the final deliverable, NOT the intermediate compaction text
    assert "# Human Report — VerifyHire" in complete_event.result.stdout
    assert "STATUS: CRITIQUE_COMPLETE" in complete_event.result.stdout
    assert "## Objective" not in complete_event.result.stdout
    assert "Intermediate compaction" not in complete_event.result.stdout


def test_opencode_compaction_stage_raw_markdown_contains_final_output_not_compaction(tmp_path):
    """Verify that end-to-end StageResult.raw_markdown contains the final assistant output
    rather than an intermediate compaction artifact.
    """
    from forge.core.role import Role
    from forge.core.context import Context
    from forge.stages.stage import Stage
    from forge.storage.run_manager import RunManager

    stream_lines = [
        # Step 1: Investigation tool call
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "tool_use",
            "part": {"tool": "read", "state": {"status": "completed", "output": "code content"}},
        }),
        json.dumps({"type": "step_finish", "part": {"reason": "tool-calls"}}),

        # Step 2: Context compaction (the run-013 incident scenario)
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "text",
            "part": {
                "text": (
                    "## Objective\n"
                    "- Complete the CRITIC audit of the VerifyHire codebase\n"
                    "## Work State\n"
                    "### Completed\n- Read project docs\n"
                    "### Active\n- final Human Report + YAML Machine Report had NOT yet been written\n"
                    "## Next Move\n1. Write the Human Report\n"
                ),
            },
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {"total": 51549, "input": 45539, "output": 6010}},
        }),

        # Step 3: Post-compaction continuation step (the true final response)
        json.dumps({"type": "step_start"}),
        json.dumps({
            "type": "text",
            "part": {
                "text": (
                    "# Human Report — VerifyHire Audit\n\n"
                    "Executive Summary: Strong security baseline with modular separation.\n\n"
                    "```yaml\n"
                    "ROLE: CRITIC\n"
                    "STATUS: CRITIQUE_COMPLETE\n"
                    "HANDOFF: NONE\n"
                    "EXIT_CODE: 0\n"
                    "HEALTH_SCORE: 8\n"
                    "REASON: \"Audit completed successfully with zero blockers.\"\n"
                    "```\n"
                ),
            },
        }),
        json.dumps({
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {"total": 55000, "input": 47000, "output": 8000}},
        }),
    ]

    adapter = OpenCodeAdapter()

    # Create mock Popen process that streams stream_lines and then exits cleanly
    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = iter(stream_lines)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read.return_value = ""
    mock_proc.poll.return_value = 0
    mock_proc.wait.return_value = 0

    role = Role(
        name="critic",
        sequence_number=0,
        template_content="Audit codebase.",
        phase="pre_run",
    )

    from forge.core.config import Config
    from forge.core.git import GitService

    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Audit codebase.")
    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    stage = Stage(
        role=role,
        adapter=adapter,
        run_manager=run_mgr,
    )

    with patch("shutil.which", return_value="/bin/opencode"), \
         patch("subprocess.Popen", return_value=mock_proc), \
         patch.object(adapter, "_kill_process_group"):

        result = stage.run(context)

    # 1. Assert Stage success and protocol validation
    assert result.success is True
    assert result.status == "CRITIQUE_COMPLETE"
    assert result.machine_report.role == "CRITIC"
    assert result.machine_report.status == "CRITIQUE_COMPLETE"
    assert result.machine_report.is_valid is True

    # 2. Assert raw_markdown contains the final assistant deliverable
    assert "# Human Report — VerifyHire Audit" in result.raw_markdown
    assert "Executive Summary: Strong security baseline" in result.raw_markdown
    assert "STATUS: CRITIQUE_COMPLETE" in result.raw_markdown

    # 3. Assert raw_markdown does NOT contain the intermediate compaction artifact
    assert "## Objective" not in result.raw_markdown
    assert "final Human Report + YAML Machine Report had NOT yet been written" not in result.raw_markdown
    assert "## Next Move" not in result.raw_markdown


def test_opencode_adapter_tracks_and_cancels_active_subprocess():
    """Verify OpenCodeAdapter registers running subprocess and terminates it upon cancel()."""
    adapter = OpenCodeAdapter()
    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.poll.return_value = None

    adapter._register_proc(mock_proc, instance=adapter)
    assert mock_proc in adapter._active_procs
    assert mock_proc in OpenCodeAdapter._all_active_procs

    with patch.object(OpenCodeAdapter, "_kill_process_group") as mock_kill_pg:
        adapter.cancel()
        mock_kill_pg.assert_called_once_with(mock_proc)

    assert mock_proc not in adapter._active_procs
    assert mock_proc not in OpenCodeAdapter._all_active_procs


def test_stage_timeout_cancels_opencode_subprocess_and_unblocks_worker(tmp_path):
    """Verify that Stage timeout invokes adapter.cancel(), terminating child process and unblocking worker thread."""
    from forge.stages.stage import Stage
    from forge.core.role import Role
    from forge.core.context import Context
    from forge.core.config import Config
    from forge.core.git import GitService
    from forge.storage.run_manager import RunManager

    if sys.platform == "win32":
        fake_bin = tmp_path / "opencode.cmd"
        fake_bin.write_text(f'@echo off\n"{sys.executable}" -c "import time; time.sleep(30)"\n')
    else:
        fake_bin = tmp_path / "opencode"
        fake_bin.write_text("#!/bin/sh\nsleep 30\n")
        fake_bin.chmod(0o755)

    adapter = OpenCodeAdapter()
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run("test timeout cleanup")
    role = Role(name="critic", sequence_number=0, template_content="Test prompt template", phase="pre_run")
    context = Context(run=run, project_root=tmp_path, config=Config.default(), git=GitService(tmp_path))
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=0.3)

    with patch.object(adapter, "_resolve_binary", return_value=(str(fake_bin), None)):
        start = time.time()
        result = stage.run(context)
        elapsed = time.time() - start

    assert result.response.exit_code == 124
    assert result.success is False
    assert elapsed < 3.0, f"Stage took too long to terminate: {elapsed}s"
    assert len(adapter._active_procs) == 0



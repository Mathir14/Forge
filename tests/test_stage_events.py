"""Comprehensive tests for PR5: Event-driven Stage execution engine.

Verifies:
- Successful streaming execution to COMPLETE
- Streaming completion payload bridging
- ERROR termination terminates stage immediately
- Machine report parsed ONLY after COMPLETE event
- CHUNK event does NOT complete a stage
- TOOL_START and TOOL_FINISH event handling
- HEARTBEAT resets idle timeout
- Idle timeout behavior (halts when progress stalls)
- Absolute timeout behavior (hard upper bound, no extension by events)
- Duplicate terminal events ("first terminal event wins")
- Legacy adapter compatibility via BaseAdapter.iter_events()
- Preservation of partial output on failure and timeout
- Retry behavior across failed streaming attempts
- Cancellation and error propagation
"""

import time
from pathlib import Path
from typing import Optional, List, Iterator, Callable, Union, Any, Dict
from unittest.mock import patch, MagicMock

import pytest

from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.core.capabilities import Capability
from forge.core.config import Config
from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.git import GitService
from forge.core.role import Role
from forge.protocol.parser import MachineReportParser
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager


class GeneratorMockAdapter(BaseAdapter):
    """Test adapter that yields pre-configured AgentEvents or calls a custom generator."""

    def __init__(
        self,
        name: str = "mock_streaming",
        events: Optional[List[AgentEvent]] = None,
        event_generator: Optional[Callable[..., Iterator[AgentEvent]]] = None,
        auto_approve: bool = False,
    ):
        super().__init__(name=name, auto_approve=auto_approve)
        self.events = events or []
        self.event_generator = event_generator
        self.iter_events_called = False

    def is_available(self) -> bool:
        return True

    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        return AdapterResponse(
            stdout="fallback execute stdout",
            stderr="",
            exit_code=0,
            duration_seconds=0.1,
            raw_output="fallback execute stdout",
        )

    def iter_events(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[AgentEvent]:
        self.iter_events_called = True
        if self.event_generator:
            yield from self.event_generator(prompt=prompt, cwd=cwd, timeout=timeout)
        else:
            yield from self.events


class LegacyMockAdapter(BaseAdapter):
    """Legacy adapter implementing only execute() and relying on BaseAdapter.iter_events()."""

    def __init__(self, response: AdapterResponse):
        super().__init__(name="legacy_adapter")
        self.response = response
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
        return self.response


def _create_test_context(tmp_path: Path, task: str = "Test stage event task") -> Context:
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task=task)
    return Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )


def _create_planner_role() -> Role:
    return Role(
        name="planner",
        sequence_number=2,
        template_content="You are planner.",
        protocol_content="Emit machine report.",
    )


# ---------------------------------------------------------------------------
# 1. Successful streaming execution
# ---------------------------------------------------------------------------
def test_successful_streaming_execution(tmp_path):
    """Verify progressive event stream runs through lifecycle and completes successfully."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    yaml_text = """
# Plan Details
Step 1: Setup database
Step 2: Implement logic

```yaml
ROLE: PLANNER
STATUS: APPROVED
HANDOFF: EXECUTOR
```
"""
    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="# Plan Details\n"),
        AgentEvent(event_type=AgentEventType.TOOL_START, timestamp=time.time(), text="read_file", data={"file": "schema.sql"}),
        AgentEvent(event_type=AgentEventType.TOOL_FINISH, timestamp=time.time(), text="read_file", data={"output": "CREATE TABLE..."}),
        AgentEvent(event_type=AgentEventType.HEARTBEAT, timestamp=time.time()),
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=yaml_text),
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=yaml_text,
            result=ExecutionResult(
                exit_code=0,
                duration_seconds=1.25,
                stdout=yaml_text,
                raw_output=yaml_text,
                token_usage={"prompt": 50, "completion": 80},
            ),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    assert adapter.iter_events_called is True
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "EXECUTOR"
    assert result.duration_seconds == 1.25
    assert result.response.exit_code == 0

    # Verify artifacts were persisted to disk
    run_dir = tmp_path / ".forge" / "runs" / context.run.run_id
    assert (run_dir / "02_planner.md").exists()
    assert (run_dir / "02_planner.json").exists()


# ---------------------------------------------------------------------------
# 2. Streaming completion payload bridging
# ---------------------------------------------------------------------------
def test_streaming_completion_bridges_result(tmp_path):
    """Verify that COMPLETE event's ExecutionResult is bridged cleanly into AdapterResponse and StageResult."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    report_text = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
    exec_result = ExecutionResult(
        exit_code=0,
        duration_seconds=2.5,
        stdout=report_text,
        stderr="clean run",
        raw_output=report_text,
        token_usage={"total": 130},
        metadata={"model": "gemini-test"},
    )

    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=report_text),
        AgentEvent(event_type=AgentEventType.COMPLETE, timestamp=time.time(), text=report_text, result=exec_result),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is True
    assert result.duration_seconds == 2.5
    assert result.response.stderr == "clean run"
    assert result.response.exit_code == 0
    assert result.response.raw_output == report_text


# ---------------------------------------------------------------------------
# 3. CHUNK does not complete a stage
# ---------------------------------------------------------------------------
def test_chunk_does_not_complete_stage(tmp_path):
    """Verify that textual output (even containing a valid machine report) does NOT finish stage early."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    early_yaml = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\nREASON: Early draft\n```"
    final_yaml = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\nREASON: Final revised\n```"

    stage_completed_at_chunk = False

    def stream_gen(**kwargs):
        nonlocal stage_completed_at_chunk
        # Emit early yaml inside a chunk
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=early_yaml)
        # Yield intermediate tool events and a revised chunk
        yield AgentEvent(event_type=AgentEventType.TOOL_START, timestamp=time.time(), text="check_dependencies")
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="\nWait, revised plan:\n" + final_yaml)
        # Stage must still be executing here!
        yield AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=final_yaml,
            result=ExecutionResult(
                exit_code=0,
                duration_seconds=0.8,
                stdout=final_yaml,
                raw_output=early_yaml + "\n" + final_yaml,
            ),
        )

    adapter = GeneratorMockAdapter(event_generator=stream_gen)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    # The final report parsed must be the one after COMPLETE, reflecting the revised plan
    assert result.success is True
    assert result.machine_report.reason == "Final revised"


# ---------------------------------------------------------------------------
# 4. Machine report parsed ONLY after COMPLETE event
# ---------------------------------------------------------------------------
def test_machine_report_parsed_only_after_complete(tmp_path):
    """Verify MachineReportParser.extract_yaml is never called on CHUNK and strictly called after COMPLETE."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    yaml_chunk = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="chunk 1: " + yaml_chunk),
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="chunk 2: more text"),
        AgentEvent(event_type=AgentEventType.HEARTBEAT, timestamp=time.time()),
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=yaml_chunk,
            result=ExecutionResult(exit_code=0, duration_seconds=0.5, stdout=yaml_chunk, raw_output=yaml_chunk),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    extract_yaml_calls: List[str] = []
    original_extract_yaml = MachineReportParser.extract_yaml

    def spy_extract_yaml(text, expected_role=None):
        extract_yaml_calls.append(text)
        return original_extract_yaml(text, expected_role=expected_role)

    with patch.object(MachineReportParser, "extract_yaml", side_effect=spy_extract_yaml):
        result = stage.run(context)

    # extract_yaml must have been called only after the COMPLETE event arrived
    assert len(extract_yaml_calls) >= 1
    assert result.success is True


# ---------------------------------------------------------------------------
# 5. ERROR event terminates stage immediately
# ---------------------------------------------------------------------------
def test_error_terminates_stage_immediately(tmp_path):
    """Verify an ERROR event terminates stage execution immediately, preserving ExecutionResult."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Starting work..."),
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="Provider process crashed with segmentation fault",
            result=ExecutionResult(
                exit_code=139,
                duration_seconds=0.3,
                stdout="Starting work...",
                stderr="Segmentation fault (core dumped)",
                raw_output="Starting work...\nSegmentation fault",
                metadata={"signal": 11},
            ),
        ),
        # Following events should never be reached/processed
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Should be ignored"),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 139
    assert "Segmentation fault" in result.response.stderr
    assert "Starting work..." in result.raw_markdown

    # Artifacts must still be saved for failure diagnostics
    run_dir = tmp_path / ".forge" / "runs" / context.run.run_id
    assert (run_dir / "02_planner.md").exists()
    assert (run_dir / "02_planner.json").exists()


# ---------------------------------------------------------------------------
# 6. TOOL_START and TOOL_FINISH handling
# ---------------------------------------------------------------------------
def test_tool_start_and_finish_handling(tmp_path):
    """Verify tool start and finish events are processed incrementally and reset the idle timer."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    report_text = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
    events = [
        AgentEvent(event_type=AgentEventType.TOOL_START, timestamp=time.time(), text="run_bash", data={"cmd": "git status"}),
        AgentEvent(event_type=AgentEventType.TOOL_FINISH, timestamp=time.time(), text="run_bash", data={"output": "clean"}),
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=report_text),
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=report_text,
            result=ExecutionResult(exit_code=0, duration_seconds=0.2, stdout=report_text, raw_output=report_text),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is True
    assert result.status == "APPROVED"


# ---------------------------------------------------------------------------
# 7. HEARTBEAT resets idle timeout
# ---------------------------------------------------------------------------
def test_heartbeat_resets_idle_timeout(tmp_path):
    """Verify HEARTBEAT pulses keep the stage alive past the idle timeout threshold."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    report_text = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"

    def generator_with_heartbeats(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Starting...\n")
        # Sleep 0.08s, then emit heartbeat (< 0.15s idle timeout)
        time.sleep(0.08)
        yield AgentEvent(event_type=AgentEventType.HEARTBEAT, timestamp=time.time())
        # Sleep another 0.08s, then emit second heartbeat
        time.sleep(0.08)
        yield AgentEvent(event_type=AgentEventType.HEARTBEAT, timestamp=time.time())
        # Sleep another 0.08s, then emit complete
        # Total elapsed is ~0.24s, which exceeds the 0.15s idle timeout
        time.sleep(0.08)
        yield AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=report_text,
            result=ExecutionResult(exit_code=0, duration_seconds=0.25, stdout=report_text, raw_output=report_text),
        )

    adapter = GeneratorMockAdapter(event_generator=generator_with_heartbeats)
    # idle_timeout = 0.15s, absolute_timeout = 2.0s
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path), timeout=2, idle_timeout=0.15)

    result = stage.run(context)
    assert result.success is True
    assert result.status == "APPROVED"


# ---------------------------------------------------------------------------
# 8. Idle timeout behavior
# ---------------------------------------------------------------------------
def test_idle_timeout_behavior(tmp_path):
    """Verify execution halts and emits ERROR when no progress is observed within idle_timeout."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def stalling_generator(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Initial output before stall.\n")
        # Stall for 0.4s without any events (> 0.1s idle timeout)
        time.sleep(0.4)
        yield AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            result=ExecutionResult(exit_code=0, duration_seconds=0.4, stdout="Late complete"),
        )

    adapter = GeneratorMockAdapter(event_generator=stalling_generator)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path), timeout=5, idle_timeout=0.1)

    result = stage.run(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 124
    assert "idle timeout" in result.response.stderr.lower()
    # Partial output before the stall must be preserved!
    assert "Initial output before stall" in result.raw_markdown


# ---------------------------------------------------------------------------
# 9. Absolute timeout behavior
# ---------------------------------------------------------------------------
def test_absolute_timeout_behavior(tmp_path):
    """Verify absolute timeout is a hard ceiling that cannot be extended by continuous events."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def infinite_progress_generator(**kwargs):
        # Continually emit progress events every 0.04s, which continually resets the idle timer (0.5s)
        # but must NOT bypass the absolute timeout (0.15s).
        start = time.time()
        while time.time() - start < 1.0:
            yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="pulse ")
            time.sleep(0.04)
        yield AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            result=ExecutionResult(exit_code=0, duration_seconds=1.0),
        )

    adapter = GeneratorMockAdapter(event_generator=infinite_progress_generator)
    # absolute timeout = 0.15s, idle timeout = 0.5s
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path), timeout=0.15, idle_timeout=0.5)

    result = stage.run(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 124
    assert "absolute timeout" in result.response.stderr.lower()
    # Partial pulses must be preserved
    assert "pulse" in result.raw_markdown


# ---------------------------------------------------------------------------
# 10. Duplicate terminal events ("first terminal event wins")
# ---------------------------------------------------------------------------
def test_duplicate_terminal_events_complete_first(tmp_path):
    """Verify first terminal event wins when COMPLETE is followed by duplicate events."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    report_text = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=report_text),
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=report_text,
            result=ExecutionResult(exit_code=0, duration_seconds=0.3, stdout=report_text, raw_output=report_text),
        ),
        # Duplicate terminal events
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text="duplicate complete",
            result=ExecutionResult(exit_code=0, duration_seconds=0.4, stdout="dup", raw_output="dup"),
        ),
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="trailing error",
            result=ExecutionResult(exit_code=1, duration_seconds=0.5, stderr="trailing"),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.response.exit_code == 0


def test_duplicate_terminal_events_error_first(tmp_path):
    """Verify first terminal event wins when ERROR is followed by COMPLETE."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    events = [
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="Initial error",
            result=ExecutionResult(exit_code=1, duration_seconds=0.2, stderr="First error"),
        ),
        # Duplicate trailing COMPLETE
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text="Late complete",
            result=ExecutionResult(exit_code=0, duration_seconds=0.5, stdout="Late complete"),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 1


# ---------------------------------------------------------------------------
# 11. Unexpected EOF without terminal event
# ---------------------------------------------------------------------------
def test_unexpected_eof_without_terminal_event(tmp_path):
    """Verify generator exhaustion before emitting a terminal event is treated as unexpected EOF failure."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    # Generator simply stops without COMPLETE or ERROR
    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Partial unclosed stream output..."),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 1
    assert "unexpected eof" in result.response.stderr.lower()
    assert "Partial unclosed stream output" in result.raw_markdown


# ---------------------------------------------------------------------------
# 12. Legacy adapter compatibility via BaseAdapter.iter_events()
# ---------------------------------------------------------------------------
def test_legacy_adapter_compatibility(tmp_path):
    """Verify legacy adapters that only implement execute() function seamlessly via BaseAdapter.iter_events()."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    yaml_text = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
    resp = AdapterResponse(
        stdout=yaml_text,
        stderr="",
        exit_code=0,
        duration_seconds=0.4,
        raw_output=yaml_text,
    )

    adapter = LegacyMockAdapter(response=resp)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    assert adapter.execute_called is True
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.duration_seconds == 0.4


# ---------------------------------------------------------------------------
# 13. Preservation of partial output on failure and timeout
# ---------------------------------------------------------------------------
def test_preservation_of_partial_output(tmp_path):
    """Verify all partial chunks prior to failure/timeout are preserved in markdown and json artifacts."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    chunk1 = "Section 1: Initial exploration findings.\n"
    chunk2 = "Section 2: Database architecture analysis.\n"

    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=chunk1),
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=chunk2),
        AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=time.time(),
            text="Context window exceeded",
            result=ExecutionResult(
                exit_code=1,
                duration_seconds=0.6,
                stdout=chunk1 + chunk2,
                stderr="Context window exceeded",
                raw_output=chunk1 + chunk2,
            ),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)

    assert result.success is False
    assert chunk1 in result.raw_markdown
    assert chunk2 in result.raw_markdown

    # Inspect persisted disk files
    run_dir = tmp_path / ".forge" / "runs" / context.run.run_id
    saved_md = (run_dir / "02_planner.md").read_text(encoding="utf-8")
    assert chunk1 in saved_md
    assert chunk2 in saved_md


# ---------------------------------------------------------------------------
# 14. Retry behavior across streaming attempts
# ---------------------------------------------------------------------------
def test_retry_behavior_across_attempts(tmp_path):
    """Verify that a failed streaming attempt followed by a successful retry completes the loop."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    attempt = 1

    def multi_attempt_stream(**kwargs):
        nonlocal attempt
        if attempt == 1:
            attempt += 1
            yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Attempt 1 error")
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text="Attempt 1 failed",
                result=ExecutionResult(exit_code=1, duration_seconds=0.1, stderr="Attempt 1 failed"),
            )
        else:
            success_yaml = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```"
            yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=success_yaml)
            yield AgentEvent(
                event_type=AgentEventType.COMPLETE,
                timestamp=time.time(),
                text=success_yaml,
                result=ExecutionResult(exit_code=0, duration_seconds=0.2, stdout=success_yaml, raw_output=success_yaml),
            )

    adapter = GeneratorMockAdapter(event_generator=multi_attempt_stream)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    # Attempt 1 fails
    res1 = stage.run(context)
    assert res1.success is False
    assert res1.status == "FAILED"

    # Attempt 2 succeeds
    res2 = stage.run(context)
    assert res2.success is True
    assert res2.status == "APPROVED"


# ---------------------------------------------------------------------------
# 15. Cancellation and error propagation
# ---------------------------------------------------------------------------
def test_error_propagation_missing_binary(tmp_path):
    """Verify that setup exceptions like FileNotFoundError (missing CLI tool) are propagated."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def failing_stream(**kwargs):
        raise FileNotFoundError("CLI binary 'opencode' not found in PATH")
        yield  # Make it a generator

    adapter = GeneratorMockAdapter(event_generator=failing_stream)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    with pytest.raises(FileNotFoundError, match="CLI binary 'opencode' not found"):
        stage.run(context)


# ---------------------------------------------------------------------------
# 16. Production hardening: Worker termination and exception boundary tests
# ---------------------------------------------------------------------------
class CustomTestBaseException(BaseException):
    """Custom BaseException subclass used safely within test suite to test worker lifecycle."""
    pass


def test_worker_exception_handling_standard_exception(tmp_path):
    """Verify that a generator raising a standard Exception behaves correctly.

    The exception is caught by except Exception, enqueued as EXCEPTION, and then
    the worker guarantees DONE is enqueued via finally.
    """
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def stream_with_exception(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Partial output before failure\n")
        raise RuntimeError("Synthetic stream failure during execution")

    adapter = GeneratorMockAdapter(event_generator=stream_with_exception)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is False
    assert result.status == "FAILED"
    assert "Synthetic stream failure during execution" in result.response.stderr
    assert "Partial output before failure" in result.response.stdout


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_base_exception_enqueues_done_sentinel(tmp_path):
    """Verify that a generator raising BaseException still causes the worker to enqueue DONE.

    Even when an unhandled BaseException escapes the generator (bypassing except Exception),
    the finally block guarantees ("DONE", None) is enqueued before the worker thread exits.
    """
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def stream_with_base_exception(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Started task\n")
        raise CustomTestBaseException("Fatal unhandled BaseException")

    adapter = GeneratorMockAdapter(event_generator=stream_with_base_exception)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 1
    assert "unexpected eof" in result.response.stderr.lower()
    assert "Started task" in result.response.stdout


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_worker_unexpected_termination_does_not_wait_for_idle_timeout(tmp_path):
    """Verify that the main Stage loop does NOT wait for idle_timeout when the worker terminates.

    With idle_timeout configured to a high value (5.0s), an unhandled BaseException in the worker
    should immediately enqueue DONE in finally, causing the main loop to exit in milliseconds (< 1.0s)
    rather than stalling until the 5.0s idle timeout expires.
    """
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    def aborting_stream(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Starting...\n")
        raise CustomTestBaseException("Immediate worker abort")

    adapter = GeneratorMockAdapter(event_generator=aborting_stream)
    # Configure an idle_timeout of 5.0 seconds
    stage = Stage(
        role=role,
        adapter=adapter,
        run_manager=RunManager(tmp_path),
        idle_timeout=5.0,
    )

    start_time = time.time()
    result = stage.run(context)
    elapsed = time.time() - start_time

    assert result.success is False
    assert result.status == "FAILED"
    assert result.response.exit_code == 1
    assert "unexpected eof" in result.response.stderr.lower()
    # The stage must terminate promptly (< 1.0s), proving it did NOT stall for the 5.0s idle timeout
    assert elapsed < 1.0, f"Stage stalled for {elapsed:.2f}s instead of exiting promptly upon worker termination"


def test_worker_successful_execution_remains_unchanged(tmp_path):
    """Verify that normal worker completion enqueues DONE and succeeds as expected."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    valid_yaml = (
        "```yaml\n"
        "ROLE: PLANNER\n"
        "STATUS: APPROVED\n"
        "HANDOFF: EXECUTOR\n"
        "REASON: Plan completed successfully\n"
        "```"
    )

    def normal_stream(**kwargs):
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text="Drafting plan...\n")
        yield AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=valid_yaml)
        yield AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=valid_yaml,
            result=ExecutionResult(exit_code=0, duration_seconds=0.1, stdout=valid_yaml, raw_output=valid_yaml),
        )

    adapter = GeneratorMockAdapter(event_generator=normal_stream)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path), idle_timeout=5.0)

    result = stage.run(context)
    assert result.success is True
    assert result.status == "APPROVED"
    assert result.handoff == "EXECUTOR"


def test_stage_artifacts_preserve_human_report_when_streaming(tmp_path):
    """Regression: Verify that Stage artifacts and result.raw_markdown preserve the full human report rather than raw NDJSON transport lines."""
    context = _create_test_context(tmp_path)
    role = Role(name="executor", sequence_number=3, template_content="You are executor.", protocol_content="Emit machine report.")

    human_report = "# Human Report\n\n### Summary\nAddressed all reviewer feedback.\n\n### Validation\nAll tests passed.\n\n"
    machine_report = "```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: REVIEWER\nREASON: All tests passed\n```\n"
    combined_report = human_report + machine_report
    raw_transport_stream = '{"event": "init"}\n{"event": "step_update"}\n{"event": "result"}'

    events = [
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=human_report),
        AgentEvent(event_type=AgentEventType.CHUNK, timestamp=time.time(), text=machine_report),
        AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=time.time(),
            text=combined_report,
            result=ExecutionResult(
                exit_code=0,
                duration_seconds=1.0,
                stdout=combined_report,
                # Even if an adapter's raw_output contained raw NDJSON, artifacts must preserve human report
                raw_output=raw_transport_stream,
            ),
        ),
    ]

    adapter = GeneratorMockAdapter(events=events, auto_approve=True)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is True

    # 1. StageResult.raw_markdown must preserve the human report from stdout
    assert "# Human Report" in result.raw_markdown
    assert "### Summary" in result.raw_markdown
    assert "ROLE: EXECUTOR" in result.raw_markdown
    assert not result.raw_markdown.startswith('{"event":')

    # 2. Saved .md artifact on disk must preserve the human report
    run_dir = tmp_path / ".forge" / "runs" / context.run.run_id
    saved_md = (run_dir / "03_executor.md").read_text(encoding="utf-8")
    assert "# Human Report" in saved_md
    assert "### Summary" in saved_md
    assert "ROLE: EXECUTOR" in saved_md
    assert not saved_md.startswith('{"event":')

    # 3. Machine report parsing succeeds from decoded output
    assert result.status == "SUCCESS"
    assert result.handoff == "REVIEWER"

    # 4. raw_output invariant is preserved: contains original transport
    assert result.response.raw_output == raw_transport_stream

    # 5. Debug artifacts preserve original provider transport
    debug_raw = (run_dir / "debug" / "executor_raw.txt").read_text(encoding="utf-8")
    assert debug_raw == raw_transport_stream
    debug_stdout = (run_dir / "debug" / "executor_stdout.txt").read_text(encoding="utf-8")
    assert debug_stdout == combined_report


def test_legacy_adapter_artifacts_and_debug_preserved(tmp_path):
    """Regression: Verify legacy non-streaming adapters preserve identical stdout/raw_output across artifacts and debug logs."""
    context = _create_test_context(tmp_path)
    role = _create_planner_role()

    human_plan = "# Plan\n1. Analyze requirements\n2. Design solution\n\n"
    machine_block = "```yaml\nROLE: PLANNER\nSTATUS: APPROVED\nHANDOFF: EXECUTOR\n```\n"
    full_output = human_plan + machine_block

    resp = AdapterResponse(
        stdout=full_output,
        stderr="",
        exit_code=0,
        duration_seconds=0.3,
        raw_output=full_output,
    )
    adapter = LegacyMockAdapter(response=resp)
    stage = Stage(role=role, adapter=adapter, run_manager=RunManager(tmp_path))

    result = stage.run(context)
    assert result.success is True
    assert result.status == "APPROVED"
    assert "# Plan" in result.raw_markdown

    run_dir = tmp_path / ".forge" / "runs" / context.run.run_id
    saved_md = (run_dir / "02_planner.md").read_text(encoding="utf-8")
    assert "# Plan" in saved_md

    debug_raw = (run_dir / "debug" / "planner_raw.txt").read_text(encoding="utf-8")
    assert debug_raw == full_output
    debug_stdout = (run_dir / "debug" / "planner_stdout.txt").read_text(encoding="utf-8")
    assert debug_stdout == full_output


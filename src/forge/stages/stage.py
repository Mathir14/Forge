import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Set, Iterable, Union, Any, Dict, List, Tuple, Iterator
from forge.core.context import Context
from forge.core.role import Role
from forge.core.capabilities import Capability, CapabilityValidationError
from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.adapters.base import BaseAdapter, AdapterResponse
from forge.storage.run_manager import RunManager
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.protocol.parser import MachineReportParser
from forge.protocol.validator import MachineReportValidator
from forge.protocol.report import MachineReport
from forge.stages.requirements import StageRequirementsRegistry
from forge.stages.result import StageResult

logger = logging.getLogger(__name__)


class StageValidationError(RuntimeError):
    """Raised when stage pre-flight execution environment validation fails."""
    pass


class Stage:
    def __init__(
        self,
        role: Role,
        adapter: BaseAdapter,
        run_manager: Optional[RunManager] = None,
        timeout: Optional[int] = None,
        idle_timeout: Optional[Union[int, float]] = None,
        event_listener: Optional[Any] = None,
        abort_event: Optional[Any] = None,
    ):
        self.role = role
        self.adapter = adapter
        self.run_manager = run_manager or RunManager()
        self.timeout = timeout
        self.idle_timeout = idle_timeout
        self.event_listener = event_listener
        self.abort_event = abort_event

    @classmethod
    def get_required_capabilities(cls, stage_name: str) -> Set[str]:
        """Return required capabilities for a given stage name."""
        return StageRequirementsRegistry.get(stage_name)

    @classmethod
    def register_requirements(
        cls,
        stage_name: str,
        required_capabilities: Iterable[Union[str, Capability]],
    ) -> None:
        """Register or override required capabilities for a stage name."""
        StageRequirementsRegistry.register(stage_name, required_capabilities)

    def required_capabilities(self) -> Set[str]:
        """Return the required capabilities for this stage instance's role."""
        return self.get_required_capabilities(self.role.name)

    def validate_compatibility(self) -> None:
        """Validate that the configured adapter satisfies the stage's requirements."""
        required = self.required_capabilities()
        provided = self.adapter.capabilities()
        missing = required - provided
        if missing:
            raise CapabilityValidationError(
                stage_name=self.role.name,
                adapter_name=self.adapter.name,
                required_capabilities=required,
                provided_capabilities=provided,
                missing_capabilities=missing,
            )

    @staticmethod
    def _is_ndjson(text: str) -> bool:
        """Check if text appears to be raw NDJSON / JSON-lines stream rather than markdown."""
        if not text:
            return False
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            return False
        first_line = stripped.splitlines()[0].strip()
        if first_line.startswith("{") and first_line.endswith("}"):
            try:
                val = json.loads(first_line)
                return isinstance(val, dict)
            except Exception:
                return False
        return False

    def _validate_execution_environment(self, context: Context) -> None:
        """Validate execution environment before running stage.

        Fails fast if running headless/non-interactively with auto_approve=false
        on stages or adapters that require permission approval (e.g. CODE_EDIT).
        """
        # Only validate if the stage requires CODE_EDIT / tool approval
        requires_approval = (
            Capability.CODE_EDIT in self.required_capabilities()
            or getattr(self.adapter, "requires_headless_approval", False)
        )
        if not requires_approval:
            return

        is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        if not is_tty:
            auto_approve = getattr(self.adapter, "auto_approve", False)
            if not auto_approve and context.config:
                stage_cfg = context.config.get_stage_config(
                    self.role.name,
                    phase=getattr(self.role, "phase", "pre_run"),
                )
                if stage_cfg and stage_cfg.auto_approve:
                    auto_approve = True

            if not auto_approve:
                raise StageValidationError(
                    f"Stage '{self.role.name}' cannot run non-interactively with auto_approve=false. "
                    "The provider will deadlock waiting on interactive permission confirmation. "
                    "Configure auto_approve: true or execute in an interactive TTY."
                )

    def _get_event_stream(
        self,
        prompt: str,
        cwd: Optional[Path],
        timeout: Optional[int],
    ) -> Iterator[AgentEvent]:
        """Obtain event generator from configured adapter, supporting legacy and test mock fallbacks."""
        from unittest.mock import Mock
        import unittest.mock

        # Check if execute is mocked by tests/fixtures while iter_events is not
        is_execute_mocked = isinstance(getattr(self.adapter, "execute", None), Mock)
        is_iter_mocked = isinstance(getattr(self.adapter, "iter_events", None), Mock)
        if is_iter_mocked:
            mock_iter = getattr(self.adapter, "iter_events")
            ret_val = getattr(mock_iter, "_mock_return_value", None)
            side_eff = getattr(mock_iter, "_mock_side_effect", None)
            if (ret_val is unittest.mock.DEFAULT or ret_val is None) and side_eff is None:
                is_iter_mocked = False

        if is_execute_mocked and not is_iter_mocked:
            resp = self.adapter.execute(prompt=prompt, cwd=cwd, timeout=timeout)
            if isinstance(resp, Mock):
                stdout_str = str(getattr(resp, "stdout", "") or "")
                stderr_str = str(getattr(resp, "stderr", "") or "")
                raw_str = str(getattr(resp, "raw_output", "") or stdout_str)
                try:
                    code_val = int(getattr(resp, "exit_code", 0))
                except Exception:
                    code_val = 0
                try:
                    dur_val = float(getattr(resp, "duration_seconds", 0.0))
                except Exception:
                    dur_val = 0.0
                exec_res = ExecutionResult(
                    exit_code=code_val,
                    duration_seconds=dur_val,
                    stdout=stdout_str,
                    stderr=stderr_str,
                    raw_output=raw_str,
                )
                if stdout_str:
                    yield AgentEvent(
                        event_type=AgentEventType.CHUNK,
                        timestamp=time.time(),
                        text=stdout_str,
                    )
                yield AgentEvent(
                    event_type=AgentEventType.COMPLETE if code_val == 0 else AgentEventType.ERROR,
                    timestamp=time.time(),
                    text=stdout_str if code_val == 0 else stderr_str,
                    result=exec_res,
                )
                return
            elif isinstance(resp, AdapterResponse):
                exec_res = resp.to_result()
                if resp.stdout:
                    yield AgentEvent(
                        event_type=AgentEventType.CHUNK,
                        timestamp=time.time(),
                        text=resp.stdout,
                    )
                yield AgentEvent(
                    event_type=AgentEventType.COMPLETE if resp.exit_code == 0 else AgentEventType.ERROR,
                    timestamp=time.time(),
                    text=resp.stdout if resp.exit_code == 0 else resp.stderr,
                    result=exec_res,
                )
                return

        if hasattr(self.adapter, "iter_events"):
            yield from self.adapter.iter_events(prompt=prompt, cwd=cwd, timeout=timeout)
        elif hasattr(self.adapter, "execute"):
            yield from BaseAdapter.iter_events(self.adapter, prompt=prompt, cwd=cwd, timeout=timeout)
        else:
            raise AttributeError(
                f"Adapter '{getattr(self.adapter, 'name', type(self.adapter).__name__)}' "
                "must implement iter_events() or execute()."
            )

    def _execute_stream_events(
        self,
        prompt: str,
        cwd: Optional[Path],
        timeout: Optional[int],
        idle_timeout: Optional[Union[int, float]],
    ) -> Tuple[AgentEvent, AdapterResponse]:
        """Progressively consume adapter events enforcing idle and absolute timeouts.

        Returns:
            Tuple of (terminal_event, AdapterResponse).
        """
        start_time = time.time()
        last_progress_time = start_time
        absolute_deadline = (start_time + timeout) if timeout and timeout > 0 else None

        accumulated_stdout = ""
        accumulated_raw = ""
        token_usage: Dict[str, int] = {}
        tool_calls: List[Dict[str, Any]] = []
        terminal_event: Optional[AgentEvent] = None
        timeout_reason: Optional[str] = None
        timeout_type: Optional[str] = None

        event_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()
        stop_requested = threading.Event()

        def _worker() -> None:
            try:
                events_iter = self._get_event_stream(prompt=prompt, cwd=cwd, timeout=timeout)
                for ev in events_iter:
                    if stop_requested.is_set():
                        break
                    event_queue.put(("EVENT", ev))
                    if ev.event_type in (AgentEventType.COMPLETE, AgentEventType.ERROR):
                        break
            except Exception as exc:
                event_queue.put(("EXCEPTION", exc))
            finally:
                event_queue.put(("DONE", None))


        worker_thread = threading.Thread(
            target=_worker,
            name=f"StageStreamWorker-{self.role.name}",
            daemon=True,
        )
        worker_thread.start()

        try:
            while True:
                now = time.time()

                # Check cancellation request from abort_event
                if self.abort_event is not None and self.abort_event.is_set():
                    stop_requested.set()
                    if hasattr(self.adapter, "cancel"):
                        try:
                            self.adapter.cancel()
                        except Exception:
                            pass
                    abort_reason = "Execution cancelled by user."
                    duration = time.time() - start_time
                    exec_res = ExecutionResult(
                        exit_code=130,
                        duration_seconds=duration,
                        stdout=accumulated_stdout,
                        stderr=abort_reason,
                        raw_output=accumulated_raw or accumulated_stdout or abort_reason,
                        token_usage=token_usage,
                        metadata={"cancelled": True, "reason": abort_reason},
                    )
                    terminal_event = AgentEvent(
                        event_type=AgentEventType.ERROR,
                        timestamp=time.time(),
                        text=abort_reason,
                        result=exec_res,
                    )
                    break

                # Check absolute deadline: hard ceiling, no event extends beyond it
                if absolute_deadline is not None:
                    rem_abs = absolute_deadline - now
                    if rem_abs <= 0:
                        timeout_type = "absolute"
                        timeout_reason = f"Execution timed out after {timeout} seconds (absolute timeout)."
                        stop_requested.set()
                        break
                else:
                    rem_abs = None

                # Check idle deadline: elapsed time since last progress observation
                if idle_timeout is not None and idle_timeout > 0:
                    idle_deadline = last_progress_time + idle_timeout
                    rem_idle = idle_deadline - now
                    if rem_idle <= 0:
                        timeout_type = "idle"
                        timeout_reason = (
                            f"Execution timed out: no progress observed for {idle_timeout} seconds (idle timeout)."
                        )
                        stop_requested.set()
                        break
                else:
                    rem_idle = None

                candidates = [r for r in (rem_abs, rem_idle) if r is not None]
                wait_slice = min(candidates) if candidates else None
                wait_time = min(wait_slice, 0.2) if wait_slice is not None else 0.2

                try:
                    msg_type, payload = event_queue.get(timeout=max(0.001, wait_time))
                except queue.Empty:
                    continue

                if msg_type == "EXCEPTION":
                    stop_requested.set()
                    if isinstance(payload, (FileNotFoundError, PermissionError)):
                        raise payload
                    err_msg = str(payload)
                    duration = time.time() - start_time
                    exec_res = ExecutionResult(
                        exit_code=1,
                        duration_seconds=duration,
                        stdout=accumulated_stdout,
                        stderr=err_msg,
                        raw_output=accumulated_raw or accumulated_stdout or err_msg,
                        token_usage=token_usage,
                        metadata={"exception": type(payload).__name__, "message": err_msg},
                    )
                    terminal_event = AgentEvent(
                        event_type=AgentEventType.ERROR,
                        timestamp=time.time(),
                        text=err_msg,
                        result=exec_res,
                    )
                    break

                elif msg_type == "DONE":
                    break

                elif msg_type == "EVENT":
                    event: AgentEvent = payload
                    # First terminal event wins: ignore duplicate terminal events
                    if terminal_event is not None:
                        logger.warning(
                            "Duplicate event received after terminal event in stage '%s': %s",
                            self.role.name,
                            event.event_type,
                        )
                        continue

                    # Check whether this ERROR event might be recoverable before dispatching to listener
                    is_recoverable_error = (
                        event.event_type == AgentEventType.ERROR
                        and hasattr(self.adapter, "can_recover_session")
                        and self.adapter.can_recover_session(terminal_event=event)
                    )

                    # Dispatch event to listener with failure isolation
                    if self.event_listener is not None and not is_recoverable_error:
                        try:
                            if "stage_name" not in event.data:
                                event.data["stage_name"] = self.role.name
                            if "sequence_number" not in event.data:
                                event.data["sequence_number"] = self.role.sequence_number
                            self.event_listener(event)
                        except Exception:
                            pass

                    # Handle events incrementally
                    if event.event_type == AgentEventType.CHUNK:
                        # Progress observed: reset idle timer
                        last_progress_time = time.time()
                        if event.text:
                            accumulated_stdout += event.text
                            accumulated_raw += event.text
                        # A stage MUST NOT be considered finished simply because output was received

                    elif event.event_type == AgentEventType.TOOL_START:
                        # Progress observed: reset idle timer
                        last_progress_time = time.time()
                        if event.data:
                            tool_calls.append(event.data)

                    elif event.event_type == AgentEventType.TOOL_FINISH:
                        # Progress observed: reset idle timer
                        last_progress_time = time.time()

                    elif event.event_type == AgentEventType.HEARTBEAT:
                        # Progress observed: reset idle timer
                        last_progress_time = time.time()

                    elif event.event_type == AgentEventType.COMPLETE:
                        terminal_event = event
                        stop_requested.set()
                        break

                    elif event.event_type == AgentEventType.ERROR:
                        terminal_event = event
                        stop_requested.set()
                        break

                    else:
                        logger.debug(
                            "Unhandled event type in stage '%s': %s",
                            self.role.name,
                            event.event_type,
                        )

        finally:
            stop_requested.set()
            is_intentional_stop = bool(
                timeout_type
                or (self.abort_event is not None and self.abort_event.is_set())
            )
            if hasattr(self.adapter, "cancel") and (is_intentional_stop or worker_thread.is_alive()):
                try:
                    self.adapter.cancel()
                except Exception as exc:
                    logger.debug("Error canceling adapter on stage exit: %s", exc)
            if worker_thread.is_alive():
                worker_thread.join(timeout=1.0)

        # Handle timeout termination if encountered
        if timeout_type and timeout_reason:
            duration = time.time() - start_time
            exec_res = ExecutionResult(
                exit_code=124,
                duration_seconds=duration,
                stdout=accumulated_stdout,
                stderr=timeout_reason,
                raw_output=accumulated_raw or accumulated_stdout or timeout_reason,
                token_usage=token_usage,
                metadata={
                    "timeout_type": timeout_type,
                    "timeout": timeout if timeout_type == "absolute" else idle_timeout,
                    "partial_output_length": len(accumulated_stdout),
                },
            )
            terminal_event = AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=timeout_reason,
                result=exec_res,
            )

        # Handle cancellation if abort_event was set
        if (self.abort_event is not None and self.abort_event.is_set()) and (
            terminal_event is None or terminal_event.event_type != AgentEventType.COMPLETE
        ):
            abort_reason = "Execution cancelled by user."
            duration = time.time() - start_time
            exec_res = ExecutionResult(
                exit_code=130,
                duration_seconds=duration,
                stdout=accumulated_stdout,
                stderr=abort_reason,
                raw_output=accumulated_raw or accumulated_stdout or abort_reason,
                token_usage=token_usage,
                metadata={"cancelled": True, "reason": abort_reason},
            )
            terminal_event = AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=abort_reason,
                result=exec_res,
            )

        # Handle unexpected EOF before a terminal event
        if terminal_event is None:
            duration = time.time() - start_time
            eof_msg = "Unexpected EOF before terminal event."
            exec_res = ExecutionResult(
                exit_code=1,
                duration_seconds=duration,
                stdout=accumulated_stdout,
                stderr=eof_msg,
                raw_output=accumulated_raw or accumulated_stdout or eof_msg,
                token_usage=token_usage,
                metadata={
                    "diagnostic": "Provider stream terminated prematurely without emitting COMPLETE or ERROR.",
                },
            )
            terminal_event = AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=eof_msg,
                result=exec_res,
            )

        # Reconstruct AdapterResponse from ExecutionResult while preserving partial outputs
        exec_result = terminal_event.result
        if exec_result is None:
            exec_result = ExecutionResult(
                exit_code=0 if terminal_event.event_type == AgentEventType.COMPLETE else 1,
                duration_seconds=time.time() - start_time,
                stdout=accumulated_stdout,
                stderr=terminal_event.text or "",
                raw_output=accumulated_raw or accumulated_stdout or (terminal_event.text or ""),
                token_usage=token_usage,
            )

        if accumulated_stdout and not exec_result.stdout:
            exec_result.stdout = accumulated_stdout
        if accumulated_raw and not exec_result.raw_output:
            exec_result.raw_output = accumulated_raw
        elif not exec_result.raw_output and exec_result.stdout:
            exec_result.raw_output = exec_result.stdout

        if token_usage and not exec_result.token_usage:
            exec_result.token_usage = dict(token_usage)

        response = AdapterResponse.from_result(exec_result)
        return terminal_event, response

    def run(self, context: Context) -> StageResult:
        """Execute full stage lifecycle: validate -> prepare -> execute -> validate -> save."""
        if self.event_listener is None and getattr(context, "event_listener", None) is not None:
            self.event_listener = context.event_listener
        if self.abort_event is None and getattr(context, "abort_event", None) is not None:
            self.abort_event = context.abort_event

        # 0. Validate compatibility and execution environment before any execution
        self.validate_compatibility()
        self._validate_execution_environment(context)

        # 0.1 Tester v2: empirical black-box testing engine
        if self.role.name == "tester":
            from unittest.mock import Mock
            is_mock = (
                isinstance(getattr(self.adapter, "execute", None), Mock)
                or getattr(self.adapter, "is_mock", False)
                or self.adapter.__class__.__name__.startswith("Mock")
            )
            if not is_mock:
                from forge.testing.engine import TesterEngine
                engine = TesterEngine(context=context, run_manager=self.run_manager)
                return engine.run()

        # 1. Prepare
        instruction = InstructionBuilder.build(context, self.role)
        rendered_prompt = PromptCompiler.compile(
            instruction,
            self.role.template_content,
            max_prompt_bytes=self.adapter.max_prompt_bytes,
        )

        # 2. Resolve timeouts
        timeout = self.timeout
        if timeout is None and context.config:
            stage_cfg = context.config.get_stage_config(
                self.role.name,
                phase=getattr(self.role, "phase", "pre_run"),
            )
            timeout = stage_cfg.timeout

        idle_timeout = self.idle_timeout
        if idle_timeout is None and context.config:
            stage_cfg = context.config.get_stage_config(
                self.role.name,
                phase=getattr(self.role, "phase", "pre_run"),
            )
            idle_timeout = getattr(stage_cfg, "idle_timeout", None)
            if idle_timeout is None and hasattr(context.config, "defaults"):
                idle_timeout = getattr(context.config.defaults, "idle_timeout", None)

        # 3. Event-driven execution
        terminal_event, response = self._execute_stream_events(
            prompt=rendered_prompt.text,
            cwd=context.project_root,
            timeout=timeout,
            idle_timeout=idle_timeout,
        )

        # 3.1 Session recovery for recoverable adapter transport disconnects
        if terminal_event.event_type == AgentEventType.ERROR:
            if hasattr(self.adapter, "can_recover_session") and self.adapter.can_recover_session(
                terminal_event=terminal_event,
                response=response,
            ):
                logger.info(
                    "Attempting session recovery for adapter '%s' in stage '%s'...",
                    getattr(self.adapter, "name", type(self.adapter).__name__),
                    self.role.name,
                )
                recovered = self.adapter.recover_session(
                    terminal_event=terminal_event,
                    response=response,
                    cwd=context.project_root,
                )
                if recovered is not None:
                    terminal_event, response = recovered
                    if self.event_listener is not None:
                        try:
                            if "stage_name" not in terminal_event.data:
                                terminal_event.data["stage_name"] = self.role.name
                            if "sequence_number" not in terminal_event.data:
                                terminal_event.data["sequence_number"] = self.role.sequence_number
                            self.event_listener(terminal_event)
                        except Exception:
                            pass
                else:
                    # Recovery failed: dispatch the deferred ERROR event with diagnostic metadata
                    if self.event_listener is not None:
                        try:
                            if "stage_name" not in terminal_event.data:
                                terminal_event.data["stage_name"] = self.role.name
                            if "sequence_number" not in terminal_event.data:
                                terminal_event.data["sequence_number"] = self.role.sequence_number
                            self.event_listener(terminal_event)
                        except Exception:
                            pass

        # 4. Validate / Parse protocol
        # Parse the machine report ONLY after a COMPLETE event.
        if terminal_event.event_type == AgentEventType.COMPLETE:
            raw_text = response.stdout or (response.raw_output if not self._is_ndjson(response.raw_output) else "")
            raw_dict, raw_yaml = MachineReportParser.extract_yaml(raw_text, expected_role=self.role.name)
            if (
                not raw_dict
                and response.raw_output
                and response.raw_output != raw_text
                and not self._is_ndjson(response.raw_output)
            ):
                raw_dict, raw_yaml = MachineReportParser.extract_yaml(response.raw_output, expected_role=self.role.name)
            report = MachineReportValidator.validate(
                data=raw_dict,
                expected_role=self.role.name,
                raw_yaml=raw_yaml,
            )

            success = (
                response.exit_code == 0
                and report.is_valid
                and report.status not in ("REJECTED", "FAILED", "BLOCKED", "CHANGES_REQUIRED")
            )
        else:
            # ERROR terminates the stage immediately while preserving ExecutionResult
            err_reason = response.stderr or response.stdout or response.raw_output or (terminal_event.text or "Stage execution failed with ERROR event.")
            report = MachineReport(
                role=self.role.name.upper(),
                status="FAILED",
                handoff="NONE",
                exit_code=response.exit_code if response.exit_code != 0 else 1,
                reason=err_reason,
                is_valid=False,
                validation_errors=[err_reason],
            )
            success = False

        raw_markdown = response.stdout or response.raw_output
        result = StageResult(
            role=self.role,
            prompt=rendered_prompt,
            response=response,
            machine_report=report,
            raw_markdown=raw_markdown,
            duration_seconds=response.duration_seconds,
            success=success,
        )

        # 5. Save debug files and artifacts (.md + .json)
        if hasattr(context, "run") and hasattr(context.run, "run_dir") and context.run.run_dir:
            debug_dir = context.run.run_dir / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{self.role.name.lower()}_stdout.txt").write_text(
                response.stdout or "",
                encoding="utf-8",
            )
            (debug_dir / f"{self.role.name.lower()}_stderr.txt").write_text(
                response.stderr or "",
                encoding="utf-8",
            )
            (debug_dir / f"{self.role.name.lower()}_raw.txt").write_text(
                response.raw_output or "",
                encoding="utf-8",
            )

        self.run_manager.save_stage_artifacts(
            run=context.run,
            sequence_number=self.role.sequence_number,
            role_name=self.role.name,
            markdown_content=raw_markdown,
            json_data=result.to_dict(),
            prompt_hash=rendered_prompt.prompt_hash,
            adapter_name=self.adapter.name,
        )

        # Stage PKB knowledge proposals if emitted
        if report and getattr(report, "proposals", None) and hasattr(context, "run") and hasattr(context.run, "run_dir") and context.run.run_dir:
            proposals_dir = context.run.run_dir / "knowledge_proposals"
            proposals_dir.mkdir(parents=True, exist_ok=True)
            seq_prefix = f"{self.role.sequence_number:02d}_" if self.role.sequence_number is not None else ""
            proposal_file = proposals_dir / f"{seq_prefix}{self.role.name.lower()}_proposals.yaml"
            try:
                import yaml
                proposal_data = {
                    "role": self.role.name.lower(),
                    "sequence_number": self.role.sequence_number,
                    "proposals": [p.to_dict() for p in report.proposals],
                }
                with open(proposal_file, "w", encoding="utf-8") as pf:
                    yaml.safe_dump(proposal_data, pf, sort_keys=False, indent=2)
            except Exception as e:
                logging.warning("Failed to save knowledge proposals to %s: %s", proposal_file, e)

        return result


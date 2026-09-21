import os
import re
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Tuple, Union, Iterator

from forge.core.events import AgentEvent, AgentEventType, ExecutionResult


_ORIGINAL_SUBPROCESS_RUN = subprocess.run


@dataclass
class AdapterResponse:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    raw_output: str

    @classmethod
    def from_result(cls, result: ExecutionResult) -> "AdapterResponse":
        """Construct an AdapterResponse directly from a transport-neutral ExecutionResult."""
        return cls(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            raw_output=result.raw_output,
        )

    def to_result(self, duration_seconds: Optional[float] = None) -> ExecutionResult:
        """Export this AdapterResponse to a transport-neutral ExecutionResult."""
        final_duration = (
            duration_seconds
            if duration_seconds is not None
            else self.duration_seconds
        )
        return ExecutionResult(
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            duration_seconds=final_duration,
            raw_output=self.raw_output,
        )


class BaseAdapter(ABC):
    DEFAULT_TIMEOUT: int = 300
    CAPABILITIES: Optional[Set[Union[str, Any]]] = None
    DEFAULT_MODEL: Optional[str] = None
    DEFAULT_EFFORT: Optional[str] = None

    def __init__(
        self,
        name: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        auto_approve: bool = False,
        extra_flags: Optional[Dict[str, Any]] = None,
    ):
        self.name = name or self.__class__.__name__.lower().replace("adapter", "")
        self.model = model
        self.effort = effort
        self.auto_approve = auto_approve
        self.extra_flags = extra_flags or {}

    @classmethod
    def get_capabilities(cls) -> Set[str]:
        """Return declared capabilities for this adapter class.

        If CAPABILITIES is None (legacy adapter not yet declaring capabilities),
        returns all known capabilities for backward compatibility.
        """
        if cls.CAPABILITIES is None:
            from forge.core.capabilities import CapabilityRegistry
            return CapabilityRegistry.all_names()
        return {str(c) for c in cls.CAPABILITIES}

    def capabilities(self) -> Set[str]:
        """Return the set of capability names provided by this adapter instance."""
        return self.get_capabilities()

    def has_capability(self, capability: Union[str, Any]) -> bool:
        """Check whether this adapter provides the specified capability."""
        return str(capability) in self.capabilities()

    @property
    def supports_session_resume(self) -> bool:
        return self.has_capability("session_resume")

    @property
    def supports_browser(self) -> bool:
        return self.has_capability("browser")

    @property
    def supports_playwright(self) -> bool:
        return self.has_capability("playwright")

    @property
    def supports_streaming(self) -> bool:
        return self.has_capability("streaming")

    @property
    def supports_structured_output(self) -> bool:
        return self.has_capability("structured_output")

    @property
    def max_prompt_bytes(self) -> Optional[int]:
        """Maximum prompt size in bytes supported by adapter transport, or None if unbounded."""
        return getattr(self, "MAX_PROMPT_BYTES", None)

    @staticmethod
    def render_flags(extra_flags: Optional[Dict[str, Any]]) -> List[str]:
        """Render extra CLI flags safely from a dictionary."""
        if not extra_flags or not isinstance(extra_flags, dict):
            return []
        rendered: List[str] = []
        for name, value in extra_flags.items():
            clean_name = str(name).lstrip("-").strip()
            if not clean_name or not re.match(r"^[a-zA-Z0-9_\-]+$", clean_name):
                continue
            if isinstance(value, bool):
                if value is True:
                    rendered.append(f"--{clean_name}")
            elif value is None or value is False:
                continue
            elif isinstance(value, str) and value.strip().lower() in ("true", "yes", "on"):
                rendered.append(f"--{clean_name}")
            elif isinstance(value, str) and value.strip().lower() in ("false", "no", "off"):
                continue
            else:
                rendered.extend([f"--{clean_name}", str(value)])
        return rendered

    def _render_extra_flags(self) -> List[str]:
        """Render self.extra_flags to argv list."""
        return self.render_flags(self.extra_flags)

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the CLI binary is available in PATH."""
        pass

    @abstractmethod
    def execute(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> AdapterResponse:
        """Run the prompt against the CLI tool and return response."""
        pass

    def iter_events(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Iterator[AgentEvent]:
        """Universal event generator wrapping execute() for non-streaming adapters.

        Yields:
            - CHUNK: when execute() produces non-empty stdout.
            - COMPLETE: on successful termination (exit code 0), carrying ExecutionResult.
            - ERROR: on non-zero exit code, carrying ExecutionResult and error details.
        """
        start = time.time()
        resp = self.execute(prompt=prompt, cwd=cwd, timeout=timeout)
        duration = time.time() - start

        if resp.stdout:
            yield AgentEvent(
                event_type=AgentEventType.CHUNK,
                timestamp=start,
                text=resp.stdout,
            )

        final_duration = resp.duration_seconds if resp.duration_seconds else duration
        exec_result = resp.to_result(duration_seconds=final_duration)

        if resp.exit_code == 0:
            yield AgentEvent(
                event_type=AgentEventType.COMPLETE,
                timestamp=time.time(),
                text=resp.stdout,
                result=exec_result,
            )
        else:
            yield AgentEvent(
                event_type=AgentEventType.ERROR,
                timestamp=time.time(),
                text=resp.stderr or resp.raw_output,
                result=exec_result,
            )

    execute_events = iter_events

    @staticmethod
    def _decode_stream(stream: Any) -> str:
        """Decode subprocess stream output safely to text."""
        if stream is None:
            return ""
        if isinstance(stream, str):
            return stream
        if isinstance(stream, (bytes, bytearray)):
            return stream.decode("utf-8", errors="replace")
        return str(stream)

    @classmethod
    def _create_timeout_response(
        cls,
        exc: subprocess.TimeoutExpired,
        timeout_val: Optional[int],
        duration: float,
    ) -> AdapterResponse:
        """Construct an AdapterResponse preserving all partial output upon subprocess timeout."""
        partial_stdout = cls._decode_stream(getattr(exc, "stdout", None) or getattr(exc, "output", None))
        partial_stderr = cls._decode_stream(getattr(exc, "stderr", None))

        timeout_msg = f"Execution timed out after {timeout_val} seconds."
        if partial_stderr:
            err_str = f"{partial_stderr}\n{timeout_msg}"
        else:
            err_str = timeout_msg

        if partial_stdout and partial_stderr:
            raw = f"{partial_stdout}\n{partial_stderr}"
        elif partial_stdout:
            raw = partial_stdout
        elif partial_stderr:
            raw = partial_stderr
        else:
            raw = timeout_msg

        return AdapterResponse(
            stdout=partial_stdout,
            stderr=err_str,
            exit_code=124,
            duration_seconds=duration,
            raw_output=raw,
        )

    @staticmethod
    def _safe_kill(proc: subprocess.Popen) -> None:
        """Best-effort process termination ignoring missing or already-exited processes."""
        try:
            proc.kill()
        except Exception:
            pass

    @classmethod
    def _kill_process_group(cls, proc: subprocess.Popen) -> None:
        """Terminate or kill the entire process group of proc to prevent orphaned children."""
        try:
            pid = getattr(proc, "pid", None)
            # Guard against invalid or mocked PIDs. Cleanup must only target process
            # groups belonging to genuine child subprocesses. Reject non-integer PIDs
            # and protected/system process groups (PGID <= 1) to avoid signalling
            # unintended processes.
            if type(pid) is not int or pid <= 1:
                if type(pid) is not int:
                    cls._safe_kill(proc)
                return

            if os.name == "posix":
                try:
                    pgid = os.getpgid(pid)
                except (ProcessLookupError, PermissionError, OSError):
                    cls._safe_kill(proc)
                    return

                # Guard against protected/system process groups or non-integer PGIDs.
                if type(pgid) is not int or pgid <= 1:
                    cls._safe_kill(proc)
                    return

                os.killpg(pgid, signal.SIGTERM)
                time.sleep(0.1)
                os.killpg(pgid, signal.SIGKILL)
            else:
                cls._safe_kill(proc)
        except Exception:
            cls._safe_kill(proc)

    @classmethod
    def _run_subprocess(
        cls,
        cmd: List[str],
        input_data: Optional[str] = None,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[str, str, int]:
        """Run subprocess with process group isolation and guaranteed cleanup of child processes."""
        work_dir = cwd or Path.cwd()
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            res = subprocess.run(
                cmd,
                input=input_data,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return res.stdout or "", res.stderr or "", res.returncode

        popen_kwargs: Dict[str, Any] = {
            "cwd": work_dir,
            "stdin": subprocess.PIPE if input_data is not None else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **popen_kwargs)
        try:
            stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
            return stdout or "", stderr or "", proc.returncode
        except subprocess.TimeoutExpired as e:
            cls._kill_process_group(proc)
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=timeout,
                output=getattr(e, "stdout", None) or getattr(e, "output", None),
                stderr=getattr(e, "stderr", None),
            )
        except BaseException:
            cls._kill_process_group(proc)
            raise

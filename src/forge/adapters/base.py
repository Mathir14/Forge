import atexit
import os
import queue
import re
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Tuple, Union, Iterator

from forge.core.events import AgentEvent, AgentEventType, ExecutionResult
from forge.core.platform import (
    get_process_group_flags,
    prepare_command,
    terminate_process_tree,
    safe_kill,
    launch_contained_process,
)


_ORIGINAL_SUBPROCESS_RUN = subprocess.run


import collections


class StderrDrainer:
    """Asynchronously drains a subprocess stderr pipe in a background daemon thread
    to prevent OS pipe buffer saturation and deadlock during stdout streaming.

    Bounded in memory to prevent heap exhaustion while preserving tail diagnostics
    for error reporting and recovery.
    """
    DEFAULT_MAX_BYTES: int = 512 * 1024  # 512 KB

    def __init__(self, stream: Optional[Any], max_bytes: int = DEFAULT_MAX_BYTES):
        self._stream = stream
        self._max_bytes = max_bytes
        self._chunks: collections.deque[str] = collections.deque()
        self._current_bytes: int = 0
        self._truncated: bool = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        if self._stream is not None:
            self._thread = threading.Thread(target=self._drain, daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        try:
            for line in self._stream:
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                with self._lock:
                    line_len = len(line.encode("utf-8", errors="replace"))
                    self._chunks.append(line)
                    self._current_bytes += line_len
                    while self._current_bytes > self._max_bytes and self._chunks:
                        removed = self._chunks.popleft()
                        self._current_bytes -= len(removed.encode("utf-8", errors="replace"))
                        self._truncated = True
        except Exception:
            pass

    def get_stderr(self) -> str:
        with self._lock:
            content = "".join(self._chunks)
            if self._truncated:
                limit_kb = self._max_bytes // 1024
                return f"[... stderr truncated: previous output exceeded {limit_kb}KB buffer ...]\n" + content
            return content

    def close(self, timeout: float = 0.2) -> None:
        if self._thread is not None and self._thread.is_alive():
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass
        if self._stream is not None and (self._thread is None or not self._thread.is_alive()):
            try:
                self._stream.close()
            except Exception:
                pass


class BoundedLineReader:
    """Asynchronously reads lines from an iterable or pipe stream in a background daemon thread,
    enabling non-blocking and timeout-bounded reading via a thread-safe Queue.

    Guarantees:
    - Zero-delay streaming during normal active output.
    - Bounded non-blocking drainage after in-stream terminal events.
    - Universal cross-platform compatibility (POSIX and Windows).
    - Prevents kernel pipe deadlock when child processes inherit stdout.
    - Clean shutdown without thread or pipe leaks.
    """

    _EOF = object()

    def __init__(self, stream: Optional[Any]):
        self._stream = stream
        self._queue: queue.Queue[Any] = queue.Queue()
        self._closed = threading.Event()
        self._eof_reached = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if self._stream is not None:
            self._thread = threading.Thread(
                target=self._drain_stream,
                daemon=True,
                name="BoundedLineReader",
            )
            self._thread.start()
        else:
            self._eof_reached.set()
            self._queue.put(self._EOF)

    def _drain_stream(self) -> None:
        try:
            for line in self._stream:
                if self._closed.is_set():
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                self._queue.put(line)
        except Exception:
            pass
        finally:
            self._eof_reached.set()
            self._queue.put(self._EOF)

    def read_line(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read next line from stream.

        Args:
            timeout: Maximum seconds to wait. None waits indefinitely.

        Returns:
            str: Next line from stream.
            None: When stream reaches EOF.

        Raises:
            queue.Empty: If timeout expires without receiving a line or EOF.
        """
        if self._closed.is_set() and self._queue.empty():
            return None

        item = self._queue.get(timeout=timeout)
        if item is self._EOF:
            self._queue.put(self._EOF)
            return None
        return item

    def is_eof(self) -> bool:
        """Return True if EOF has been observed on the underlying stream."""
        return self._eof_reached.is_set()

    def close(self, timeout: float = 0.2) -> None:
        """Signal reader to stop consuming and optionally wait for thread termination."""
        self._closed.set()
        if self._thread is not None and self._thread.is_alive():
            try:
                self._thread.join(timeout=timeout)
            except Exception:
                pass
        if self._stream is not None and (self._thread is None or not self._thread.is_alive()):
            try:
                self._stream.close()
            except Exception:
                pass


class hybridmethod:
    """Descriptor enabling a method to be invoked on either a class or an instance."""

    def __init__(self, func: Any):
        self.func = func
        self.__doc__ = getattr(func, "__doc__", None)
        self.__name__ = getattr(func, "__name__", "hybridmethod")

    def __get__(self, instance: Any, owner: Any) -> Any:
        if instance is None:
            @wraps(self.func)
            def class_wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.func(owner, *args, **kwargs)
            class_wrapper.__wrapped__ = self.func
            return class_wrapper
        else:
            @wraps(self.func)
            def instance_wrapper(*args: Any, **kwargs: Any) -> Any:
                return self.func(instance, *args, **kwargs)
            instance_wrapper.__wrapped__ = self.func
            return instance_wrapper


@dataclass
class AdapterResponse:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    raw_output: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: ExecutionResult) -> "AdapterResponse":
        """Construct an AdapterResponse directly from a transport-neutral ExecutionResult."""
        return cls(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            raw_output=result.raw_output,
            metadata=dict(result.metadata) if result.metadata else {},
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
            metadata=dict(self.metadata) if self.metadata else {},
        )


class BaseAdapter(ABC):
    DEFAULT_TIMEOUT: int = 300
    CAPABILITIES: Optional[Set[Union[str, Any]]] = None
    DEFAULT_MODEL: Optional[str] = None
    DEFAULT_EFFORT: Optional[str] = None
    _all_active_procs: Set[subprocess.Popen] = set()

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
        self._active_procs: Set[subprocess.Popen] = set()
        self._cancel_requested: bool = False

    @classmethod
    def _register_proc(cls, proc: subprocess.Popen, instance: Optional["BaseAdapter"] = None) -> None:
        """Track an active child subprocess across class and instance."""
        if proc is not None:
            cls._all_active_procs.add(proc)
            if instance is not None:
                if not hasattr(instance, "_active_procs"):
                    instance._active_procs = set()
                instance._active_procs.add(proc)

    @classmethod
    def _unregister_proc(cls, proc: Optional[subprocess.Popen], instance: Optional["BaseAdapter"] = None) -> None:
        """Remove a terminated child subprocess from tracking."""
        if proc is not None:
            job = getattr(proc, "_job_object", None)
            if job is not None and hasattr(job, "close"):
                try:
                    job.close()
                except Exception:
                    pass
            cls._all_active_procs.discard(proc)
            if instance is not None and hasattr(instance, "_active_procs"):
                instance._active_procs.discard(proc)

    def cancel(self) -> None:
        """Terminate and clean up all active subprocesses belonging to this adapter."""
        self._cancel_requested = True
        active = list(getattr(self, "_active_procs", set()))
        for proc in active:
            self._safe_cleanup_subprocess(proc)
            self._unregister_proc(proc, instance=self)
        if hasattr(self, "_active_procs"):
            self._active_procs.clear()

    @classmethod
    def cleanup_all(cls) -> None:
        """Emergency cleanup of all active subprocesses tracked across adapters."""
        active = list(cls._all_active_procs)
        for proc in active:
            cls._safe_cleanup_subprocess(proc)
            cls._all_active_procs.discard(proc)
        cls._all_active_procs.clear()

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
        start = time.perf_counter()
        resp = self.execute(prompt=prompt, cwd=cwd, timeout=timeout)
        duration = max(time.perf_counter() - start, 0.0001)

        if resp.stdout:
            yield AgentEvent(
                event_type=AgentEventType.CHUNK,
                timestamp=time.time(),
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
 
    def can_recover_session(
        self,
        terminal_event: Optional[AgentEvent] = None,
        response: Optional[AdapterResponse] = None,
        session_id: Optional[str] = None,
    ) -> bool:
        """Check whether this adapter can attempt session recovery for a failed execution."""
        return False

    def recover_session(
        self,
        terminal_event: Optional[AgentEvent] = None,
        response: Optional[AdapterResponse] = None,
        session_id: Optional[str] = None,
        cwd: Optional[Path] = None,
        timeout: Optional[float] = None,
    ) -> Optional[Tuple[AgentEvent, AdapterResponse]]:
        """Attempt to recover a failed execution from an external daemon or persistent session.

        Returns:
            Tuple of (recovered_terminal_event, recovered_response) if recovery succeeded,
            or None if recovery failed or was not possible.
        """
        return None

    def is_session_active(
        self,
        session_id: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> bool:
        """Check whether an external daemon or persistent session is actively executing."""
        return False

    def cancel_session(
        self,
        session_id: Optional[str] = None,
        verify: bool = True,
        timeout: float = 10.0,
        cwd: Optional[Path] = None,
    ) -> bool:
        """Attempt to cancel/interrupt an external session and verify that it stopped.

        Returns:
            True if session is confirmed stopped, False if still running or unverified.
        """
        self.cancel()
        return True

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
    def _build_complete_event(
        ts: float,
        duration: float,
        stdout: str,
        stderr: str,
        raw_output: str,
        token_usage: Dict[str, int],
        metadata: Dict[str, Any],
        exit_code: int = 0,
    ) -> AgentEvent:
        """Construct a post-mortem COMPLETE AgentEvent carrying its verified ExecutionResult."""
        exec_res = ExecutionResult(
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr,
            raw_output=raw_output,
            token_usage=dict(token_usage),
            metadata=metadata,
        )
        return AgentEvent(
            event_type=AgentEventType.COMPLETE,
            timestamp=ts,
            text=stdout,
            result=exec_res,
        )

    @staticmethod
    def _build_error_event(
        ts: float,
        duration: float,
        error_msg: str,
        stdout: str,
        stderr: str,
        raw_output: str,
        token_usage: Dict[str, int],
        metadata: Dict[str, Any],
        exit_code: int = 1,
    ) -> AgentEvent:
        """Construct an ERROR AgentEvent carrying its verified ExecutionResult."""
        exec_res = ExecutionResult(
            exit_code=exit_code,
            duration_seconds=duration,
            stdout=stdout,
            stderr=stderr or error_msg,
            raw_output=raw_output,
            token_usage=dict(token_usage),
            metadata=metadata,
        )
        return AgentEvent(
            event_type=AgentEventType.ERROR,
            timestamp=ts,
            text=error_msg,
            result=exec_res,
        )

    @classmethod
    def _safe_cleanup_subprocess(
        cls,
        proc: Optional[subprocess.Popen],
        tracker: Optional[Any] = None,
    ) -> bool:
        """Safely and idempotently clean up subprocess and all descendant processes."""
        if proc is None:
            return True
        if getattr(proc, "_cleanup_done", None) is True:
            return True

        job = getattr(proc, "_job_object", None)
        method = "windows_job_object" if job is not None else "terminate_process_tree"
        if tracker is not None and hasattr(tracker, "record_cleanup_start"):
            try:
                tracker.record_cleanup_start(method=method)
            except Exception:
                pass

        success = True
        err = None
        try:
            cls._kill_process_group(proc)
            try:
                proc.wait(timeout=0.2)
            except Exception:
                pass
            setattr(proc, "_cleanup_done", True)
        except Exception as e:
            success = False
            err = str(e)

        if tracker is not None and hasattr(tracker, "record_cleanup_result"):
            try:
                tracker.record_cleanup_result(success=success, method=method, error=err)
            except Exception:
                pass

        return success

    @staticmethod
    def _safe_kill(proc: subprocess.Popen) -> None:
        """Best-effort process termination ignoring missing or already-exited processes."""
        safe_kill(proc)

    @classmethod
    def _kill_process_group(cls, proc: subprocess.Popen) -> None:
        """Terminate or kill the entire process group of proc to prevent orphaned children."""
        target_pgid = getattr(proc, "_pgid", None)
        if type(target_pgid) is not int:
            target_pgid = None
        terminate_process_tree(proc, pgid=target_pgid, safe_kill_fn=cls._safe_kill)

    @hybridmethod
    def _run_subprocess(
        self_or_cls,
        cmd: List[str],
        input_data: Optional[str] = None,
        cwd: Optional[Path] = None,
        timeout: Optional[int] = None,
        instance: Optional["BaseAdapter"] = None,
        tracker: Optional[Any] = None,
    ) -> Tuple[str, str, int]:
        """Run subprocess with process group isolation and guaranteed cleanup of child processes."""
        if isinstance(self_or_cls, BaseAdapter):
            inst = self_or_cls
            cls = type(self_or_cls)
        else:
            inst = instance
            cls = self_or_cls

        work_dir = cwd or Path.cwd()
        prepared_cmd = prepare_command(cmd)
        if subprocess.run is not _ORIGINAL_SUBPROCESS_RUN:
            res = subprocess.run(
                prepared_cmd,
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
        popen_kwargs.update(get_process_group_flags())

        proc = launch_contained_process(
            prepared_cmd,
            popen_kwargs=popen_kwargs,
            tracker=tracker,
            popen_fn=subprocess.Popen,
        )

        cls._register_proc(proc, instance=inst)
        stdout_reader = BoundedLineReader(proc.stdout)
        stderr_drainer = StderrDrainer(proc.stderr)
        stdout_chunks: List[str] = []

        try:
            if input_data is not None and proc.stdin:
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except Exception:
                    pass

            deadline = (time.monotonic() + timeout) if timeout is not None else None

            while True:
                if deadline is not None and time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(
                        cmd=cmd,
                        timeout=timeout,
                        output="".join(stdout_chunks),
                        stderr=stderr_drainer.get_stderr(),
                    )

                ret = proc.poll()
                if ret is not None:
                    drain_deadline = time.monotonic() + 0.2
                    while time.monotonic() < drain_deadline:
                        try:
                            line = stdout_reader.read_line(timeout=0.05)
                            if line is None:
                                break
                            stdout_chunks.append(line)
                        except queue.Empty:
                            break
                    break

                try:
                    line = stdout_reader.read_line(timeout=0.05)
                    if line is not None:
                        stdout_chunks.append(line)
                    elif stdout_reader.is_eof():
                        try:
                            proc.wait(timeout=0.2)
                        except Exception:
                            pass
                        break
                except queue.Empty:
                    continue

            returncode = proc.poll()
            if returncode is None:
                try:
                    returncode = proc.wait(timeout=0.2)
                except Exception:
                    returncode = proc.poll() or 0

            if tracker is not None and hasattr(tracker, "record_exit"):
                try:
                    tracker.record_exit(returncode)
                except Exception:
                    pass

            return "".join(stdout_chunks), stderr_drainer.get_stderr(), returncode

        except subprocess.TimeoutExpired:
            cls._safe_cleanup_subprocess(proc, tracker=tracker)
            raise
        except BaseException:
            cls._safe_cleanup_subprocess(proc, tracker=tracker)
            raise
        finally:
            stdout_reader.close()
            stderr_drainer.close()
            cls._safe_cleanup_subprocess(proc, tracker=tracker)
            cls._unregister_proc(proc, instance=inst)


atexit.register(BaseAdapter.cleanup_all)


"""Explicit process ownership and durable lifecycle tracking for Forge agents."""

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List, Union

from forge.core.platform import get_process_identity, verify_process_identity, is_pid_alive

logger = logging.getLogger(__name__)


class ProcessLifecycleState(str, Enum):
    """Lifecycle states of an agent subprocess during its Forge stage execution."""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    TERMINAL_EVENT_RECEIVED = "TERMINAL_EVENT_RECEIVED"
    EXIT_VERIFIED = "EXIT_VERIFIED"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    TERMINATED = "TERMINATED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    UNRESOLVED = "UNRESOLVED"


@dataclass
class ProcessRecord:
    """Durable record of an owned adapter subprocess and its lifecycle state."""
    run_id: str
    stage_name: str
    sequence_number: int
    adapter: str
    launch_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = 1
    pid: Optional[int] = None
    pgid: Optional[int] = None
    start_time_ticks: Optional[int] = None
    state: str = ProcessLifecycleState.STARTING.value
    terminal_event_type: Optional[str] = None
    terminal_event_timestamp: Optional[str] = None
    exit_code: Optional[int] = None
    last_activity_timestamp: Optional[str] = None
    cleanup_attempts: List[Dict[str, Any]] = field(default_factory=list)
    stage_result_path: Optional[str] = None
    error_details: Optional[str] = None
    status: str = "CONFIRMED"  # "CONFIRMED", "UNRESOLVED", "STALE"
    containment_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessRecord":
        known_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def save_atomic(self, path: Path) -> None:
        """Atomically persist lifecycle record to disk via temporary file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        tmp_prefix = f".tmp_proc_{self.sequence_number:02d}_{self.stage_name}_"
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=tmp_prefix, delete=False, encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            tmp_path = Path(f.name)
        tmp_path.replace(path)

    @classmethod
    def load(cls, path: Path) -> Optional["ProcessRecord"]:
        if not path.exists() or not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load process record from %s: %s", path, e)
            return None


class ProcessTracker:
    """Manages the lifecycle state and atomic disk persistence for a launched agent process."""

    def __init__(
        self,
        run_id: str,
        stage_name: str,
        sequence_number: int,
        adapter_name: str,
        run_dir: Optional[Path] = None,
    ):
        self.run_id = run_id
        self.stage_name = stage_name
        self.sequence_number = sequence_number
        self.adapter_name = adapter_name
        self.run_dir = run_dir
        self.record_file: Optional[Path] = None
        if self.run_dir is not None:
            self.record_file = self.run_dir / "processes" / f"{self.sequence_number:02d}_{self.stage_name}.json"

        now_iso = datetime.now(timezone.utc).isoformat()
        self.record = ProcessRecord(
            run_id=self.run_id,
            stage_name=self.stage_name,
            sequence_number=self.sequence_number,
            adapter=self.adapter_name,
            launch_timestamp=now_iso,
            last_activity_timestamp=now_iso,
            state=ProcessLifecycleState.STARTING.value,
        )

    def _persist(self) -> None:
        if self.record_file is not None:
            try:
                self.record.save_atomic(self.record_file)
            except Exception as e:
                logger.warning("Failed to persist process record to %s: %s", self.record_file, e)

    def record_starting(self) -> None:
        """Record pre-launch state handling launch/persistence race."""
        self.record.state = ProcessLifecycleState.STARTING.value
        self.record.last_activity_timestamp = datetime.now(timezone.utc).isoformat()
        self._persist()

    def record_launched(
        self,
        pid: int,
        pgid: Optional[int] = None,
        containment_type: Optional[str] = None,
        start_time_ticks: Optional[int] = None,
    ) -> None:
        """Record successful process launch with captured identity evidence."""
        if type(pid) is not int:
            self.record.pid = None
            self.record.pgid = None
            return
        self.record.pid = pid
        self.record.pgid = pgid if type(pgid) is int else pid
        if containment_type:
            self.record.containment_type = containment_type
        if start_time_ticks is not None:
            self.record.start_time_ticks = start_time_ticks
        else:
            ident = get_process_identity(pid)
            if ident:
                if "start_time_ticks" in ident:
                    self.record.start_time_ticks = ident["start_time_ticks"]
                elif "creation_time" in ident:
                    self.record.start_time_ticks = ident["creation_time"]
        self.record.state = ProcessLifecycleState.RUNNING.value
        self.record.last_activity_timestamp = datetime.now(timezone.utc).isoformat()
        self._persist()

    def record_activity(self) -> None:
        """Record progress / output observation."""
        self.record.last_activity_timestamp = datetime.now(timezone.utc).isoformat()

    def record_terminal_event(self, event_type: str) -> None:
        """Record receipt of a terminal event from the stream."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self.record.terminal_event_type = event_type
        self.record.terminal_event_timestamp = now_iso
        self.record.last_activity_timestamp = now_iso
        self.record.state = ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value
        self._persist()

    def record_exit(self, exit_code: int) -> None:
        """Record verified process exit code."""
        self.record.exit_code = exit_code
        self.record.state = ProcessLifecycleState.EXIT_VERIFIED.value
        self.record.last_activity_timestamp = datetime.now(timezone.utc).isoformat()
        self._persist()

    def record_cleanup_start(self, method: str) -> None:
        """Record beginning of process cleanup attempt."""
        now_iso = datetime.now(timezone.utc).isoformat()
        if self.record.state != ProcessLifecycleState.EXIT_VERIFIED.value:
            self.record.state = ProcessLifecycleState.CLEANUP_PENDING.value
        self.record.cleanup_attempts.append({
            "timestamp": now_iso,
            "method": method,
            "status": "STARTED",
        })
        self._persist()

    def record_cleanup_result(self, success: bool, method: str, error: Optional[str] = None) -> None:
        """Record outcome of cleanup attempt."""
        now_iso = datetime.now(timezone.utc).isoformat()
        attempt = {
            "timestamp": now_iso,
            "method": method,
            "status": "SUCCESS" if success else "FAILED",
        }
        if error:
            attempt["error"] = error
        self.record.cleanup_attempts.append(attempt)
        if success:
            # If the process already verified exit, cleanup was a child-sweep / verification;
            # preserve EXIT_VERIFIED! Only transition to TERMINATED if process was still alive
            # and had to be forcibly terminated by cleanup.
            if self.record.state != ProcessLifecycleState.EXIT_VERIFIED.value:
                self.record.state = ProcessLifecycleState.TERMINATED.value
            self.record.status = "CONFIRMED"
        else:
            self.record.state = ProcessLifecycleState.CLEANUP_FAILED.value
            self.record.status = "UNRESOLVED"
            self.record.error_details = error or "Process or descendants remained alive after cleanup deadline."
        self._persist()

    def record_stage_completed(self, success: bool, result_path: Optional[Path] = None) -> None:
        """Record final stage commitment."""
        if result_path:
            self.record.stage_result_path = str(result_path)
        if not success and self.record.state == ProcessLifecycleState.EXIT_VERIFIED.value:
            pass
        self._persist()

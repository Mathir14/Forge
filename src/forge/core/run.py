"""Run entity representing the persistent history of a task execution."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import json
import tempfile
import time


@dataclass
class Run:
    run_id: str
    task: str
    run_dir: Path
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "IN_PROGRESS"  # "IN_PROGRESS", "APPROVED", "FAILED", "BLOCKED"
    prompt_hashes: Dict[str, str] = field(default_factory=dict)
    adapters_used: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    _initial_task: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._initial_task is None:
            if "\n\n### Auto-Repair Feedback" in self.task:
                self._initial_task = self.task.split("\n\n### Auto-Repair Feedback")[0]
            else:
                self._initial_task = self.task

    @property
    def metadata_file(self) -> Path:
        return self.run_dir / "metadata.json"

    @property
    def summary(self) -> Optional["RunSummary"]:
        from forge.core.summary import RunSummary
        if "summary" in self.metadata and isinstance(self.metadata["summary"], dict):
            return RunSummary.from_dict(self.metadata["summary"])
        return None

    @summary.setter
    def summary(self, val: Optional["RunSummary"]) -> None:
        if val is None:
            self.metadata.pop("summary", None)
        else:
            self.metadata["summary"] = val.to_dict()

    def get_or_create_summary(self, planned_stages: Optional[List[str]] = None) -> "RunSummary":
        from forge.core.summary import RunSummary
        s = self.summary
        if s is None:
            s = RunSummary(run_id=self.run_id)
            if planned_stages:
                for name in planned_stages:
                    s.record_stage(name=name, status="—", execution_state="NOT REACHED")
            self.summary = s
            self.save_metadata()
        elif planned_stages:
            for name in planned_stages:
                if not s.get_stage(name):
                    s.record_stage(name=name, status="—", execution_state="NOT REACHED")
            self.summary = s
            self.save_metadata()
        return s

    def lock(self) -> Any:
        """Obtain a RunLock instance for this run."""
        from forge.storage.run_lock import RunLock
        return RunLock(run_dir=self.run_dir, run_id=self.run_id)

    def save_metadata(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "run_id": self.run_id,
            "task": self._initial_task or self.task,
            "created_at": self.created_at,
            "status": self.status,
            "prompt_hashes": self.prompt_hashes,
            "adapters_used": self.adapters_used,
            "metadata": self.metadata,
        }
        with tempfile.NamedTemporaryFile(
            "w", dir=self.run_dir, prefix=".tmp_meta_", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f, indent=2)
            tmp_path = Path(f.name)
        # Retry loop for atomic replace on Windows where concurrent readers/writers
        # may transiently lock the target file.
        max_retries = 10
        for attempt in range(max_retries):
            try:
                tmp_path.replace(self.metadata_file)
                break
            except OSError:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))

    @classmethod
    def load(cls, run_dir: Path) -> "Run":
        meta_path = run_dir / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No metadata.json found in {run_dir}")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Corrupted metadata in {meta_path}: {e}") from e
        raw_task = data.get("task", "")
        if "\n\n### Auto-Repair Feedback" in raw_task:
            clean_task = raw_task.split("\n\n### Auto-Repair Feedback")[0]
        else:
            clean_task = raw_task
        return cls(
            run_id=data.get("run_id", run_dir.name),
            task=clean_task,
            run_dir=run_dir,
            created_at=data.get("created_at", ""),
            status=data.get("status", "UNKNOWN"),
            prompt_hashes=data.get("prompt_hashes", {}),
            adapters_used=data.get("adapters_used", {}),
            metadata=data.get("metadata", {}),
            _initial_task=clean_task,
        )

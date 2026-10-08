"""Run entity representing the persistent history of a task execution."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import hashlib
import json
import re
import shutil
import tempfile
import time


def compute_task_fingerprint(task: Optional[str]) -> str:
    """Compute deterministic SHA-256 fingerprint for a task string,
    treating user task text as completely opaque data."""
    if not task:
        return ""
    clean = task.strip()
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


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
    repair_feedback: Optional[str] = None
    _initial_task: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._initial_task is None:
            self._initial_task = self.task

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "task" and value is not None:
            old_task = getattr(self, "task", None)
            super().__setattr__("task", value)
            super().__setattr__("_initial_task", value)
            if old_task is not None:
                old_fp = compute_task_fingerprint(old_task)
                new_fp = compute_task_fingerprint(value)
                if old_fp and new_fp and old_fp != new_fp:
                    self._archive_stale_artifacts(old_fp)
            return
        super().__setattr__(name, value)

    def set_auto_repair_feedback(self, feedback: Optional[str]) -> None:
        """Explicit internal API to record auto-repair feedback without mutating canonical user task."""
        self.repair_feedback = feedback

    @property
    def auto_repair_feedback(self) -> Optional[str]:
        return self.repair_feedback

    @property
    def task_fingerprint(self) -> str:
        return compute_task_fingerprint(self.task)

    def _archive_stale_artifacts(self, old_task_fingerprint: str) -> None:
        """Archive existing stage deliverables, proposals, evidence, and debug state
        when task identity changes, isolating historical generations."""
        if not hasattr(self, "run_dir") or not self.run_dir.exists():
            return

        base_history_dir = self.run_dir / "history" / f"task_{old_task_fingerprint[:8]}"
        history_dir = base_history_dir
        idx = 1
        while history_dir.exists():
            history_dir = self.run_dir / "history" / f"task_{old_task_fingerprint[:8]}_{idx}"
            idx += 1

        history_dir.mkdir(parents=True, exist_ok=True)

        stage_file_pattern = re.compile(r"^(\d+_[a-zA-Z0-9_\-]+?)(?:_attempt_\d+)?\.(json|md)$")
        archived_files: List[str] = []
        archived_dirs: List[str] = []

        # Archive task-scoped subdirectories
        for dname in ("knowledge_proposals", "evidence", "debug"):
            src_dir = self.run_dir / dname
            if src_dir.exists() and src_dir.is_dir():
                dest_dir = history_dir / dname
                try:
                    if dest_dir.exists():
                        shutil.rmtree(dest_dir, ignore_errors=True)
                    shutil.move(str(src_dir), str(dest_dir))
                    archived_dirs.append(dname)
                except Exception:
                    pass

        # Archive stage deliverable files and task-scoped files
        try:
            for p in list(self.run_dir.iterdir()):
                if p.is_file():
                    if stage_file_pattern.match(p.name) or p.name == "git_baseline.json":
                        dest = history_dir / p.name
                        shutil.move(str(p), str(dest))
                        archived_files.append(p.name)
        except Exception:
            pass

        # Reset execution tracking and stage fingerprints in metadata
        if "summary" in self.metadata:
            self.metadata.pop("summary", None)
        self.metadata["stages_executed"] = []
        self.metadata["auto_committed"] = False
        self.metadata["stage_fingerprints"] = {}
        if hasattr(self, "prompt_hashes") and isinstance(self.prompt_hashes, dict):
            self.prompt_hashes.clear()
        self.status = "PENDING"

        history_meta = self.metadata.setdefault("task_history", [])
        history_meta.append({
            "old_task_fingerprint": old_task_fingerprint,
            "archived_files": archived_files,
            "archived_directories": archived_dirs,
            "archived_to": str(history_dir.name),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        })

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
            "task": self.task,
            "task_fingerprint": self.task_fingerprint,
            "created_at": self.created_at,
            "status": self.status,
            "prompt_hashes": self.prompt_hashes,
            "adapters_used": self.adapters_used,
            "metadata": self.metadata,
        }
        if self.repair_feedback:
            data["repair_feedback"] = self.repair_feedback
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
        task_text = data.get("task", "")
        repair_fb = data.get("repair_feedback")
        return cls(
            run_id=data.get("run_id", run_dir.name),
            task=task_text,
            run_dir=run_dir,
            created_at=data.get("created_at", ""),
            status=data.get("status", "UNKNOWN"),
            prompt_hashes=data.get("prompt_hashes", {}),
            adapters_used=data.get("adapters_used", {}),
            metadata=data.get("metadata", {}),
            repair_feedback=repair_fb,
            _initial_task=task_text,
        )

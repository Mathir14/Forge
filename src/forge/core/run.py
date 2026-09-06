"""Run entity representing the persistent history of a task execution."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import json


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

    @property
    def metadata_file(self) -> Path:
        return self.run_dir / "metadata.json"

    def save_metadata(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "run_id": self.run_id,
            "task": self.task,
            "created_at": self.created_at,
            "status": self.status,
            "prompt_hashes": self.prompt_hashes,
            "adapters_used": self.adapters_used,
            "metadata": self.metadata,
        }
        with open(self.metadata_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, run_dir: Path) -> "Run":
        meta_path = run_dir / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"No metadata.json found in {run_dir}")
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            run_id=data.get("run_id", run_dir.name),
            task=data.get("task", ""),
            run_dir=run_dir,
            created_at=data.get("created_at", ""),
            status=data.get("status", "UNKNOWN"),
            prompt_hashes=data.get("prompt_hashes", {}),
            adapters_used=data.get("adapters_used", {}),
            metadata=data.get("metadata", {}),
        )

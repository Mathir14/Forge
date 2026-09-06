"""Storage manager for Forge runs and stage artifacts."""

import json
import re
import shutil
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from forge.core.run import Run


class RunManager:
    RUN_DIR_PATTERN = re.compile(r"^run-(\d+)$")

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path.cwd()
        self.forge_dir = self.project_root / ".forge"
        self.runs_dir = self.forge_dir / "runs"

    def _ensure_dirs(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def list_runs(self) -> List[Run]:
        """List all runs ordered chronologically."""
        self._ensure_dirs()
        runs = []
        for p in sorted(self.runs_dir.iterdir()):
            if p.is_dir() and self.RUN_DIR_PATTERN.match(p.name):
                try:
                    runs.append(Run.load(p))
                except Exception:
                    continue
        return runs

    def latest(self) -> Optional[Run]:
        """Get the most recent run, if any exists."""
        runs = self.list_runs()
        return runs[-1] if runs else None

    def create_run(self, task: str) -> Run:
        """Create a new sequentially numbered run directory (run-001, run-002, etc.)."""
        self._ensure_dirs()
        runs = self.list_runs()
        next_num = 1
        if runs:
            last_name = runs[-1].run_dir.name
            m = self.RUN_DIR_PATTERN.match(last_name)
            if m:
                next_num = int(m.group(1)) + 1

        run_id = f"run-{next_num:03d}"
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        run = Run(run_id=run_id, task=task, run_dir=run_dir)
        run.save_metadata()
        return run

    def resume(self, run_id: Optional[str] = None) -> Run:
        """Resume an existing run by ID or the latest run."""
        self._ensure_dirs()
        if run_id:
            run_dir = self.runs_dir / run_id
            if not run_dir.exists():
                raise FileNotFoundError(f"Run {run_id} not found in {self.runs_dir}")
            return Run.load(run_dir)

        latest_run = self.latest()
        if not latest_run:
            raise FileNotFoundError("No existing runs found to resume.")
        return latest_run

    def delete_run(self, run_id: str) -> bool:
        """Delete a run directory by ID."""
        run_dir = self.runs_dir / run_id
        if run_dir.exists() and run_dir.is_dir():
            shutil.rmtree(run_dir)
            return True
        return False

    def save_stage_artifacts(
        self,
        run: Run,
        sequence_number: int,
        role_name: str,
        markdown_content: str,
        json_data: Dict[str, Any],
        prompt_hash: Optional[str] = None,
        adapter_name: Optional[str] = None,
    ) -> Tuple[Path, Path]:
        """Save paired 0X_role.md and 0X_role.json files."""
        run.run_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{sequence_number:02d}_{role_name}"
        md_file = run.run_dir / f"{prefix}.md"
        json_file = run.run_dir / f"{prefix}.json"

        with open(md_file, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)

        if prompt_hash:
            run.prompt_hashes[role_name] = prompt_hash
        if adapter_name:
            run.adapters_used[role_name] = adapter_name

        run.save_metadata()
        return md_file, json_file

    def load_stage_json(self, run: Run, role_name: str) -> Optional[Dict[str, Any]]:
        """Load JSON report for a given role from a run."""
        for p in run.run_dir.glob(f"*_{role_name}.json"):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def load_stage_markdown(self, run: Run, role_name: str) -> Optional[str]:
        """Load Markdown report for a given role from a run."""
        for p in run.run_dir.glob(f"*_{role_name}.md"):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
        return None

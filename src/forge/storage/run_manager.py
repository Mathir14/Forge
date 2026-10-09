"""Storage manager for Forge runs and stage artifacts."""

import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from forge.core.run import Run


class RunManager:
    RUN_DIR_PATTERN = re.compile(r"^run-(\d+)$")
    TEMP_DIR_STALE_SECONDS = 300

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or Path.cwd()
        self.forge_dir = self.project_root / ".forge"
        self.runs_dir = self.forge_dir / "runs"

    def _ensure_dirs(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _cleanup_orphaned_temp_dirs(self) -> None:
        """Remove stale temp run dirs left over from crashed processes."""
        try:
            cutoff = time.time() - self.TEMP_DIR_STALE_SECONDS
            for p in self.runs_dir.glob(".tmp_run_*"):
                if not p.is_dir():
                    continue
                try:
                    if p.stat().st_mtime < cutoff:
                        shutil.rmtree(p, ignore_errors=True)
                        logging.warning("Removed orphaned temp run dir %s", p)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as e:
                    logging.warning("Failed to clean orphaned temp dir %s: %s", p, e)
        except Exception as e:
            logging.warning("Error during temp dir cleanup: %s", e)

    def _run_sort_key(self, p: Path) -> int:
        m = self.RUN_DIR_PATTERN.match(p.name)
        return int(m.group(1)) if m else -1

    def _get_max_run_num(self) -> int:
        """Find highest run number by scanning directory names."""
        max_num = 0
        if not self.runs_dir.exists():
            return 0
        for p in self.runs_dir.iterdir():
            if p.is_dir():
                m = self.RUN_DIR_PATTERN.match(p.name)
                if m:
                    max_num = max(max_num, int(m.group(1)))
        return max_num

    def _validate_run_id(self, run_id: str) -> Path:
        """Validate run_id format and ensure resolved path stays within runs_dir."""
        if not isinstance(run_id, str) or not self.RUN_DIR_PATTERN.match(run_id):
            raise ValueError(f"Invalid run ID format '{run_id}'. Expected 'run-XXX'.")
        target = (self.runs_dir / run_id).resolve()
        if not target.is_relative_to(self.runs_dir.resolve()):
            raise PermissionError(f"Access denied: Path traversal detected for '{run_id}'.")
        return target

    def count_runs(self) -> int:
        """Count total run directories without loading metadata files."""
        if not self.runs_dir.exists():
            return 0
        return sum(
            1 for p in self.runs_dir.iterdir()
            if p.is_dir() and self.RUN_DIR_PATTERN.match(p.name)
        )

    def list_runs(self, limit: Optional[int] = None) -> List[Run]:
        """List all runs ordered chronologically, optionally limited to the most recent."""
        self._ensure_dirs()
        runs = []
        matching_dirs = [
            p for p in self.runs_dir.iterdir()
            if p.is_dir() and self.RUN_DIR_PATTERN.match(p.name)
        ]
        matching_dirs.sort(key=self._run_sort_key)
        if limit is not None and limit > 0:
            matching_dirs = matching_dirs[-limit:]
        for p in matching_dirs:
            try:
                runs.append(Run.load(p))
            except Exception:
                continue
        return runs

    def latest(self) -> Optional[Run]:
        """Get the most recent run, if any exists."""
        self._ensure_dirs()
        matching_dirs = [
            p for p in self.runs_dir.iterdir()
            if p.is_dir() and self.RUN_DIR_PATTERN.match(p.name)
        ]
        if not matching_dirs:
            return None
        matching_dirs.sort(key=self._run_sort_key)
        for p in reversed(matching_dirs):
            try:
                return Run.load(p)
            except Exception:
                continue
        return None

    def create_run(self, task: str) -> Run:
        """Create a new sequentially numbered run directory (run-001, run-002, etc.) atomically."""
        self._ensure_dirs()
        self._cleanup_orphaned_temp_dirs()
        while True:
            next_num = self._get_max_run_num() + 1
            run_id = f"run-{next_num:03d}"
            target_dir = self.runs_dir / run_id

            if target_dir.exists():
                continue

            temp_dir = Path(tempfile.mkdtemp(prefix=".tmp_run_", dir=self.runs_dir))
            try:
                run = Run(run_id=run_id, task=task, run_dir=temp_dir)
                run.save_metadata()
                if target_dir.exists():
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    continue
                temp_dir.rename(target_dir)
                run.run_dir = target_dir
                return run
            except (FileExistsError, OSError):
                shutil.rmtree(temp_dir, ignore_errors=True)
                continue

    def resume(self, run_id: Optional[str] = None) -> Run:
        """Resume an existing run by ID or the latest run."""
        self._ensure_dirs()
        if run_id:
            run_dir = self._validate_run_id(run_id)
            if not run_dir.exists():
                raise FileNotFoundError(f"Run {run_id} not found in {self.runs_dir}")
            return Run.load(run_dir)

        latest_run = self.latest()
        if not latest_run:
            raise FileNotFoundError("No existing runs found to resume.")
        return latest_run

    def delete_run(self, run_id: str) -> bool:
        """Delete a run directory by ID."""
        self._ensure_dirs()
        run_dir = self._validate_run_id(run_id)
        if run_dir.exists() and run_dir.is_dir():
            from forge.storage.run_lock import RunLock, RunOwnershipError
            if RunLock.is_run_locked(run_dir):
                owner = RunLock.read_owner_metadata(run_dir / RunLock.LOCK_FILE_NAME)
                raise RunOwnershipError(
                    f"Cannot delete run '{run_id}': run is currently owned by an active Forge process.",
                    run_id=run_id,
                    owner_info=owner,
                    lock_file=run_dir / RunLock.LOCK_FILE_NAME,
                )
            shutil.rmtree(run_dir)
            return True
        return False

    def acquire_run_lock(self, run: Run) -> Any:
        """Obtain a RunLock instance for the given run."""
        from forge.storage.run_lock import RunLock
        return RunLock(run_dir=run.run_dir, run_id=run.run_id)

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
        """Save paired 0X_role.md and 0X_role.json files atomically."""
        run.run_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{sequence_number:02d}_{role_name}"
        md_file = run.run_dir / f"{prefix}.md"
        json_file = run.run_dir / f"{prefix}.json"

        with tempfile.NamedTemporaryFile(
            "w", dir=run.run_dir, prefix=f".tmp_{prefix}_md_", delete=False, encoding="utf-8"
        ) as f:
            f.write(markdown_content)
            tmp_md = Path(f.name)
        tmp_md.replace(md_file)

        json_payload = dict(json_data)
        if hasattr(run, "task_fingerprint") and run.task_fingerprint:
            json_payload["task_fingerprint"] = run.task_fingerprint
            if "stage_fingerprints" not in run.metadata:
                run.metadata["stage_fingerprints"] = {}
            run.metadata["stage_fingerprints"][role_name] = run.task_fingerprint

        json_payload["role"] = role_name
        json_payload["sequence_number"] = sequence_number
        if hasattr(run, "run_id") and run.run_id:
            json_payload["run_id"] = str(run.run_id)

        with tempfile.NamedTemporaryFile(
            "w", dir=run.run_dir, prefix=f".tmp_{prefix}_json_", delete=False, encoding="utf-8"
        ) as f:
            json.dump(json_payload, f, indent=2)
            tmp_json = Path(f.name)
        tmp_json.replace(json_file)

        if prompt_hash:
            run.prompt_hashes[role_name] = prompt_hash
        if adapter_name:
            run.adapters_used[role_name] = adapter_name

        run.save_metadata()
        return md_file, json_file

    def archive_stale_artifacts(self, run: Run, old_task: Optional[str] = None) -> None:
        """Archive existing stage deliverables and reset execution state when task changes."""
        from forge.core.run import compute_task_fingerprint
        old_fp = compute_task_fingerprint(old_task) if old_task else run.task_fingerprint
        run._archive_stale_artifacts(old_fp)
        run.save_metadata()

    def recover_stage_from_session(
        self,
        run: Run,
        stage_name: str,
        session_id: str,
        adapter: Optional[Any] = None,
        sequence_number: Optional[int] = None,
    ) -> Optional[Any]:
        """Recover and reconstruct a completed stage result from an external daemon session.

        Retrieves the authoritative session output, runs it through normal MachineReportParser
        and MachineReportValidator, writes the canonical stage artifacts, and updates run metadata.
        """
        from forge.adapters.opencode import OpenCodeAdapter
        from forge.protocol.parser import MachineReportParser
        from forge.protocol.validator import MachineReportValidator
        from forge.stages.result import StageResult

        adapter_inst = adapter or OpenCodeAdapter()
        recovered = adapter_inst.recover_session(
            session_id=session_id,
            cwd=self.project_root,
        )
        if not recovered:
            return None

        event, response = recovered
        raw_text = response.stdout or ""
        raw_dict, raw_yaml = MachineReportParser.extract_yaml(raw_text, expected_role=stage_name)
        report = MachineReportValidator.validate(
            data=raw_dict,
            expected_role=stage_name,
            raw_yaml=raw_yaml,
        )

        from forge.core.role import Role
        from forge.prompts.rendered_prompt import RenderedPrompt
        from forge.stages.definition import StageOrder

        stage_def = StageOrder.resolve_definition(stage_name)
        seq = sequence_number if sequence_number is not None else (stage_def.sequence_number if stage_def else 1)
        role = Role(
            name=stage_name,
            sequence_number=seq,
            template_content="",
            protocol_content="",
        )
        rendered_prompt = RenderedPrompt.from_text("")

        is_success = (
            response.exit_code == 0
            and report.is_valid
            and report.status not in ("REJECTED", "FAILED", "BLOCKED", "CHANGES_REQUIRED")
        )

        stage_result = StageResult(
            role=role,
            prompt=rendered_prompt,
            response=response,
            machine_report=report,
            raw_markdown=raw_text,
            duration_seconds=response.duration_seconds,
            success=is_success,
        )

        if stage_result.success:
            self.save_stage_artifacts(
                run=run,
                sequence_number=seq,
                role_name=stage_name,
                markdown_content=raw_text,
                json_data=stage_result.to_dict(),
                adapter_name=getattr(adapter_inst, "name", "opencode"),
            )

        return stage_result

    def load_stage_json(self, run: Run, role_name: Any, sequence_number: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Load JSON report for a given role or StageDefinition from a run, optionally matching exact sequence number."""
        if hasattr(role_name, "name"):
            if sequence_number is None and hasattr(role_name, "sequence_number"):
                sequence_number = role_name.sequence_number
            role_name = role_name.name

        if sequence_number is not None:
            p = run.run_dir / f"{sequence_number:02d}_{role_name}.json"
            if p.exists() and p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logging.warning("Failed to load stage JSON %s: %s", p, e)
            return None

        matching = sorted(run.run_dir.glob(f"*_{role_name}.json"))
        for p in matching:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logging.warning("Failed to load stage JSON %s: %s", p, e)
        return None

    def load_stage_markdown(self, run: Run, role_name: Any, sequence_number: Optional[int] = None) -> Optional[str]:
        """Load Markdown report for a given role or StageDefinition from a run, optionally matching exact sequence number."""
        if hasattr(role_name, "name"):
            if sequence_number is None and hasattr(role_name, "sequence_number"):
                sequence_number = role_name.sequence_number
            role_name = role_name.name

        if sequence_number is not None:
            p = run.run_dir / f"{sequence_number:02d}_{role_name}.md"
            if p.exists() and p.is_file():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception as e:
                    logging.warning("Failed to load stage markdown %s: %s", p, e)
            return None

        matching = sorted(run.run_dir.glob(f"*_{role_name}.md"))
        for p in matching:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logging.warning("Failed to load stage markdown %s: %s", p, e)
        return None

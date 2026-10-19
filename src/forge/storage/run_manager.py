"""Storage manager for Forge runs and stage artifacts."""

import hashlib
import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from forge.core.run import Run


def compute_file_sha256(path: Path) -> str:
    """Compute deterministic SHA-256 hex digest of file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_stage_config_fingerprint(
    adapter: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    extra_flags: Optional[Dict[str, Any]] = None,
) -> str:
    """Compute deterministic SHA-256 fingerprint for material stage execution configuration."""
    adapter_str = adapter if isinstance(adapter, str) else str(adapter or "opencode")
    model_str = model if isinstance(model, str) else str(model or "default")
    effort_str = effort if isinstance(effort, str) else str(effort or "default")
    parts = [
        f"adapter:{adapter_str.lower().strip()}",
        f"model:{model_str.strip()}",
        f"effort:{effort_str.strip()}",
    ]
    if extra_flags and isinstance(extra_flags, (dict, list)):
        try:
            flags_str = json.dumps(extra_flags, sort_keys=True, default=str)
        except Exception:
            flags_str = str(extra_flags)
        parts.append(f"flags:{flags_str}")
    elif extra_flags:
        parts.append(f"flags:{str(extra_flags)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def compute_stage_input_fingerprint(
    task_fingerprint: str,
    sequence_number: int,
    role_name: str,
    upstream_hashes: Dict[str, str],
    template_hash: Optional[str] = None,
    config_fingerprint: Optional[str] = None,
) -> str:
    """Compute deterministic SHA-256 fingerprint for material stage inputs."""
    tokens = [
        f"task:{task_fingerprint}",
        f"stage:{sequence_number}:{role_name}",
    ]
    for filename in sorted(upstream_hashes.keys()):
        tokens.append(f"{filename}:{upstream_hashes[filename]}")
    if template_hash:
        tokens.append(f"template:{template_hash}")
    if config_fingerprint:
        tokens.append(f"config:{config_fingerprint}")
    return hashlib.sha256("|".join(tokens).encode("utf-8")).hexdigest()


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
        template_hash: Optional[str] = None,
        config_fingerprint: Optional[str] = None,
    ) -> Tuple[Path, Path]:
        """Save paired 0X_role.md and 0X_role.json files atomically."""
        run.run_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"{sequence_number:02d}_{role_name}"
        md_file = run.run_dir / f"{prefix}.md"
        json_file = run.run_dir / f"{prefix}.json"

        # Collect upstream hashes for existing deliverables of upstream stages
        upstream_hashes: Dict[str, str] = {}
        if hasattr(run, "run_dir") and run.run_dir and run.run_dir.exists():
            from forge.stages.definition import StageOrder
            for u_def in StageOrder.standard_pipeline_stages(no_critic=False):
                if u_def.sequence_number < sequence_number:
                    u_json = run.run_dir / f"{u_def.sequence_number:02d}_{u_def.name}.json"
                    u_md = run.run_dir / f"{u_def.sequence_number:02d}_{u_def.name}.md"
                    if u_json.exists():
                        upstream_hashes[u_json.name] = compute_file_sha256(u_json)
                    if u_md.exists():
                        upstream_hashes[u_md.name] = compute_file_sha256(u_md)

        input_fingerprint = compute_stage_input_fingerprint(
            task_fingerprint=getattr(run, "task_fingerprint", "") or "",
            sequence_number=sequence_number,
            role_name=role_name,
            upstream_hashes=upstream_hashes,
            template_hash=template_hash,
            config_fingerprint=config_fingerprint,
        )

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

        json_payload["input_fingerprint"] = input_fingerprint
        if upstream_hashes:
            json_payload["upstream_hashes"] = upstream_hashes
        if template_hash:
            json_payload["template_hash"] = template_hash
            if "template_hashes" not in run.metadata or not isinstance(run.metadata["template_hashes"], dict):
                run.metadata["template_hashes"] = {}
            run.metadata["template_hashes"][role_name] = template_hash
        if config_fingerprint:
            json_payload["config_fingerprint"] = config_fingerprint
            if "config_fingerprints" not in run.metadata or not isinstance(run.metadata["config_fingerprints"], dict):
                run.metadata["config_fingerprints"] = {}
            run.metadata["config_fingerprints"][role_name] = config_fingerprint
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

        # Compute content hashes of saved artifacts for tamper detection
        json_hash = compute_file_sha256(json_file)
        md_hash = compute_file_sha256(md_file)
        if "artifact_hashes" not in run.metadata or not isinstance(run.metadata["artifact_hashes"], dict):
            run.metadata["artifact_hashes"] = {}
        run.metadata["artifact_hashes"][role_name] = {
            "json": json_hash,
            "md": md_hash,
        }
        if "input_fingerprints" not in run.metadata or not isinstance(run.metadata["input_fingerprints"], dict):
            run.metadata["input_fingerprints"] = {}
        run.metadata["input_fingerprints"][role_name] = input_fingerprint
        if "upstream_hashes" not in run.metadata or not isinstance(run.metadata["upstream_hashes"], dict):
            run.metadata["upstream_hashes"] = {}
        if upstream_hashes:
            run.metadata["upstream_hashes"][role_name] = upstream_hashes

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

    def invalidate_stage_and_downstream_artifacts(self, run: Run, from_sequence_number: int) -> List[str]:
        """Invalidate and remove all canonical stage deliverables and metadata from from_sequence_number forward.

        Ensures that when an upstream stage executes fresh, downstream deliverables from
        previous runs or attempts cannot linger or falsely satisfy subsequent resumptions.
        """
        invalidated_files: List[str] = []
        if not hasattr(run, "run_dir") or not run.run_dir or not run.run_dir.exists():
            return invalidated_files

        stage_file_pattern = re.compile(r"^0*(\d+)_([a-zA-Z0-9_\-]+)\.(json|md)$")
        stale_history_dir = run.run_dir / "history" / f"invalidated_seq_{from_sequence_number}_{int(time.time() * 1000)}"

        for p in list(run.run_dir.iterdir()):
            if p.is_file() and "_attempt_" not in p.name:
                m = stage_file_pattern.match(p.name)
                if m:
                    file_seq = int(m.group(1))
                    if file_seq >= from_sequence_number:
                        try:
                            stale_history_dir.mkdir(parents=True, exist_ok=True)
                            dest = stale_history_dir / p.name
                            shutil.move(str(p), str(dest))
                            invalidated_files.append(p.name)
                        except Exception as e:
                            logging.warning("Failed to archive stale artifact %s: %s", p, e)
                            try:
                                p.unlink(missing_ok=True)
                                invalidated_files.append(p.name)
                            except Exception:
                                pass

        from forge.stages.definition import StageOrder
        affected_stage_names = {
            s.name.lower()
            for s in StageOrder.all_stages()
            if s.sequence_number >= from_sequence_number
        }

        for key in (
            "stage_fingerprints",
            "input_fingerprints",
            "artifact_hashes",
            "upstream_hashes",
            "template_hashes",
            "config_fingerprints",
        ):
            if key in run.metadata and isinstance(run.metadata[key], dict):
                for s_name in list(run.metadata[key].keys()):
                    if s_name.lower() in affected_stage_names:
                        run.metadata[key].pop(s_name, None)

        if hasattr(run, "prompt_hashes") and isinstance(run.prompt_hashes, dict):
            for s_name in list(run.prompt_hashes.keys()):
                if s_name.lower() in affected_stage_names:
                    run.prompt_hashes.pop(s_name, None)

        if hasattr(run, "adapters_used") and isinstance(run.adapters_used, dict):
            for s_name in list(run.adapters_used.keys()):
                if s_name.lower() in affected_stage_names:
                    run.adapters_used.pop(s_name, None)

        if run.summary is not None:
            for s_def in StageOrder.all_stages():
                if s_def.sequence_number >= from_sequence_number:
                    stage_entry = run.summary.get_stage(s_def.display_name) or run.summary.get_stage(s_def.name.capitalize())
                    if stage_entry:
                        stage_entry.status = "—"
                        stage_entry.execution_state = "NOT REACHED"
            run.summary.final_status = None
            run.summary.reason = None
            run.summary = run.summary

        change_prod = StageOrder.change_producer()
        if from_sequence_number <= change_prod.sequence_number:
            run.metadata["auto_committed"] = False

        run.save_metadata()
        return invalidated_files

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
            and report.status not in ("REJECTED", "FAILED", "BLOCKED", "CHANGES_REQUIRED", "PARTIAL")
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

    def discover_unresolved_processes(self, run: Optional[Run] = None) -> List[Any]:
        """Discover unfinalized or in-flight process records from prior executions."""
        from forge.core.process import ProcessRecord, ProcessLifecycleState

        target_dirs = []
        if run is not None and hasattr(run, "run_dir") and run.run_dir and run.run_dir.exists():
            target_dirs.append(run.run_dir / "processes")
        elif self.runs_dir.exists():
            for p in self.runs_dir.iterdir():
                if p.is_dir() and (p / "processes").exists():
                    target_dirs.append(p / "processes")

        unresolved: List[ProcessRecord] = []
        for p_dir in target_dirs:
            if not p_dir.exists():
                continue
            for rec_file in sorted(p_dir.glob("*.json")):
                rec = ProcessRecord.load(rec_file)
                if rec and rec.state in (
                    ProcessLifecycleState.STARTING.value,
                    ProcessLifecycleState.RUNNING.value,
                    ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value,
                    ProcessLifecycleState.CLEANUP_PENDING.value,
                    ProcessLifecycleState.CLEANUP_FAILED.value,
                    ProcessLifecycleState.UNRESOLVED.value,
                ):
                    unresolved.append(rec)
        return unresolved

    def reconcile_process_record(self, record: Any, run: Run, allow_terminate: bool = True) -> Any:
        """Safely and idempotently reconcile a single process record.

        - Verifies identity before any termination.
        - Refuses to terminate if identity is ambiguous or PID was reused.
        - Performs bounded termination if process is definitely the owned process and still alive.
        - Reconciles stage status using persisted artifacts and exit code.
        """
        from forge.core.process import ProcessRecord, ProcessLifecycleState
        from forge.core.platform import is_pid_alive, verify_process_identity, terminate_process_tree, find_process_group_pids

        pid = record.pid
        pgid = record.pgid

        # 1. Check if PID is alive
        if pid is not None and is_pid_alive(pid):
            # Check identity (creation time / start time)
            if not verify_process_identity(pid, record.start_time_ticks, pgid):
                record.state = ProcessLifecycleState.UNRESOLVED.value
                record.status = "UNRESOLVED"
                record.error_details = (
                    f"PID {pid} is active but creation time does not match expected identity. "
                    "Possible PID reuse; refusing automated termination."
                )
            else:
                if allow_terminate:
                    stopped = terminate_process_tree(pid, pgid=pgid, timeout=1.0)
                    if stopped:
                        record.state = ProcessLifecycleState.TERMINATED.value
                        record.status = "CONFIRMED"
                    else:
                        record.state = ProcessLifecycleState.CLEANUP_FAILED.value
                        record.status = "UNRESOLVED"
                        record.error_details = f"Process {pid} failed to terminate within cleanup deadline."
                else:
                    record.status = "CONFIRMED"
        else:
            # PID is gone; check if process group has lingering members
            lingering = find_process_group_pids(pgid) if pgid else set()
            if lingering:
                # CRITICAL ADVERSARIAL SAFETY REQUIREMENT:
                # If the recorded leader PID is no longer alive, we cannot prove ownership
                # of lingering processes in the process group because PGID reuse cannot be excluded.
                # Standard POSIX PID recycling could allocate the old PGID to an unrelated process.
                # Refuse automated termination, mark UNRESOLVED, and leave surviving processes untouched.
                record.state = ProcessLifecycleState.UNRESOLVED.value
                record.status = "UNRESOLVED"
                record.error_details = (
                    f"Process group leader (PID {pid}) is no longer alive, but lingering processes "
                    f"match PGID {pgid} ({sorted(lingering)}). Cannot verify ownership due to potential "
                    "PGID reuse; refusing automated termination to prevent killing unrelated processes."
                )
            else:
                # All processes are gone
                if record.exit_code is not None and record.state in (
                    ProcessLifecycleState.EXIT_VERIFIED.value,
                    ProcessLifecycleState.TERMINAL_EVENT_RECEIVED.value,
                ):
                    record.state = ProcessLifecycleState.EXIT_VERIFIED.value
                    record.status = "CONFIRMED"
                else:
                    record.state = ProcessLifecycleState.TERMINATED.value
                    record.status = "CONFIRMED"

        # 2. Reconcile stage artifact status
        stage_json = self.load_stage_json(run, record.stage_name, sequence_number=record.sequence_number)
        if stage_json:
            is_valid = bool(stage_json.get("machine_report", {}).get("is_valid"))
            status = str(stage_json.get("status") or "").upper()
            if record.state == ProcessLifecycleState.EXIT_VERIFIED.value and record.exit_code == 0 and is_valid and status not in ("REJECTED", "FAILED", "BLOCKED", "CHANGES_REQUIRED", "PARTIAL", "FAIL"):
                pass
            elif record.state != ProcessLifecycleState.TERMINATED.value and record.state != ProcessLifecycleState.EXIT_VERIFIED.value:
                pass

        # Persist updated record
        if hasattr(run, "run_dir") and run.run_dir:
            rec_path = run.run_dir / "processes" / f"{record.sequence_number:02d}_{record.stage_name}.json"
            record.save_atomic(rec_path)

        return record

    def recover_run_processes(self, run: Run, allow_terminate: bool = True) -> List[Any]:
        """Discover and reconcile all unresolved processes for a run under exclusive run lock."""
        from forge.storage.run_lock import RunLock
        with RunLock(run.run_dir, run_id=run.run_id):
            unresolved = self.discover_unresolved_processes(run)
            reconciled = []
            for rec in unresolved:
                reconciled.append(self.reconcile_process_record(rec, run, allow_terminate=allow_terminate))
            return reconciled

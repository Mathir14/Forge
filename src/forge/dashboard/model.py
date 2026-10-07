"""RunModel: Canonical in-memory representation of Forge runs and stage artifacts.

Provides a clean abstraction between raw disk artifacts / AgentEvents and the UI layer.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set
import json
import re

from forge.protocol.parser import MachineReportParser
from forge.core.events import AgentEvent, AgentEventType
from forge.stages.definition import StageOrder


@dataclass
class MachineReportModel:
    """Structured representation of a parsed Machine Report."""
    role: str = ""
    status: str = "UNKNOWN"
    handoff: Optional[str] = None
    reason: str = ""
    confidence: str = ""
    issues: Dict[str, List[str]] = field(default_factory=dict)
    next_action: str = ""
    raw_yaml: str = ""
    raw_data: Dict[str, Any] = field(default_factory=dict)
    is_valid: bool = False
    validation_errors: List[str] = field(default_factory=list)


@dataclass
class StageAttemptModel:
    """Historical auto-repair attempt for a stage."""
    attempt_number: int
    duration_seconds: float = 0.0
    status: str = "UNKNOWN"
    exit_code: Optional[int] = None
    raw_content: str = ""
    machine_report: Optional[MachineReportModel] = None


@dataclass
class StageModel:
    """Model of a single stage within a Forge run."""
    stage_name: str
    role_name: str
    sequence_number: int
    status: str = "PENDING"
    duration_seconds: float = 0.0
    exit_code: Optional[int] = None
    adapter: Optional[str] = None
    raw_content: str = ""
    human_report: str = ""
    machine_report: Optional[MachineReportModel] = None
    attempts: List[StageAttemptModel] = field(default_factory=list)
    prompt_hash: Optional[str] = None
    is_partial: bool = False
    has_error: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_status(self) -> str:
        if self.status:
            return self.status.upper()
        if self.machine_report and self.machine_report.status:
            return self.machine_report.status.upper()
        return "PENDING"


@dataclass
class RunModel:
    """Canonical in-memory representation of a Forge run."""
    run_id: str
    task: str
    status: str
    created_at: str
    run_dir: Path
    stages: List[StageModel] = field(default_factory=list)
    adapters_used: Dict[str, str] = field(default_factory=dict)
    total_duration_seconds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = False
    console_logs: List[str] = field(default_factory=list)
    active_stage_name: Optional[str] = None
    stage_start_times: Dict[str, float] = field(default_factory=dict)
    summary: Optional[Any] = None

    @property
    def stage_names(self) -> List[str]:
        return [s.stage_name for s in self.stages]

    def get_stage(self, name_or_role: str, sequence_number: Optional[int] = None) -> Optional[StageModel]:
        target = name_or_role.lower().strip()
        if sequence_number is not None:
            for s in self.stages:
                if s.sequence_number == sequence_number:
                    return s
        for s in self.stages:
            if s.stage_name.lower() == target:
                return s
        for s in self.stages:
            if s.role_name.lower() == target:
                return s
        return None

    def apply_event(self, event: AgentEvent) -> None:
        """Process a live AgentEvent and incrementally update in-memory run state."""
        import time
        ts = event.timestamp or time.time()
        ev_data = event.data or {}
        lifecycle = ev_data.get("lifecycle")

        # 1. Lifecycle: stage_start
        if lifecycle == "stage_start":
            s_name = ev_data.get("stage_name") or ev_data.get("role_name")
            seq = ev_data.get("sequence_number")
            if s_name:
                stage = self.get_stage(s_name, sequence_number=seq)
                if not stage:
                    seq_val = seq if seq is not None else len(self.stages) + 1
                    stage = StageModel(
                        stage_name=s_name,
                        role_name=ev_data.get("role_name", s_name),
                        sequence_number=seq_val,
                        status="RUNNING",
                    )
                    self.stages.append(stage)
                    self.stages.sort(key=lambda s: (s.sequence_number, s.stage_name))
                else:
                    if stage.status in (
                        "APPROVED",
                        "FAILED",
                        "CHANGES_REQUIRED",
                        "BLOCKED",
                        "REJECTED",
                        "CRITIQUE_COMPLETE",
                        "READY",
                        "PASSED",
                        "COMPLETED",
                        "SUCCESS",
                        "DONE",
                    ) or stage.duration_seconds > 0:
                        att_num = len(stage.attempts) + 1
                        stage.attempts.append(StageAttemptModel(
                            attempt_number=att_num,
                            duration_seconds=stage.duration_seconds,
                            status=stage.status,
                            exit_code=stage.exit_code,
                            raw_content=stage.raw_content,
                            machine_report=stage.machine_report,
                        ))
                        stage.raw_content = ""
                        stage.human_report = ""
                        stage.machine_report = None
                        stage.duration_seconds = 0.0
                    stage.status = "RUNNING"

                self.active_stage_name = stage.stage_name
                self.stage_start_times[stage.stage_name] = ts
                self.is_active = True
                banner = ev_data.get("banner_prefix", "")
                prefix = f" {banner}" if banner else ""
                self._add_log(f"▶ [{stage.stage_name}]{prefix} Stage execution started")
            return

        # 1b. Lifecycle: stage_record_update
        if lifecycle == "stage_record_update":
            if self.summary is not None:
                self.summary.record_stage(
                    name=ev_data.get("name", ""),
                    status=ev_data.get("status", "—"),
                    execution_state=ev_data.get("execution_state", "COMPLETED"),
                    duration_seconds=float(ev_data.get("duration_seconds", 0.0)),
                    role_name=ev_data.get("role_name"),
                    sequence_number=ev_data.get("sequence_number"),
                )
            return

        # 1c. Lifecycle: stage_skipped
        if lifecycle == "stage_skipped":
            s_name = ev_data.get("stage_name") or ev_data.get("role_name") or ev_data.get("display_name")
            sk_status = ev_data.get("status", "APPROVED")
            disp_name = ev_data.get("display_name") or (s_name.capitalize() if s_name else "Stage")
            if self.summary is not None and s_name:
                self.summary.record_stage(
                    name=disp_name,
                    status=str(sk_status).upper(),
                    execution_state="SKIPPED",
                    role_name=ev_data.get("role_name"),
                    sequence_number=ev_data.get("sequence_number"),
                )
            self._add_log(f"⏭ [{disp_name}] Skipped (already completed with status '{sk_status}')")
            return

        # 2. Lifecycle: stage_finish
        if lifecycle == "stage_finish":
            s_name = ev_data.get("stage_name") or ev_data.get("role_name")
            seq = ev_data.get("sequence_number")
            if s_name:
                stage = self.get_stage(s_name, sequence_number=seq)
                if stage:
                    fin_status = ev_data.get("status")
                    if fin_status:
                        stage.status = str(fin_status).upper()
                    dur = ev_data.get("duration_seconds")
                    if dur is not None:
                        stage.duration_seconds = float(dur)
                    if self.summary is not None:
                        clean_disp = stage.stage_name
                        m = re.match(r"^(\d+)_([a-zA-Z0-9_\-]+)$", stage.stage_name)
                        if m:
                            clean_disp = m.group(2).capitalize()
                        elif stage.role_name:
                            clean_disp = stage.role_name.capitalize()
                        if clean_disp.lower() == "critic":
                            clean_disp = "Critic"
                        self.summary.record_stage(
                            name=clean_disp,
                            status=stage.status,
                            execution_state="COMPLETED",
                            duration_seconds=stage.duration_seconds,
                            role_name=stage.role_name,
                            sequence_number=stage.sequence_number,
                        )
                    self._add_log(f"✓ [{stage.stage_name}] Finished with status: {stage.status} ({stage.duration_seconds:.1f}s)")
            return

        # 3. Locate active stage for stream events
        target_stage = None
        s_name = ev_data.get("stage_name") or ev_data.get("role_name") or self.active_stage_name
        seq = ev_data.get("sequence_number")
        if s_name:
            target_stage = self.get_stage(s_name, sequence_number=seq)
        if not target_stage and self.stages:
            for s in self.stages:
                if s.status == "RUNNING":
                    target_stage = s
                    break


        # 4. Stream Event: CHUNK
        if event.event_type == AgentEventType.CHUNK:
            chunk_text = event.text or ""
            if target_stage:
                target_stage.raw_content += chunk_text
                sec_match = MachineReportParser.SECTION_HEADER_REGEX.search(target_stage.raw_content)
                if sec_match:
                    target_stage.human_report = target_stage.raw_content[:sec_match.start()].strip()
                else:
                    target_stage.human_report = target_stage.raw_content.strip()
            clean_chunk = chunk_text.strip()
            if clean_chunk:
                first_line = clean_chunk.splitlines()[0]
                if len(first_line) > 100:
                    first_line = first_line[:97] + "..."
                self._add_log(f"  {first_line}")

        # 5. Stream Event: TOOL_START
        elif event.event_type == AgentEventType.TOOL_START:
            tool_name = ev_data.get("tool") or event.text or "tool"
            call_id = ev_data.get("call_id", "")
            tool_input = ev_data.get("input", {})
            if target_stage:
                if "tool_calls" not in target_stage.metadata:
                    target_stage.metadata["tool_calls"] = []
                target_stage.metadata["tool_calls"].append({
                    "tool": tool_name,
                    "call_id": call_id,
                    "input": tool_input,
                    "timestamp": ts,
                    "status": "running",
                })
            inp_summary = str(tool_input)[:50] if tool_input else ""
            self._add_log(f"  ⚡ TOOL_START: {tool_name} ({inp_summary})")

        # 6. Stream Event: TOOL_FINISH
        elif event.event_type == AgentEventType.TOOL_FINISH:
            tool_name = ev_data.get("tool") or event.text or "tool"
            self._add_log(f"  ✔ TOOL_FINISH: {tool_name}")

        # 7. Stream Event: COMPLETE
        elif event.event_type == AgentEventType.COMPLETE:
            if target_stage:
                target_stage.status = "APPROVED"
                if event.result:
                    target_stage.exit_code = event.result.exit_code
                    target_stage.duration_seconds = event.result.duration_seconds
                    if event.result.stdout:
                        target_stage.raw_content = event.result.stdout
                h_rep, m_rep = self._extract_human_and_machine_reports(target_stage.raw_content, target_stage.role_name)
                if h_rep:
                    target_stage.human_report = h_rep
                if m_rep:
                    target_stage.machine_report = m_rep
                    if m_rep.status:
                        target_stage.status = m_rep.status
                self._add_log(f"✓ [{target_stage.stage_name}] COMPLETE (exit: {target_stage.exit_code})")

        # 8. Stream Event: ERROR
        elif event.event_type == AgentEventType.ERROR:
            if target_stage:
                target_stage.status = "FAILED"
                target_stage.has_error = True
                if event.result:
                    target_stage.exit_code = event.result.exit_code or 1
                    target_stage.duration_seconds = event.result.duration_seconds
            err_msg = event.text or "Unknown error"
            self._add_log(f"✗ [{target_stage.stage_name if target_stage else 'Run'}] ERROR: {err_msg}")

        # Recalculate total duration
        self.total_duration_seconds = sum(s.duration_seconds for s in self.stages)

    def _add_log(self, msg: str) -> None:
        """Append line to bounded console log ring buffer."""
        self.console_logs.append(msg)
        if len(self.console_logs) > 2500:
            self.console_logs = self.console_logs[-2500:]

    @classmethod
    def from_dir(cls, run_dir: Path) -> "RunModel":
        """Construct a RunModel by inspecting the contents of a run directory.

        Robust against missing, corrupted, or partially-written files.
        """
        run_id = run_dir.name
        task = ""
        created_at = ""
        status = "UNKNOWN"
        adapters_used: Dict[str, str] = {}
        meta_dict: Dict[str, Any] = {}

        # 1. Load metadata.json if present
        meta_path = run_dir / "metadata.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta_dict = json.load(f)
                run_id = meta_dict.get("run_id", run_id)
                task = meta_dict.get("task", "")
                created_at = meta_dict.get("created_at", "")
                status = meta_dict.get("status", "UNKNOWN")
                adapters_used = meta_dict.get("adapters_used", {})
            except Exception:
                status = "CORRUPT_METADATA"

        # Check for active run lock
        from forge.storage.run_lock import RunLock
        is_active = RunLock.is_run_locked(run_dir)

        # 2. Discover stage files
        # Canonical stages
        canonical_roles = ["critic", "architect", "planner", "executor", "tester", "reviewer", "critic"]
        stage_files_map: Dict[str, Dict[str, Path]] = {}

        if run_dir.exists():
            for p in run_dir.iterdir():
                if not p.is_file():
                    continue

                # Ignore metadata and git baseline
                if p.name in ("metadata.json", "git_baseline.json", "run_lock.json", "run.lock"):
                    continue

                # Match patterns like 01_architect.json, 01_architect.md, 03_executor_attempt_1.json
                m = re.match(r"^(\d+_[a-zA-Z0-9_\-]+?)(?:_attempt_(\d+))?\.([a-zA-Z0-9]+)$", p.name)
                if not m:
                    continue

                base_name = m.group(1)
                attempt_str = m.group(2)
                ext = m.group(3).lower()

                key = base_name if not attempt_str else f"{base_name}_attempt_{attempt_str}"
                if key not in stage_files_map:
                    stage_files_map[key] = {}
                stage_files_map[key][ext] = p

        # Identify unique base stages
        base_stages: Dict[str, Dict[str, Any]] = {}
        attempts_map: Dict[str, List[StageAttemptModel]] = {}

        for key, files in stage_files_map.items():
            if "_attempt_" in key:
                parts = key.split("_attempt_")
                base = parts[0]
                att_num = int(parts[1]) if parts[1].isdigit() else 1
                att_model = cls._parse_stage_attempt(att_num, files)
                if base not in attempts_map:
                    attempts_map[base] = []
                attempts_map[base].append(att_model)
            else:
                base_stages[key] = files

        # Determine whether to reconcile the full canonical autonomous pipeline
        has_summary = bool(
            ("metadata" in meta_dict and isinstance(meta_dict["metadata"], dict) and "summary" in meta_dict["metadata"])
            or ("summary" in meta_dict and isinstance(meta_dict["summary"], dict))
        )
        has_pre_critic = "00_critic" in base_stages or any(k.startswith("00_") for k in base_stages)
        is_only_critic = has_pre_critic and all(k == "00_critic" or k.startswith("00_") for k in base_stages)
        should_reconcile = (
            not base_stages
            or is_only_critic
            or is_active
            or status in ("PENDING", "RUNNING", "IN_PROGRESS", "UNKNOWN")
            or meta_dict.get("reconcile_canonical", False)
            or meta_dict.get("pipeline_type") in ("auto", "pipeline")
            or has_summary
        )

        no_critic = meta_dict.get("no_critic", False)
        if not no_critic and isinstance(meta_dict.get("config"), dict):
            no_critic = meta_dict["config"].get("execution", {}).get("no_critic", False)

        if should_reconcile:
            canonical_stage_defs = StageOrder.full_autonomous_stages(no_critic=no_critic, has_pre_critic=has_pre_critic)

            stages: List[StageModel] = []
            total_duration = 0.0
            handled_keys: Set[str] = set()

            for c_def in canonical_stage_defs:
                target_key = c_def.artifact_prefix
                if target_key in base_stages:
                    files = base_stages[target_key]
                    stage = cls._parse_stage_files(target_key, files, adapters_used)
                    if target_key in attempts_map:
                        stage.attempts = sorted(attempts_map[target_key], key=lambda a: a.attempt_number)
                    handled_keys.add(target_key)
                else:
                    alt_key = None
                    for k in base_stages:
                        if k not in handled_keys:
                            k_lower = k.lower()
                            if k_lower == target_key.lower() or k_lower == c_def.name.lower():
                                alt_key = k
                                break
                    if alt_key:
                        files = base_stages[alt_key]
                        stage = cls._parse_stage_files(alt_key, files, adapters_used)
                        stage.sequence_number = c_def.sequence_number
                        if alt_key in attempts_map:
                            stage.attempts = sorted(attempts_map[alt_key], key=lambda a: a.attempt_number)
                        handled_keys.add(alt_key)
                    else:
                        stage = StageModel(
                            stage_name=target_key,
                            role_name=c_def.name,
                            sequence_number=c_def.sequence_number,
                            status="PENDING",
                        )
                stages.append(stage)
                total_duration += stage.duration_seconds

            # Also include any non-canonical or extra stages discovered on disk
            for key in sorted(base_stages.keys()):
                if key not in handled_keys:
                    files = base_stages[key]
                    stage = cls._parse_stage_files(key, files, adapters_used)
                    if key in attempts_map:
                        stage.attempts = sorted(attempts_map[key], key=lambda a: a.attempt_number)
                    stages.append(stage)
                    total_duration += stage.duration_seconds

            stages.sort(key=lambda s: (s.sequence_number, s.stage_name))
            final_status = status if (status != "UNKNOWN" or base_stages) else "PENDING"
        else:
            stages = []
            total_duration = 0.0
            for key in sorted(base_stages.keys()):
                files = base_stages[key]
                stage = cls._parse_stage_files(key, files, adapters_used)
                if key in attempts_map:
                    stage.attempts = sorted(attempts_map[key], key=lambda a: a.attempt_number)
                stages.append(stage)
                total_duration += stage.duration_seconds
            stages.sort(key=lambda s: (s.sequence_number, s.stage_name))
            final_status = status

        from forge.core.summary import RunSummary
        summary = None
        if "metadata" in meta_dict and isinstance(meta_dict["metadata"], dict) and "summary" in meta_dict["metadata"]:
            try:
                summary = RunSummary.from_dict(meta_dict["metadata"]["summary"])
            except Exception:
                pass
        elif "summary" in meta_dict and isinstance(meta_dict["summary"], dict):
            try:
                summary = RunSummary.from_dict(meta_dict["summary"])
            except Exception:
                pass

        if summary is None:
            summary = RunSummary(run_id=run_id, final_status=final_status)
            for s in stages:
                clean_name = s.stage_name
                m = re.match(r"^(\d+)_([a-zA-Z0-9_\-]+)$", s.stage_name)
                if m:
                    clean_name = m.group(2).capitalize()
                elif s.role_name:
                    clean_name = s.role_name.capitalize()
                if clean_name.lower() == "critic":
                    clean_name = "Critic"

                if s.duration_seconds > 0 or s.status not in ("PENDING", "UNKNOWN") or s.machine_report or s.attempts:
                    summary.record_stage(
                        name=clean_name,
                        status=s.display_status,
                        execution_state="COMPLETED",
                        duration_seconds=s.duration_seconds,
                        role_name=s.role_name,
                        sequence_number=s.sequence_number,
                    )
                else:
                    summary.record_stage(
                        name=clean_name,
                        status="—",
                        execution_state="NOT REACHED",
                        duration_seconds=0.0,
                        role_name=s.role_name,
                        sequence_number=s.sequence_number,
                    )

            if meta_dict.get("auto_commit") or (isinstance(meta_dict.get("config"), dict) and meta_dict["config"].get("execution", {}).get("auto_commit")):
                summary.record_stage(
                    name="Commit",
                    status="—",
                    execution_state="NOT REACHED",
                    role_name="commit",
                    sequence_number=99,
                )

        return cls(
            run_id=run_id,
            task=task,
            status=final_status,
            created_at=created_at,
            run_dir=run_dir,
            stages=stages,
            adapters_used=adapters_used,
            total_duration_seconds=total_duration,
            metadata=meta_dict,
            is_active=is_active,
            summary=summary,
        )



    @classmethod
    def _parse_stage_files(
        cls,
        stage_name: str,
        files: Dict[str, Path],
        adapters_used: Dict[str, str],
    ) -> StageModel:
        """Parse stage JSON and Markdown files into a StageModel."""
        # Derive role and sequence number
        seq_num = 0
        role_name = stage_name
        m = re.match(r"^(\d+)_([a-zA-Z0-9_\-]+)$", stage_name)
        if m:
            seq_num = int(m.group(1))
            role_name = m.group(2)

        adapter = adapters_used.get(role_name)
        status = "PENDING"
        duration = 0.0
        exit_code: Optional[int] = None
        prompt_hash: Optional[str] = None
        machine_report: Optional[MachineReportModel] = None
        json_meta: Dict[str, Any] = {}

        # 1. Parse JSON metadata if available
        if "json" in files:
            try:
                with open(files["json"], "r", encoding="utf-8") as f:
                    json_meta = json.load(f)
                status = json_meta.get("status") or json_meta.get("STATUS") or "UNKNOWN"
                duration = float(json_meta.get("duration_seconds") or json_meta.get("DURATION") or json_meta.get("duration") or 0.0)
                exit_code = json_meta.get("exit_code") if json_meta.get("exit_code") is not None else json_meta.get("EXIT_CODE")
                prompt_hash = json_meta.get("prompt_hash") or json_meta.get("PROMPT_HASH")

                raw_mr = json_meta.get("machine_report") or json_meta.get("MACHINE_REPORT")
                if isinstance(raw_mr, dict):
                    raw_mr_role = raw_mr.get("role") or raw_mr.get("ROLE")
                    mr_is_valid = raw_mr.get("is_valid", True)
                    mr_validation_errors = list(raw_mr.get("validation_errors", []))
                    if raw_mr_role and str(raw_mr_role).upper() != role_name.upper():
                        mr_is_valid = False
                        err_msg = f"Expected role '{role_name.upper()}', got '{str(raw_mr_role).upper()}'"
                        if err_msg not in mr_validation_errors:
                            mr_validation_errors.append(err_msg)
                    mr_status = str(raw_mr.get("status") or raw_mr.get("STATUS") or "UNKNOWN")
                    if not mr_is_valid and StageOrder.is_success_status(mr_status, role_name):
                        mr_status = "FAILED"
                        if status in StageOrder.ALL_SUCCESS_STATUSES:
                            status = "FAILED"
                    machine_report = MachineReportModel(
                        role=role_name.upper(),
                        status=mr_status,
                        handoff=raw_mr.get("handoff") or raw_mr.get("HANDOFF"),
                        reason=raw_mr.get("reason") or raw_mr.get("REASON") or "",
                        confidence=raw_mr.get("confidence") or raw_mr.get("CONFIDENCE") or "",
                        issues=raw_mr.get("issues") or raw_mr.get("ISSUES") or {},
                        next_action=raw_mr.get("next_action") or raw_mr.get("NEXT_ACTION") or "",
                        raw_data=raw_mr,
                        is_valid=mr_is_valid,
                        validation_errors=mr_validation_errors,
                    )
            except Exception:
                status = "CORRUPT_STAGE_JSON"

        # 2. Parse Markdown deliverable if available
        raw_content = ""
        human_report = ""
        is_partial = False

        if "md" in files:
            try:
                with open(files["md"], "r", encoding="utf-8") as f:
                    raw_content = f.read()

                # Separate Human Report from Machine Report
                human_report, parsed_mr = cls._extract_human_and_machine_reports(raw_content, role_name)
                if not machine_report and parsed_mr:
                    machine_report = parsed_mr
                    if status in ("PENDING", "UNKNOWN") and machine_report.status:
                        status = machine_report.status
            except Exception:
                is_partial = True

        return StageModel(
            stage_name=stage_name,
            role_name=role_name,
            sequence_number=seq_num,
            status=status,
            duration_seconds=duration,
            exit_code=exit_code,
            adapter=adapter,
            raw_content=raw_content,
            human_report=human_report or raw_content,
            machine_report=machine_report,
            prompt_hash=prompt_hash,
            is_partial=is_partial,
            has_error=bool(exit_code is not None and exit_code != 0),
            metadata=json_meta,
        )

    @classmethod
    def _parse_stage_attempt(cls, attempt_num: int, files: Dict[str, Path]) -> StageAttemptModel:
        status = "UNKNOWN"
        duration = 0.0
        exit_code = None
        raw_content = ""
        machine_report = None

        if "json" in files:
            try:
                with open(files["json"], "r", encoding="utf-8") as f:
                    data = json.load(f)
                status = data.get("status", "UNKNOWN")
                duration = float(data.get("duration_seconds", 0.0))
                exit_code = data.get("exit_code")
            except Exception:
                pass

        if "md" in files:
            try:
                with open(files["md"], "r", encoding="utf-8") as f:
                    raw_content = f.read()
            except Exception:
                pass

        return StageAttemptModel(
            attempt_number=attempt_num,
            duration_seconds=duration,
            status=status,
            exit_code=exit_code,
            raw_content=raw_content,
            machine_report=machine_report,
        )

    @classmethod
    def _extract_human_and_machine_reports(
        cls,
        raw_text: str,
        role_name: str,
    ) -> Tuple[str, Optional[MachineReportModel]]:
        """Split markdown deliverable into Human Report and Machine Report."""
        if not raw_text:
            return "", None

        # 1. Parse YAML Machine Report using the robust ADR-016 parser
        import io
        import contextlib

        with contextlib.redirect_stderr(io.StringIO()):
            data, raw_yaml = MachineReportParser.extract_yaml(raw_text, expected_role=role_name)
        machine_model = None
        if data:
            emitted_role = str(data.get("ROLE") or data.get("role") or role_name.upper())
            errors = []
            is_valid = True
            if emitted_role.upper() != role_name.upper():
                is_valid = False
                errors.append(f"Expected role '{role_name.upper()}', got '{emitted_role.upper()}'")
            mr_status = str(data.get("STATUS") or data.get("status") or "UNKNOWN")
            if not is_valid and StageOrder.is_success_status(mr_status, role_name):
                mr_status = "FAILED"
            machine_model = MachineReportModel(
                role=role_name.upper(),
                status=mr_status,
                handoff=data.get("HANDOFF") or data.get("handoff"),
                reason=str(data.get("REASON") or data.get("reason") or ""),
                confidence=str(data.get("CONFIDENCE") or data.get("confidence") or ""),
                issues=data.get("ISSUES") or data.get("issues") or {},
                next_action=str(data.get("NEXT_ACTION") or data.get("next_action") or ""),
                raw_yaml=raw_yaml,
                raw_data=data,
                is_valid=is_valid,
                validation_errors=errors,
            )

        # 2. Slice human report (everything prior to ## Machine Report)
        section_match = MachineReportParser.SECTION_HEADER_REGEX.search(raw_text)
        if section_match:
            human_report = raw_text[:section_match.start()].strip()
        else:
            human_report = raw_text.strip()

        return human_report, machine_model

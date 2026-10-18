"""Tester Engine for Tester v2.

Central coordinator that integrates:
- Archetype detection
- Runtime supervision (background dev server / daemon lifecycle)
- Interaction driver execution (Web via BrowserDriver, API, CLI, Library)
- Execution budget enforcement
- Evidence collection (screenshots, telemetry, repro scripts)
- Explicit coverage calculation and report compilation
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.core.context import Context
from forge.core.events import AgentEvent, AgentEventType
from forge.core.role import Role
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult
from forge.storage.run_manager import RunManager
from forge.testing.ai_planner import AiJourneyPlanner
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.browser import BrowserDriverFactory
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.drivers.base import InteractionDriver
from forge.testing.drivers.cli import CliInteractionDriver
from forge.testing.drivers.library import LibraryInteractionDriver
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.edge_cases import EdgeCaseAnalyzer, EdgeCaseRequirement
from forge.testing.evidence import EvidenceCollector
from forge.testing.fixtures import TestingFixtureManager
from forge.testing.models import (
    ActionType,
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner
from forge.testing.report import TesterReportGenerator
from forge.testing.supervisor import RuntimeSupervisor

logger = logging.getLogger(__name__)


def _redact_secrets(text: str) -> str:
    """Safely redact API keys, tokens, and credentials."""
    if not text:
        return ""
    redacted = re.sub(r'(bearer\s+)[A-Za-z0-9_\-\.]{15,}', r'\1[REDACTED]', text, flags=re.IGNORECASE)
    redacted = re.sub(r'(AIzaSy[A-Za-z0-9_\-]{20,})', r'[REDACTED_GEMINI_KEY]', redacted)
    redacted = re.sub(r'(sk-[A-Za-z0-9_\-]{15,})', r'[REDACTED_API_KEY]', redacted)
    redacted = re.sub(r'((?:api[_-]?key|secret|password|token)\s*[:=]\s*["\']?)[^"\'\s\n]{8,}(["\']?)', r'\1[REDACTED]\2', redacted, flags=re.IGNORECASE)
    return redacted


class TesterEngine:
    """Coordinates empirical black-box testing for the Tester role."""
    __test__ = False

    def __init__(
        self,
        context: Context,
        run_manager: Optional[RunManager] = None,
        budget: Optional[TestingBudget] = None,
        adapter: Optional[Any] = None,
    ):
        self.context = context
        self.project_root = context.project_root
        self.active_run = context.run
        self.run_manager = run_manager or RunManager(self.project_root)
        self.adapter = adapter
        if budget is None:
            max_runtime = 120.0
            if context and getattr(context, "config", None):
                stage_cfg = context.config.get_stage_config("tester", phase="pre_run")
                if stage_cfg and stage_cfg.timeout:
                    max_runtime = float(stage_cfg.timeout)
                elif getattr(context.config, "defaults", None) and getattr(context.config.defaults, "timeout", None):
                    max_runtime = float(context.config.defaults.timeout)
            self.budget = TestingBudget(max_runtime_seconds=max_runtime)
        else:
            self.budget = budget
        attempt_raw = getattr(self.context, "iteration", None) or getattr(self.context, "current_attempt", None)
        attempt_num: Optional[int] = attempt_raw if (isinstance(attempt_raw, int) and not isinstance(attempt_raw, bool)) else None
        self.evidence = EvidenceCollector(self.active_run.run_dir, attempt=attempt_num)
        self.tracker = BudgetTracker(self.budget)
        self.fixture_manager = TestingFixtureManager(self.project_root, getattr(self.context, "config", None))

    def _is_aborted(self) -> bool:
        """Check whether execution has been cancelled/aborted."""
        abort_ev = getattr(self.context, "abort_event", None)
        return bool(abort_ev is not None and abort_ev.is_set())

    def _emit_event(
        self,
        event_type: AgentEventType,
        text: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Dispatch lifecycle/telemetry AgentEvent to context.event_listener if registered."""
        listener = getattr(self.context, "event_listener", None)
        if listener is not None and callable(listener):
            try:
                ev_data: Dict[str, Any] = {"role_name": "tester", "stage_name": "04_tester"}
                if data:
                    ev_data.update(data)
                listener(
                    AgentEvent(
                        event_type=event_type,
                        timestamp=time.time(),
                        text=text,
                        data=ev_data,
                    )
                )
            except Exception as e:
                logger.debug("Failed emitting AgentEvent from TesterEngine: %s", e)

    def _build_cancelled_result(
        self,
        duration: float,
        reason: str = "Execution cancelled by user.",
    ) -> StageResult:
        """Construct StageResult for cancelled/aborted run with exit_code 130."""
        coverage = CoverageReport(
            planned_journeys=0,
            executed_journeys=0,
            passed_journeys=0,
            failed_journeys=0,
            blocked_journeys=0,
            confidence="LOW",
            summary=reason,
        )
        md_content = f"# Tester Verification\n\n**STATUS: BLOCKED**\n\n{reason}\n"
        json_dict = {
            "ROLE": "TESTER",
            "STATUS": "BLOCKED",
            "HANDOFF": "NONE",
            "EXIT_CODE": 130,
            "DURATION": round(duration, 2),
            "COVERAGE": coverage.to_dict(),
            "DATA": {
                "cancelled": True,
                "reason": reason,
            },
            "ISSUES": {
                "CRITICAL": [],
                "MAJOR": [],
                "MINOR": [],
            },
        }
        return self._build_stage_result(
            status="BLOCKED",
            md_content=md_content,
            json_dict=json_dict,
            duration=duration,
            exit_code=130,
            reason=reason,
            stdout=f"Tester cancelled: {reason}",
            stderr=reason,
            raw_output=md_content,
        )

    def run(self) -> StageResult:
        """Execute full Tester v2 empirical verification lifecycle."""
        start_time = time.time()
        logger.info("Starting Tester v2 empirical verification for run %s", self.active_run.run_id)

        if self._is_aborted():
            return self._build_cancelled_result(time.time() - start_time)

        # 1. Detect archetype
        archetype = ArchetypeDetector.detect(self.project_root, self.context.config)
        logger.info("Detected project archetype: %s", archetype.value)
        self._emit_event(
            AgentEventType.CHUNK,
            text=f"[tester] Detected project archetype: {archetype.value}",
        )

        if self._is_aborted():
            return self._build_cancelled_result(time.time() - start_time)

        # 2. Gather git context for journey planning
        git_diff = ""
        changed_files: List[str] = []
        git_retrieval_error: Optional[str] = None
        git_retrieval_source: str = "none"

        # 3. Read prior executor report if available (needed for fallback)
        exec_report_txt = ""
        exec_md = self.run_manager.load_stage_markdown(self.active_run, "executor")
        if exec_md:
            exec_report_txt = exec_md

        if self.context.git and self.context.git.is_git_repo():
            try:
                # Resolve baseline if available (from context or run_dir)
                baseline = getattr(self.context, "baseline", None)
                if baseline is None and self.active_run and hasattr(self.active_run, "run_dir"):
                    baseline_file = self.active_run.run_dir / "git_baseline.json"
                    if baseline_file.exists():
                        try:
                            from forge.core.git import GitBaseline
                            baseline = GitBaseline.load(baseline_file)
                        except Exception as e:
                            logger.warning("Failed to load baseline in TesterEngine: %s", e)

                cur_head = ""
                try:
                    head_res = self.context.git._run(["rev-parse", "HEAD"])
                    if head_res.returncode == 0:
                        cur_head = head_res.stdout.strip()
                except Exception:
                    pass

                if baseline is not None:
                    attr = self.context.git.attribute_changes(baseline)
                    baseline_files = list(attr.all_run_relevant_changes)
                    diff_text = self.context.git.diff_since(baseline)

                    # If HEAD has moved from baseline commit (e.g. commits made by Executor/auto-commit),
                    # also include the committed changes between baseline and HEAD
                    if baseline.head_commit and cur_head and baseline.head_commit != cur_head:
                        try:
                            rev_range = f"{baseline.head_commit}..{cur_head}"
                            log_res = self.context.git._run(["diff", "--name-only", rev_range])
                            if log_res.returncode == 0 and log_res.stdout.strip():
                                for f in log_res.stdout.splitlines():
                                    f_clean = self.context.git.resolve_relative_path(f.strip())
                                    if f_clean and not self.context.git.is_protected_path(f_clean):
                                        if f_clean not in baseline_files:
                                            baseline_files.append(f_clean)
                            commit_diff_res = self.context.git._run(["diff", rev_range])
                            if commit_diff_res.returncode == 0 and commit_diff_res.stdout.strip():
                                diff_text = f"{commit_diff_res.stdout.strip()}\n{diff_text}".strip()
                        except Exception as e:
                            logger.debug("Failed retrieving commit range diff: %s", e)

                    changed_files = baseline_files
                    git_diff = diff_text
                    git_retrieval_source = "baseline"
                else:
                    changed_files = self.context.git.changed_files()
                    git_diff = self.context.git.diff()
                    git_retrieval_source = "worktree"
            except Exception as e:
                git_retrieval_error = f"Git context retrieval failed: {e}"
                logger.error(git_retrieval_error, exc_info=True)

        # Fallback: cross-check with prior executor report outputs if files or diff are empty
        if not changed_files or not git_diff:
            if exec_report_txt:
                try:
                    import yaml
                    m_out = re.search(r"OUTPUTS:\s*\n((?:\s*-\s*[^\n]+\n?)+)", exec_report_txt)
                    if m_out:
                        parsed_outputs = yaml.safe_load(m_out.group(0)).get("OUTPUTS", [])
                        for po in parsed_outputs:
                            if isinstance(po, str) and po.strip():
                                p_clean = po.strip().strip('"').strip("'")
                                if p_clean not in changed_files:
                                    changed_files.append(p_clean)
                    m_fc = re.search(r"##\s*Files Changed\s*\n((?:\s*-\s*[^\n]+\n?)+)", exec_report_txt)
                    if m_fc:
                        for line in m_fc.group(1).splitlines():
                            line_str = line.strip().lstrip("-").strip()
                            f_candidate = line_str.split(":")[0].strip().strip("`*")
                            if f_candidate and not f_candidate.startswith("http") and ("/" in f_candidate or "." in f_candidate):
                                if f_candidate not in changed_files:
                                    changed_files.append(f_candidate)
                    if changed_files and not git_retrieval_source.startswith("baseline"):
                        git_retrieval_source = "executor_report"
                except Exception as e:
                    logger.debug("Failed extracting executor outputs fallback: %s", e)

        # 4. Plan journeys based on task & changes
        supervisor: Optional[RuntimeSupervisor] = None
        base_url = "http://127.0.0.1:3000"
        planned_journeys: List[Journey] = []
        journey_results: List[JourneyResult] = []
        all_defects: List[Defect] = []
        planner_metadata: Dict[str, Any] = {"planner": "heuristic", "planner_fallback": False}

        # Check explicit configuration override from stage config
        explicit_cmd = None
        explicit_port = None
        explicit_env = None
        if self.context and self.context.config:
            stage_cfg = self.context.config.get_stage_config("tester")
            if stage_cfg and stage_cfg.extra_flags:
                explicit_cmd = stage_cfg.extra_flags.get("start_command") or stage_cfg.extra_flags.get("command")
                explicit_port = stage_cfg.extra_flags.get("port")
                explicit_env = stage_cfg.extra_flags.get("env")

        # Grounded discovery invariant: when archetype is ambiguous or unknown and no start_command is provided,
        # report a configuration gap instead of guessing silently.
        if archetype == ProjectArchetype.UNKNOWN and not explicit_cmd:
            logger.warning("Project archetype is ambiguous or unknown; reporting configuration gap.")
            coverage = CoverageReport(
                planned_journeys=1,
                executed_journeys=0,
                passed_journeys=0,
                failed_journeys=0,
                blocked_journeys=1,
                confidence="LOW",
                summary="Target project archetype is ambiguous or unknown. Please configure 'archetype' or 'start_command' explicitly in forge.yaml.",
            )
            md_content, json_dict = TesterReportGenerator.generate(
                archetype=archetype,
                runtime_target=None,
                journey_results=[],
                coverage=coverage,
                duration_seconds=time.time() - start_time,
                evidence_dir=str(self.evidence.evidence_dir),
                planner_metadata={"planner": "none", "planner_fallback": True, "planner_fallback_reason": "Ambiguous or unknown archetype."},
            )
            return self._build_stage_result(
                status="BLOCKED",
                md_content=md_content,
                json_dict=json_dict,
                duration=time.time() - start_time,
                exit_code=1,
                reason="Target archetype is ambiguous or unknown; configuration required.",
                raw_output=md_content,
            )

        try:
            if self._is_aborted():
                return self._build_cancelled_result(time.time() - start_time)

            # 5. Runtime Supervision (if Web or API)
            if archetype in (ProjectArchetype.WEB_SPA, ProjectArchetype.API):
                supervisor = RuntimeSupervisor(self.project_root, log_dir=self.evidence.telemetry_dir)
                startup_timeout = min(60.0, max(35.0, self.budget.max_runtime_seconds / 10.0))
                started = supervisor.start(
                    command=explicit_cmd,
                    port=int(explicit_port) if explicit_port else None,
                    env=explicit_env,
                    timeout=startup_timeout,
                )
                if not started:
                    # Application failed to start
                    stdout_log, stderr_log = supervisor.get_logs()
                    fail_reason_val = getattr(supervisor, "failure_reason", None)
                    fail_str = str(fail_reason_val) if (fail_reason_val and not str(type(fail_reason_val)).startswith("<class 'unittest.mock")) else ""
                    port_val = getattr(supervisor, "port", None)
                    port_str = str(port_val) if (port_val and not str(type(port_val)).startswith("<class 'unittest.mock")) else ""

                    is_port_conflict = (
                        getattr(supervisor, "port_conflict", False) is True
                        or getattr(supervisor, "bind_conflict", False) is True
                        or "Address already in use" in (stderr_log or "")
                        or "bind error" in (stderr_log or "")
                    )
                    is_missing_dep = (
                        getattr(supervisor, "missing_dependency", False) is True
                        or "ModuleNotFoundError" in (stderr_log or "")
                        or "ImportError" in (stderr_log or "")
                    )

                    if is_port_conflict:
                        summary_msg = f"Target port conflict or bind collision on port {port_str}: {fail_str or 'Address already in use'}"
                        defect_cat = DefectCategory.TEST_CONFIGURATION_GAP
                        reason_msg = f"Port collision: {fail_str or 'Address already in use'}"
                    elif is_missing_dep:
                        summary_msg = f"Missing prerequisite or dependency: {fail_str or 'Dependency missing'}"
                        defect_cat = DefectCategory.PREREQUISITE_FAILURE
                        reason_msg = f"Missing prerequisite: {fail_str or 'Dependency missing'}"
                    else:
                        summary_msg = f"Application runtime failed to start or become ready: {fail_str or 'Readiness timeout'}"
                        defect_cat = DefectCategory.PREREQUISITE_FAILURE
                        reason_msg = f"Application runtime failed to start: {fail_str or 'Readiness timeout'}"

                    startup_defect = Defect(
                        id="DEF-RUNTIME-STARTUP-1",
                        title=summary_msg,
                        category=defect_cat,
                        severity=DefectSeverity.CRITICAL,
                        journey_id="J-STARTUP-01",
                        steps_to_reproduce=[fail_str or "Start application dev server"],
                        expected="Application runtime starts cleanly and responds to readiness probes.",
                        actual=((stderr_log or "").strip() or (stdout_log or "").strip() or summary_msg)[:300],
                    )

                    coverage = CoverageReport(
                        planned_journeys=1,
                        executed_journeys=0,
                        passed_journeys=0,
                        failed_journeys=0,
                        blocked_journeys=1,
                        confidence="LOW",
                        summary=summary_msg,
                        coverage_depth="BLOCKED_PREREQUISITE",
                    )
                    self.evidence.save_telemetry([], [], [], stdout_log, stderr_log)
                    startup_journey = Journey(
                        id="J-STARTUP-01",
                        title="Runtime Startup",
                        description=summary_msg,
                        priority=1,
                        steps=[],
                    )
                    startup_jr = JourneyResult(
                        journey=startup_journey,
                        status="BLOCKED",
                        duration_seconds=0.1,
                        steps_completed=0,
                        defects=[startup_defect],
                    )
                    md_content, json_dict = TesterReportGenerator.generate(
                        archetype=archetype,
                        runtime_target=None,
                        journey_results=[startup_jr],
                        coverage=coverage,
                        duration_seconds=time.time() - start_time,
                        evidence_dir=str(self.evidence.evidence_dir),
                        planner_metadata={"planner": "none", "planner_fallback": True, "planner_fallback_reason": summary_msg},
                        planned_journeys=[startup_journey],
                    )
                    return self._build_stage_result(
                        status="BLOCKED",
                        md_content=md_content,
                        json_dict=json_dict,
                        duration=time.time() - start_time,
                        exit_code=1,
                        reason=reason_msg,
                        stdout=stdout_log,
                        stderr=stderr_log or reason_msg,
                        raw_output=md_content,
                    )

                if supervisor.base_url:
                    base_url = supervisor.base_url

            if self._is_aborted():
                return self._build_cancelled_result(time.time() - start_time)

            # 6. Select & initialize interaction driver
            driver: InteractionDriver
            browser_driver = None
            try:
                if archetype == ProjectArchetype.WEB_SPA:
                    browser_driver = BrowserDriverFactory.create(viewport=(1440, 900))
                    driver = WebInteractionDriver(
                        browser_driver,
                        base_url=base_url,
                        project_root=self.project_root,
                        fixture_manager=self.fixture_manager,
                    )
                elif archetype == ProjectArchetype.API:
                    driver = ApiInteractionDriver(base_url=base_url)
                elif archetype == ProjectArchetype.CLI:
                    driver = CliInteractionDriver(cwd=self.project_root)
                elif archetype == ProjectArchetype.LIBRARY:
                    driver = LibraryInteractionDriver(project_root=self.project_root)
                else:
                    browser_driver = BrowserDriverFactory.create(viewport=(1440, 900))
                    driver = WebInteractionDriver(
                        browser_driver,
                        base_url=base_url,
                        project_root=self.project_root,
                        fixture_manager=self.fixture_manager,
                    )
            except Exception as e:
                logger.error("Failed to initialize interaction driver: %s", e)
                coverage = CoverageReport(
                    planned_journeys=1,
                    executed_journeys=0,
                    passed_journeys=0,
                    failed_journeys=0,
                    blocked_journeys=1,
                    confidence="LOW",
                    summary=f"Interaction driver failed to initialize: {e}",
                )
                md_content, json_dict = TesterReportGenerator.generate(
                    archetype=archetype,
                    runtime_target=base_url,
                    journey_results=[],
                    coverage=coverage,
                    duration_seconds=time.time() - start_time,
                    evidence_dir=str(self.evidence.evidence_dir),
                    planner_metadata={"planner": "none", "planner_fallback": True, "planner_fallback_reason": f"Driver init failed: {e}"},
                )
                return self._build_stage_result(
                    status="BLOCKED",
                    md_content=md_content,
                    json_dict=json_dict,
                    duration=time.time() - start_time,
                    exit_code=1,
                    reason=f"Interaction driver failed to initialize: {e}",
                    stdout="",
                    stderr=str(e),
                    raw_output=md_content,
                )

            # 7. Driver execution lifecycle
            aborted_during_loop = False
            try:
                # For WEB_SPA: discover live DOM elements before planning
                interactive_elements = []
                if archetype == ProjectArchetype.WEB_SPA and browser_driver:
                    try:
                        browser_driver.goto(base_url, timeout=10.0)
                        # Wait for client-side SPA hydration and DOM stabilization
                        hydration_timeout = min(8.0, max(2.0, self.budget.max_runtime_seconds / 4.0))
                        hydrated = browser_driver.wait_for_hydration(timeout=hydration_timeout)
                        if not hydrated:
                            logger.info("SPA hydration wait timed out or incomplete; proceeding with available DOM elements")
                        interactive_elements = browser_driver.get_interactive_elements()
                        logger.info("Discovered %d live interactive DOM elements at %s", len(interactive_elements), base_url)
                        if hasattr(driver, "set_discovered_elements"):
                            driver.set_discovered_elements(interactive_elements)
                    except Exception as e:
                        logger.warning("Failed initial navigation for DOM element discovery: %s", e)

                planner_prompt: Optional[str] = None
                planner_raw_response: Optional[str] = None

                # Extract changed-branch and edge-case requirements from git diff
                edge_case_reqs: List[EdgeCaseRequirement] = []
                try:
                    edge_case_reqs = EdgeCaseAnalyzer.analyze_diff(
                        git_diff=git_diff,
                        relevant_files=changed_files,
                    )
                    if edge_case_reqs:
                        self.fixture_manager.generate_edge_case_fixtures(
                            target_dir=self.active_run.run_dir / "tmp_fixtures",
                            edge_case_requirements=edge_case_reqs,
                        )
                except Exception as e:
                    logger.warning("Failed preparing edge-case requirements or fixtures: %s", e)

                # Plan journeys (AI planning for WEB_SPA if adapter configured, otherwise heuristic)
                if archetype == ProjectArchetype.WEB_SPA and self.adapter:
                    self._emit_event(
                        AgentEventType.CHUNK,
                        text="[tester] Formulating DOM-grounded journeys with AI planner...",
                    )
                    plan_res = AiJourneyPlanner.plan(
                        task=self.active_run.task,
                        archetype=archetype,
                        adapter=self.adapter,
                        interactive_elements=interactive_elements,
                        git_diff=git_diff,
                        changed_files=changed_files,
                        executor_report=exec_report_txt,
                        base_url=base_url,
                        budget=self.budget,
                        max_journeys=self.budget.max_journeys,
                        project_root=self.project_root,
                        fixtures_summary=self.fixture_manager.get_fixtures_prompt_summary(),
                        edge_case_requirements=edge_case_reqs,
                    )
                    planned_journeys = plan_res.journeys
                    planner_prompt = getattr(plan_res, "prompt", None)
                    planner_raw_response = getattr(plan_res, "raw_response", None)
                    planner_metadata = {
                        "planner": plan_res.planner_used,
                        "planner_fallback": plan_res.fallback_occurred,
                        "planner_fallback_reason": plan_res.fallback_reason,
                    }
                    if plan_res.fallback_occurred:
                        self._emit_event(
                            AgentEventType.CHUNK,
                            text=f"[tester] AI journey planning fell back to heuristic planner: {plan_res.fallback_reason}",
                        )
                        self._emit_event(
                            AgentEventType.CHUNK,
                            text=f"[tester] Planned {len(planned_journeys)} empirical verification journeys via heuristic fallback",
                        )
                    else:
                        self._emit_event(
                            AgentEventType.CHUNK,
                            text=f"[tester] Planned {len(planned_journeys)} empirical verification journeys via AI planner",
                        )
                else:
                    planned_journeys = JourneyPlanner.plan_journeys(
                        task=self.active_run.task,
                        archetype=archetype,
                        git_diff=git_diff,
                        changed_files=changed_files,
                        executor_report=exec_report_txt,
                        base_url=base_url,
                        max_journeys=self.budget.max_journeys,
                        project_root=self.project_root,
                    )
                    planner_metadata = {"planner": "heuristic", "planner_fallback": False}
                    self._emit_event(
                        AgentEventType.CHUNK,
                        text=f"[tester] Planned {len(planned_journeys)} empirical verification journeys",
                    )

                if hasattr(driver, "set_task_context"):
                    ac = AiJourneyPlanner.extract_acceptance_criteria(
                        self.active_run.task, executor_report=exec_report_txt
                    )
                    driver.set_task_context(
                        task=self.active_run.task,
                        acceptance_criteria=ac or "",
                        git_diff=git_diff,
                    )

                # 8. Execute journeys within budget
                for journey in planned_journeys:
                    if self._is_aborted():
                        logger.info("Testing aborted by user during journey loop.")
                        aborted_during_loop = True
                        break

                    if not self.tracker.can_start_journey():
                        logger.info("Testing budget exhausted; stopping journey execution.")
                        break

                    self._emit_event(
                        AgentEventType.TOOL_START,
                        text=f"journey:{journey.id}",
                        data={
                            "tool": f"journey:{journey.id}",
                            "input": {"title": journey.title, "priority": journey.priority},
                        },
                    )

                    # Check if an earlier journey encountered a test configuration gap / fixture unsuitability
                    prereq_gaps = [
                        d for d in all_defects if d.category == DefectCategory.TEST_CONFIGURATION_GAP
                    ]
                    if prereq_gaps and any(s.action == ActionType.UPLOAD_FILE for s in journey.steps):
                        blocked_reason = f"Blocked by upstream test configuration gap: {prereq_gaps[0].title}"
                        logger.info("Skipping impossible workflow in %s: %s", journey.id, blocked_reason)
                        res = JourneyResult(
                            journey=journey,
                            status="BLOCKED",
                            duration_seconds=0.01,
                            steps_completed=0,
                            defects=[],
                            telemetry_summary={
                                "steps_total": len(journey.steps),
                                "steps_completed": 0,
                                "steps_skipped": len(journey.steps),
                                "blocked_reason": blocked_reason,
                            },
                        )
                        journey_results.append(res)
                        self._emit_event(
                            AgentEventType.CHUNK,
                            text=f"[tester] Journey {journey.id} ({journey.title}) -> BLOCKED ({blocked_reason})",
                        )
                        continue

                    res = driver.execute_journey(journey, self.tracker, self.evidence)
                    journey_results.append(res)
                    all_defects.extend(res.defects)

                    self._emit_event(
                        AgentEventType.TOOL_FINISH,
                        text=f"journey:{journey.id}",
                        data={
                            "tool": f"journey:{journey.id}",
                            "status": res.status,
                            "defects": len(res.defects),
                        },
                    )
                    self._emit_event(
                        AgentEventType.CHUNK,
                        text=f"[tester] Journey {journey.id} ({journey.title}) -> {res.status}",
                    )

                if aborted_during_loop:
                    return self._build_cancelled_result(time.time() - start_time)

                # Capture process logs if supervised
                stdout_log, stderr_log = "", ""
                if supervisor:
                    stdout_log, stderr_log = supervisor.get_logs()

                # If browser driver was used, harvest telemetry logs
                if browser_driver:
                    self.evidence.save_telemetry(
                        console_logs=browser_driver.get_console_logs(),
                        failed_requests=browser_driver.get_failed_requests(),
                        page_errors=browser_driver.get_page_errors(),
                        process_stdout=stdout_log,
                        process_stderr=stderr_log,
                    )
            finally:
                driver.close()

            if self._is_aborted():
                return self._build_cancelled_result(time.time() - start_time)

            # 9. Compute explicit coverage & depth using canonical journey results
            latest_by_id: Dict[str, JourneyResult] = {}
            for r in journey_results:
                latest_by_id[r.journey.id] = r

            unique_passed_ids = {jid for jid, r in latest_by_id.items() if r.status == "PASS"}
            unique_failed_ids = {jid for jid, r in latest_by_id.items() if r.status == "FAIL"}
            unique_blocked_ids = {jid for jid, r in latest_by_id.items() if r.status == "BLOCKED"}
            unique_executed_ids = {jid for jid, r in latest_by_id.items() if r.status != "BLOCKED"}

            planned_ids = [j.id for j in planned_journeys]
            unexecuted_ids = set(planned_ids) - set(latest_by_id.keys())

            executed_count = len(unique_executed_ids)
            passed_count = len(unique_passed_ids)
            failed_count = len(unique_failed_ids)
            blocked_count = len(unique_blocked_ids)
            unexecuted_count = len(unexecuted_ids)

            # Determine confidence
            if not planned_journeys:
                confidence = "LOW"
                summary = "No test journeys could be planned for the detected repository structure."
            elif (
                planned_ids
                and unique_passed_ids == set(planned_ids)
                and len(unique_passed_ids) >= 3
                and failed_count == 0
                and blocked_count == 0
                and unexecuted_count == 0
            ):
                confidence = "HIGH"
                summary = f"All {len(unique_passed_ids)} planned primary, adjacent, and smoke journeys were successfully exercised and passed."
            elif passed_count > 0:
                confidence = "MEDIUM"
                summary = f"{passed_count} of {len(planned_journeys)} planned journeys passed ({failed_count} failed, {blocked_count} blocked, {unexecuted_count} unexecuted)."
            else:
                confidence = "LOW"
                summary = f"Zero of {len(planned_journeys)} planned journeys passed ({failed_count} failed, {blocked_count} blocked, {unexecuted_count} unexecuted)."

            if planner_metadata.get("planner_fallback"):
                fb_r = planner_metadata.get("planner_fallback_reason", "unknown")
                summary += f" (Heuristic fallback: {fb_r})"
            elif planner_metadata.get("planner") == "ai":
                summary += " (AI-guided DOM-grounded planning)"

            # Analyze coverage depth and changed components
            coverage_depth = "FUNCTIONAL_VERIFIED"
            exercised_changed: List[str] = []
            visited_without_meaningful_assertions: List[str] = []
            failed_verification: List[str] = []
            blocked_by_prerequisite: List[str] = []
            unexercised_changed: List[str] = []
            validation_gaps: List[str] = []

            # 1. Check if git change context retrieval failed
            if git_retrieval_error:
                validation_gaps.append(git_retrieval_error)

            # 2. Identify relevant application code components vs docs/tests
            code_extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".css", ".go", ".rs", ".rb", ".java", ".c", ".cpp"}
            non_code_prefixes = (".ai/", "docs/", "doc/", ".github/", ".forge/")

            relevant_code_files: List[str] = []
            if changed_files:
                for cf in changed_files:
                    posix = cf.replace("\\", "/").strip()
                    if any(posix.startswith(pfx) for pfx in non_code_prefixes):
                        continue
                    p = Path(posix)
                    if p.suffix.lower() not in code_extensions:
                        continue
                    if "test" in p.name.lower() or "tests/" in posix.lower():
                        continue
                    if cf not in relevant_code_files:
                        relevant_code_files.append(cf)

            # Helper to detect empty-state blocker copy in assertions
            def _is_empty_state_copy(target_str: str) -> bool:
                t_low = target_str.lower()
                return any(kw in t_low for kw in (
                    "no data", "not loaded", "no models", "not prepared",
                    "please load", "complete module", "empty", "no items",
                    "no records", "no results"
                ))

            def _journey_targets_component(j: Journey, cf: str, tokens: List[str]) -> bool:
                cf_stem = Path(cf).stem.lower()
                j_text = f"{j.title} {j.description}".lower()
                if cf_stem in j_text or cf.lower() in j_text or any(tok in j_text for tok in tokens):
                    return True
                for st in j.steps:
                    st_text = f"{st.description or ''} {st.target} {st.value or ''}".lower()
                    if cf_stem in st_text or any(tok in st_text for tok in tokens):
                        return True
                return False

            def _is_behavior_assertion(st: JourneyStep, cf: str, tokens: List[str]) -> bool:
                """Determine whether an assertion verifies functional outputs/calculations rather than pure navigation header."""
                if st.action not in (ActionType.ASSERT_TEXT, ActionType.ASSERT_ELEMENT, ActionType.CLI_COMMAND, ActionType.HTTP_REQUEST):
                    return False
                target_low = st.target.lower().strip()
                if _is_empty_state_copy(target_low):
                    return False
                cf_stem = Path(cf).stem.lower()
                nav_titles = {
                    cf_stem,
                    cf_stem.replace("_", " "),
                    f"{cf_stem} module",
                    "model comparison module",
                    "reliability scoring module",
                    "baseline model training",
                    "stress testing",
                    "data management",
                    "post-stress evaluation",
                }
                if target_low in nav_titles or any(target_low == f"{i}️⃣ {nt}" for i in range(1, 10) for nt in nav_titles):
                    return False
                behavior_indicators = (
                    "accuracy", "f1", "score", "robustness", "rating", "grade", "metric",
                    "best model", "recommendation", "completed", "result", "total", "table",
                    "chart", "dataset", "split", "trained", "success", "loss", "percent",
                    "stats", "overview", "detail", "rank", "ranking",
                    "status", "response", "output", "json", "data", "id", "count", "message",
                    "error", "code", "valid", "version", "help", "usage", "calculated",
                    "items", "records", "user", "created", "value", "expected"
                )
                if any(ind in target_low for ind in behavior_indicators):
                    return True
                if st.expected_state and any(ind in st.expected_state.lower() for ind in behavior_indicators):
                    return True
                if st.description and any(ind in st.description.lower() for ind in behavior_indicators):
                    return True
                if st.action == ActionType.ASSERT_ELEMENT and any(ind in target_low for ind in ("metric", "dataframe", "chart", "alert", "card", "table", "button", "input")):
                    return True
                if st.action in (ActionType.CLI_COMMAND, ActionType.HTTP_REQUEST):
                    return True
                return False

            # 3. Check which changed components were meaningfully exercised vs blocked vs unexercised
            if relevant_code_files:
                for cf in relevant_code_files:
                    cf_stem = Path(cf).stem.lower()
                    # Derive semantic search tokens (e.g. 'module_05_post_stress' -> ['module_05', 'post_stress', 'post-stress'])
                    tokens = [cf_stem, cf_stem.replace("_", "-")]
                    m_mod = re.match(r"^(module[_-]\w+?)[_-](.+)$", cf_stem)
                    if m_mod:
                        mod_pfx = m_mod.group(1)
                        mod_sfx = m_mod.group(2)
                        tokens.extend([mod_pfx, mod_pfx.replace("_", "-"), mod_sfx, mod_sfx.replace("_", "-")])
                    if cf_stem.endswith("_module") or cf_stem.endswith("-module"):
                        base_name = cf_stem[:-7]
                        tokens.extend([base_name, base_name.replace("_", "-")])
                        if base_name == "comparison":
                            tokens.extend(["compare", "comparator", "model comparison"])
                        elif base_name == "model":
                            tokens.extend(["baseline model", "model trainer", "train model", "train baseline", "baseline modeling", "best model"])
                        elif base_name == "stress":
                            tokens.extend(["stress test", "batch stress"])
                        elif base_name == "reliability":
                            tokens.extend(["reliability score", "reliability scoring"])
                    tokens = [t for t in set(tokens) if len(t) >= 4 and t not in ("views", "modules", "module", "src", "core")]

                    is_exercised = False
                    for jr in journey_results:
                        if jr.status != "PASS":
                            continue

                        component_targeted = _journey_targets_component(jr.journey, cf, tokens)
                        has_meaningful_step = False
                        has_empty_state_assertion_only = False
                        has_behavior_assertion = False
                        assertions_checked = 0
                        empty_assertions = 0

                        for st in jr.journey.steps:
                            st_text = f"{st.description or ''} {st.target} {st.value or ''}".lower()
                            if cf_stem in st_text or any(tok in st_text for tok in tokens):
                                component_targeted = True
                            if st.action in (ActionType.FILL, ActionType.CLICK, ActionType.UPLOAD_FILE, ActionType.CLI_COMMAND, ActionType.HTTP_REQUEST):
                                has_meaningful_step = True
                            if st.action in (ActionType.ASSERT_TEXT, ActionType.ASSERT_ELEMENT):
                                assertions_checked += 1
                                if _is_empty_state_copy(st.target):
                                    empty_assertions += 1
                                if _is_behavior_assertion(st, cf, tokens):
                                    has_behavior_assertion = True
                            elif st.action in (ActionType.CLI_COMMAND, ActionType.HTTP_REQUEST):
                                assertions_checked += 1
                                if _is_behavior_assertion(st, cf, tokens):
                                    has_behavior_assertion = True

                        if assertions_checked > 0 and empty_assertions == assertions_checked:
                            has_empty_state_assertion_only = True

                        if component_targeted and has_meaningful_step and not has_empty_state_assertion_only and has_behavior_assertion and assertions_checked > 0:
                            is_exercised = True
                            break

                    if is_exercised:
                        exercised_changed.append(cf)
                    else:
                        # Check targeting journeys
                        targeting_results = [
                            jr for jr in journey_results
                            if _journey_targets_component(jr.journey, cf, tokens)
                        ]
                        passing_journeys_targeting = [
                            jr for jr in targeting_results
                            if jr.status == "PASS"
                        ]
                        failed_journeys_targeting = [
                            jr for jr in targeting_results
                            if jr.status in ("FAIL", "BLOCKED")
                        ]
                        if passing_journeys_targeting and not is_exercised:
                            visited_without_meaningful_assertions.append(cf)
                            unexercised_changed.append(cf)
                        elif not failed_journeys_targeting:
                            unexercised_changed.append(cf)
                        else:
                            failed_verification_found = False
                            blocked_upstream_found = False
                            for jr in failed_journeys_targeting:
                                if jr.status == "BLOCKED":
                                    blocked_upstream_found = True
                                    continue

                                has_prereq_defect = any(
                                    d.category in (DefectCategory.PREREQUISITE_FAILURE, DefectCategory.TEST_CONFIGURATION_GAP)
                                    for d in jr.defects
                                )
                                if has_prereq_defect:
                                    blocked_upstream_found = True
                                    continue

                                reached_cf = False
                                for st in jr.journey.steps[:jr.steps_completed + 1]:
                                    st_text = f"{st.description or ''} {st.target} {st.value or ''}".lower()
                                    if cf_stem in st_text or any(tok in st_text for tok in tokens):
                                        reached_cf = True
                                        break

                                if reached_cf and jr.defects:
                                    failed_verification_found = True
                                    break
                                else:
                                    blocked_upstream_found = True

                            if failed_verification_found:
                                failed_verification.append(cf)
                            elif blocked_upstream_found:
                                blocked_by_prerequisite.append(cf)
                            else:
                                unexercised_changed.append(cf)


            # 4. Check whether UPLOAD_FILE was followed by confirmed data ingestion
            upload_attempted = False
            upload_ingested = False
            for jr in journey_results:
                for idx, st in enumerate(jr.journey.steps):
                    if st.action == ActionType.UPLOAD_FILE:
                        upload_attempted = True
                        # Check remaining steps for a click/submit and a confirming non-empty assertion
                        remaining = jr.journey.steps[idx + 1 :]
                        has_confirm_click = any(
                            r.action == ActionType.CLICK and any(kw in r.target.lower() for kw in ("load", "submit", "import"))
                            for r in remaining
                        )
                        def _is_ingest_kw(txt: str) -> bool:
                            t = txt.lower()
                            if "uploaded" in t and "data" not in t:
                                return False
                            return any(kw in t for kw in (
                                "data loaded", "dataset loaded", "rows loaded", "records",
                                "summary", "shape", "distribution", "preview", "columns",
                                "dataset ready", "successfully loaded", "table"
                            ))

                        has_ingest_assert = any(
                            r.action in (ActionType.ASSERT_TEXT, ActionType.ASSERT_ELEMENT)
                            and not _is_empty_state_copy(r.target)
                            and (_is_ingest_kw(r.target) or _is_ingest_kw(r.description or ""))
                            for r in remaining
                        )
                        if has_confirm_click or has_ingest_assert:
                            upload_ingested = True
                            break

            upload_without_ingestion = upload_attempted and not upload_ingested

            # 5. Check for genuine functional interactions
            has_functional_steps = False
            for jr in journey_results:
                if jr.status != "PASS":
                    continue
                for st in jr.journey.steps:
                    if st.action in (ActionType.FILL, ActionType.UPLOAD_FILE, ActionType.CLI_COMMAND, ActionType.HTTP_REQUEST):
                        has_functional_steps = True
                        break
                    if st.action == ActionType.CLICK and not any(nav in st.target.lower() for nav in ("nav", "tab", "link", "a[href", "menu", "header")):
                        has_functional_steps = True
                        break
                if has_functional_steps:
                    break

            # 6. Evaluate final coverage depth with integrity
            if executed_count == 0 or passed_count == 0:
                if blocked_by_prerequisite:
                    coverage_depth = "BLOCKED_PREREQUISITE"
                    confidence = "LOW"
                    summary = f"All {len(planned_journeys)} journeys were blocked or halted by upstream prerequisite failures."
                    validation_gaps.append(
                        f"{len(blocked_by_prerequisite)} modified components were targeted but blocked by prerequisite failures: {', '.join(blocked_by_prerequisite[:4])}"
                    )
                else:
                    coverage_depth = "UNTESTED"
                    confidence = "LOW"
                    summary = "Zero planned journeys could be exercised."
            elif git_retrieval_error:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "LOW"
                summary += " (Shallow smoke coverage: git change context could not be retrieved)"
            elif failed_count > 0 or blocked_count > 0:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "MEDIUM" if passed_count > 0 else "LOW"
                downgrade_reasons = []
                if failed_count > 0:
                    downgrade_reasons.append(f"{failed_count} journey(s) failed")
                if blocked_count > 0:
                    downgrade_reasons.append(f"{blocked_count} journey(s) blocked")
                if failed_verification:
                    downgrade_reasons.append(f"{len(failed_verification)} changed component(s) failed verification ({', '.join(failed_verification[:2])})")
                if blocked_by_prerequisite:
                    downgrade_reasons.append(f"{len(blocked_by_prerequisite)} component(s) blocked by prerequisites ({', '.join(blocked_by_prerequisite[:2])})")
                validation_gaps.append(f"Incomplete verification: {'; '.join(downgrade_reasons)}")
                summary += f" [Confidence downgraded to {confidence}: {'; '.join(downgrade_reasons)}]"
            elif visited_without_meaningful_assertions:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "MEDIUM"
                validation_gaps.append(
                    f"{len(visited_without_meaningful_assertions)} modified component(s) reached without meaningful behavioral assertions: {', '.join(visited_without_meaningful_assertions)}"
                )
                summary += f" [Confidence downgraded to MEDIUM: {len(visited_without_meaningful_assertions)} component(s) visited without meaningful assertions]"
            elif relevant_code_files and len(exercised_changed) == len(relevant_code_files):
                coverage_depth = "FUNCTIONAL_VERIFIED"
                confidence = "HIGH"
                summary += f" (All {len(relevant_code_files)} modified application code components exercised and verified)"
            elif relevant_code_files and len(exercised_changed) == 0:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "LOW"
                if blocked_by_prerequisite:
                    validation_gaps.append(
                        f"All {len(relevant_code_files)} modified components remained unverified ({len(blocked_by_prerequisite)} blocked by upstream prerequisites: {', '.join(blocked_by_prerequisite[:4])})"
                    )
                    summary += f" (Shallow smoke: {len(blocked_by_prerequisite)} of {len(relevant_code_files)} changed components blocked by prerequisite failure)"
                else:
                    validation_gaps.append(
                        f"None of the {len(relevant_code_files)} modified application code components were exercised: {', '.join(unexercised_changed[:4])}"
                    )
                    summary += f" (Shallow smoke: 0 of {len(relevant_code_files)} changed code components exercised)"
            elif unexercised_changed or blocked_by_prerequisite or failed_verification:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "MEDIUM"
                gap_parts = []
                if failed_verification:
                    gap_parts.append(f"{len(failed_verification)} failed verification ({', '.join(failed_verification[:3])})")
                if blocked_by_prerequisite:
                    gap_parts.append(f"{len(blocked_by_prerequisite)} blocked by prerequisites ({', '.join(blocked_by_prerequisite[:3])})")
                if unexercised_changed:
                    gap_parts.append(f"{len(unexercised_changed)} unexercised ({', '.join(unexercised_changed[:3])})")
                validation_gaps.append(f"Partial coverage: {'; '.join(gap_parts)}")
                summary += f" (Partial coverage: {len(exercised_changed)} of {len(relevant_code_files)} changed components verified)"
            elif upload_without_ingestion:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "MEDIUM"
                validation_gaps.append("File fixture was selected in uploader, but application data ingestion was not confirmed.")
                summary += " (Shallow smoke: file fixture selected without confirmed data ingestion)"
            elif not has_functional_steps:
                coverage_depth = "SHALLOW_SMOKE"
                confidence = "MEDIUM"
                validation_gaps.append(
                    "Journeys only exercised surface page navigation and empty UI states without testing data submission or functional workflows."
                )
                summary += " (Shallow smoke: surface navigation and structural presence checks only)"
            else:
                coverage_depth = "FUNCTIONAL_VERIFIED"
                confidence = "HIGH"
                summary += " (Empirical functional regression verification completed)"

            # 7. Evaluate edge-case verification and UI-unreachable changed branches
            verified_edge_cases: List[str] = []
            unit_test_verified_branches: List[str] = []
            untested_changed_branches: List[str] = []

            for req in edge_case_reqs:
                if not req.testable_via_ui:
                    if getattr(req, "unit_test_verified", False):
                        unit_test_verified_branches.append(
                            f"{req.component}: {req.title} ({req.branch_condition}) [Unit Test Verified: {req.untestable_reason}]"
                        )
                    else:
                        untested_changed_branches.append(
                            f"{req.component}: {req.title} ({req.branch_condition}) - {req.untestable_reason}"
                        )
                else:
                    req_exercised = False
                    if req.id == "EC-MULTI-MODEL-COMPARISON":
                        for jr in journey_results:
                            if jr.status == "PASS":
                                jr_text = f"{jr.journey.title} {jr.journey.description}".lower()
                                steps_text = " ".join(f"{s.target} {s.value or ''}" for s in jr.journey.steps).lower()
                                if any(k in steps_text for k in ("random forest", "xgboost", "best model")):
                                    req_exercised = True
                                    break
                                elif any("module_02" in cf or "model_module" in cf for cf in exercised_changed) and "best model" in steps_text:
                                    req_exercised = True
                                    break
                                elif "model" in jr_text and any("module_02" in cf or "model_module" in cf for cf in exercised_changed):
                                    req_exercised = True
                                    break
                    elif req.id == "EC-POST-STRESS-RECOMMENDATION":
                        if any("module_05" in cf or "post_stress" in cf for cf in exercised_changed):
                            req_exercised = True
                    elif req.id == "EC-COMPARISON-COMPOSITE-POLICY":
                        if any("module_07" in cf or "comparison" in cf for cf in exercised_changed):
                            req_exercised = True
                    elif req.id == "EC-RELIABILITY-SCALING":
                        if any("module_08" in cf or "reliability" in cf for cf in exercised_changed):
                            req_exercised = True
                    elif req.id == "EC-DATASET-SPLIT-BOUNDARY":
                        for jr in journey_results:
                            if any("boundary" in (s.value or "").lower() or "missing" in (s.value or "").lower() for s in jr.journey.steps):
                                req_exercised = True
                                break

                    if req_exercised:
                        verified_edge_cases.append(f"{req.title} ({req.component})")
                    else:
                        untested_changed_branches.append(
                            f"{req.component}: {req.title} ({req.branch_condition}) - unexercised in executed journeys"
                        )

            if untested_changed_branches:
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                    summary += f" [Confidence downgraded to MEDIUM: {len(untested_changed_branches)} changed branch(es) remain untested]"

            # Failed, blocked, or missing/unexecuted journeys strictly prevent HIGH confidence and FUNCTIONAL_VERIFIED
            if failed_count > 0 or blocked_count > 0 or unexecuted_count > 0:
                if confidence == "HIGH":
                    confidence = "MEDIUM"
                    summary += f" [Confidence downgraded to MEDIUM: {failed_count} failed, {blocked_count} blocked, {unexecuted_count} unexecuted journey(s)]"
                if coverage_depth == "FUNCTIONAL_VERIFIED":
                    coverage_depth = "SHALLOW_SMOKE"
                    validation_gaps.append(
                        f"Full verification incomplete: {failed_count} failed, {blocked_count} blocked, {unexecuted_count} unexecuted journey(s)."
                    )

            if verified_edge_cases or unit_test_verified_branches or untested_changed_branches:
                parts = []
                if verified_edge_cases:
                    parts.append(f"{len(verified_edge_cases)} live UI edge case(s) verified")
                if unit_test_verified_branches:
                    parts.append(f"{len(unit_test_verified_branches)} UI-unreachable branch(es) verified via isolated unit tests")
                if untested_changed_branches:
                    parts.append(f"{len(untested_changed_branches)} branch(es) untested")
                summary += f" [{'; '.join(parts)}]"

            coverage = CoverageReport(
                planned_journeys=len(planned_journeys),
                executed_journeys=executed_count,
                passed_journeys=passed_count,
                failed_journeys=failed_count,
                blocked_journeys=blocked_count,
                unexecuted_journeys=unexecuted_count,
                confidence=confidence,
                summary=summary,
                categories_covered=list(set(j.category for j in planned_journeys)),
                coverage_depth=coverage_depth,
                exercised_changed_components=exercised_changed,
                visited_without_meaningful_assertions=visited_without_meaningful_assertions,
                verified_edge_cases=verified_edge_cases,
                unit_test_verified_branches=unit_test_verified_branches,
                untested_changed_branches=untested_changed_branches,
                failed_verification_components=failed_verification,
                blocked_by_prerequisite_components=blocked_by_prerequisite,
                unexercised_changed_components=unexercised_changed,
                validation_gaps=validation_gaps,
            )

            # Build debug telemetry
            hydration_diag = getattr(browser_driver, "last_hydration_diagnostics", {}) if browser_driver else {}
            stdout_lines = [
                f"=== Tester v2 Empirical Verification (Attempt {getattr(self.evidence, 'attempt', 1)}) ===",
                f"Archetype: {archetype.value}",
                f"Runtime Target: {base_url}",
                f"Planner: {planner_metadata.get('planner')} (Fallback: {planner_metadata.get('planner_fallback')})",
                f"Coverage Depth: {coverage_depth}",
                f"Confidence: {confidence}",
                f"Journeys: {len(planned_journeys)} planned, {executed_count} executed, {passed_count} passed, {failed_count} failed",
                "",
                "=== Journey Execution Log ===",
            ]
            for r in journey_results:
                stdout_lines.append(f"[{r.journey.id}] {r.journey.title} -> {r.status} (steps: {r.steps_completed}/{len(r.journey.steps)})")

            stderr_lines = []
            if supervisor:
                _, s_err = supervisor.get_logs()
                if s_err:
                    stderr_lines.append(f"=== Supervisor Stderr ===\n{s_err}")
            if all_defects:
                stderr_lines.append(f"=== Discovered Defects ({len(all_defects)}) ===")
                for d in all_defects:
                    stderr_lines.append(f"[{d.id}] ({d.severity.value}) {d.title}")
                    stderr_lines.append(f"  Expected: {d.expected}")
                    stderr_lines.append(f"  Actual: {d.actual}")

            execution_log = {
                "attempt": getattr(self.evidence, "attempt", 1),
                "archetype": archetype.value,
                "base_url": base_url,
                "git_retrieval_source": git_retrieval_source,
                "git_retrieval_error": git_retrieval_error,
                "changed_files": changed_files,
                "relevant_code_files": relevant_code_files,
                "hydration_diagnostics": hydration_diag,
                "interactive_elements_count": len(interactive_elements),
                "planner": planner_metadata,
                "coverage_depth": coverage_depth,
                "exercised_changed_components": exercised_changed,
                "blocked_by_prerequisite_components": blocked_by_prerequisite,
                "unexercised_changed_components": unexercised_changed,
                "validation_gaps": validation_gaps,
                "verified_edge_cases": verified_edge_cases,
                "untested_changed_branches": untested_changed_branches,
                "journeys_planned": [
                    {
                        "id": j.id,
                        "title": j.title,
                        "priority": j.priority,
                        "category": j.category,
                        "steps_count": len(j.steps),
                    }
                    for j in planned_journeys
                ],
                "journeys_executed": [
                    {
                        "id": res.journey.id,
                        "status": res.status,
                        "steps_completed": res.steps_completed,
                        "defects": [d.to_dict() for d in res.defects],
                        "telemetry": res.telemetry_summary,
                    }
                    for res in journey_results
                ],
            }

            # 10. Generate reports
            total_duration = time.time() - start_time
            md_content, json_dict = TesterReportGenerator.generate(
                archetype=archetype,
                runtime_target=base_url if archetype in (ProjectArchetype.WEB_SPA, ProjectArchetype.API) else "Local Process",
                journey_results=journey_results,
                coverage=coverage,
                duration_seconds=total_duration,
                evidence_dir=str(self.evidence.evidence_dir),
                planner_metadata=planner_metadata,
                planned_journeys=planned_journeys,
            )

            final_status = json_dict.get("STATUS", "PASS")
            exit_code = json_dict.get("EXIT_CODE", 0)

            parsed_response_obj = None
            if planner_raw_response:
                try:
                    parsed_response_obj = json.loads(planner_raw_response)
                except Exception:
                    parsed_response_obj = {"raw": planner_raw_response}

            return self._build_stage_result(
                status=final_status,
                md_content=md_content,
                json_dict=json_dict,
                duration=total_duration,
                exit_code=exit_code,
                stdout="\n".join(stdout_lines),
                stderr="\n".join(stderr_lines),
                raw_output=planner_raw_response or md_content,
                planner_prompt=planner_prompt,
                planner_response=parsed_response_obj,
                execution_log=execution_log,
            )

        finally:
            try:
                self.fixture_manager.cleanup_temporary_fixtures()
            except Exception as e:
                logger.warning("Failed cleaning temporary fixtures: %s", e)
            if supervisor:
                supervisor.stop()

    def _save_debug_artifacts(
        self,
        stdout: str = "",
        stderr: str = "",
        raw_output: str = "",
        planner_prompt: Optional[str] = None,
        planner_response: Optional[Any] = None,
        execution_log: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist attempt-scoped debug artifacts with secrets redacted and size bounds."""
        if not (hasattr(self.active_run, "run_dir") and self.active_run.run_dir):
            return

        debug_dir = self.active_run.run_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        attempt = getattr(self.evidence, "attempt", 1)

        def _write_bounded(filename: str, content: str) -> None:
            clean = _redact_secrets(content or "")
            max_bytes = 500 * 1024
            clean_bytes = clean.encode("utf-8")
            if len(clean_bytes) > max_bytes:
                clean = clean_bytes[:max_bytes].decode("utf-8", errors="ignore") + f"\n[... Truncated remaining {len(clean_bytes) - max_bytes} bytes ...]"
            (debug_dir / filename).write_text(clean, encoding="utf-8")
            if attempt > 1:
                stem = filename.rsplit(".", 1)[0]
                ext = filename.rsplit(".", 1)[1] if "." in filename else "txt"
                att_filename = f"{stem}_attempt_{attempt}.{ext}"
                (debug_dir / att_filename).write_text(clean, encoding="utf-8")

        _write_bounded("tester_stdout.txt", stdout)
        _write_bounded("tester_stderr.txt", stderr)
        _write_bounded("tester_raw.txt", raw_output)

        if planner_prompt:
            _write_bounded("tester_planner_prompt.txt", planner_prompt)

        if planner_response:
            resp_str = json.dumps(planner_response, indent=2) if not isinstance(planner_response, str) else planner_response
            _write_bounded("tester_planner_response.json", resp_str)

        if execution_log:
            log_str = json.dumps(execution_log, indent=2)
            _write_bounded("tester_execution_log.json", log_str)

    def _build_stage_result(
        self,
        status: str,
        md_content: str,
        json_dict: dict,
        duration: float,
        exit_code: int = 0,
        reason: str = "",
        stdout: str = "",
        stderr: str = "",
        raw_output: str = "",
        planner_prompt: Optional[str] = None,
        planner_response: Optional[Any] = None,
        execution_log: Optional[Dict[str, Any]] = None,
    ) -> StageResult:
        """Construct StageResult and persist artifacts to the run directory."""
        role = Role.load("tester", project_root=self.project_root, sequence_number=4)
        rendered_prompt = RenderedPrompt.from_text("Tester v2 Empirical Execution")

        # Save attempt-scoped debug artifacts
        self._save_debug_artifacts(
            stdout=stdout or md_content,
            stderr=stderr or reason,
            raw_output=raw_output or md_content,
            planner_prompt=planner_prompt,
            planner_response=planner_response,
            execution_log=execution_log,
        )

        # Ensure status and handoff in json_dict match passed status if passed explicitly
        if status:
            json_dict["STATUS"] = status
            if json_dict.get("HANDOFF") != "NONE":
                if status in ("BLOCKED", "PARTIAL", "NOT_TESTABLE"):
                    json_dict["HANDOFF"] = "NONE"
                elif status == "PASS":
                    json_dict["HANDOFF"] = "REVIEWER"
                elif status in ("FAIL", "FAILED", "CHANGES_REQUIRED"):
                    json_dict["HANDOFF"] = "EXECUTOR"

        if reason:
            json_dict["REASON"] = reason

        # Validate with MachineReportValidator
        validated_report = MachineReportValidator.validate(
            data=json_dict,
            expected_role="TESTER",
            raw_yaml="",
        )

        # Build json_output containing both standard StageResult keys AND machine_report
        json_output = dict(json_dict)
        json_output["role"] = role.name
        json_output["sequence_number"] = role.sequence_number
        json_output["status"] = status
        json_output["handoff"] = json_dict.get(
            "HANDOFF",
            "REVIEWER" if status == "PASS" else ("NONE" if status in ("BLOCKED", "PARTIAL", "NOT_TESTABLE") else "EXECUTOR"),
        )
        json_output["duration_seconds"] = duration
        json_output["exit_code"] = exit_code
        json_output["prompt_hash"] = rendered_prompt.prompt_hash
        json_output["machine_report"] = validated_report.to_dict()

        # Save artifacts to run directory
        self.run_manager.save_stage_artifacts(
            run=self.active_run,
            sequence_number=4,
            role_name="tester",
            markdown_content=md_content,
            json_data=json_output,
        )

        from forge.adapters.base import AdapterResponse

        adapter_resp = AdapterResponse(
            stdout=md_content,
            stderr=reason if exit_code != 0 else "",
            exit_code=exit_code,
            duration_seconds=duration,
            raw_output=md_content,
        )

        success = (status == "PASS" and exit_code == 0)

        self._emit_event(
            AgentEventType.COMPLETE,
            text=f"[tester] Empirical testing finished with status {status} ({duration:.2f}s)",
            data={
                "status": status,
                "duration_seconds": duration,
                "exit_code": exit_code,
            },
        )

        return StageResult(
            role=role,
            prompt=rendered_prompt,
            response=adapter_resp,
            machine_report=validated_report,
            raw_markdown=md_content,
            duration_seconds=duration,
            success=success,
        )

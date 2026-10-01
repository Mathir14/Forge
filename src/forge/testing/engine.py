"""Tester Engine for Tester v2.

Central coordinator that integrates:
- Archetype detection
- Runtime supervision (background dev server / daemon lifecycle)
- Interaction driver execution (Web via BrowserDriver, API, CLI, Library)
- Execution budget enforcement
- Evidence collection (screenshots, telemetry, repro scripts)
- Explicit coverage calculation and report compilation
"""

import logging
import time
from pathlib import Path
from typing import List, Optional

from forge.core.context import Context
from forge.core.role import Role
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult
from forge.storage.run_manager import RunManager
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.browser import BrowserDriverFactory
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.drivers.base import InteractionDriver
from forge.testing.drivers.cli import CliInteractionDriver
from forge.testing.drivers.library import LibraryInteractionDriver
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    CoverageReport,
    Defect,
    Journey,
    JourneyResult,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner
from forge.testing.report import TesterReportGenerator
from forge.testing.supervisor import RuntimeSupervisor

logger = logging.getLogger(__name__)


class TesterEngine:
    """Coordinates empirical black-box testing for the Tester role."""
    __test__ = False

    def __init__(
        self,
        context: Context,
        run_manager: Optional[RunManager] = None,
        budget: Optional[TestingBudget] = None,
    ):
        self.context = context
        self.project_root = context.project_root
        self.active_run = context.run
        self.run_manager = run_manager or RunManager(self.project_root)
        self.budget = budget or TestingBudget()
        self.evidence = EvidenceCollector(self.active_run.run_dir)
        self.tracker = BudgetTracker(self.budget)

    def run(self) -> StageResult:
        """Execute full Tester v2 empirical verification lifecycle."""
        start_time = time.time()
        logger.info("Starting Tester v2 empirical verification for run %s", self.active_run.run_id)

        # 1. Detect archetype
        archetype = ArchetypeDetector.detect(self.project_root, self.context.config)
        logger.info("Detected project archetype: %s", archetype.value)

        # 2. Gather git context for journey planning
        git_diff = ""
        changed_files: List[str] = []
        if self.context.git and self.context.git.is_git_repo():
            try:
                git_diff = self.context.git.get_diff() or ""
                changed_files = self.context.git.get_changed_files() or []
            except Exception as e:
                logger.debug("Failed getting git diff: %s", e)

        # 3. Read prior executor report if available
        exec_report_txt = ""
        exec_md = self.run_manager.load_stage_markdown(self.active_run, "executor")
        if exec_md:
            exec_report_txt = exec_md

        # 4. Plan journeys based on task & changes
        supervisor: Optional[RuntimeSupervisor] = None
        base_url = "http://127.0.0.1:3000"
        planned_journeys: List[Journey] = []
        journey_results: List[JourneyResult] = []
        all_defects: List[Defect] = []

        try:
            # 5. Runtime Supervision (if Web or API)
            if archetype in (ProjectArchetype.WEB_SPA, ProjectArchetype.API):
                supervisor = RuntimeSupervisor(self.project_root, log_dir=self.evidence.telemetry_dir)
                started = supervisor.start(timeout=25.0)
                if not started:
                    # Application failed to start
                    coverage = CoverageReport(
                        planned_journeys=1,
                        executed_journeys=0,
                        passed_journeys=0,
                        failed_journeys=0,
                        blocked_journeys=1,
                        confidence="LOW",
                        summary="Application dev server failed to start or become ready on target port.",
                    )
                    stdout_log, stderr_log = supervisor.get_logs()
                    self.evidence.save_telemetry([], [], [], stdout_log, stderr_log)
                    md_content, json_dict = TesterReportGenerator.generate(
                        archetype=archetype,
                        runtime_target=None,
                        journey_results=[],
                        coverage=coverage,
                        duration_seconds=time.time() - start_time,
                        evidence_dir=str(self.evidence.evidence_dir),
                    )
                    return self._build_stage_result(
                        status="BLOCKED",
                        md_content=md_content,
                        json_dict=json_dict,
                        duration=time.time() - start_time,
                        exit_code=1,
                        reason="Application runtime failed to become ready.",
                    )

                if supervisor.base_url:
                    base_url = supervisor.base_url

            # 6. Plan journeys
            planned_journeys = JourneyPlanner.plan_journeys(
                task=self.active_run.task,
                archetype=archetype,
                git_diff=git_diff,
                changed_files=changed_files,
                executor_report=exec_report_txt,
                base_url=base_url,
                max_journeys=self.budget.max_journeys,
            )

            # 7. Select & initialize interaction driver
            driver: InteractionDriver
            browser_driver = None
            if archetype == ProjectArchetype.WEB_SPA:
                browser_driver = BrowserDriverFactory.create(viewport=(1440, 900))
                driver = WebInteractionDriver(browser_driver, base_url=base_url)
            elif archetype == ProjectArchetype.API:
                driver = ApiInteractionDriver(base_url=base_url)
            elif archetype == ProjectArchetype.CLI:
                driver = CliInteractionDriver(cwd=self.project_root)
            elif archetype == ProjectArchetype.LIBRARY:
                driver = LibraryInteractionDriver(project_root=self.project_root)
            else:
                browser_driver = BrowserDriverFactory.create(viewport=(1440, 900))
                driver = WebInteractionDriver(browser_driver, base_url=base_url)

            # 8. Execute journeys within budget
            try:
                for journey in planned_journeys:
                    if not self.tracker.can_start_journey():
                        logger.info("Testing budget exhausted; stopping journey execution.")
                        break

                    res = driver.execute_journey(journey, self.tracker, self.evidence)
                    journey_results.append(res)
                    all_defects.extend(res.defects)

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

            # 9. Compute explicit coverage
            executed_count = len(journey_results)
            passed_count = len([r for r in journey_results if r.status == "PASS"])
            failed_count = len([r for r in journey_results if r.status == "FAIL"])
            blocked_count = len(planned_journeys) - executed_count

            # Determine confidence
            if not planned_journeys:
                confidence = "LOW"
                summary = "No test journeys could be planned for the detected repository structure."
            elif executed_count == len(planned_journeys) and executed_count >= 3:
                confidence = "HIGH"
                summary = f"All {executed_count} planned primary, adjacent, and smoke journeys were successfully exercised."
            elif executed_count > 0:
                confidence = "MEDIUM"
                summary = f"{executed_count} of {len(planned_journeys)} planned journeys were exercised before budget ceiling."
            else:
                confidence = "LOW"
                summary = "Zero planned journeys could be exercised."

            coverage = CoverageReport(
                planned_journeys=len(planned_journeys),
                executed_journeys=executed_count,
                passed_journeys=passed_count,
                failed_journeys=failed_count,
                blocked_journeys=blocked_count,
                confidence=confidence,
                summary=summary,
            )

            # 10. Generate reports
            total_duration = time.time() - start_time
            md_content, json_dict = TesterReportGenerator.generate(
                archetype=archetype,
                runtime_target=base_url if archetype in (ProjectArchetype.WEB_SPA, ProjectArchetype.API) else "Local Process",
                journey_results=journey_results,
                coverage=coverage,
                duration_seconds=total_duration,
                evidence_dir=str(self.evidence.evidence_dir),
            )

            final_status = json_dict.get("STATUS", "PASS")
            exit_code = json_dict.get("EXIT_CODE", 0)

            return self._build_stage_result(
                status=final_status,
                md_content=md_content,
                json_dict=json_dict,
                duration=total_duration,
                exit_code=exit_code,
            )

        finally:
            if supervisor:
                supervisor.stop()

    def _build_stage_result(
        self,
        status: str,
        md_content: str,
        json_dict: dict,
        duration: float,
        exit_code: int = 0,
        reason: str = "",
    ) -> StageResult:
        """Construct StageResult and persist artifacts to the run directory."""
        role = Role.load("tester", project_root=self.project_root, sequence_number=4)
        rendered_prompt = RenderedPrompt.from_text("Tester v2 Empirical Execution")

        # Validate with MachineReportValidator
        validated_report = MachineReportValidator.validate(
            data=json_dict,
            expected_role="TESTER",
            raw_yaml="",
        )

        # Save artifacts to run directory
        self.run_manager.save_stage_artifacts(
            run=self.active_run,
            sequence_number=4,
            role_name="tester",
            markdown_content=md_content,
            json_data=json_dict,
        )

        from forge.adapters.base import AdapterResponse

        adapter_resp = AdapterResponse(
            stdout=md_content,
            stderr="",
            exit_code=exit_code,
            duration_seconds=duration,
            raw_output=md_content,
        )

        success = status in ("PASS", "NOT_TESTABLE")

        return StageResult(
            role=role,
            prompt=rendered_prompt,
            response=adapter_resp,
            machine_report=validated_report,
            raw_markdown=md_content,
            duration_seconds=duration,
            success=success,
        )

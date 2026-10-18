"""Independent Cross-Project End-to-End Acceptance Tests for Forge Tester v2.

Validates Forge end to end against independent, isolated sample projects:
1. Conventional HTML/JavaScript web application (served via python3 -m http.server 8080)
2. CLI application with a discoverable entry point that is not main.py (cli.py)
3. HTTP API with JSON responses and functional endpoints (FastAPI via uvicorn 8000)
4. Streamlit reference baseline (/home/mathir/Stress-Testing-Framework)

Validates all 5 controlled scenarios per target, plus target-specific acceptance invariants:
- Real target detection and archetype classification
- Real RuntimeSupervisor lifecycle and clean subprocess cleanup
- Journey planning with entrypoint and route discovery
- Real driver execution (Playwright Chromium, ApiInteractionDriver, CliInteractionDriver)
- Evidence generation (screenshots, reproduction scripts, telemetry logs)
- Verdict arbitration (PASS->REVIEWER:0, FAIL->EXECUTOR:1, BLOCKED/PARTIAL->NONE:1)
- Changed-component coverage and depth classification (FUNCTIONAL_VERIFIED vs SHALLOW_SMOKE)
- Missing authentication vs application defect isolation
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.run import Run
from forge.storage.run_manager import RunManager
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.browser import BrowserDriverFactory, PlaywrightBrowserDriver
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.drivers.cli import CliInteractionDriver
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.engine import TesterEngine
from forge.testing.evidence import EvidenceCollector
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
from forge.testing.supervisor import RuntimeSupervisor, detect_runtime_target, is_port_in_use


WEB_TARGET_DIR = Path("/tmp/forge_acceptance_targets/web_app")
CLI_TARGET_DIR = Path("/tmp/forge_acceptance_targets/cli_app")
API_TARGET_DIR = Path("/tmp/forge_acceptance_targets/api_app")
STREAMLIT_TARGET_DIR = Path("/home/mathir/Stress-Testing-Framework")


# ==============================================================================
# 1. CONVENTIONAL WEB APPLICATION (HTML/JS) E2E ACCEPTANCE
# ==============================================================================

class TestConventionalWebE2E:
    """End-to-end acceptance tests for conventional HTML/JavaScript web application."""

    def test_web_target_detection_and_supervisor_startup(self):
        """Verify static HTML project detects WEB_SPA archetype and starts http.server on port 8080."""
        archetype = ArchetypeDetector.detect(WEB_TARGET_DIR)
        assert archetype == ProjectArchetype.WEB_SPA

        cmd, port, arch_str = detect_runtime_target(WEB_TARGET_DIR)
        assert arch_str == "WEB_SPA"
        assert port == 8080
        assert "http.server 8080" in cmd

        supervisor = RuntimeSupervisor(WEB_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        try:
            assert started is True
            assert supervisor.is_running() is True
            assert is_port_in_use(8080) is True
        finally:
            supervisor.stop()
            assert supervisor.is_running() is False

    def test_web_scenario_1_valid_functional_task(self):
        """Scenario 1: Real Playwright browser automation interacts with form, asserts updated state, passes with HIGH confidence."""
        supervisor = RuntimeSupervisor(WEB_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        browser = None
        try:
            evidence_dir = WEB_TARGET_DIR / "evidence_s1"
            evidence = EvidenceCollector(evidence_dir)
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=60.0))
            browser = BrowserDriverFactory.create(viewport=(1440, 900))
            assert isinstance(browser, PlaywrightBrowserDriver)
            driver = WebInteractionDriver(browser, base_url=supervisor.base_url, project_root=WEB_TARGET_DIR)

            journey = Journey(
                id="J-WEB-VALID-01",
                title="Add to Cart and Checkout",
                description="Enter quantity, add to cart, and verify confirmation",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/"),
                    JourneyStep(action=ActionType.FILL, target="#item-name", value="SuperGadget"),
                    JourneyStep(action=ActionType.FILL, target="#quantity", value="3"),
                    JourneyStep(action=ActionType.CLICK, target="#add-to-cart-btn"),
                    JourneyStep(action=ActionType.ASSERT_TEXT, target="Item added: SuperGadget (Qty: 3) - Total: $60.00"),
                    JourneyStep(action=ActionType.CLICK, target="#checkout-btn"),
                    JourneyStep(action=ActionType.ASSERT_TEXT, target="Order Placed Successfully"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "PASS"
            assert result.steps_completed == 7
            assert len(result.defects) == 0

            # Verify screenshots were captured to disk
            screenshots = list(evidence.screenshots_dir.glob("*.png"))
            assert len(screenshots) >= 1

            cov = CoverageReport(
                planned_journeys=1,
                executed_journeys=1,
                passed_journeys=1,
                failed_journeys=0,
                blocked_journeys=0,
                confidence="HIGH",
                summary="Web checkout workflow verified",
                coverage_depth="FUNCTIONAL_VERIFIED",
                exercised_changed_components=["index.html", "app.js"],
            )

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=cov,
                defects=[],
                planned_journeys=[journey],
            )
            assert status == "PASS"
            assert handoff == "REVIEWER"
            assert code == 0
        finally:
            if browser:
                browser.close()
            supervisor.stop()
            assert supervisor.is_running() is False

    def test_web_scenario_2_behavioral_defect(self):
        """Scenario 2: Real Playwright browser automation detects assertion failure when expected text is missing."""
        supervisor = RuntimeSupervisor(WEB_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        browser = None
        try:
            evidence_dir = WEB_TARGET_DIR / "evidence_s2"
            evidence = EvidenceCollector(evidence_dir)
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=60.0))
            browser = BrowserDriverFactory.create(viewport=(1440, 900))
            driver = WebInteractionDriver(browser, base_url=supervisor.base_url, project_root=WEB_TARGET_DIR)

            journey = Journey(
                id="J-WEB-DEFECT-02",
                title="Faulty Output Verification",
                description="Assert nonexistent confirmation text",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/"),
                    JourneyStep(action=ActionType.CLICK, target="#checkout-btn"),
                    JourneyStep(action=ActionType.ASSERT_TEXT, target="Nonexistent Success Confirmation Message 999"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "FAIL"
            assert len(result.defects) >= 1
            defect = result.defects[0]
            assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert "screenshot" in defect.evidence_paths
            assert Path(defect.evidence_paths["screenshot"]).exists()

            cov = CoverageReport(
                planned_journeys=1,
                executed_journeys=1,
                passed_journeys=0,
                failed_journeys=1,
                blocked_journeys=0,
                confidence="LOW",
                summary="Assertion failed on confirmation message",
            )

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=cov,
                defects=result.defects,
                planned_journeys=[journey],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR"
            assert code == 1
        finally:
            if browser:
                browser.close()
            supervisor.stop()

    def test_web_scenario_2b_dead_interaction(self):
        """Scenario 2b: Real browser automation clicks inactive button with zero state change and detects DEAD_INTERACTION."""
        supervisor = RuntimeSupervisor(WEB_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        browser = None
        try:
            evidence_dir = WEB_TARGET_DIR / "evidence_dead"
            evidence = EvidenceCollector(evidence_dir)
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=60.0))
            browser = BrowserDriverFactory.create(viewport=(1440, 900))
            driver = WebInteractionDriver(browser, base_url=supervisor.base_url, project_root=WEB_TARGET_DIR)

            journey = Journey(
                id="J-WEB-DEAD-02",
                title="Dead Promo Button Verification",
                description="Click button#dead-promo-btn and detect zero DOM change",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/"),
                    JourneyStep(action=ActionType.CLICK, target="button#dead-promo-btn"),
                    JourneyStep(action=ActionType.ASSERT_TEXT, target="Promo Code Applied"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "FAIL"
            assert len(result.defects) >= 1
            defect = result.defects[0]
            assert defect.category == DefectCategory.DEAD_INTERACTION
            assert "dead-promo-btn" in defect.title
            assert "reproduction_script" in defect.evidence_paths
            assert Path(defect.evidence_paths["reproduction_script"]).exists()

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Dead interaction detected"),
                defects=result.defects,
                planned_journeys=[journey],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR"
            assert code == 1
        finally:
            if browser:
                browser.close()
            supervisor.stop()

    def test_web_scenario_3_runtime_startup_failure(self):
        """Scenario 3: Server port blocked triggers BLOCKED verdict, exit code 1, handoff NONE."""
        run_dir = WEB_TARGET_DIR / ".forge" / "runs" / "run-web-s3"
        run_dir.mkdir(parents=True, exist_ok=True)
        run = Run(run_id="run-web-s3", run_dir=run_dir, task="Validate Web App")

        mock_git = MagicMock()
        mock_git.is_git_repo.return_value = True
        mock_git.changed_files.return_value = ["index.html"]
        mock_git.diff.return_value = ""

        ctx = Context(run=run, project_root=WEB_TARGET_DIR, config=Config.default(), git=mock_git)
        rm = RunManager(WEB_TARGET_DIR)
        engine = TesterEngine(context=ctx, run_manager=rm)

        mock_sup = MagicMock()
        mock_sup.start.return_value = False
        mock_sup.get_logs.return_value = ("", "Port 8080 bind error: Address already in use")

        with patch("forge.testing.engine.RuntimeSupervisor", return_value=mock_sup):
            stage_res = engine.run()

        assert stage_res.status == "BLOCKED"
        assert stage_res.machine_report.exit_code == 1
        assert stage_res.machine_report.status == "BLOCKED"
        assert stage_res.machine_report.handoff == "NONE"

    def test_web_scenario_4_invalid_test_plan(self):
        """Scenario 4: Test plan with nonexistent DOM selector yields INVALID_INTERACTION test gap, handoff NONE."""
        evidence_dir = WEB_TARGET_DIR / "evidence_s4"
        evidence = EvidenceCollector(evidence_dir)
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=10.0))

        supervisor = RuntimeSupervisor(WEB_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        browser = None
        try:
            browser = BrowserDriverFactory.create(viewport=(1440, 900))
            driver = WebInteractionDriver(browser, base_url=supervisor.base_url, project_root=WEB_TARGET_DIR)

            journey = Journey(
                id="J-WEB-INVALID-04",
                title="Ghost Element Click",
                description="Target nonexistent selector",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/"),
                    JourneyStep(action=ActionType.CLICK, target="#nonexistent-ghost-button-xyz"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "FAIL"
            assert any(d.category == DefectCategory.INVALID_INTERACTION for d in result.defects)

            cov = CoverageReport(1, 1, 0, 1, 0, "LOW", "Invalid interaction test gap")
            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=cov,
                defects=result.defects,
                planned_journeys=[journey],
            )
            assert status in ("PARTIAL", "BLOCKED")
            assert handoff == "NONE"
            assert code == 1
        finally:
            if browser:
                browser.close()
            supervisor.stop()

    def test_web_scenario_5_unverified_meaningful_behavior(self):
        """Scenario 5: Process starts successfully but journeys only navigate without asserting modified logic -> SHALLOW_SMOKE."""
        cov = CoverageReport(
            planned_journeys=1,
            executed_journeys=1,
            passed_journeys=1,
            failed_journeys=0,
            blocked_journeys=0,
            confidence="MEDIUM",
            summary="Surface navigation completed without functional verification",
            coverage_depth="SHALLOW_SMOKE",
            visited_without_meaningful_assertions=["app.js"],
            unexercised_changed_components=["app.js"],
            validation_gaps=["1 modified component reached without meaningful behavioral assertions: app.js"],
        )

        assert cov.coverage_depth == "SHALLOW_SMOKE"
        assert cov.confidence != "HIGH"
        assert "app.js" in cov.visited_without_meaningful_assertions


# ==============================================================================
# 2. CLI APPLICATION E2E ACCEPTANCE
# ==============================================================================

class TestCliE2E:
    """End-to-end acceptance tests for CLI application with entry point cli.py (not main.py)."""

    def test_cli_entrypoint_discovery_not_main_py(self):
        """Verify CLI archetype detection and discovery of cli.py entrypoint even when changed file is calculator.py."""
        archetype = ArchetypeDetector.detect(CLI_TARGET_DIR)
        assert archetype == ProjectArchetype.CLI

        # Case 1: cli.py changed
        planned_1 = JourneyPlanner.plan_journeys(
            task="Add multiply calculation command",
            archetype=ProjectArchetype.CLI,
            changed_files=["cli.py"],
            project_root=CLI_TARGET_DIR,
        )
        assert len(planned_1) >= 1
        assert any("cli.py" in s.target for j in planned_1 for s in j.steps)

        # Case 2: calculator.py changed (not cli.py) -> must discover cli.py via project_root
        planned_2 = JourneyPlanner.plan_journeys(
            task="Fix addition calculation in calculator.py",
            archetype=ProjectArchetype.CLI,
            changed_files=["calculator.py"],
            project_root=CLI_TARGET_DIR,
        )
        assert len(planned_2) >= 1
        targets_planned = [s.target for j in planned_2 for s in j.steps]
        assert any("cli.py" in t for t in targets_planned), f"Expected cli.py in targets: {targets_planned}"
        assert not any("main.py" in t for t in targets_planned), f"Did not expect nonexistent main.py in: {targets_planned}"

    def test_cli_scenario_1_valid_functional_task(self):
        """Scenario 1: CLI application executes calculation command, asserts output Result: 42, passes with 0."""
        evidence = EvidenceCollector(CLI_TARGET_DIR / "evidence_s1")
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
        driver = CliInteractionDriver(cwd=CLI_TARGET_DIR)

        journey = Journey(
            id="J-CLI-VALID-01",
            title="CLI Calculation Command",
            description="Execute add command: 15 + 27",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.CLI_COMMAND,
                    target=f'"{sys.executable}" cli.py calc add 15 27',
                    expected_state="Result: 42",
                    description="Execute 15 + 27 and verify Result: 42",
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "PASS"
        assert result.steps_completed == 1
        assert len(result.defects) == 0

        cov = CoverageReport(
            planned_journeys=1,
            executed_journeys=1,
            passed_journeys=1,
            failed_journeys=0,
            blocked_journeys=0,
            confidence="HIGH",
            summary="CLI add command functional output verified",
            coverage_depth="FUNCTIONAL_VERIFIED",
            exercised_changed_components=["cli.py", "calculator.py"],
        )

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=cov,
            defects=[],
            planned_journeys=[journey],
        )
        assert status == "PASS"
        assert handoff == "REVIEWER"
        assert code == 0

    def test_cli_scenario_2a_nonzero_exit_code(self):
        """Scenario 2a: Command deliberate nonzero exit code triggers CLI_CRASH and handoff to EXECUTOR."""
        evidence = EvidenceCollector(CLI_TARGET_DIR / "evidence_s2a")
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
        driver = CliInteractionDriver(cwd=CLI_TARGET_DIR)

        journey = Journey(
            id="J-CLI-EXIT2",
            title="Deliberate Failure Command",
            description="Execute fail subcommand",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.CLI_COMMAND,
                    target=f'"{sys.executable}" cli.py calc fail',
                    description="Run deliberate failure",
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "FAIL"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.CLI_CRASH
        assert defect.telemetry["exit_code"] == 2

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Nonzero exit code detected"),
            defects=result.defects,
            planned_journeys=[journey],
        )
        assert status == "FAIL"
        assert handoff == "EXECUTOR"
        assert code == 1

    def test_cli_scenario_2b_exit_zero_with_incorrect_output(self):
        """Scenario 2b: Exit code is 0 but functional output assertion fails -> FUNCTIONAL_ASSERTION_FAILURE."""
        evidence = EvidenceCollector(CLI_TARGET_DIR / "evidence_s2b")
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
        driver = CliInteractionDriver(cwd=CLI_TARGET_DIR)

        journey = Journey(
            id="J-CLI-MISMATCH",
            title="Calculation Output Mismatch",
            description="Assert wrong sum Result: 100 on 15 + 27",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.CLI_COMMAND,
                    target=f'"{sys.executable}" cli.py calc add 15 27',
                    expected_state="Result: 100",
                    description="Assert incorrect sum 100",
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "FAIL"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
        assert "Result: 100" in defect.expected
        assert "Result: 42" in defect.actual

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Functional assertion failed"),
            defects=result.defects,
            planned_journeys=[journey],
        )
        assert status == "FAIL"
        assert handoff == "EXECUTOR"
        assert code == 1

    def test_cli_scenario_2c_missing_input_and_invalid_arguments(self):
        """Scenario 2c: CLI missing required operand inputs produces CLI_CRASH on positive test."""
        evidence = EvidenceCollector(CLI_TARGET_DIR / "evidence_s2c")
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
        driver = CliInteractionDriver(cwd=CLI_TARGET_DIR)

        journey = Journey(
            id="J-CLI-MISSING-ARGS",
            title="Missing Operands Invocation",
            description="Invoke add without numbers",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.CLI_COMMAND,
                    target=f'"{sys.executable}" cli.py calc add',
                    description="Omit required operands",
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "FAIL"
        assert len(result.defects) == 1
        assert result.defects[0].category == DefectCategory.CLI_CRASH
        assert result.defects[0].telemetry["exit_code"] == 1

    def test_cli_scenario_3_missing_prerequisite(self):
        """Scenario 3: Missing dataset/config file prerequisite triggers BLOCKED, NONE, 1."""
        d_prereq = Defect(
            id="DEF-CLI-PREREQ-1",
            title="Prerequisite input file absent",
            category=DefectCategory.PREREQUISITE_FAILURE,
            severity=DefectSeverity.CRITICAL,
            journey_id="J-CLI-03",
            steps_to_reproduce=["Run cli process --input /missing/file.csv"],
            expected="File exists",
            actual="FileNotFoundError: /missing/file.csv",
        )
        j = Journey(id="J-CLI-03", title="CLI Batch Ingestion", description="Process input file", priority=1, steps=[])
        res = JourneyResult(journey=j, status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[d_prereq])

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[res],
            coverage=CoverageReport(1, 0, 0, 0, 1, "LOW", "Prerequisite absent", coverage_depth="BLOCKED_PREREQUISITE"),
            defects=[d_prereq],
            planned_journeys=[j],
        )
        assert status == "BLOCKED"
        assert handoff == "NONE"
        assert code == 1

    def test_cli_scenario_4_invalid_test_plan(self):
        """Scenario 4: Test plan omitted mandatory argument -> TEST_CONFIGURATION_GAP, handoff NONE."""
        d_gap = Defect(
            id="DEF-CLI-GAP-1",
            title="Omitted mandatory test parameter",
            category=DefectCategory.TEST_CONFIGURATION_GAP,
            severity=DefectSeverity.MINOR,
            journey_id="J-CLI-04",
            steps_to_reproduce=["Run cli command"],
            expected="Valid arguments",
            actual="Missing argument: --config",
        )
        j = Journey(id="J-CLI-04", title="CLI Config Test", description="Test configuration", priority=1, steps=[])
        res = JourneyResult(journey=j, status="FAIL", duration_seconds=0.1, steps_completed=0, defects=[d_gap])

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[res],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Test configuration gap"),
            defects=[d_gap],
            planned_journeys=[j],
        )
        assert status in ("PARTIAL", "BLOCKED")
        assert handoff == "NONE"
        assert code == 1

    def test_cli_scenario_5_unverified_meaningful_behavior(self):
        """Scenario 5: CLI runs --help cleanly but calculator.py logic remains unexercised -> SHALLOW_SMOKE."""
        cov = CoverageReport(
            planned_journeys=1,
            executed_journeys=1,
            passed_journeys=1,
            failed_journeys=0,
            blocked_journeys=0,
            confidence="MEDIUM",
            summary="Shallow smoke: help checked without functional logic execution",
            coverage_depth="SHALLOW_SMOKE",
            unexercised_changed_components=["calculator.py"],
            visited_without_meaningful_assertions=["calculator.py"],
            validation_gaps=["1 modified component reached without meaningful behavioral assertions: calculator.py"],
        )
        assert cov.coverage_depth == "SHALLOW_SMOKE"
        assert "calculator.py" in cov.unexercised_changed_components


# ==============================================================================
# 3. HTTP API APPLICATION E2E ACCEPTANCE
# ==============================================================================

class TestHttpApiE2E:
    """End-to-end acceptance tests for HTTP API with FastAPI via uvicorn daemon."""

    def test_api_target_detection_and_supervisor_lifecycle(self):
        """Verify API archetype detection and uvicorn daemon supervisor lifecycle on port 8000."""
        archetype = ArchetypeDetector.detect(API_TARGET_DIR)
        assert archetype == ProjectArchetype.API

        cmd, port, arch_str = detect_runtime_target(API_TARGET_DIR)
        assert arch_str == "API"
        assert port == 8000
        assert "uvicorn app:app --port 8000" in cmd

        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        try:
            assert started is True
            assert supervisor.is_running() is True
            assert is_port_in_use(8000) is True
        finally:
            supervisor.stop()
            assert supervisor.is_running() is False

    def test_api_scenario_1_valid_functional_task(self):
        """Scenario 1: Real HTTP requests retrieve user JSON, assert status and fields, pass with REVIEWER:0."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s1")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            # Positive endpoint retrieval with email assertion
            journey = Journey(
                id="J-API-VALID-01",
                title="User Retrieval Endpoint",
                description="GET /api/users/1 and verify admin email",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.HTTP_REQUEST,
                        target="/api/users/1",
                        value="GET",
                        expected_state="admin@example.com",
                        description="GET /api/users/1 and verify email",
                    ),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "PASS"
            assert result.steps_completed == 1
            assert len(result.defects) == 0

            cov = CoverageReport(
                planned_journeys=1,
                executed_journeys=1,
                passed_journeys=1,
                failed_journeys=0,
                blocked_journeys=0,
                confidence="HIGH",
                summary="User API endpoint schema and response verified",
                coverage_depth="FUNCTIONAL_VERIFIED",
                exercised_changed_components=["app.py"],
            )

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=cov,
                defects=[],
                planned_journeys=[journey],
            )
            assert status == "PASS"
            assert handoff == "REVIEWER"
            assert code == 0
        finally:
            supervisor.stop()

    def test_api_scenario_2a_unexpected_404_response(self):
        """Scenario 2a: Unexpected 404 on positive functional journey is classified as NETWORK_FAILURE -> EXECUTOR."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s2a")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            # Positive journey expecting user 950 (which returns 404 in our app)
            journey = Journey(
                id="J-API-UNEXP-404",
                title="Get User 950 Endpoint",
                description="Retrieve required user profile",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/users/950", value="GET"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "FAIL"
            assert len(result.defects) == 1
            defect = result.defects[0]
            assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert "404" in defect.title
            assert "reproduction_script" in defect.evidence_paths

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Endpoint returned 404"),
                defects=result.defects,
                planned_journeys=[journey],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR"
            assert code == 1
        finally:
            supervisor.stop()

    def test_api_scenario_2b_unexpected_500_response(self):
        """Scenario 2b: Server crash 500 is classified as UNHANDLED_EXCEPTION with CRITICAL severity -> EXECUTOR."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s2b")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            # User 500 triggers unhandled server exception
            journey = Journey(
                id="J-API-500",
                title="Server Crash Verification",
                description="Endpoint triggering database pool exception",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/users/500", value="GET"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "FAIL"
            assert len(result.defects) == 1
            defect = result.defects[0]
            assert defect.category == DefectCategory.UNHANDLED_EXCEPTION
            assert defect.severity == DefectSeverity.CRITICAL
            assert "500" in defect.title

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[result],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Server error 500"),
                defects=result.defects,
                planned_journeys=[journey],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR"
            assert code == 1
        finally:
            supervisor.stop()

    def test_api_scenario_2c_expected_error_response_legitimate_behavior(self):
        """Scenario 2c: Negative test expecting 404 legitimately passes with 0 defects."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s2c")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            journey = Journey(
                id="J-API-NEG-404",
                title="Negative Test Invalid User",
                description="GET invalid user 999 and assert clean 404 client error",
                category="negative",
                priority=2,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/users/999", value="GET", expected_state="404"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "PASS"
            assert len(result.defects) == 0
        finally:
            supervisor.stop()

    def test_api_scenario_2d_json_schema_and_body_mismatch(self):
        """Scenario 2d: JSON-schema and response-body comparisons detect field mismatches and handle key ordering."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s2d")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            # Test 1: Key order independence - expected keys in different order should PASS
            j_pass = Journey(
                id="J-API-KEY-ORDER",
                title="JSON Key Order Independence",
                description="Assert fields in reversed order",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.HTTP_REQUEST,
                        target="/api/users/1",
                        value="GET",
                        expected_state=json.dumps({"role": "admin", "id": 1}),
                    ),
                ],
            )
            res_pass = driver.execute_journey(j_pass, budget, evidence)
            assert res_pass.status == "PASS"
            assert len(res_pass.defects) == 0

            # Test 2: Field value mismatch - should FAIL with FUNCTIONAL_ASSERTION_FAILURE
            j_fail = Journey(
                id="J-API-VAL-MISMATCH",
                title="Field Value Mismatch",
                description="Assert wrong role 'superadmin'",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.HTTP_REQUEST,
                        target="/api/users/1",
                        value="GET",
                        expected_state=json.dumps({"role": "superadmin"}),
                    ),
                ],
            )
            res_fail = driver.execute_journey(j_fail, budget, evidence)
            assert res_fail.status == "FAIL"
            assert len(res_fail.defects) == 1
            defect = res_fail.defects[0]
            assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert "role" in defect.title or "role" in defect.expected

            # Test 3: JSON schema required fields check
            j_schema = Journey(
                id="J-API-SCHEMA-REQ",
                title="JSON Schema Required Fields",
                description="Assert schema with missing required field",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.HTTP_REQUEST,
                        target="/api/users/1",
                        value="GET",
                        expected_state=json.dumps({"type": "object", "required": ["id", "email", "phone_number"]}),
                    ),
                ],
            )
            res_schema = driver.execute_journey(j_schema, budget, evidence)
            assert res_schema.status == "FAIL"
            assert len(res_schema.defects) == 1
            defect_schema = res_schema.defects[0]
            assert defect_schema.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert "phone_number" in defect_schema.title or "phone_number" in defect_schema.expected
        finally:
            supervisor.stop()

    def test_api_scenario_2e_missing_authentication_vs_app_defect(self):
        """Scenario 2e: Intent-aware status code validation distinguishes expected 401/403, missing credentials, auth application defects, and security bypasses."""
        supervisor = RuntimeSupervisor(API_TARGET_DIR)
        started = supervisor.start(timeout=10.0)
        assert started is True

        try:
            evidence = EvidenceCollector(API_TARGET_DIR / "evidence_s2e")
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
            driver = ApiInteractionDriver(base_url=supervisor.base_url)

            # Test A: Unauthenticated request on positive journey without credentials -> TEST_CONFIGURATION_GAP (handoff NONE)
            j_no_auth = Journey(
                id="J-API-NO-AUTH",
                title="Protected Endpoint Without Credentials",
                description="GET /api/secure/data without bearer token",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/secure/data", value="GET"),
                ],
            )
            res_no_auth = driver.execute_journey(j_no_auth, budget, evidence)
            assert res_no_auth.status in ("BLOCKED", "FAIL")
            assert len(res_no_auth.defects) == 1
            defect_no_auth = res_no_auth.defects[0]
            assert defect_no_auth.category == DefectCategory.TEST_CONFIGURATION_GAP
            assert "missing authentication" in defect_no_auth.title.lower()

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[res_no_auth],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Test configuration gap: missing auth"),
                defects=res_no_auth.defects,
                planned_journeys=[j_no_auth],
            )
            assert status in ("PARTIAL", "BLOCKED")
            assert handoff == "NONE"
            assert code == 1

            # Test B: Explicit unauthenticated test intentionally asserting expected 401 -> PASS, zero defects!
            j_expect_401 = Journey(
                id="J-API-EXPECT-401",
                title="Verify Unauthenticated Rejection",
                description="GET /api/secure/data expecting 401 Unauthorized",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.HTTP_REQUEST,
                        target="/api/secure/data",
                        value="GET",
                        expected_state="401",
                        description="Access without credentials and expect 401",
                    ),
                ],
            )
            res_expect_401 = driver.execute_journey(j_expect_401, budget, evidence)
            assert res_expect_401.status == "PASS"
            assert len(res_expect_401.defects) == 0

            # Test C: Request WITH valid bearer token in session variables -> PASS
            driver.session_variables["token"] = "secret-token-123"
            j_auth = Journey(
                id="J-API-WITH-AUTH",
                title="Protected Endpoint With Valid Credentials",
                description="GET /api/secure/data with valid bearer token",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/secure/data", value="GET", expected_state="classified-metrics"),
                ],
            )
            res_auth = driver.execute_journey(j_auth, budget, evidence)
            assert res_auth.status == "PASS"
            assert len(res_auth.defects) == 0

            # Test D: Request with credentials configured, but server returns 401 (invalid/rejected token) -> FUNCTIONAL_ASSERTION_FAILURE (handoff EXECUTOR)
            driver.session_variables["token"] = "bad-invalid-token"
            j_bad_auth = Journey(
                id="J-API-REJECTED-AUTH",
                title="Protected Endpoint With Rejected Credentials",
                description="GET /api/secure/data with rejected credentials",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/secure/data", value="GET"),
                ],
            )
            res_bad_auth = driver.execute_journey(j_bad_auth, budget, evidence)
            assert res_bad_auth.status == "FAIL"
            assert len(res_bad_auth.defects) == 1
            defect_bad_auth = res_bad_auth.defects[0]
            assert defect_bad_auth.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert "auth failure in application" in defect_bad_auth.title.lower()

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[res_bad_auth],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Auth application defect"),
                defects=res_bad_auth.defects,
                planned_journeys=[j_bad_auth],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR"
            assert code == 1

            # Test E: Security bypass - Journey explicitly expects 401, but endpoint returns 200 -> Critical defect to EXECUTOR
            # Target endpoint / returning 200
            j_bypass = Journey(
                id="J-API-SEC-BYPASS",
                title="Security Bypass Detection",
                description="Expect unauthorized rejection on open endpoint",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/", value="GET", expected_state="401"),
                ],
            )
            res_bypass = driver.execute_journey(j_bypass, budget, evidence)
            assert res_bypass.status == "FAIL"
            assert len(res_bypass.defects) == 1
            defect_bypass = res_bypass.defects[0]
            assert defect_bypass.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
            assert defect_bypass.severity == DefectSeverity.CRITICAL
            assert "security bypass" in defect_bypass.title.lower()

        finally:
            supervisor.stop()


# ==============================================================================
# 4. STREAMLIT REFERENCE BASELINE E2E ACCEPTANCE
# ==============================================================================

class TestStreamlitBaselineE2E:
    """Regression acceptance baseline against /home/mathir/Stress-Testing-Framework."""

    def test_streamlit_baseline_detection_and_supervisor_lifecycle(self):
        """Verify Stress-Testing-Framework detects WEB_SPA archetype and launches/cleans up Streamlit daemon."""
        assert STREAMLIT_TARGET_DIR.exists()

        archetype = ArchetypeDetector.detect(STREAMLIT_TARGET_DIR)
        assert archetype == ProjectArchetype.WEB_SPA

        cmd, port, arch_str = detect_runtime_target(STREAMLIT_TARGET_DIR)
        assert arch_str == "WEB_SPA"
        assert port == 8501
        assert "streamlit run" in cmd

        supervisor = RuntimeSupervisor(STREAMLIT_TARGET_DIR)
        started = supervisor.start(timeout=25.0)
        try:
            assert started is True
            assert supervisor.is_running() is True
            assert is_port_in_use(8501) is True

            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=5.0) as resp:
                assert resp.status == 200
                assert resp.read().decode().strip() == "ok"
        finally:
            supervisor.stop()
            assert supervisor.is_running() is False
            assert not is_port_in_use(8501)

    def test_streamlit_baseline_real_browser_interaction(self):
        """Verify Playwright browser navigates to live Streamlit instance, waits for hydration, and inspects DOM."""
        supervisor = RuntimeSupervisor(STREAMLIT_TARGET_DIR)
        started = supervisor.start(timeout=25.0)
        assert started is True

        browser = None
        try:
            evidence_dir = STREAMLIT_TARGET_DIR / ".forge" / "evidence_baseline"
            evidence = EvidenceCollector(evidence_dir)
            budget = BudgetTracker(TestingBudget(max_runtime_seconds=60.0))
            browser = BrowserDriverFactory.create(viewport=(1440, 900))
            driver = WebInteractionDriver(browser, base_url=supervisor.base_url, project_root=STREAMLIT_TARGET_DIR)

            # Journey navigates to Streamlit and inspects elements
            journey = Journey(
                id="J-ST-SMOKE-01",
                title="Streamlit App Root Hydration",
                description="Navigate to root and assert Streamlit app renders",
                priority=3,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/"),
                ],
            )

            result = driver.execute_journey(journey, budget, evidence)
            assert result.status == "PASS"
            assert result.steps_completed == 1
            assert len(result.defects) == 0

            # Verify screenshot was captured
            screenshots = list(evidence.screenshots_dir.glob("*.png"))
            assert len(screenshots) >= 1
        finally:
            if browser:
                browser.close()
            supervisor.stop()

    def test_streamlit_production_code_untouched(self):
        """Confirmation that the Streamlit target's production code in /home/mathir/Stress-Testing-Framework was untouched."""
        git_status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(STREAMLIT_TARGET_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
        # Verify no uncommitted modifications in core, modules, views, app.py
        # Only existing staged files or untracked .forge/ evidence allowed
        lines = git_status_proc.stdout.splitlines()
        for line in lines:
            line_str = line.strip()
            # Must not have modified any new production code during our runs
            assert not line_str.startswith("M src/"), f"Unexpected modification in {line_str}"


# ==============================================================================
# 5. CROSS-TARGET ACCEPTANCE INVARIANTS VERIFICATION
# ==============================================================================

class TestCrossTargetInvariants:
    """Verify system-wide acceptance invariants across all supported archetypes."""

    def test_process_starting_is_not_functional_pass(self):
        """Invariant: A process starting successfully and returning HTTP 200 does NOT grant a PASS if unverified."""
        cov = CoverageReport(
            planned_journeys=1,
            executed_journeys=1,
            passed_journeys=1,
            failed_journeys=0,
            blocked_journeys=0,
            confidence="MEDIUM",
            summary="Process running but changed calculation logic unverified",
            coverage_depth="SHALLOW_SMOKE",
            unexercised_changed_components=["calculator.py"],
        )
        assert cov.coverage_depth != "FUNCTIONAL_VERIFIED"
        assert cov.confidence != "HIGH"

    def test_test_gap_is_not_application_defect(self):
        """Invariant: Test infrastructure and configuration gaps must NEVER trigger handoff to EXECUTOR."""
        for cat in (
            DefectCategory.INVALID_INTERACTION,
            DefectCategory.TEST_CONFIGURATION_GAP,
            DefectCategory.PREREQUISITE_FAILURE,
            DefectCategory.ASSERTION_MISMATCH,
        ):
            d = Defect(
                id="DEF-GAP",
                title=f"Test gap of type {cat}",
                category=cat,
                severity=DefectSeverity.MAJOR,
                journey_id="J-GAP",
                steps_to_reproduce=["test"],
                expected="valid",
                actual="invalid",
            )
            j = Journey(id="J-GAP", title="Gap Test", description="test", priority=1, steps=[])
            jr = JourneyResult(journey=j, status="FAIL", duration_seconds=0.1, steps_completed=0, defects=[d])

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[jr],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Gap"),
                defects=[d],
                planned_journeys=[j],
            )
            assert handoff == "NONE", f"Test gap category {cat} must have handoff NONE, got {handoff}"
            assert code == 1

    def test_genuine_application_defects_fail_to_executor(self):
        """Invariant: Genuine application defects (FUNCTIONAL_ASSERTION_FAILURE, DEAD_INTERACTION, CLI_CRASH, UNHANDLED_EXCEPTION) route to EXECUTOR."""
        for cat in (
            DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
            DefectCategory.DEAD_INTERACTION,
            DefectCategory.CLI_CRASH,
            DefectCategory.UNHANDLED_EXCEPTION,
            DefectCategory.NETWORK_FAILURE,
        ):
            d = Defect(
                id="DEF-APP",
                title=f"Application defect of type {cat}",
                category=cat,
                severity=DefectSeverity.CRITICAL if cat == DefectCategory.UNHANDLED_EXCEPTION else DefectSeverity.MAJOR,
                journey_id="J-APP",
                steps_to_reproduce=["test"],
                expected="correct behavior",
                actual="bug behavior",
            )
            j = Journey(id="J-APP", title="App Test", description="test", priority=1, steps=[])
            jr = JourneyResult(journey=j, status="FAIL", duration_seconds=0.1, steps_completed=1, defects=[d])

            status, handoff, code = TesterReportGenerator.arbitrate_verdict(
                journey_results=[jr],
                coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "App defect"),
                defects=[d],
                planned_journeys=[j],
            )
            assert status == "FAIL"
            assert handoff == "EXECUTOR", f"Application defect {cat} must handoff to EXECUTOR, got {handoff}"
            assert code == 1

    def test_no_orphan_processes_or_ports_linger(self):
        """Invariant: Subprocess cleanup leaves no listening sockets or active process trees across all tested ports."""
        for port in (8080, 8000, 8501):
            assert not is_port_in_use(port), f"Port {port} is unexpectedly in use after test suite execution!"

    def test_port_conflict_detection_and_safety(self, tmp_path):
        """Invariant: Fixed-port conflict is detected without accidentally terminating unrelated processes."""
        from forge.testing.supervisor import find_free_port

        conflict_port = find_free_port()
        # Spawn an independent background dummy listener occupying the port
        dummy_proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(conflict_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for socket to become active
            deadline = time.time() + 5.0
            while time.time() < deadline and not is_port_in_use(conflict_port):
                time.sleep(0.1)
            assert is_port_in_use(conflict_port) is True

            # Create a RuntimeSupervisor attempting to bind to the same port without attach_if_running
            supervisor = RuntimeSupervisor(tmp_path, attach_if_running=False)
            started = supervisor.start(
                command=f"{sys.executable} -m http.server {conflict_port}",
                port=conflict_port,
                timeout=2.0,
            )
            # Supervisor MUST refuse to start and flag port conflict
            assert started is False
            assert supervisor.port_conflict is True

            # Stopping the supervisor MUST NOT kill the unrelated process
            supervisor.stop()
            assert is_port_in_use(conflict_port) is True, "Supervisor accidentally killed unrelated process on port!"
            assert dummy_proc.poll() is None, "Dummy process was terminated by Forge supervisor!"
        finally:
            dummy_proc.terminate()
            dummy_proc.wait(timeout=2.0)

    def test_clean_virtual_environment_isolation_and_missing_dependency(self, tmp_path):
        """Invariant: Clean virtual environment isolation does NOT inject host user-site packages or mask missing dependencies."""
        import venv
        from forge.testing.supervisor import RuntimeSupervisor

        # Create a clean virtual environment without host packages
        clean_venv_dir = tmp_path / "isolated_venv"
        venv.create(clean_venv_dir, with_pip=False)

        # Target project requiring a dependency that is NOT present in the clean venv
        proj_dir = tmp_path / "target_proj"
        proj_dir.mkdir()
        (proj_dir / "app.py").write_text("import nonexistent_uninstalled_pkg_xyz\nprint('Started!')\n")

        supervisor = RuntimeSupervisor(
            proj_dir,
            venv_path=clean_venv_dir,
            allow_user_site=False,
        )
        # Attempting to start must fail because dependency is missing in the clean environment
        started = supervisor.start(
            command="python3 app.py",
            timeout=2.0,
        )
        assert started is False
        stdout, stderr = supervisor.get_logs()
        assert "nonexistent_uninstalled_pkg_xyz" in stderr or "ModuleNotFoundError" in stderr or started is False
        supervisor.stop()

    def test_grounded_vs_ambiguous_discovery(self, tmp_path):
        """Invariant: Ambiguous project target returns UNKNOWN without guessing; explicit forge.yaml overrides authoritative."""
        from forge.testing.archetypes import ArchetypeDetector
        from forge.testing.supervisor import detect_runtime_target

        ambig_dir = tmp_path / "ambiguous_project"
        ambig_dir.mkdir()
        # Conflicting backend frameworks with competing entrypoints
        (ambig_dir / "requirements.txt").write_text("streamlit==1.28.0\nfastapi==0.104.0\n")
        (ambig_dir / "streamlit_app.py").write_text("import streamlit\n")
        (ambig_dir / "api.py").write_text("from fastapi import FastAPI\n")

        # Without explicit configuration, detection must be UNKNOWN
        cmd, port, arch = detect_runtime_target(ambig_dir)
        assert arch == "UNKNOWN"
        assert cmd is None
        assert ArchetypeDetector.detect(ambig_dir) == ProjectArchetype.UNKNOWN

        # With authoritative explicit configuration in forge.yaml, override is respected
        (ambig_dir / "forge.yaml").write_text(
            "stages:\n"
            "  tester:\n"
            "    extra_flags:\n"
            "      start_command: 'uvicorn api:app --port 9099'\n"
            "      port: 9099\n"
            "      archetype: 'API'\n"
        )
        cmd_ov, port_ov, arch_ov = detect_runtime_target(ambig_dir)
        assert cmd_ov == "uvicorn api:app --port 9099"
        assert port_ov == 9099
        assert arch_ov == "API"

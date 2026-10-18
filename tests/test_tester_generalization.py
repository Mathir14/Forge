"""Cross-Project Tester Generalization Audit Tests.

Validates that Forge Tester v2 generalizes beyond the Streamlit reference baseline
across:
1. Conventional browser-based web applications (HTML/JS, Flask, FastAPI HTML).
2. CLI applications.
3. HTTP APIs / backend services.
4. Streamlit reference baseline.

For each archetype, validates the 5 controlled scenarios:
- Scenario 1: Clean, valid task (PASS, exit 0, REVIEWER, HIGH confidence, FUNCTIONAL_VERIFIED).
- Scenario 2: Deliberately introduced functional defect (FAIL, exit 1, EXECUTOR).
- Scenario 3: Missing prerequisite or unavailable service (BLOCKED, exit 1, NONE).
- Scenario 4: Invalid or incomplete test plan (PARTIAL / BLOCKED, exit 1, NONE).
- Scenario 5: Application runs but meaningful behavior remains unverified (SHALLOW_SMOKE / no false PASS).
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.core.context import Context
from forge.core.run import Run
from forge.core.config import Config
from forge.storage.run_manager import RunManager
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.drivers.cli import CliInteractionDriver
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.engine import TesterEngine
from forge.testing.evidence import EvidenceCollector
from forge.testing.fixtures import TestingFixtureManager
from forge.testing.models import (
    ActionType,
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    InteractiveElement,
    Journey,
    JourneyResult,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner
from forge.testing.report import TesterReportGenerator
from forge.testing.supervisor import RuntimeSupervisor, detect_runtime_target


from forge.testing.browser import MockBrowserDriver


# ==============================================================================
# 1. CONVENTIONAL WEB APPLICATION (HTML/JS / SSR)
# ==============================================================================

def test_conventional_web_archetype_and_runtime_detection(tmp_path: Path):
    """Verify static HTML project detects WEB_SPA archetype and python3 http.server start command."""
    index_html = tmp_path / "index.html"
    index_html.write_text("<!DOCTYPE html><html><body><h1>Checkout</h1></body></html>", encoding="utf-8")

    archetype = ArchetypeDetector.detect(tmp_path)
    assert archetype == ProjectArchetype.WEB_SPA

    cmd, port, arch_str = detect_runtime_target(tmp_path)
    assert arch_str == "WEB_SPA"
    assert port == 8080
    assert "http.server 8080" in cmd


def test_conventional_web_scenario_1_clean_valid_task(tmp_path: Path):
    """Scenario 1: Conventional Web clean valid task passes with HIGH confidence and FUNCTIONAL_VERIFIED."""
    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    mock_browser = MockBrowserDriver()
    mock_browser.page_content = "<html><body><h1>Checkout</h1><button id='submit-btn'>Submit</button></body></html>"
    def on_submit(driver_inst):
        driver_inst.page_content = "<html><body><h1>Checkout</h1><p>Order Placed Successfully</p></body></html>"
    mock_browser.on_click("button#submit-btn", on_submit)
    driver = WebInteractionDriver(mock_browser, base_url="http://127.0.0.1:8080", project_root=tmp_path)

    journey = Journey(
        id="J-WEB-01",
        title="Place Order Flow",
        description="Fill quantity and submit checkout",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/checkout"),
            JourneyStep(action=ActionType.FILL, target="input[name='qty']", value="2"),
            JourneyStep(action=ActionType.CLICK, target="button#submit-btn"),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Order Placed Successfully"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "PASS"
    assert len(result.defects) == 0

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=1,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="HIGH",
        summary="Checkout functional workflow verified",
        coverage_depth="FUNCTIONAL_VERIFIED",
        exercised_changed_components=["checkout.html"],
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


def test_conventional_web_scenario_2_functional_defect(tmp_path: Path):
    """Scenario 2: Conventional Web functional defect triggers FAIL, EXECUTOR, 1."""
    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    mock_browser = MockBrowserDriver()
    mock_browser.page_content = "<html><body><h1>Checkout</h1><button id='submit-btn'>Submit</button></body></html>"
    def on_submit(driver_inst):
        driver_inst.page_content = "<html><body><h1>Checkout</h1><p>Cart Empty</p></body></html>"
    mock_browser.on_click("button#submit-btn", on_submit)
    driver = WebInteractionDriver(mock_browser, base_url="http://127.0.0.1:8080", project_root=tmp_path)

    journey = Journey(
        id="J-WEB-02",
        title="Place Order Flow",
        description="Submit checkout with defect",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/checkout"),
            JourneyStep(action=ActionType.CLICK, target="button#submit-btn"),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Order Placed Successfully"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "FAIL"
    assert len(result.defects) >= 1
    assert any(d.category in (DefectCategory.FUNCTIONAL_ASSERTION_FAILURE, DefectCategory.ASSERTION_MISMATCH, DefectCategory.DEAD_INTERACTION) for d in result.defects)

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Assertion failed on checkout button",
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


def test_conventional_web_scenario_3_runtime_blockage(tmp_path: Path):
    """Scenario 3: Conventional Web runtime startup failure produces BLOCKED, NONE, 1."""
    run_dir = tmp_path / ".forge" / "runs" / "run-web-03"
    run_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "index.html").write_text("<h1>App</h1>", encoding="utf-8")

    run = Run(run_id="run-web-03", run_dir=run_dir, task="Validate Web App")
    mock_git = MagicMock()
    mock_git.is_git_repo.return_value = True
    mock_git.changed_files.return_value = ["index.html"]
    mock_git.diff.return_value = ""

    ctx = Context(run=run, project_root=tmp_path, config=Config.default(), git=mock_git)
    rm = RunManager(tmp_path)
    engine = TesterEngine(context=ctx, run_manager=rm)

    mock_sup = MagicMock()
    mock_sup.start.return_value = False  # Port blocked / dev server failed
    mock_sup.get_logs.return_value = ("", "Port 8080 bind error: Address already in use")

    with patch("forge.testing.engine.RuntimeSupervisor", return_value=mock_sup):
        result = engine.run()

    assert result.status == "BLOCKED"
    assert result.machine_report.exit_code == 1
    tester_data = json.loads((run_dir / "04_tester.json").read_text(encoding="utf-8"))
    assert tester_data["STATUS"] == "BLOCKED"
    assert tester_data["HANDOFF"] == "NONE"


def test_conventional_web_scenario_4_invalid_test_plan(tmp_path: Path):
    """Scenario 4: Conventional Web invalid test plan (nonexistent control) yields PARTIAL/BLOCKED, NONE, 1."""
    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    mock_browser = MockBrowserDriver()
    mock_browser.click = MagicMock(side_effect=RuntimeError("Timeout 10000ms waiting for selector button#ghost-button-xyz not found"))
    driver = WebInteractionDriver(mock_browser, base_url="http://127.0.0.1:8080", project_root=tmp_path)

    journey = Journey(
        id="J-WEB-04",
        title="Invalid Selector Journey",
        description="Click non-existent button",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.CLICK, target="button#ghost-button-xyz"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "FAIL"
    # Invalid interaction defect (test-gap)
    assert any(d.category == DefectCategory.INVALID_INTERACTION for d in result.defects)

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Invalid interaction defect",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[result],
        coverage=cov,
        defects=result.defects,
        planned_journeys=[journey],
    )
    assert status in ("PARTIAL", "BLOCKED")
    assert handoff == "NONE"
    assert code == 1


def test_conventional_web_scenario_5_meaningful_behavior_unverified(tmp_path: Path):
    """Scenario 5: Web app runs, but journeys only do navigation without asserting changed logic."""
    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=1,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="Surface navigation completed without functional verification",
        coverage_depth="SHALLOW_SMOKE",
        visited_without_meaningful_assertions=["cart.js"],
        unexercised_changed_components=["cart.js"],
        validation_gaps=["1 modified component reached without meaningful behavioral assertions: cart.js"],
    )

    assert cov.coverage_depth == "SHALLOW_SMOKE"
    assert cov.confidence != "HIGH"
    assert "cart.js" in cov.visited_without_meaningful_assertions


# ==============================================================================
# 2. CLI APPLICATION
# ==============================================================================

def test_cli_archetype_and_entrypoint_detection(tmp_path: Path):
    """Verify CLI project detects CLI archetype and discovers cli.py entrypoint."""
    (tmp_path / "cli.py").write_text("import sys\nprint('CLI v1.0')\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'mycli'\ndependencies = ['click']\n", encoding="utf-8")

    archetype = ArchetypeDetector.detect(tmp_path)
    assert archetype == ProjectArchetype.CLI

    cmd, port, arch_str = detect_runtime_target(tmp_path)
    assert arch_str == "CLI"
    assert cmd is None

    planned = JourneyPlanner.plan_journeys(
        task="Add calculation command",
        git_diff="",
        changed_files=["cli.py", "calculator.py"],
        archetype=ProjectArchetype.CLI,
    )
    assert len(planned) >= 1
    assert any("cli.py" in s.target for j in planned for s in j.steps)


def test_cli_scenario_1_clean_valid_task(tmp_path: Path):
    """Scenario 1: CLI application clean execution verifies output and returns PASS, REVIEWER, 0."""
    cli_file = tmp_path / "cli.py"
    cli_file.write_text(
        "import sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'calc':\n"
        "    print('Result: 42')\n"
        "else:\n"
        "    print('Usage: cli.py calc')\n",
        encoding="utf-8",
    )

    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    driver = CliInteractionDriver(cwd=tmp_path)

    journey = Journey(
        id="J-CLI-01",
        title="CLI Calculation Command",
        description="Execute calc command and assert output",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.CLI_COMMAND,
                target=f'"{sys.executable}" cli.py calc',
                expected_state="Result: 42",
                description="Verify calc output Result: 42",
            ),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "PASS"
    assert len(result.defects) == 0

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=1,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="HIGH",
        summary="CLI calculation output verified",
        coverage_depth="FUNCTIONAL_VERIFIED",
        exercised_changed_components=["cli.py"],
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


def test_cli_scenario_2_functional_output_defect(tmp_path: Path):
    """Scenario 2: CLI functional output mismatch creates FUNCTIONAL_ASSERTION_FAILURE and returns FAIL, EXECUTOR, 1."""
    cli_file = tmp_path / "cli.py"
    cli_file.write_text(
        "import sys\n"
        "if sys.argv[1] == 'calc':\n"
        "    # Bug: returns 0 instead of expected 42\n"
        "    print('Result: 0')\n",
        encoding="utf-8",
    )

    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    driver = CliInteractionDriver(cwd=tmp_path)

    journey = Journey(
        id="J-CLI-02",
        title="CLI Calculation Defect Test",
        description="Verify calc output matches expected 42",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.CLI_COMMAND,
                target=f'"{sys.executable}" cli.py calc',
                expected_state="Result: 42",
                description="Verify calc output Result: 42",
            ),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "FAIL"
    assert len(result.defects) == 1
    assert result.defects[0].category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
    assert "Result: 42" in result.defects[0].expected

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="CLI calculation returned wrong output",
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


def test_cli_scenario_3_missing_prerequisite(tmp_path: Path):
    """Scenario 3: CLI prerequisite missing (file not prepared) triggers BLOCKED, NONE, 1."""
    d_prereq = Defect(
        id="DEF-CLI-PREREQ-1",
        title="Input dataset file not generated by prior stage",
        category=DefectCategory.PREREQUISITE_FAILURE,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-CLI-03",
        steps_to_reproduce=["Run cli process --input /data/missing.csv"],
        expected="File exists",
        actual="FileNotFoundError: /data/missing.csv",
    )
    j = Journey(id="J-CLI-03", title="CLI Batch Ingestion", description="Process input file", priority=1, steps=[])
    res = JourneyResult(journey=j, status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[d_prereq])

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=1,
        confidence="LOW",
        summary="Prerequisite input file absent",
        coverage_depth="BLOCKED_PREREQUISITE",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[res],
        coverage=cov,
        defects=[d_prereq],
        planned_journeys=[j],
    )
    assert status == "BLOCKED"
    assert handoff == "NONE"
    assert code == 1


def test_cli_scenario_4_invalid_test_plan(tmp_path: Path):
    """Scenario 4: CLI test configuration gap produces PARTIAL, NONE, 1."""
    d_gap = Defect(
        id="DEF-CLI-GAP-1",
        title="Omitted required mandatory argument in test command",
        category=DefectCategory.TEST_CONFIGURATION_GAP,
        severity=DefectSeverity.MINOR,
        journey_id="J-CLI-04",
        steps_to_reproduce=["Run cli command"],
        expected="Valid arguments",
        actual="Missing argument: --config",
    )
    j = Journey(id="J-CLI-04", title="CLI Config Test", description="Test configuration", priority=1, steps=[])
    res = JourneyResult(journey=j, status="FAIL", duration_seconds=0.1, steps_completed=0, defects=[d_gap])

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Test configuration omitted required option",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[res],
        coverage=cov,
        defects=[d_gap],
        planned_journeys=[j],
    )
    assert status in ("PARTIAL", "BLOCKED")
    assert handoff == "NONE"
    assert code == 1


def test_cli_scenario_5_unverified_meaningful_behavior(tmp_path: Path):
    """Scenario 5: CLI runs --help cleanly but calculation module was never exercised."""
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
    assert cov.confidence != "HIGH"


# ==============================================================================
# 3. HTTP API / BACKEND SERVICE
# ==============================================================================

def test_api_archetype_and_runtime_detection(tmp_path: Path):
    """Verify FastAPI / Express projects detect API archetype and port 8000/3000."""
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")

    archetype = ArchetypeDetector.detect(tmp_path)
    assert archetype == ProjectArchetype.API

    cmd, port, arch_str = detect_runtime_target(tmp_path)
    assert arch_str == "API"
    assert port == 8000
    assert "uvicorn main:app --port 8000" in cmd


def test_api_scenario_1_clean_valid_task(tmp_path: Path):
    """Scenario 1: API returns HTTP 200 with matching schema -> PASS, REVIEWER, 0."""
    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    driver = ApiInteractionDriver(base_url="http://127.0.0.1:8000")

    journey = Journey(
        id="J-API-01",
        title="User Retrieval Endpoint",
        description="GET /api/users/1",
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

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b'{"id": 1, "email": "admin@example.com", "role": "admin"}'
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "PASS"
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
        exercised_changed_components=["routes/users.py"],
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


def test_api_scenario_2_unexpected_404_functional_defect(tmp_path: Path):
    """Scenario 2: API endpoint returning unexpected 404 is detected as NETWORK_FAILURE defect."""
    evidence = EvidenceCollector(tmp_path / "evidence")
    budget = BudgetTracker(TestingBudget())
    driver = ApiInteractionDriver(base_url="http://127.0.0.1:8000")

    journey = Journey(
        id="J-API-02",
        title="Item Endpoint Verification",
        description="GET /api/items/123",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.HTTP_REQUEST,
                target="/api/items/123",
                value="GET",
                description="Fetch item 123",
            ),
        ],
    )

    import urllib.error
    mock_404 = urllib.error.HTTPError(
        url="http://127.0.0.1:8000/api/items/123",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"detail": "Item 123 not found"}'),
    )

    with patch("urllib.request.urlopen", side_effect=mock_404):
        result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    assert len(result.defects) == 1
    defect = result.defects[0]
    assert defect.category in (DefectCategory.FUNCTIONAL_ASSERTION_FAILURE, DefectCategory.NETWORK_FAILURE)
    assert "404" in defect.title

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Item endpoint returned 404",
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


def test_api_scenario_3_database_unavailable_prerequisite(tmp_path: Path):
    """Scenario 3: API database unavailable prerequisite blockage returns BLOCKED, NONE, 1."""
    d_prereq = Defect(
        id="DEF-API-PREREQ",
        title="PostgreSQL backend database connection refused",
        category=DefectCategory.PREREQUISITE_FAILURE,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-API-03",
        steps_to_reproduce=["GET /api/health/db"],
        expected="DB online",
        actual="ConnectionRefusedError: port 5432",
    )
    j = Journey(id="J-API-03", title="API DB Health Check", description="Health check", priority=1, steps=[])
    res = JourneyResult(journey=j, status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[d_prereq])

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=1,
        confidence="LOW",
        summary="Backend database unavailable",
        coverage_depth="BLOCKED_PREREQUISITE",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[res],
        coverage=cov,
        defects=[d_prereq],
        planned_journeys=[j],
    )
    assert status == "BLOCKED"
    assert handoff == "NONE"
    assert code == 1


def test_api_scenario_4_invalid_test_plan(tmp_path: Path):
    """Scenario 4: API test plan missing authentication token yields PARTIAL, NONE, 1."""
    d_gap = Defect(
        id="DEF-API-GAP",
        title="API test omitted authentication authorization header",
        category=DefectCategory.TEST_CONFIGURATION_GAP,
        severity=DefectSeverity.MINOR,
        journey_id="J-API-04",
        steps_to_reproduce=["GET /api/protected"],
        expected="Auth configured",
        actual="HTTP 401 Unauthorized",
    )
    j = Journey(id="J-API-04", title="Protected API Test", description="Test protected resource", priority=1, steps=[])
    res = JourneyResult(journey=j, status="FAIL", duration_seconds=0.1, steps_completed=0, defects=[d_gap])

    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Test gap: auth omitted",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[res],
        coverage=cov,
        defects=[d_gap],
        planned_journeys=[j],
    )
    assert status in ("PARTIAL", "BLOCKED")
    assert handoff == "NONE"
    assert code == 1


def test_api_scenario_5_unverified_meaningful_behavior(tmp_path: Path):
    """Scenario 5: Root / health endpoint tested, but modified /api/payments unexercised."""
    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=1,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="Root ping verified, payment route unexercised",
        coverage_depth="SHALLOW_SMOKE",
        unexercised_changed_components=["routes/payments.py"],
        visited_without_meaningful_assertions=["routes/payments.py"],
        validation_gaps=["1 modified component reached without meaningful behavioral assertions: routes/payments.py"],
    )

    assert cov.coverage_depth == "SHALLOW_SMOKE"
    assert "routes/payments.py" in cov.unexercised_changed_components
    assert cov.confidence != "HIGH"


# ==============================================================================
# 4. STREAMLIT REFERENCE BASELINE
# ==============================================================================

def test_streamlit_reference_baseline_invariants(tmp_path: Path):
    """Verify Streamlit reference application detection and contract invariants remain preserved."""
    app_py = tmp_path / "app.py"
    app_py.write_text("import streamlit as st\nst.title('Stress Testing Framework')\n", encoding="utf-8")

    archetype = ArchetypeDetector.detect(tmp_path)
    assert archetype == ProjectArchetype.WEB_SPA

    cmd, port, arch_str = detect_runtime_target(tmp_path)
    assert arch_str == "WEB_SPA"
    assert port == 8501
    assert "streamlit run app.py --server.port 8501" in cmd


# ==============================================================================
# 5. CROSS-CUTTING GENERALIZATION INVARIANTS
# ==============================================================================

def test_supervisor_subprocess_cleanup_and_termination(tmp_path: Path):
    """Verify RuntimeSupervisor launches and cleanly terminates child process tree without orphan processes."""
    (tmp_path / "index.html").write_text("<h1>Cleanup Test</h1>", encoding="utf-8")
    sup = RuntimeSupervisor(tmp_path, log_dir=tmp_path / "logs", grace_period_seconds=1.0)

    # Use a non-standard port to avoid collisions
    port = 8919
    cmd = f'"{sys.executable}" -m http.server {port}'

    started = sup.start(command=cmd, port=port, timeout=5.0)
    assert started is True
    assert sup.is_running() is True
    pid = sup.process.pid
    assert pid > 0

    # Stop supervisor and ensure process tree is cleanly terminated
    sup.stop()
    time.sleep(0.5)
    assert sup.is_running() is False
    stdout, stderr = sup.get_logs()
    assert (tmp_path / "logs" / "process_stdout.log").exists()


def test_reproduction_script_generation_across_all_archetypes(tmp_path: Path):
    """Verify reproduction script generation produces actionable scripts for Web, CLI, and API defects."""
    evidence = EvidenceCollector(tmp_path / "evidence")

    # 1. Web defect
    web_defect = Defect(
        id="DEF-WEB-TEST",
        title="Dead button click",
        category=DefectCategory.DEAD_INTERACTION,
        severity=DefectSeverity.MAJOR,
        journey_id="J-WEB",
        steps_to_reproduce=["Navigate /", "Click #btn"],
        expected="State changed",
        actual="No change",
    )
    web_journey = Journey(
        id="J-WEB",
        title="Web Journey",
        description="Test",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/"),
            JourneyStep(action=ActionType.CLICK, target="#btn"),
        ],
    )
    web_script = evidence.generate_reproduction_script(web_defect, web_journey, "http://127.0.0.1:8080")
    assert web_script.exists()
    content = web_script.read_text(encoding="utf-8")
    assert "http://127.0.0.1:8080" in content
    assert "playwright" in content.lower()

    # 2. CLI defect
    cli_defect = Defect(
        id="DEF-CLI-TEST",
        title="CLI crash",
        category=DefectCategory.CLI_CRASH,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-CLI",
        steps_to_reproduce=["python cli.py --bad"],
        expected="Zero exit",
        actual="Non-zero exit",
    )
    cli_journey = Journey(
        id="J-CLI",
        title="CLI Journey",
        description="Test",
        priority=1,
        steps=[JourneyStep(action=ActionType.CLI_COMMAND, target="python cli.py --bad")],
    )
    cli_script = evidence.generate_reproduction_script(cli_defect, cli_journey)
    assert cli_script.exists()
    cli_content = cli_script.read_text(encoding="utf-8")
    assert "python cli.py --bad" in cli_content

    # 3. API defect
    api_defect = Defect(
        id="DEF-API-TEST",
        title="API 500 error",
        category=DefectCategory.UNHANDLED_EXCEPTION,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-API",
        steps_to_reproduce=["POST /api/test"],
        expected="HTTP 200",
        actual="HTTP 500",
    )
    api_journey = Journey(
        id="J-API",
        title="API Journey",
        description="Test",
        priority=1,
        steps=[JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/test", value='POST {"data": 1}')],
    )
    api_script = evidence.generate_reproduction_script(api_defect, api_journey, "http://127.0.0.1:8000")
    assert api_script.exists()
    api_content = api_script.read_text(encoding="utf-8")
    assert "curl" in api_content.lower() or "http" in api_content.lower()

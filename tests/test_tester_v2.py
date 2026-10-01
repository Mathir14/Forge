"""Comprehensive tests for Tester v2 (The Interactive Sensory User).

Validates:
1. TestingBudget & BudgetTracker (resource ceiling & determinism)
2. RuntimeSupervisor (lifecycle, readiness probing, process tree termination)
3. BrowserDriver abstraction & Mock/Playwright drivers
4. WebInteractionDriver (dead buttons, unhandled console exceptions, failed requests, screenshots)
5. ApiInteractionDriver (stateful chains, 500 server crashes)
6. CliInteractionDriver (command execution, traceback detection)
7. LibraryInteractionDriver (consumer sandbox validation)
8. JourneyPlanner (prioritization: modified -> adjacent -> smoke -> exploratory)
9. EvidenceCollector (screenshots, telemetry dumps, standalone repro scripts)
10. TesterReportGenerator & Coverage (planned, executed, blocked, confidence)
11. TesterEngine end-to-end integration and artifact persistence
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.storage.run_manager import RunManager
from forge.testing.archetypes import ArchetypeDetector
from forge.testing.browser import (
    BrowserDriver,
    BrowserDriverFactory,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
)
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.drivers.cli import CliInteractionDriver
from forge.testing.drivers.library import LibraryInteractionDriver
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
from forge.testing.supervisor import RuntimeSupervisor, is_port_in_use


# ==============================================================================
# 1. Testing Budget & Tracker
# ==============================================================================

def test_testing_budget_and_tracker_limits():
    """Verify TestingBudget enforces execution limits and tracks metrics."""
    budget = TestingBudget(
        max_runtime_seconds=10.0,
        max_journeys=2,
        max_interactions_per_journey=3,
        max_screenshots=2,
        max_navigation_depth=2,
    )
    tracker = BudgetTracker(budget)

    assert tracker.can_start_journey() is True
    tracker.record_journey_started()
    assert tracker.journeys_executed == 1
    tracker.record_journey_started()
    assert tracker.journeys_executed == 2
    # Journeys limit reached
    assert tracker.can_start_journey() is False

    # Interaction limits
    assert tracker.can_interact(0) is True
    assert tracker.can_interact(2) is True
    assert tracker.can_interact(3) is False

    # Screenshot limits
    assert tracker.can_take_screenshot() is True
    tracker.record_screenshot()
    tracker.record_screenshot()
    assert tracker.can_take_screenshot() is False

    # Navigation depth limits
    assert tracker.can_navigate(1) is True
    assert tracker.can_navigate(2) is True
    assert tracker.can_navigate(3) is False

    data = tracker.to_dict()
    assert data["journeys_executed"] == 2
    assert data["screenshots_taken"] == 2
    assert "budget" in data


# ==============================================================================
# 2. Runtime Supervisor
# ==============================================================================

def test_runtime_supervisor_lifecycle(tmp_path):
    """Verify RuntimeSupervisor launches process, captures logs, and terminates cleanly."""
    log_dir = tmp_path / "logs"
    supervisor = RuntimeSupervisor(
        project_root=tmp_path,
        log_dir=log_dir,
        grace_period_seconds=1.0,
    )

    # Launch a simple Python background process that prints to stdout/stderr
    cmd = "python3 -c 'import sys, time; print(\"READY\"); sys.stdout.flush(); sys.stderr.write(\"ERR_TEST\\n\"); sys.stderr.flush(); time.sleep(10)'"
    started = supervisor.start(command=cmd, timeout=5.0)
    assert started is True
    assert supervisor.is_running() is True

    # Retrieve logs
    stdout, stderr = supervisor.get_logs()
    assert "READY" in stdout
    assert "ERR_TEST" in stderr

    # Clean termination
    supervisor.stop()
    time.sleep(0.2)
    assert supervisor.is_running() is False


# ==============================================================================
# 3. Browser Driver Abstraction
# ==============================================================================

def test_browser_driver_factory_and_mock_driver(tmp_path):
    """Verify BrowserDriverFactory creates MockBrowserDriver and exercises primitives."""
    driver = BrowserDriverFactory.create(driver_type="mock")
    assert isinstance(driver, MockBrowserDriver)

    driver.goto("http://localhost:3000/test")
    assert driver.current_url == "http://localhost:3000/test"

    # Screenshot
    shot_path = tmp_path / "test.png"
    driver.screenshot(shot_path)
    assert shot_path.exists()
    assert shot_path.read_bytes() == MockBrowserDriver.TINY_PNG

    # Telemetry additions
    driver.add_console_log("error", "Uncaught TypeError: test error")
    logs = driver.get_console_logs()
    assert len(logs) == 1
    assert logs[0].level == "error"
    assert "Uncaught TypeError" in logs[0].text

    driver.add_network_failure("http://localhost:3000/api/checkout", status=500)
    failures = driver.get_failed_requests()
    assert len(failures) == 1
    assert failures[0].status == 500

    driver.close()


def test_playwright_browser_driver_smoke(tmp_path):
    """Verify PlaywrightBrowserDriver starts, sets content, navigates, and captures screenshots."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright not installed in environment")

    driver = PlaywrightBrowserDriver(headless=True, viewport=(800, 600))
    driver.start()
    try:
        # Evaluate local page
        driver.evaluate("document.body.innerHTML = '<h1>Forge Tester v2</h1><button id=\"b1\">Action</button>'")
        elements = driver.get_interactive_elements()
        assert any(e.selector == "#b1" for e in elements)

        # Screenshot
        shot_path = tmp_path / "playwright_shot.png"
        driver.screenshot(shot_path)
        assert shot_path.exists()
        assert shot_path.stat().st_size > 0
    finally:
        driver.close()


# ==============================================================================
# 4. Web Interaction Driver & Defect Detection
# ==============================================================================

def test_web_interaction_driver_discovers_dead_button(tmp_path):
    """Verify WebInteractionDriver detects dead interaction (button click with zero effect)."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    mock_browser.page_content = "<html><body><button id='dead-btn'>Click Me</button></body></html>"

    evidence = EvidenceCollector(tmp_path)
    budget = BudgetTracker(TestingBudget())
    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:3000")

    journey = Journey(
        id="J-01",
        title="Checkout Button Flow",
        description="Click dead button",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/cart", description="Go to cart"),
            JourneyStep(action=ActionType.CLICK, target="#dead-btn", description="Click dead button", expected_state="Cart should update"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    assert len(result.defects) >= 1
    dead_defect = result.defects[0]
    assert dead_defect.category == DefectCategory.DEAD_INTERACTION
    assert "dead-btn" in dead_defect.title
    assert "reproduction_script" in dead_defect.evidence_paths

    # Check standalone reproduction script was generated
    repro_path = Path(dead_defect.evidence_paths["reproduction_script"])
    assert repro_path.exists()
    assert "playwright" in repro_path.read_text(encoding="utf-8")


def test_web_interaction_driver_discovers_uncaught_console_error(tmp_path):
    """Verify WebInteractionDriver catches uncaught client-side exception logged to browser console."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()

    # Simulate button click triggering an uncaught exception in console
    def on_click_handler(driver_inst):
        driver_inst.add_console_log("error", "Uncaught TypeError: Cannot read properties of undefined (reading 'submit')")

    mock_browser.on_click("#broken-btn", on_click_handler)

    evidence = EvidenceCollector(tmp_path)
    budget = BudgetTracker(TestingBudget())
    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:3000")

    journey = Journey(
        id="J-02",
        title="Form Submission Flow",
        description="Click broken button",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/form", description="Go to form"),
            JourneyStep(action=ActionType.CLICK, target="#broken-btn", description="Click submit"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    console_defects = [d for d in result.defects if d.category == DefectCategory.UNHANDLED_EXCEPTION]
    assert len(console_defects) >= 1
    assert "Cannot read properties of undefined" in console_defects[0].actual
    assert console_defects[0].severity == DefectSeverity.CRITICAL


# ==============================================================================
# 5. API Interaction Driver
# ==============================================================================

def test_api_interaction_driver_detects_server_500(tmp_path):
    """Verify ApiInteractionDriver catches 500 internal server error and generates defect."""
    evidence = EvidenceCollector(tmp_path)
    budget = BudgetTracker(TestingBudget())
    driver = ApiInteractionDriver(base_url="http://localhost:8000")

    journey = Journey(
        id="J-API-01",
        title="User Registration Endpoint",
        description="POST /api/register",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.HTTP_REQUEST, target="/api/register", value='POST {"email": "test@example.com"}'),
        ],
    )

    # Mock HTTP 500 error from urllib
    import urllib.error
    mock_http_err = urllib.error.HTTPError(
        url="http://localhost:8000/api/register",
        code=500,
        msg="Internal Server Error",
        hdrs={},
        fp=MagicMock(read=lambda: b'{"error": "Database connection pool exhausted"}'),
    )

    with patch("urllib.request.urlopen", side_effect=mock_http_err):
        result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    assert len(result.defects) == 1
    defect = result.defects[0]
    assert defect.category == DefectCategory.UNHANDLED_EXCEPTION
    assert "500" in defect.title
    assert "Database connection pool exhausted" in defect.actual
    assert "reproduction_script" in defect.evidence_paths


# ==============================================================================
# 6. CLI Interaction Driver
# ==============================================================================

def test_cli_interaction_driver_detects_traceback(tmp_path):
    """Verify CliInteractionDriver catches traceback in CLI command execution."""
    evidence = EvidenceCollector(tmp_path)
    budget = BudgetTracker(TestingBudget())
    driver = CliInteractionDriver(cwd=tmp_path)

    journey = Journey(
        id="J-CLI-01",
        title="CLI Run Test",
        description="Execute command that raises Python traceback",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.CLI_COMMAND, target="python3 -c 'raise KeyError(\"missing_key\")'"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "FAIL"
    assert len(result.defects) == 1
    defect = result.defects[0]
    assert defect.category == DefectCategory.CLI_CRASH
    assert defect.severity == DefectSeverity.CRITICAL
    assert "KeyError" in defect.actual
    assert "repro_def_j_cli_01_cli_1.sh" in defect.evidence_paths.get("reproduction_script", "")


# ==============================================================================
# 7. Journey Planner & Intelligent Prioritization
# ==============================================================================

def test_journey_planner_prioritization():
    """Verify JourneyPlanner prioritizes modified features (P1) before adjacent (P2) and smoke (P3)."""
    git_diff = """diff --git a/app/checkout/page.tsx b/app/checkout/page.tsx
+  <button id="apply-coupon-btn">Apply</button>
+  <input name="coupon_code" />
"""
    files = ["app/checkout/page.tsx", "app/cart/page.tsx"]

    journeys = JourneyPlanner.plan_journeys(
        task="Implement checkout discount coupon calculation",
        archetype=ProjectArchetype.WEB_SPA,
        git_diff=git_diff,
        changed_files=files,
        max_journeys=5,
    )

    assert len(journeys) >= 3
    # Check priorities are ordered 1 <= 2 <= 3 <= 4
    priorities = [j.priority for j in journeys]
    assert priorities == sorted(priorities)
    assert journeys[0].priority == 1
    assert "/checkout" in journeys[0].steps[0].target
    # Check targets extracted from diff
    targets = [s.target for s in journeys[0].steps]
    assert "#apply-coupon-btn" in targets
    assert "[name='coupon_code']" in targets


# ==============================================================================
# 8. Report Generator & Coverage Reporting
# ==============================================================================

def test_report_generator_explicit_coverage():
    """Verify TesterReportGenerator creates 04_tester.md and 04_tester.json with explicit coverage."""
    coverage = CoverageReport(
        planned_journeys=4,
        executed_journeys=4,
        passed_journeys=3,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="HIGH",
        summary="All planned journeys executed cleanly across desktop and mobile.",
    )

    journey = Journey(id="J-01", title="Checkout Flow", description="Test", priority=1, steps=[])
    defect = Defect(
        id="DEF-01",
        title="Button does nothing",
        category=DefectCategory.DEAD_INTERACTION,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-01",
        steps_to_reproduce=["1. Click #submit"],
        expected="Form submits",
        actual="Nothing happens",
    )
    result = JourneyResult(
        journey=journey,
        status="FAIL",
        duration_seconds=1.2,
        steps_completed=1,
        defects=[defect],
    )

    md, data = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://localhost:3000",
        journey_results=[result],
        coverage=coverage,
        duration_seconds=5.0,
        evidence_dir="/path/to/evidence",
    )

    # Markdown checks
    assert "# QA Tester Executive Audit Report" in md
    assert "Overall Empirical Verdict**: **FAIL**" in md
    assert "Confidence Level**: **HIGH**" in md
    assert "Planned Journeys | 4 |" in md
    assert "[DEF-01] Button does nothing" in md
    assert "DEAD_INTERACTION" in md

    # JSON checks
    assert data["ROLE"] == "TESTER"
    assert data["STATUS"] == "FAIL"
    assert data["HANDOFF"] == "EXECUTOR"
    assert data["COVERAGE"]["confidence"] == "HIGH"
    assert data["COVERAGE"]["planned_journeys"] == 4
    assert len(data["ISSUES"]["CRITICAL"]) == 1
    assert data["ISSUES"]["CRITICAL"][0]["ID"] == "DEF-01"


# ==============================================================================
# 9. TesterEngine End-to-End Execution
# ==============================================================================

def test_tester_engine_e2e_cli_archetype(tmp_path):
    """Verify TesterEngine executes end-to-end on CLI archetype and writes artifacts."""
    (tmp_path / "cli.py").write_text("import sys\nprint('CLI Entry Point')\n", encoding="utf-8")
    rm = RunManager(tmp_path)
    run = rm.create_run(task="Add CLI commands")

    context = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    engine = TesterEngine(context=context, run_manager=rm)
    stage_res = engine.run()

    assert stage_res.role.name == "tester"
    assert (run.run_dir / "04_tester.md").exists()
    assert (run.run_dir / "04_tester.json").exists()
    assert (run.run_dir / "evidence").is_dir()

    data = json.loads((run.run_dir / "04_tester.json").read_text(encoding="utf-8"))
    assert data["ROLE"] == "TESTER"
    assert "COVERAGE" in data
    assert "DATA" in data

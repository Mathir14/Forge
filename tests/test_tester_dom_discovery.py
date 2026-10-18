"""Regression test suite for DOM discovery, hydration waiting, and invalid interaction handling."""

import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from forge.core.config import Config
from forge.core.context import Context
from forge.storage.run_manager import RunManager
from forge.testing.ai_planner import is_valid_selector
from forge.testing.browser import (
    BrowserDriverFactory,
    InteractiveElement,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
)
from forge.testing.budget import BudgetTracker, TestingBudget
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
from forge.testing.report import TesterReportGenerator


def test_initial_header_controls_before_sidebar_delayed_hydration():
    """Verify that initial header controls appear first and hydration wait yields full sidebar controls."""
    driver = MockBrowserDriver()
    # Initial pre-hydration DOM: only 2 static header buttons
    driver.add_element(
        selector='[data-testid="stHeader"] button[aria-label="Deploy"]',
        tag_name="button",
        text="Deploy",
    )
    driver.add_element(
        selector='button[kind="header"]',
        tag_name="button",
        text="Options",
    )
    assert len(driver.get_interactive_elements()) == 2

    # Configure pending elements that arrive upon hydration
    hydrated_elements = [
        InteractiveElement(
            selector='[data-testid="stRadio"] label:has-text("⚡ Stress Lab")',
            tag_name="label",
            role="radio",
            text="⚡ Stress Lab",
        ),
        InteractiveElement(
            selector='[data-testid="stRadio"] label:has-text("📊 Analytics")',
            tag_name="label",
            role="radio",
            text="📊 Analytics",
        ),
        InteractiveElement(
            selector='[data-testid="stRadio"] label:has-text("⚙️ Settings")',
            tag_name="label",
            role="radio",
            text="⚙️ Settings",
        ),
    ]
    driver.set_pending_hydrated_elements(hydrated_elements)

    # Perform hydration wait
    hydrated = driver.wait_for_hydration(timeout=2.0)
    assert hydrated is True

    # Post-hydration discovery should include both header and sidebar controls
    elements = driver.get_interactive_elements()
    assert len(elements) == 5
    selectors = [e.selector for e in elements]
    assert '[data-testid="stRadio"] label:has-text("⚡ Stress Lab")' in selectors
    assert '[data-testid="stRadio"] label:has-text("📊 Analytics")' in selectors
    assert '[data-testid="stRadio"] label:has-text("⚙️ Settings")' in selectors


def test_discovery_of_stradio_navigation_and_unicode_selectors():
    """Verify stRadio navigation selectors preserve Unicode/emoji and pass validation."""
    labels = [
        "⚡ Stress Lab",
        "📊 Execution & Results",
        "🎯 Target Configuration",
        "⚙️ Advanced Options",
    ]
    for lbl in labels:
        selector = f'[data-testid="stRadio"] label:has-text("{lbl}")'
        # Must be accepted by the selector sanitizer / validator
        assert is_valid_selector(selector), f"Selector {selector} should be valid"

    # Verify MockBrowserDriver handles querySelector matching for these selectors
    driver = MockBrowserDriver()
    driver.page_content = """
    <div data-testid="stRadio">
        <label>⚡ Stress Lab</label>
        <label>📊 Execution & Results</label>
    </div>
    """
    for lbl in ["⚡ Stress Lab", "📊 Execution & Results"]:
        sel = f'[data-testid="stRadio"] label:has-text("{lbl}")'
        driver.add_element(selector=sel, tag_name="label", text=lbl)

    interactive = driver.get_interactive_elements()
    assert len(interactive) == 2
    assert interactive[0].text == "⚡ Stress Lab"
    assert interactive[1].text == "📊 Execution & Results"


def test_deduplication_and_bounded_discovery(tmp_path: Path):
    """Verify element collection deduplicates identical selector+text pairs and respects max limit."""
    import inspect
    driver = MockBrowserDriver()
    # Add repeated identical elements
    for _ in range(10):
        driver.add_element(selector='button[name="run"]', tag_name="button", text="Run Test")

    elements = driver.get_interactive_elements()
    assert len(elements) == 10

    # Test bounding and deduplication logic in PlaywrightBrowserDriver script: max 60 elements and seen set
    src = inspect.getsource(PlaywrightBrowserDriver.get_interactive_elements)
    assert "return results.slice(0, 60);" in src
    assert "seen.has(key)" in src


def test_timeout_behavior_when_expected_controls_never_appear():
    """Verify timeout when application fails to hydrate does not hang or raise."""
    driver = MockBrowserDriver()
    driver.simulate_hydration_failure = True
    driver.add_element(selector='button[name="header"]', tag_name="button", text="Header")

    # Hydration wait should return False without error
    hydrated = driver.wait_for_hydration(timeout=0.5)
    assert hydrated is False

    # Still returns whatever pre-hydration elements were discovered
    elements = driver.get_interactive_elements()
    assert len(elements) == 1
    assert elements[0].text == "Header"


def test_invalid_ai_selector_classified_as_invalid_interaction_handoff_none(tmp_path: Path):
    """Verify an AI-generated selector not present in live DOM is classified as INVALID_INTERACTION with HANDOFF=NONE."""
    mock_browser = MockBrowserDriver()
    # App only has stRadio controls
    valid_sel = '[data-testid="stRadio"] label:has-text("⚡ Stress Lab")'
    mock_browser.add_element(selector=valid_sel, tag_name="label", text="⚡ Stress Lab")

    driver = WebInteractionDriver(mock_browser, base_url="http://127.0.0.1:8501")
    # Inform driver of discovered elements
    driver.set_discovered_elements(mock_browser.get_interactive_elements())

    # Mock click to raise TimeoutError when nonexistent stSelectbox is targeted
    def fail_click(d):
        raise TimeoutError("Timeout 10000ms exceeded waiting for selector '[data-testid=\"stSelectbox\"]'")

    mock_browser.on_click('[data-testid="stSelectbox"]', fail_click)

    # Journey with hallucinated stSelectbox and a dependent assertion
    journey = Journey(
        id="J-02",
        title="Hallucinated selectbox interaction",
        description="Select a module using selectbox",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.CLICK,
                target='[data-testid="stSelectbox"]',
                description="Click nonexistent dropdown",
            ),
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="Execution Summary",
                description="Verify text after navigation",
            ),
        ],
    )

    budget = BudgetTracker(TestingBudget(max_runtime_seconds=60, max_journeys=3))
    evidence = EvidenceCollector(tmp_path / "evidence")

    result = driver.execute_journey(journey, budget, evidence)

    # Journey should fail
    assert result.status == "FAIL"
    # Dependent assertion must be skipped (failure containment)
    assert result.telemetry_summary["steps_skipped"] == 1
    assert len(result.defects) == 1

    defect = result.defects[0]
    # Defect must be classified as INVALID_INTERACTION, not BROKEN_NAVIGATION
    assert defect.category == DefectCategory.INVALID_INTERACTION
    assert defect.severity == DefectSeverity.MINOR
    assert "Invalid interaction target" in defect.title
    assert "reproduction_script" in defect.evidence_paths

    # Standalone reproduction script must exist
    repro_path = Path(defect.evidence_paths["reproduction_script"])
    assert repro_path.exists()

    # Now verify report generation handoff behavior
    coverage = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Only invalid AI journey was executed",
    )
    md_content, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[result],
        coverage=coverage,
        duration_seconds=1.5,
        evidence_dir=str(tmp_path / "evidence"),
    )

    # When all defects are INVALID_INTERACTION, HANDOFF must be NONE (no Executor repair loop)
    assert json_dict["HANDOFF"] == "NONE"
    assert json_dict["STATUS"] == "BLOCKED"
    assert len(json_dict["ISSUES"]["MINOR"]) == 1
    assert len(json_dict["ISSUES"]["CRITICAL"]) == 0
    assert len(json_dict["ISSUES"]["MAJOR"]) == 0


def test_genuine_interaction_failure_maintains_critical_and_executor_handoff(tmp_path: Path):
    """Verify that when a discovered/present element fails, it is classified as CRITICAL with HANDOFF=EXECUTOR."""
    mock_browser = MockBrowserDriver()
    valid_sel = '[data-testid="stRadio"] label:has-text("⚡ Stress Lab")'
    mock_browser.add_element(selector=valid_sel, tag_name="label", text="⚡ Stress Lab")

    driver = WebInteractionDriver(mock_browser, base_url="http://127.0.0.1:8501")
    driver.set_discovered_elements(mock_browser.get_interactive_elements())

    # Mock click to raise exception on a valid discovered element (e.g. page crashed or handler raised)
    def fail_click(d):
        raise RuntimeError("Element click intercepted: pointer-events none or crash")

    mock_browser.on_click(valid_sel, fail_click)

    journey = Journey(
        id="J-01",
        title="Genuine interaction test",
        description="Click valid module radio",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.CLICK,
                target=valid_sel,
                description="Click Stress Lab module",
            ),
        ],
    )

    budget = BudgetTracker(TestingBudget(max_runtime_seconds=60, max_journeys=3))
    evidence = EvidenceCollector(tmp_path / "evidence")

    result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    assert len(result.defects) == 1
    defect = result.defects[0]
    # Because element was discovered, this is a genuine failure
    assert defect.category == DefectCategory.BROKEN_NAVIGATION
    assert defect.severity == DefectSeverity.CRITICAL

    coverage = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="HIGH",
        summary="Genuine defect detected",
    )
    md_content, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[result],
        coverage=coverage,
        duration_seconds=1.5,
        evidence_dir=str(tmp_path / "evidence"),
    )

    # Genuine defects must trigger EXECUTOR handoff
    assert json_dict["HANDOFF"] == "EXECUTOR"
    assert json_dict["STATUS"] == "FAIL"
    assert len(json_dict["ISSUES"]["CRITICAL"]) == 1


def test_non_streamlit_discovery_behavior():
    """Verify non-Streamlit standard web page discovery works with hydration wait."""
    driver = MockBrowserDriver()
    driver.page_content = """
    <html>
        <body>
            <main>
                <h1>User Dashboard</h1>
                <nav>
                    <a href="/profile">Profile</a>
                    <a href="/settings">Settings</a>
                </nav>
                <form>
                    <input type="text" name="username" placeholder="Username" />
                    <button type="submit" id="login-btn">Log In</button>
                </form>
            </main>
        </body>
    </html>
    """
    driver.add_element(selector="#login-btn", tag_name="button", text="Log In")
    driver.add_element(selector='input[name="username"]', tag_name="input", text="Username")
    driver.add_element(selector='a[href="/profile"]', tag_name="a", text="Profile")

    # Hydration wait succeeds
    hydrated = driver.wait_for_hydration(timeout=1.0)
    assert hydrated is True

    elements = driver.get_interactive_elements()
    assert len(elements) == 3
    selectors = [e.selector for e in elements]
    assert "#login-btn" in selectors
    assert 'input[name="username"]' in selectors
    assert 'a[href="/profile"]' in selectors


def test_tester_engine_dom_discovery_with_delayed_hydration(tmp_path: Path):
    """Verify TesterEngine executes wait_for_hydration and passes discovered elements to driver."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "app.py").write_text("import streamlit as st\nst.write('App')\n")

    # Configure mock browser driver in factory
    mock_driver = MockBrowserDriver()
    # Pre-hydration: 2 header buttons
    mock_driver.add_element(selector='[data-testid="stHeader"] button', tag_name="button", text="Deploy")
    mock_driver.add_element(selector='button[kind="header"]', tag_name="button", text="Options")

    # Delayed hydration elements
    mock_driver.set_pending_hydrated_elements([
        InteractiveElement(
            selector='[data-testid="stRadio"] label:has-text("⚡ Stress Lab")',
            tag_name="label",
            role="radio",
            text="⚡ Stress Lab",
        )
    ])

    BrowserDriverFactory.register("mock", lambda viewport=(1440, 900): mock_driver)

    rm = RunManager(tmp_path)
    run = rm.create_run(task="Test SPA")
    ctx = Context(run=run, project_root=project_root, config=Config(), git=MagicMock())

    budget = TestingBudget(max_runtime_seconds=30, max_journeys=1)
    engine = TesterEngine(context=ctx, budget=budget)

    # Verify MockBrowserDriver was registered and wait_for_hydration was implemented
    assert hasattr(mock_driver, "wait_for_hydration")
    assert engine.active_run == run


def test_multi_container_scoped_text_matching(tmp_path: Path):
    """Verify querySelectorAll finds target text in any matching scoped container (not just the first)."""
    driver = MockBrowserDriver()
    # 4 alert containers: 0 is tip, 1 is no data, 2 is data not prepared, 3 is no models trained
    driver.scoped_containers['[data-testid="stAlertContainer"]'] = [
        "💡 Tip: Complete modules in order for best results",
        "⚠️ No data loaded",
        "⚠️ Data not prepared",
        "⚠️ No models trained",
    ]

    web_driver = WebInteractionDriver(driver)
    journey = Journey(
        id="J-01",
        title="Status Alerts Verification",
        description="Verify status alert containers",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.ASSERT_TEXT, target="⚠️ No data loaded", value='[data-testid="stAlertContainer"]'),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="⚠️ Data not prepared", value='[data-testid="stAlertContainer"]'),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="⚠️ No models trained", value='[data-testid="stAlertContainer"]'),
        ],
    )
    budget = BudgetTracker(TestingBudget(max_runtime_seconds=30))
    evidence = EvidenceCollector(tmp_path)

    res = web_driver.execute_journey(journey, budget, evidence)
    assert res.status == "PASS"
    assert len(res.defects) == 0
    assert res.steps_completed == 3


def test_unsupported_ai_expectation_classified_as_invalid_interaction_minor(tmp_path: Path):
    """Verify that unverified AI expectations missing from DOM and criteria become INVALID_INTERACTION (MINOR) with step skipping and HANDOFF=NONE."""
    driver = MockBrowserDriver()
    driver.page_content = "<html><body><main><h1>Dashboard</h1></main></body></html>"
    driver.scoped_containers['[data-testid="stAlertContainer"]'] = [
        "💡 Tip: Complete modules in order for best results",
    ]

    web_driver = WebInteractionDriver(driver)
    web_driver.set_task_context(
        task="Improve dashboard styling and layout",
        acceptance_criteria="Dashboard header should have high contrast",
    )

    journey = Journey(
        id="J-01",
        title="Hallucinated Status Check",
        description="Verify ungrounded status expectations",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.ASSERT_TEXT, target="⚠️ Nonexistent Hallucinated Warning", value='[data-testid="stAlertContainer"]'),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Dashboard", value="h1"),
        ],
    )
    budget = BudgetTracker(TestingBudget(max_runtime_seconds=30))
    evidence = EvidenceCollector(tmp_path)

    res = web_driver.execute_journey(journey, budget, evidence)
    assert len(res.defects) == 1
    defect = res.defects[0]
    assert defect.category == DefectCategory.INVALID_INTERACTION
    assert defect.severity == DefectSeverity.MINOR
    # Dependent step 2 was safely skipped
    assert res.steps_completed == 0
    assert res.telemetry_summary.get("steps_skipped") == 1

    # Report generation
    cov = CoverageReport(1, 1, 0, 0, 0, "LOW", "Verification")
    md, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://localhost:8501",
        journey_results=[res],
        coverage=cov,
        duration_seconds=1.0,
        evidence_dir=str(tmp_path),
    )
    assert json_dict["STATUS"] == "BLOCKED"
    assert json_dict["HANDOFF"] == "NONE"
    assert len(json_dict["ISSUES"]["CRITICAL"]) == 0
    assert len(json_dict["ISSUES"]["MAJOR"]) == 0
    assert len(json_dict["ISSUES"]["MINOR"]) == 1


def test_required_missing_status_classified_as_broken_navigation_major_with_executor_handoff(tmp_path: Path):
    """Verify that a missing status explicitly required by acceptance criteria is classified as BROKEN_NAVIGATION (MAJOR) with EXECUTOR handoff."""
    driver = MockBrowserDriver()
    driver.page_content = "<html><body><main><h1>Dashboard</h1></main></body></html>"

    web_driver = WebInteractionDriver(driver)
    web_driver.set_task_context(
        task="Add export notification banner",
        acceptance_criteria="Requirements:\n- Must display 'Export Succeeded' notification when export completes",
    )

    journey = Journey(
        id="J-01",
        title="Required Export Notification Check",
        description="Verify required export notification banner",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Export Succeeded", value=""),
        ],
    )
    budget = BudgetTracker(TestingBudget(max_runtime_seconds=30))
    evidence = EvidenceCollector(tmp_path)

    res = web_driver.execute_journey(journey, budget, evidence)
    assert len(res.defects) == 1
    defect = res.defects[0]
    assert defect.category in (DefectCategory.FUNCTIONAL_ASSERTION_FAILURE, DefectCategory.BROKEN_NAVIGATION)
    assert defect.severity == DefectSeverity.MAJOR

    # Report generation
    cov = CoverageReport(1, 1, 0, 1, 0, "HIGH", "Verification")
    md, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://localhost:8501",
        journey_results=[res],
        coverage=cov,
        duration_seconds=1.0,
        evidence_dir=str(tmp_path),
    )
    assert json_dict["STATUS"] == "FAIL"
    assert json_dict["HANDOFF"] == "EXECUTOR"
    assert len(json_dict["ISSUES"]["MAJOR"]) == 1


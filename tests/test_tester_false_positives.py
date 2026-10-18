"""Regression tests for Forge AI-Guided Tester false positives.

Validates:
1. Selector validation rules (is_valid_selector): ASCII, emojis, Unicode, Playwright pseudo-selectors,
   balanced quotes, reject injections/malformed/excessive length.
2. Emoji selector preservation: journeys with emoji selectors (e.g. "2️⃣ Baseline Modeling") are preserved.
3. Invalid journey step structural integrity: journeys with invalid steps are marked is_valid=False and
   WebInteractionDriver returns BLOCKED with 0 false defects.
4. Dependent step skipping: failed/dead interaction halts journey and skips downstream dependent assertions.
5. Context-aware ASSERT_TEXT with scoped container (step.value): asserts within specified container.
6. Context-aware ASSERT_TEXT main vs sidebar: distinguishes persistent nav from page content.
7. Blocked journeys report and handoff: blocked journeys report STATUS=BLOCKED and HANDOFF=NONE (never EXECUTOR).
8. Genuine assertion failures: genuine failures report STATUS=FAIL and HANDOFF=EXECUTOR.
"""

from pathlib import Path
from typing import Dict, List
import pytest

from forge.testing.ai_planner import (
    AiJourneyPlanner,
    is_valid_selector,
)
from forge.testing.browser import MockBrowserDriver
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    ActionType,
    CoverageReport,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.report import TesterReportGenerator


# ==============================================================================
# 1. Selector Validation Rules (is_valid_selector)
# ==============================================================================

def test_selector_validation_rules():
    """Verify is_valid_selector accepts valid Unicode/emoji/Playwright selectors and rejects unsafe input."""
    # Valid standard and CSS selectors
    assert is_valid_selector("#root") is True
    assert is_valid_selector(".btn.btn-primary") is True
    assert is_valid_selector("input[type=\"text\"]") is True
    assert is_valid_selector("div > p + span") is True
    assert is_valid_selector("[data-testid=\"stRadio\"]") is True
    assert is_valid_selector("[data-testid='stMain']") is True

    # Valid Unicode and Emoji selectors (e.g. Streamlit page navigation)
    assert is_valid_selector('label:has-text("2️⃣ Baseline Modeling")') is True
    assert is_valid_selector("[data-testid=\"stRadio\"] label:has-text(\"6️⃣ Sensitivity Analysis\")") is True
    assert is_valid_selector("button:has-text(\"Créer un compte\")") is True
    assert is_valid_selector("button:has-text(\"データ保存\")") is True
    assert is_valid_selector("div[data-name=\"résumé\"]") is True
    assert is_valid_selector("p:has-text(\"Hello, World!\")") is True

    # Invalid / Unsafe: Unclosed quotes or delimiters
    assert is_valid_selector('label:has-text("2️⃣ Baseline Modeling') is False
    assert is_valid_selector("label:has-text('mismatched\")") is False
    assert is_valid_selector("input[type=\"text\"") is False
    assert is_valid_selector("div:has-text((foo)") is False

    # Invalid / Unsafe: Injection and dangerous characters
    assert is_valid_selector("button<script>alert(1)</script>") is False
    assert is_valid_selector("button\x00") is False
    assert is_valid_selector("button; DROP TABLE users;") is False
    assert is_valid_selector("button{color:red}") is False
    assert is_valid_selector("button`id`") is False

    # Invalid: Empty or whitespace
    assert is_valid_selector("") is False
    assert is_valid_selector("   ") is False

    # Invalid: Excessive length (> 500 chars)
    assert is_valid_selector("div." + "a" * 501) is False


# ==============================================================================
# 2. Emoji Selector Preservation in Plan Sanitization
# ==============================================================================

def test_emoji_selector_plan_sanitization_and_preservation():
    """Verify validate_and_sanitize_plan preserves valid emoji selectors rather than dropping steps."""
    raw_plan = {
        "journeys": [
            {
                "id": "J-02",
                "title": "Baseline Modeling Page Navigation",
                "description": "Navigate to baseline modeling page using sidebar radio",
                "priority": 1,
                "viewport": [1440, 900],
                "steps": [
                    {
                        "action": "NAVIGATE",
                        "target": "/",
                        "description": "Load main application",
                    },
                    {
                        "action": "CLICK",
                        "target": '[data-testid="stRadio"] label:has-text("2️⃣ Baseline Modeling")',
                        "description": "Select Baseline Modeling page",
                        "expected_state": "Baseline modeling view loaded",
                    },
                    {
                        "action": "ASSERT_TEXT",
                        "target": "Baseline Model Configuration",
                        "value": '[data-testid="stMain"]',
                        "description": "Verify page title",
                    },
                ],
            }
        ]
    }

    sanitized = AiJourneyPlanner.validate_and_sanitize_plan(raw_plan)
    assert len(sanitized) == 1
    journey = sanitized[0]
    assert journey.is_valid is True
    assert journey.validation_error is None
    assert len(journey.steps) == 3
    assert journey.steps[1].action == ActionType.CLICK
    assert "2️⃣ Baseline Modeling" in journey.steps[1].target
    assert journey.steps[2].action == ActionType.ASSERT_TEXT
    assert journey.steps[2].value == '[data-testid="stMain"]'


# ==============================================================================
# 3. Invalid Journey Step Structural Integrity & Safe Blocking
# ==============================================================================

def test_invalid_step_marks_journey_invalid_and_blocks_safely(tmp_path):
    """Verify that an invalid step marks the entire journey invalid and returns BLOCKED with 0 defects."""
    # Step with unsupported action and malformed selector
    raw_plan = {
        "journeys": [
            {
                "id": "J-CORRUPT",
                "title": "Corrupt Journey",
                "description": "Contains invalid step",
                "priority": 1,
                "steps": [
                    {
                        "action": "NAVIGATE",
                        "target": "/",
                        "description": "Load application",
                    },
                    {
                        "action": "UNKNOWN_ACTION",
                        "target": "foo<script>",
                        "description": "Invalid action",
                    },
                    {
                        "action": "ASSERT_TEXT",
                        "target": "Target Text",
                        "description": "Should never execute in isolation",
                    },
                ],
            }
        ]
    }

    sanitized = AiJourneyPlanner.validate_and_sanitize_plan(raw_plan)
    assert len(sanitized) == 1
    corrupt_journey = sanitized[0]
    assert corrupt_journey.is_valid is False
    assert corrupt_journey.validation_error is not None
    assert "unsupported action" in corrupt_journey.validation_error.lower()

    # When executed, WebInteractionDriver must return BLOCKED with ZERO false defects
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:8501")
    budget = BudgetTracker(TestingBudget())
    evidence = EvidenceCollector(tmp_path)

    result = driver.execute_journey(corrupt_journey, budget, evidence)
    assert result.status == "BLOCKED"
    assert result.steps_completed == 0
    assert len(result.defects) == 0
    assert result.telemetry_summary.get("steps_skipped") == len(corrupt_journey.steps)
    assert "blocked_reason" in result.telemetry_summary


# ==============================================================================
# 4. Dependent Step Skipping on Interaction Failure
# ==============================================================================

def test_interaction_failure_halts_journey_and_skips_dependent_assertions(tmp_path):
    """Verify that when an interaction step fails, dependent assertions are halted rather than failing."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    mock_browser.page_content = "<html><body><h1>Page 1</h1></body></html>"

    # Set up click to raise an error (e.g. element not found or not clickable)
    def broken_click(driver_inst):
        raise RuntimeError("Element not clickable at point (100, 200)")

    mock_browser._click_handlers["#missing-btn"] = broken_click

    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:8501")
    budget = BudgetTracker(TestingBudget())
    evidence = EvidenceCollector(tmp_path)

    journey = Journey(
        id="J-FAILING-CLICK",
        title="Failing Click Journey",
        description="Click fails and downstream assertions should be skipped",
        priority=1,
        steps=[
            JourneyStep(action=ActionType.NAVIGATE, target="/", description="Navigate to app"),
            JourneyStep(action=ActionType.CLICK, target="#missing-btn", description="Click button that throws"),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Page 2 Header", description="Assert text on page 2"),
            JourneyStep(action=ActionType.ASSERT_TEXT, target="Page 2 Footer", description="Assert footer on page 2"),
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)

    assert result.status == "FAIL"
    # Should report exactly ONE defect for the broken interaction step, NOT 3 defects
    assert len(result.defects) == 1
    defect = result.defects[0]
    assert defect.category == DefectCategory.BROKEN_NAVIGATION
    assert "2 subsequent dependent step(s) skipped" in defect.actual
    assert result.telemetry_summary.get("steps_skipped") == 2


# ==============================================================================
# 5. Context-Aware ASSERT_TEXT with Scoped Container (step.value)
# ==============================================================================

def test_context_aware_assert_text_container_scoping(tmp_path):
    """Verify ASSERT_TEXT evaluates within step.value scoped container when specified."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    mock_browser.page_content = "<html><body><main>Dashboard Main Content</main><nav>Sidebar Menu</nav></body></html>"
    mock_browser.scoped_containers = {
        "[data-testid='stMain']": "Dashboard Main Content with Calibration Model",
        "[data-testid='stSidebar']": "Sidebar Menu 1️⃣ Data 2️⃣ Baseline",
    }

    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:8501")
    budget = BudgetTracker(TestingBudget())
    evidence = EvidenceCollector(tmp_path)

    # Subtest A: Text is in scoped container -> PASS
    pass_journey = Journey(
        id="J-SCOPE-PASS",
        title="Scoped Container Pass",
        description="Verify text within main container",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="Calibration Model",
                value="[data-testid='stMain']",
                description="Check calibration text in main area",
            )
        ],
    )
    res_pass = driver.execute_journey(pass_journey, budget, evidence)
    assert res_pass.status == "PASS"
    assert len(res_pass.defects) == 0

    # Subtest B: Text is NOT in scoped container -> FAIL with container diagnostic
    fail_journey = Journey(
        id="J-SCOPE-FAIL",
        title="Scoped Container Fail",
        description="Verify text absent from main container",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="Sidebar Menu",
                value="[data-testid='stMain']",
                description="Should fail because text is in sidebar, not main container",
            )
        ],
    )
    res_fail = driver.execute_journey(fail_journey, budget, evidence)
    assert res_fail.status == "FAIL"
    assert len(res_fail.defects) == 1
    defect = res_fail.defects[0]
    assert "Expected text 'Sidebar Menu' not found in container '[data-testid='stMain']'" in defect.actual

    # Subtest C: Scoped container missing from DOM -> FAIL with missing container diagnostic
    missing_container_journey = Journey(
        id="J-SCOPE-MISSING",
        title="Scoped Container Missing",
        description="Container selector does not exist in DOM",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="Any Text",
                value="#non-existent-panel",
                description="Assert in missing container",
            )
        ],
    )
    res_missing = driver.execute_journey(missing_container_journey, budget, evidence)
    assert res_missing.status == "FAIL"
    assert len(res_missing.defects) == 1
    assert "Scoped container '#non-existent-panel' not found in rendered DOM" in res_missing.defects[0].actual


# ==============================================================================
# 6. Context-Aware ASSERT_TEXT Main vs Persistent Sidebar
# ==============================================================================

def test_context_aware_assert_text_main_vs_sidebar(tmp_path):
    """Verify ASSERT_TEXT detects text that only exists in persistent navigation menu and fails unless explicit."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    mock_browser.main_content = "Welcome to Model Performance Analysis. Data is loaded."
    mock_browser.nav_content = "Navigation: 1️⃣ Upload Data 2️⃣ Baseline Modeling 3️⃣ Stress Testing"
    mock_browser.page_content = f"{mock_browser.main_content} {mock_browser.nav_content}"

    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:8501")
    budget = BudgetTracker(TestingBudget())
    evidence = EvidenceCollector(tmp_path)

    # Subtest A: Unscoped step targeting text only in sidebar -> FAIL
    # Because step does not mention navigation/sidebar, it expects page content
    unscoped_fail_journey = Journey(
        id="J-NAV-MISMATCH",
        title="Unscoped Nav Mismatch",
        description="Verify baseline modeling page content",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="2️⃣ Baseline Modeling",
                value="",  # unscoped
                description="Verify page rendered Baseline Modeling content",
            )
        ],
    )
    res_fail = driver.execute_journey(unscoped_fail_journey, budget, evidence)
    assert res_fail.status == "FAIL"
    assert len(res_fail.defects) == 1
    assert "only observed in persistent navigation menu" in res_fail.defects[0].actual

    # Subtest B: Explicit sidebar check -> PASS
    sidebar_pass_journey = Journey(
        id="J-NAV-EXPLICIT",
        title="Explicit Nav Check",
        description="Verify sidebar menu option",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="2️⃣ Baseline Modeling",
                value="",  # unscoped
                description="Verify sidebar navigation radio contains baseline option",
            )
        ],
    )
    res_pass = driver.execute_journey(sidebar_pass_journey, budget, evidence)
    assert res_pass.status == "PASS"
    assert len(res_pass.defects) == 0


# ==============================================================================
# 7. Blocked Journeys Report & Handoff Behavior
# ==============================================================================

def test_blocked_journeys_report_and_handoff_behavior():
    """Verify that when journeys are blocked, report shows STATUS=BLOCKED and HANDOFF=NONE (never EXECUTOR)."""
    blocked_journey = Journey(
        id="J-BLOCKED",
        title="Blocked Radio Journey",
        description="Could not be validated",
        priority=1,
        steps=[],
        is_valid=False,
        validation_error="Malformed selector syntax",
    )
    result = JourneyResult(
        journey=blocked_journey,
        status="BLOCKED",
        duration_seconds=0.0,
        steps_completed=0,
        defects=[],
        telemetry_summary={
            "blocked_reason": "Malformed selector syntax",
            "steps_total": 2,
            "steps_completed": 0,
            "steps_skipped": 2,
        },
    )

    coverage = CoverageReport(
        planned_journeys=1,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=1,
        confidence="LOW",
        summary="Zero planned journeys could be exercised.",
    )

    md, data = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://localhost:8501",
        journey_results=[result],
        coverage=coverage,
        duration_seconds=1.0,
        evidence_dir="/path/to/evidence",
    )

    # Markdown report must show BLOCKED verdict and clear blocked note in matrix
    assert "Overall Empirical Verdict**: **BLOCKED**" in md
    assert "⛔ Blocked: Malformed selector syntax" in md
    assert "| Blocked Journeys | 1 |" in md

    # JSON report must show BLOCKED status and HANDOFF must be NONE (not EXECUTOR)
    assert data["STATUS"] == "BLOCKED"
    assert data["HANDOFF"] == "NONE"
    assert data["COVERAGE"]["blocked_journeys"] == 1
    assert data["COVERAGE"]["executed_journeys"] == 0
    assert len(data["ISSUES"]["CRITICAL"]) == 0
    assert len(data["ISSUES"]["MAJOR"]) == 0


# ==============================================================================
# 8. Genuine Assertion Failure Triggers EXECUTOR Handoff
# ==============================================================================

def test_genuine_assertion_failure_triggers_executor_handoff(tmp_path):
    """Verify that a genuine defect results in STATUS=FAIL and HANDOFF=EXECUTOR."""
    mock_browser = MockBrowserDriver()
    mock_browser.start()
    mock_browser.page_content = "<html><body><div>Some other content</div></body></html>"

    driver = WebInteractionDriver(mock_browser, base_url="http://localhost:8501")
    budget = BudgetTracker(TestingBudget())
    evidence = EvidenceCollector(tmp_path)

    journey = Journey(
        id="J-GENUINE-FAIL",
        title="Genuine Failure Journey",
        description="Expected text does not exist anywhere in DOM",
        priority=1,
        steps=[
            JourneyStep(
                action=ActionType.ASSERT_TEXT,
                target="Non-existent Feature",
                description="Verify critical feature header",
            )
        ],
    )

    result = driver.execute_journey(journey, budget, evidence)
    assert result.status == "FAIL"
    assert len(result.defects) == 1
    assert result.defects[0].severity == DefectSeverity.MAJOR

    coverage = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="HIGH",
        summary="Empirical verification completed.",
    )

    md, data = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://localhost:8501",
        journey_results=[result],
        coverage=coverage,
        duration_seconds=2.0,
        evidence_dir=str(tmp_path),
    )

    assert "Overall Empirical Verdict**: **FAIL**" in md
    assert data["STATUS"] == "FAIL"
    assert data["HANDOFF"] == "EXECUTOR"
    assert len(data["ISSUES"]["MAJOR"]) == 1

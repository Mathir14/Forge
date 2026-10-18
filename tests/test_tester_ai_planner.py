"""Comprehensive tests for AI-Guided Tester DOM-grounded journey planning.

Validates:
1. Prompt construction includes task, acceptance criteria, changed files, diff, and DOM elements.
2. Valid structured plans deserialize into existing Journey and JourneyStep models.
3. Markdown-fenced JSON and plain JSON handling.
4. Validation and sanitization (invalid actions, excessive journeys/steps, invalid selectors).
5. Fallback behavior on adapter timeout, exit code errors, and malformed JSON.
6. Stage.run() passes adapter to TesterEngine on live execution path.
7. WEB_SPA engine path collects DOM elements before AI planning.
8. API, CLI, and Library archetypes retain existing heuristic behavior.
9. Testing budget accounts for planning time.
10. Validated journeys flow through driver, evidence collector, report generator, and repair feedback.
11. Browser or application startup failures cannot produce a false PASS.
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.adapters.base import AdapterResponse, BaseAdapter
from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.core.role import Role
from forge.prompts.rendered_prompt import RenderedPrompt
from forge.protocol.report import MachineReport
from forge.stages.result import StageResult
from forge.stages.stage import Stage
from forge.storage.run_manager import RunManager
from forge.testing.ai_planner import (
    AiJourneyPlanner,
    PlanningResult,
    SUPPORTED_WEB_ACTIONS,
)
from forge.testing.browser import (
    BrowserDriverFactory,
    MockBrowserDriver,
)
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.engine import TesterEngine
from forge.testing.models import (
    ActionType,
    DefectCategory,
    InteractiveElement,
    Journey,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner
from forge.testing.report import TesterReportGenerator


def make_adapter_response(
    stdout: str = "{}",
    stderr: str = "",
    exit_code: int = 0,
    duration_seconds: float = 0.1,
    raw_output: str = "",
) -> AdapterResponse:
    return AdapterResponse(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        raw_output=raw_output or stdout or stderr,
    )


class DummyNonMockAdapter(BaseAdapter):
    """Concrete adapter subclass that is not detected as an automated Mock."""

    def __init__(self, response: Optional[AdapterResponse] = None):
        super().__init__(name="dummy_adapter")
        self._response = response or make_adapter_response()

    def is_available(self) -> bool:
        return True

    def execute(self, prompt: str, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> AdapterResponse:
        return self._response


# ==============================================================================
# 1. Prompt Construction & Acceptance Criteria
# ==============================================================================

def test_prompt_construction_includes_all_context_elements():
    """Verify prompt includes task, acceptance criteria, changed files, diff, and DOM elements."""
    task = "Add discount coupon to checkout flow\nAcceptance criteria:\n- User can enter SAVE20\n- Total updates"
    elements = [
        InteractiveElement(selector="#apply-btn", tag_name="button", text="Apply", role="button", is_visible=True),
        InteractiveElement(selector="[name='code']", tag_name="input", text="", is_visible=True),
        InteractiveElement(selector="#hidden-el", tag_name="div", text="Hidden", is_visible=False),
    ]
    files = ["app/checkout/page.tsx", "app/cart/page.tsx"]
    diff = "+ <button id='apply-btn'>Apply</button>\n+ <input name='code' />"

    prompt = AiJourneyPlanner.build_planning_prompt(
        task=task,
        archetype=ProjectArchetype.WEB_SPA,
        interactive_elements=elements,
        git_diff=diff,
        changed_files=files,
        acceptance_criteria="User can enter SAVE20 and see total reduced by 20%",
        base_url="http://127.0.0.1:3000",
        max_journeys=4,
    )

    assert "Add discount coupon to checkout flow" in prompt
    assert "User can enter SAVE20 and see total reduced by 20%" in prompt
    assert "app/checkout/page.tsx" in prompt
    assert "app/cart/page.tsx" in prompt
    assert "#apply-btn" in prompt
    assert "[name='code']" in prompt
    assert "<button>" in prompt
    assert "+ <button id='apply-btn'>Apply</button>" in prompt
    assert "http://127.0.0.1:3000" in prompt
    assert "up to 4" in prompt


def test_extract_acceptance_criteria_from_task_and_executor_report():
    """Verify criteria extraction from task headers and executor report markdown."""
    task_with_ac = "Implement search filter.\nRequirements:\n- Filter by category\n- Filter by price"
    ac_task = AiJourneyPlanner.extract_acceptance_criteria(task_with_ac)
    assert ac_task is not None
    assert "- Filter by category" in ac_task

    exec_report = "# Summary\nDone.\n## Acceptance Criteria\n- Must pass on mobile\n- Fast rendering"
    ac_exec = AiJourneyPlanner.extract_acceptance_criteria("Simple task", exec_report)
    assert ac_exec is not None
    assert "- Must pass on mobile" in ac_exec


# ==============================================================================
# 2. Structured Plans Deserialization
# ==============================================================================

def test_valid_structured_plan_deserialization():
    """Verify valid JSON plan deserializes into Journey and JourneyStep models."""
    raw_plan = {
        "journeys": [
            {
                "id": "J-01",
                "title": "Coupon Code Application Flow",
                "description": "Verify user can apply a valid promo discount during checkout",
                "priority": 1,
                "category": "functional",
                "viewport": [1440, 900],
                "steps": [
                    {"action": "NAVIGATE", "target": "/checkout", "description": "Navigate to checkout"},
                    {"action": "FILL", "target": "[name='code']", "value": "SAVE20", "description": "Fill promo code"},
                    {"action": "CLICK", "target": "#apply-btn", "description": "Click apply button"},
                    {"action": "ASSERT_TEXT", "target": "Discount applied: -$20.00", "description": "Assert discount text"},
                    {"action": "ASSERT_ELEMENT", "target": "#discount-badge", "description": "Assert badge rendered"},
                ],
            },
            {
                "id": "J-02",
                "title": "Mobile Responsive Verification",
                "description": "Verify checkout on 375x812",
                "priority": 4,
                "category": "responsive",
                "viewport": [375, 812],
                "steps": [
                    {"action": "RESIZE", "target": "viewport", "value": "375x812"},
                    {"action": "NAVIGATE", "target": "/checkout"},
                ],
            },
        ]
    }

    elements = [
        InteractiveElement(selector="#apply-btn", tag_name="button", text="Apply"),
        InteractiveElement(selector="[name='code']", tag_name="input", text=""),
    ]

    journeys = AiJourneyPlanner.validate_and_sanitize_plan(
        raw_plan, max_journeys=5, discovered_elements=elements
    )

    assert len(journeys) == 2
    j1 = journeys[0]
    assert j1.id == "J-01"
    assert j1.title == "Coupon Code Application Flow"
    assert j1.priority == 1
    assert j1.viewport == (1440, 900)
    assert len(j1.steps) == 5
    assert j1.steps[0].action == ActionType.NAVIGATE
    assert j1.steps[0].target == "/checkout"
    assert j1.steps[1].action == ActionType.FILL
    assert j1.steps[1].value == "SAVE20"
    assert j1.steps[2].action == ActionType.CLICK
    assert j1.steps[2].target == "#apply-btn"
    assert j1.steps[3].action == ActionType.ASSERT_TEXT
    assert j1.steps[4].action == ActionType.ASSERT_ELEMENT

    j2 = journeys[1]
    assert j2.id == "J-02"
    assert j2.priority == 4
    assert j2.viewport == (375, 812)
    assert j2.steps[0].action == ActionType.RESIZE


# ==============================================================================
# 3. JSON Extraction (Plain, Fenced, Embedded, Bare Array)
# ==============================================================================

def test_extract_json_formats():
    """Verify JSON extractor handles plain JSON, fenced JSON, embedded JSON, and bare lists."""
    # Plain JSON
    plain = '{"journeys": [{"id": "J-01", "title": "Plain", "priority": 1, "steps": [{"action": "NAVIGATE", "target": "/"}]}]}'
    d1 = AiJourneyPlanner.extract_json(plain)
    assert "journeys" in d1
    assert d1["journeys"][0]["id"] == "J-01"

    # Fenced JSON with ```json
    fenced = """Here is the test plan:
```json
{
  "journeys": [{"id": "J-02", "title": "Fenced", "priority": 1, "steps": [{"action": "NAVIGATE", "target": "/"}]}]
}
```
Let me know if you need more."""
    d2 = AiJourneyPlanner.extract_json(fenced)
    assert d2["journeys"][0]["id"] == "J-02"

    # Fenced without language tag
    fenced_raw = "```\n{\"journeys\": [{\"id\": \"J-03\", \"title\": \"Raw\", \"priority\": 1, \"steps\": []}]}\n```"
    d3 = AiJourneyPlanner.extract_json(fenced_raw)
    assert d3["journeys"][0]["id"] == "J-03"

    # Bare array
    bare_array = '[{"id": "J-04", "title": "Bare", "priority": 1, "steps": []}]'
    d4 = AiJourneyPlanner.extract_json(bare_array)
    assert "journeys" in d4
    assert d4["journeys"][0]["id"] == "J-04"

    # Malformed JSON
    with pytest.raises(ValueError):
        AiJourneyPlanner.extract_json("Not a json string at all")


# ==============================================================================
# 4. Plan Validation & Sanitization (Invalid actions, excessive limits)
# ==============================================================================

def test_validate_and_sanitize_plan_enforces_limits_and_safety():
    """Verify invalid actions, excessive journeys/steps, and unsafe selectors are sanitized."""
    raw_plan = {
        "journeys": [
            {
                "id": "INVALID_ID",
                "title": "J1",
                "priority": 99,  # Should clamp to 1
                "steps": [
                    {"action": "NAVIGATE", "target": "/"},
                    {"action": "CLI_COMMAND", "target": "rm -rf /"},  # Unsupported web action -> rejected
                    {"action": "CLICK", "target": "<script>alert(1)</script>"},  # Unsafe selector -> rejected
                    {"action": "CLICK", "target": "#valid-btn"},
                ]
                + [{"action": "CLICK", "target": f"#extra-{i}"} for i in range(25)],  # Excessive steps -> clamped
            },
            # Add 8 more journeys to test max_journeys capping
        ] + [
            {"id": f"J-{i:02d}", "title": f"Extra {i}", "priority": 2, "steps": [{"action": "NAVIGATE", "target": "/"}]}
            for i in range(2, 10)
        ]
    }

    sanitized = AiJourneyPlanner.validate_and_sanitize_plan(
        raw_plan, max_journeys=3, max_steps_per_journey=5
    )

    assert len(sanitized) == 3  # Capped at max_journeys=3
    j1 = sanitized[0]
    assert j1.id == "J-01"  # Normalized ID
    assert j1.priority == 1  # Clamped priority
    assert len(j1.steps) <= 5  # Clamped steps

    step_actions = [s.action for s in j1.steps]
    assert ActionType.CLI_COMMAND not in step_actions
    assert not any("<script>" in s.target for s in j1.steps)
    assert any(s.target == "#valid-btn" for s in j1.steps)


def test_sanitize_preserves_future_navigation_selectors():
    """Verify selectors on subsequent pages (after NAVIGATE) are kept even if absent from initial DOM."""
    elements = [InteractiveElement(selector="#home-btn", tag_name="button", text="Home")]
    raw_plan = {
        "journeys": [
            {
                "id": "J-01",
                "title": "Multi-page journey",
                "priority": 1,
                "steps": [
                    {"action": "NAVIGATE", "target": "/checkout"},
                    {"action": "CLICK", "target": "#future-checkout-button"},
                ],
            }
        ]
    }

    journeys = AiJourneyPlanner.validate_and_sanitize_plan(
        raw_plan, discovered_elements=elements
    )
    assert len(journeys) == 1
    assert len(journeys[0].steps) == 2
    assert journeys[0].steps[1].target == "#future-checkout-button"


# ==============================================================================
# 5. Fallback on Adapter Timeout, Exit Error, or Malformed Output
# ==============================================================================

def test_fallback_on_adapter_timeout_or_error():
    """Verify adapter timeout or non-zero exit code triggers fallback to deterministic JourneyPlanner."""
    error_adapter = DummyNonMockAdapter(
        response=make_adapter_response(
            stdout="", stderr="Timeout expired after 25s", exit_code=124
        )
    )

    res = AiJourneyPlanner.plan(
        task="Checkout coupon flow",
        archetype=ProjectArchetype.WEB_SPA,
        adapter=error_adapter,
        git_diff="diff --git a/app/cart.tsx b/app/cart.tsx",
    )

    assert res.planner_used == "heuristic"
    assert res.fallback_occurred is True
    assert "Timeout" in (res.fallback_reason or "") or "124" in (res.fallback_reason or "")
    assert len(res.journeys) > 0  # Heuristic journeys produced


def test_fallback_on_malformed_json_response():
    """Verify malformed JSON from adapter triggers fallback."""
    bad_json_adapter = DummyNonMockAdapter(
        response=make_adapter_response(
            stdout="I am an LLM and I forgot to output JSON: {bad json",
            stderr="",
            exit_code=0,
        )
    )

    res = AiJourneyPlanner.plan(
        task="Checkout coupon flow",
        archetype=ProjectArchetype.WEB_SPA,
        adapter=bad_json_adapter,
        git_diff="diff --git a/app/cart.tsx b/app/cart.tsx",
    )

    assert res.planner_used == "heuristic"
    assert res.fallback_occurred is True
    assert "AI planning failed" in (res.fallback_reason or "")
    assert len(res.journeys) > 0


def test_fallback_when_adapter_is_none():
    """Verify missing adapter falls back to heuristic planner immediately."""
    res = AiJourneyPlanner.plan(
        task="Checkout coupon flow",
        archetype=ProjectArchetype.WEB_SPA,
        adapter=None,
    )
    assert res.planner_used == "heuristic"
    assert res.fallback_occurred is True
    assert "No LLM adapter provided" in (res.fallback_reason or "")


# ==============================================================================
# 6. Stage.run() Passes Adapter on Live Execution Path
# ==============================================================================

def test_stage_run_passes_adapter_to_tester_engine(tmp_path):
    """Verify Stage.run() passes self.adapter to TesterEngine on the live path."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Task")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    adapter = DummyNonMockAdapter()
    role = Role.load("tester")
    stage = Stage(role=role, adapter=adapter, run_manager=rm)

    mock_machine_report = MagicMock()
    mock_machine_report.status = "PASS"
    mock_machine_report.is_valid = True
    mock_machine_report.handoff = "REVIEWER"
    mock_machine_report.to_dict.return_value = {"STATUS": "PASS", "HANDOFF": "REVIEWER"}

    mock_stage_result = StageResult(
        role=role,
        prompt=RenderedPrompt.from_text("test"),
        response=make_adapter_response(stdout="PASS", stderr="", exit_code=0),
        machine_report=mock_machine_report,
        raw_markdown="PASS",
        duration_seconds=0.5,
        success=True,
    )

    with patch.object(TesterEngine, "__init__", return_value=None) as mock_engine_init, \
         patch.object(TesterEngine, "run", return_value=mock_stage_result) as mock_engine_run:
        res = stage.run(ctx)
        assert res.status == "PASS"
        assert mock_engine_run.called
        assert mock_engine_init.call_args.kwargs.get("adapter") is adapter
        assert mock_engine_init.call_args.kwargs.get("context") is ctx
        assert mock_engine_init.call_args.kwargs.get("run_manager") is rm


# ==============================================================================
# 7. WEB_SPA Engine Path Collects DOM Elements Before AI Planning
# ==============================================================================

def test_web_spa_engine_path_collects_dom_elements_before_planning(tmp_path):
    """Verify TesterEngine navigates to base_url and collects interactive DOM elements before calling AiJourneyPlanner."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Test Web App")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    mock_browser = MockBrowserDriver(viewport=(1440, 900))
    mock_browser.add_element("#promo-btn", "button", "Apply Promo")
    mock_browser.add_element("[name='promo']", "input", "")

    # Valid plan matching the pre-populated DOM elements
    plan_stdout = json.dumps({
        "journeys": [
            {
                "id": "J-01",
                "title": "Apply Promo Code",
                "priority": 1,
                "steps": [
                    {"action": "FILL", "target": "[name='promo']", "value": "DISCOUNT"},
                    {"action": "CLICK", "target": "#promo-btn"},
                ],
            }
        ]
    })
    adapter = DummyNonMockAdapter(response=make_adapter_response(stdout=plan_stdout, stderr="", exit_code=0))

    engine = TesterEngine(context=ctx, run_manager=rm, adapter=adapter)

    with patch("forge.testing.supervisor.RuntimeSupervisor.start", return_value=True), \
         patch.object(BrowserDriverFactory, "create", return_value=mock_browser), \
         patch("forge.testing.engine.ArchetypeDetector.detect", return_value=ProjectArchetype.WEB_SPA), \
         patch("forge.testing.ai_planner.AiJourneyPlanner.plan", wraps=AiJourneyPlanner.plan) as spy_ai_plan:

        res = engine.run()
        assert res.role.name == "tester"
        assert spy_ai_plan.called

        # Check arguments passed to AiJourneyPlanner.plan
        kwargs = spy_ai_plan.call_args[1]
        discovered = kwargs.get("interactive_elements", [])
        selectors = [e.selector for e in discovered]
        assert "#promo-btn" in selectors
        assert "[name='promo']" in selectors


# ==============================================================================
# 8. API, CLI, and Library Retain Existing Heuristic Behavior
# ==============================================================================

def test_non_web_archetypes_retain_heuristic_behavior(tmp_path):
    """Verify API, CLI, and Library archetypes bypass AiJourneyPlanner and retain heuristic planning."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Test Non-Web Archetypes")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    adapter = DummyNonMockAdapter()
    engine = TesterEngine(context=ctx, run_manager=rm, adapter=adapter)

    for arch in (ProjectArchetype.CLI, ProjectArchetype.LIBRARY):
        with patch("forge.testing.engine.ArchetypeDetector.detect", return_value=arch), \
             patch("forge.testing.ai_planner.AiJourneyPlanner.plan") as mock_ai_plan, \
             patch("forge.testing.planner.JourneyPlanner.plan_journeys", wraps=JourneyPlanner.plan_journeys) as spy_heuristic_plan:

            engine.run()
            # AiJourneyPlanner must NOT be called for non-web archetypes
            assert not mock_ai_plan.called
            assert spy_heuristic_plan.called


# ==============================================================================
# 9. Testing Budget Accounts for Planning Time
# ==============================================================================

def test_budget_accounts_for_planning_time(tmp_path):
    """Verify that elapsed time during AI planning is charged to the budget tracker."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Test Budget Tracking")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    budget = TestingBudget(max_runtime_seconds=1.0)  # Very tight budget (1 second)

    # Slow adapter that sleeps 0.2s
    class SlowAdapter(DummyNonMockAdapter):
        def execute(self, prompt: str, cwd: Optional[Path] = None, timeout: Optional[int] = None) -> AdapterResponse:
            time.sleep(0.15)
            plan = {"journeys": [{"id": "J-01", "title": "J1", "priority": 1, "steps": [{"action": "NAVIGATE", "target": "/"}]}]}
            return make_adapter_response(stdout=json.dumps(plan), stderr="", exit_code=0)

    engine = TesterEngine(context=ctx, run_manager=rm, budget=budget, adapter=SlowAdapter())

    mock_browser = MockBrowserDriver(viewport=(1440, 900))
    with patch("forge.testing.supervisor.RuntimeSupervisor.start", return_value=True), \
         patch.object(BrowserDriverFactory, "create", return_value=mock_browser), \
         patch("forge.testing.engine.ArchetypeDetector.detect", return_value=ProjectArchetype.WEB_SPA):

        res = engine.run()
        # Tracker elapsed time must be >= 0.1s
        assert engine.tracker.elapsed_seconds >= 0.1


# ==============================================================================
# 10. Validated Journeys Flow Through Driver, Evidence, Report & Repair
# ==============================================================================

def test_ai_journeys_flow_through_driver_and_generate_defect_repair_card(tmp_path):
    """Verify AI-planned journeys execute on driver, detect defects, and generate repair feedback."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Test Defect Propagation")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    # Plan with an assertion for text that will NOT be in the DOM
    plan = {
        "journeys": [
            {
                "id": "J-01",
                "title": "Failing assertion journey",
                "priority": 1,
                "steps": [
                    {"action": "NAVIGATE", "target": "/"},
                    {"action": "ASSERT_TEXT", "target": "STRING_THAT_DOES_NOT_EXIST_IN_DOM"},
                ],
            }
        ]
    }
    adapter = DummyNonMockAdapter(response=make_adapter_response(stdout=json.dumps(plan), stderr="", exit_code=0))
    engine = TesterEngine(context=ctx, run_manager=rm, adapter=adapter)

    mock_browser = MockBrowserDriver(viewport=(1440, 900))
    with patch("forge.testing.supervisor.RuntimeSupervisor.start", return_value=True), \
         patch.object(BrowserDriverFactory, "create", return_value=mock_browser), \
         patch("forge.testing.engine.ArchetypeDetector.detect", return_value=ProjectArchetype.WEB_SPA):

        res = engine.run()
        assert res.status == "FAIL"
        assert res.response.exit_code == 1
        assert res.success is False

        # Check saved 04_tester.json artifact
        json_path = run.run_dir / "04_tester.json"
        assert json_path.exists()
        report_data = json.loads(json_path.read_text(encoding="utf-8"))

        assert report_data["STATUS"] == "FAIL"
        assert report_data["HANDOFF"] == "EXECUTOR"
        assert report_data["DATA"]["planner"] == "ai"
        assert report_data["DATA"]["planner_fallback"] is False

        # Verify defect card under ISSUES
        major_issues = report_data["ISSUES"]["MAJOR"]
        assert len(major_issues) >= 1
        defect = major_issues[0]
        assert "STRING_THAT_DOES_NOT_EXIST_IN_DOM" in defect["TITLE"]
        assert "reproduction_script" in defect["EVIDENCE"]


# ==============================================================================
# 11. Startup Failures Cannot Produce False PASS
# ==============================================================================

def test_supervisor_startup_failure_reports_blocked_not_pass(tmp_path):
    """Verify application dev server startup failure results in BLOCKED status, never false PASS."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Failing Dev Server")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    adapter = DummyNonMockAdapter()
    engine = TesterEngine(context=ctx, run_manager=rm, adapter=adapter)

    with patch("forge.testing.engine.ArchetypeDetector.detect", return_value=ProjectArchetype.WEB_SPA), \
         patch("forge.testing.supervisor.RuntimeSupervisor.start", return_value=False):

        res = engine.run()
        assert res.status == "BLOCKED"
        assert res.response.exit_code == 1
        assert res.success is False
        assert "failed to start" in res.machine_report.reason or "failed to become ready" in res.machine_report.reason


def test_browser_driver_init_failure_reports_blocked_not_pass(tmp_path):
    """Verify browser driver initialization failure results in BLOCKED status, never false PASS."""
    rm = RunManager(tmp_path)
    run = rm.create_run("Failing Browser Driver")
    ctx = Context(
        run=run,
        project_root=tmp_path,
        config=Config.default(),
        git=GitService(tmp_path),
    )

    adapter = DummyNonMockAdapter()
    engine = TesterEngine(context=ctx, run_manager=rm, adapter=adapter)

    with patch("forge.testing.engine.ArchetypeDetector.detect", return_value=ProjectArchetype.WEB_SPA), \
         patch("forge.testing.supervisor.RuntimeSupervisor.start", return_value=True), \
         patch.object(BrowserDriverFactory, "create", side_effect=RuntimeError("Browser process crashed immediately")):

        res = engine.run()
        assert res.status == "BLOCKED"
        assert res.response.exit_code == 1
        assert res.success is False
        assert "Browser driver failed to initialize" in res.machine_report.reason or "failed to initialize" in res.machine_report.reason


def test_ai_planner_recovers_valid_json_on_nonzero_adapter_exit_code(tmp_path):
    """Verify that if adapter returns non-zero exit code (e.g. timeout) but captured valid JSON plan in stdout, it recovers and uses the journeys."""
    valid_plan_json = json.dumps({
        "journeys": [
            {
                "id": "J-01",
                "title": "Recovered Journey",
                "priority": 1,
                "category": "functional",
                "steps": [
                    {"action": "NAVIGATE", "target": "/"},
                    {"action": "CLICK", "target": "#submit-btn"},
                ]
            }
        ]
    })

    class TimeoutAdapter(DummyNonMockAdapter):
        def execute(self, prompt, cwd=None, timeout=None):
            return AdapterResponse(
                stdout=f"```json\n{valid_plan_json}\n```",
                stderr="Execution timed out after 300 seconds.",
                exit_code=124,
                duration_seconds=300.0,
                raw_output=f"```json\n{valid_plan_json}\n```",
            )

    adapter = TimeoutAdapter()
    res = AiJourneyPlanner.plan(
        task="Test Task",
        archetype=ProjectArchetype.WEB_SPA,
        adapter=adapter,
        interactive_elements=[InteractiveElement(selector="#submit-btn", tag_name="button", text="Submit")],
        project_root=tmp_path,
    )

    assert res.fallback_occurred is False
    assert res.planner_used == "ai"
    assert len(res.journeys) == 1
    assert res.journeys[0].id == "J-01"
    assert res.journeys[0].title == "Recovered Journey"

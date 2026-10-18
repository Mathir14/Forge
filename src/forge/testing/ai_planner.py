"""AI-Guided Journey Planner for Tester v2.

Formulates semantic, DOM-grounded test journeys for web applications by:
1. Grounding in live DOM elements discovered by BrowserDriver
2. Incorporating user task requirements, acceptance criteria, and git diff context
3. Using the configured LLM adapter via single-invocation structured prompt
4. Validating and sanitizing journeys against supported ActionType constraints
5. Falling back cleanly to deterministic JourneyPlanner on timeout or error
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.testing.budget import TestingBudget
from forge.testing.models import (
    ActionType,
    InteractiveElement,
    Journey,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner

logger = logging.getLogger(__name__)


# Supported ActionType set for Web SPA browser automation
SUPPORTED_WEB_ACTIONS: Set[ActionType] = {
    ActionType.NAVIGATE,
    ActionType.CLICK,
    ActionType.FILL,
    ActionType.UPLOAD_FILE,
    ActionType.RESIZE,
    ActionType.ASSERT_TEXT,
    ActionType.ASSERT_ELEMENT,
}

def is_valid_selector(target: str) -> bool:
    """Validate CSS and Playwright selector syntax safely.

    Accepts:
    - Standard CSS selectors (IDs, classes, tag[attr='val'], combinators >, +, ~)
    - Playwright pseudo-selectors (:has-text("..."), :text("..."), :has(...), :is(...), :not(...))
    - Unicode letters, international characters, and emojis in selectors and quoted strings
    - Attribute selectors with complex quoted values

    Rejects:
    - HTML injection or execution vectors (<script, javascript:, data:text/html, <svg, onload=, etc.)
    - Malformed selectors with unbalanced quotes, brackets, or parentheses
    - Control characters (< ASCII 32)
    - Excessively long (> 500 chars) or empty selectors
    """
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if len(target) == 0 or len(target) > 500:
        return False
    # No control characters
    if any(ord(c) < 32 for c in target):
        return False
    # Reject HTML tags or dangerous execution vectors
    target_lower = target.lower()
    if "<" in target_lower or any(bad in target_lower for bad in ("javascript:", "data:text/html", "onerror=", "onload=")):
        return False

    # Check balanced delimiters (quotes, brackets, parentheses)
    cleaned = re.sub(r"\\.", "", target)
    if cleaned.count('"') % 2 != 0 or cleaned.count("'") % 2 != 0:
        return False
    if cleaned.count("[") != cleaned.count("]") or cleaned.count("(") != cleaned.count(")"):
        return False

    # Check that selector contains only printable characters and valid selector syntax
    pattern = re.compile(r"^[\w\s\-\.\#\[\]\=\'\"\:\*\>\+\~\,\(\)\^\|\$\\\/\!\%\@\u0080-\U0010ffff\&\?]+$")
    return bool(pattern.match(target))


# Plausible CSS selector validation regex (Unicode/emoji-aware backwards-compatible pattern)
_CSS_SELECTOR_PATTERN = re.compile(
    r"^[\w\s\-\.\#\[\]\=\'\"\:\*\>\+\~\,\(\)\^\|\$\\\/\!\%\@\u0080-\U0010ffff\&\?]+$"
)


@dataclass
class PlanningResult:
    """Result of journey planning including diagnostic metadata."""
    journeys: List[Journey]
    planner_used: str  # "ai" or "heuristic"
    fallback_occurred: bool = False
    fallback_reason: Optional[str] = None
    raw_response: Optional[str] = None
    duration_seconds: float = 0.0
    prompt: Optional[str] = None


class AiJourneyPlanner:
    """LLM-guided, DOM-grounded journey planner for web applications."""

    @classmethod
    def extract_acceptance_criteria(
        cls, task: str, executor_report: str = ""
    ) -> Optional[str]:
        """Extract explicit acceptance criteria from task text or prior executor report."""
        if not task and not executor_report:
            return None

        # Check task text for criteria headers
        m = re.search(
            r"(?:acceptance criteria|requirements|expected behavior|criteria)[\s:]*\n?([\s\S]+?)(?:\n\n[A-Z][a-zA-Z\s]+:|$)",
            task,
            re.IGNORECASE,
        )
        if m:
            criteria = m.group(1).strip()
            if len(criteria) > 10:
                return criteria[:2000]

        # Check executor report for criteria section
        if executor_report:
            m_exec = re.search(
                r"##\s*(?:Acceptance Criteria|Requirements|Verification Scope|Expected Behavior)\s*\n?([\s\S]+?)(?:\n##|$)",
                executor_report,
                re.IGNORECASE,
            )
            if m_exec:
                criteria = m_exec.group(1).strip()
                if len(criteria) > 10:
                    return criteria[:2000]

        return None

    @classmethod
    def build_planning_prompt(
        cls,
        task: str,
        archetype: ProjectArchetype,
        interactive_elements: Optional[List[InteractiveElement]] = None,
        git_diff: str = "",
        changed_files: Optional[List[str]] = None,
        acceptance_criteria: Optional[str] = None,
        base_url: str = "http://127.0.0.1:3000",
        max_journeys: int = 5,
        fixtures_summary: str = "",
        edge_case_requirements: Optional[List[Any]] = None,
    ) -> str:
        """Construct a bounded planning prompt with DOM elements and task context."""
        files = changed_files or []
        elements = interactive_elements or []

        # Bound git diff to avoid blowing prompt transport budgets
        bounded_diff = git_diff
        max_diff_len = 15000
        if len(bounded_diff) > max_diff_len:
            bounded_diff = (
                bounded_diff[:max_diff_len]
                + f"\n\n[... Truncated remaining {len(git_diff) - max_diff_len} chars of diff ...]"
            )

        # Format discovered DOM elements
        element_lines: List[str] = []
        # Filter to visible elements, up to 50
        visible_elements = [e for e in elements if e.is_visible] or elements
        for el in visible_elements[:50]:
            parts = [f"selector: {el.selector}", f"tag: <{el.tag_name}>"]
            if el.text:
                parts.append(f"text: '{el.text}'")
            if el.role:
                parts.append(f"role: '{el.role}'")
            if not el.is_enabled:
                parts.append("disabled: true")
            element_lines.append("  - " + " | ".join(parts))

        elements_block = (
            "\n".join(element_lines)
            if element_lines
            else "  (No interactive elements discovered on initial landing page /)"
        )

        ac_block = ""
        if acceptance_criteria:
            ac_block = f"""
## Acceptance Criteria
{acceptance_criteria}
"""

        fixtures_block = ""
        if fixtures_summary and fixtures_summary.strip():
            fixtures_block = f"""
## Available Test Fixtures & Data
{fixtures_summary}
"""

        edge_cases_block = ""
        if edge_case_requirements:
            ec_lines = []
            for ec in edge_case_requirements:
                ui_str = f" [Testable via UI: {ec.ui_guidance}]" if getattr(ec, "testable_via_ui", False) else f" [UI-Unreachable: {getattr(ec, 'untestable_reason', '')}]"
                ec_lines.append(f"- **{getattr(ec, 'id', '')}** ({getattr(ec, 'component', '')}): {getattr(ec, 'title', '')} - {getattr(ec, 'description', '')}{ui_str}")
            edge_cases_block = f"""
## Changed-Branch & Edge-Case Requirements
{chr(10).join(ec_lines)}
"""

        changed_files_block = (
            "\n".join(f"  - {f}" for f in files[:20])
            if files
            else "  (None recorded)"
        )

        return f"""# TESTER JOURNEY PLANNER

You are Forge's Senior QA Automation Engineer.
Your mission is to formulate up to {max_journeys} empirical, multi-step browser test journeys
to verify the application runtime against user requirements and git changes.

## Task Description
{task}
{ac_block}
## Changed Files in Current Task
{changed_files_block}

## Git Changes (Diff Summary)
```diff
{bounded_diff if bounded_diff.strip() else "(No git diff available)"}
```
{fixtures_block}
{edge_cases_block}
## Discovered Live Interactive DOM Elements (at {base_url}/)
{elements_block}

## Planning Guidelines
1. **Prioritize Real User Workflows & Prerequisites**:
   - Do NOT merely test empty states or surface navigation tabs. If modified functionality requires data setup, dataset loading, or prerequisite form fills, plan the prerequisite actions first (e.g. use ActionType UPLOAD_FILE with an available fixture, then click load/submit buttons).
   - Prioritize exercising functionality modified in changed files and git diff over static navigation tabs.
   - Distinguish navigation smoke tests from functional regression tests. At least 60% of journeys should execute functional workflows exercising changed components.
   - Priority 1: Primary feature functionality modified in current task/diff (including data loading prerequisites).
   - Priority 2: Negative and boundary test cases (invalid inputs, empty form submissions, edge cases).
   - Priority 3: Adjacent navigation flows, routes, or menus.
   - Priority 4: Responsive mobile layout verification (viewport: [375, 812]).
2. **Critical File Upload Workflow (Upload + Ingestion)**:
   - Selecting a file in an `<input type='file'>` does NOT trigger data ingestion on its own.
   - When you plan an `UPLOAD_FILE` action, you MUST immediately follow it with an interaction that confirms or loads the data (e.g. `CLICK button:has-text('Load Dataset')`, `CLICK button:has-text('Load')`, `CLICK button:has-text('Submit')`, `CLICK button:has-text('Upload')`, `CLICK button:has-text('Import')`, or `button[type='submit']`).
   - After the confirmation click, you MUST include an assertion that verifies successful data ingestion (e.g. `ASSERT_TEXT 'Dataset is loaded and ready'`, `ASSERT_TEXT 'ready'`, `ASSERT_ELEMENT [data-testid='stMetric']`, `ASSERT_TEXT 'Rows'`), NEVER merely asserting that the file uploader dropzone exists.
   - An upload workflow is incomplete and invalid unless data ingestion is confirmed.
3. **Data Preparation, Form Configuration & Target Selection**:
   - Tabular, ML, and analytical workflows require data splitting, feature scaling, or label configuration before model training or evaluation can succeed.
   - **Select Suitable Fixtures**: Always use the fixture tagged `[RECOMMENDED: SUITABLE]` in the prompt under "Available Test Fixtures". Do NOT select fixtures marked `[UNSUITABLE]`.
   - **Inspect Fixture Columns & Select Target**: Look at the columns and Target Recommendation in the fixture summary. Identify the ground-truth target/label column (e.g. 'target', 'label', 'class', 'y', 'status', 'outcome', or discrete/categorical ground truth).
   - **Do NOT Accept Incompatible Defaults**: In many frameworks (like Streamlit, React, or Gradio), selectboxes default to the first column (e.g. index 0, which may be a continuous feature like 'feature_signal'). Training a classification model on a continuous feature causes `ValueError: Unknown label type: continuous`!
   - **Select the Target Column**: In the data preparation/split tab, explicitly select the target column:
     - Use `CLICK [data-testid="stSelectbox"] div[data-baseweb="select"]` (or `CLICK [data-testid="stSelectbox"]`) to open the dropdown.
     - Then use `CLICK li:has-text('target')` or `CLICK [role="option"]:has-text('target')` (matching the target column name from the fixture) to select it.
   - **Trigger Dataset Splitting / Preparation**: After selecting the target column, click the split/prepare button (e.g. `CLICK button:has-text('Split Dataset')`) and verify confirmation (e.g. `ASSERT_TEXT 'Dataset split successfully!'`). For ASSERT_TEXT scoping, scope to `[data-testid="stMain"]` or leave value empty rather than scoping to narrow container IDs like `[data-testid="stAlertContainer"]` which can match sidebar elements.
    - **Downstream Model & Stress Dependencies and Reactive Views**:
      - Distinguish between **Explicitly Triggered Operations** requiring an action button (e.g. data upload, dataset split, baseline model training, running batch stress tests, Module 5 calculate robustness) and **Reactive Views** that calculate and render outputs automatically upon mounting without an action button (e.g. Module 7 Model Comparison, Module 8 Reliability Scoring).
      - **NEVER invent nonexistent trigger buttons** such as "Compare All Models", "Calculate Reliability Scores", "Generate Comparison", or "Compute Scores".
      - **Module-specific requirements & changed behavior verification**:
        - **Module 1 (Data Management)**: Explicit trigger. Upload file -> `CLICK button:has-text('Load Dataset')` -> verify ingestion with `ASSERT_TEXT 'Dataset is loaded and ready'` (or `'ready'`) -> switch to split tab -> select target column -> `CLICK button:has-text('Split Dataset')` -> `ASSERT_TEXT 'Dataset split successfully!'`.
        - **Module 2 (Baseline Modeling)**: Explicit trigger. `CLICK button:has-text('Train')` -> verify completion with `ASSERT_TEXT 'trained successfully!'` (or `ASSERT_TEXT 'Accuracy'`). **MULTI-MODEL COMPARISON**: To verify multi-candidate model comparison (`modules/model_module.py`), train multiple models: after training Logistic Regression (verified with `ASSERT_TEXT 'trained successfully!'`), select 'Random Forest' in the model selectbox and `CLICK button:has-text('Train')`, verify `ASSERT_TEXT 'trained successfully!'`, then switch to Tab 4 (`CLICK button[role='tab']:has-text('Save/Load Models')`) and assert `ASSERT_TEXT 'Best Model'`.
        - **Module 4 (Stress Testing)**: Explicit trigger. `CLICK button[role='tab']:has-text('Batch Stress Tests')` -> `CLICK button:has-text('Run Batch Stress Tests')` -> `ASSERT_TEXT 'Completed'`.
        - **Module 5 (Post-Stress Evaluation)**: Explicit trigger. Requires Module 4 Batch Stress Tests completed first. On Module 5, `CLICK button:has-text('Calculate Robustness Score')` -> `ASSERT_TEXT 'Score Interpretation'`.
        - **Module 7 (Model Comparison)**: **REACTIVE VIEW**. Requires trained model and stress tests. When navigating to Module 7 (`CLICK [data-testid="stRadio"] label:has-text('7️⃣ Model Comparison')`), comparison metrics and charts render automatically. DO NOT CLICK any comparison button. Directly assert the rendered Tab 1 outputs: `ASSERT_TEXT 'Performance Metrics Comparison'`. Module 7 has 5 live tabs: Tab 1: 'Performance', Tab 2: 'Radar Chart', Tab 3: 'Confusion Matrices', Tab 4: 'Robustness', Tab 5: 'Best Model'. To verify `modules/comparison_module.py` (palette, grade resolution, and recommendation), switch to Tab 4 (`CLICK button[role='tab']:has-text('Robustness')` -> `ASSERT_TEXT 'Robustness Score'`) or Tab 5 (`CLICK button[role='tab']:has-text('Best Model')` -> `ASSERT_TEXT 'Best Model Recommendation'` or `ASSERT_TEXT 'Full Model Ranking'`). NEVER target 'Composite Ranking' as a tab name because Tab 5 is titled 'Best Model'.
        - **Module 8 (Reliability Scoring)**: **REACTIVE VIEW**. Unified reliability scores render automatically upon mounting. DO NOT CLICK any calculate button. Directly assert Tab 1 outputs (`ASSERT_TEXT 'Reliability Score Overview'`, `ASSERT_TEXT 'Score Summary Table'`). To assert detail scores in Tab 3, first `CLICK button[role='tab']:has-text('Model Detail')`, then `ASSERT_TEXT 'Total Score:'`.
4. **Multi-Module Sequential Workflows & SPA State Preservation**:
   - In Single Page Applications (like Streamlit, React, or Vue), calling `NAVIGATE /` causes a full page refresh that wipes in-memory session state (including uploaded datasets, trained models, and stress test results).
   - To test multi-module workflows (e.g. Data Ingestion -> Model Training -> Stress Testing -> Comparison -> Reliability):
     - Chain prerequisite steps in sequence within the journey using UI navigation (e.g. `CLICK [data-testid="stRadio"] label:has-text(...)`, tabs, or menu links), which preserves in-memory application state.
     - Avoid redundant `NAVIGATE /` steps after state is established.
     - If testing downstream views (such as Comparison or Reliability), perform the necessary prerequisites (upload dataset, load dataset, configure target, split data, train model, run batch stress tests) in sequence before asserting downstream components.
5. **Ground in Real Selectors & Avoid Ephemeral IDs**:
   - Prefer selectors from the Discovered Live Elements table (e.g. IDs, names, roles, or clear CSS selectors).
   - **BANNED EPHEMERAL SELECTORS**: NEVER use framework-generated internal IDs like `#tabs-bui...`, `#bui-...`, or dynamic incrementing hash IDs. ALWAYS use semantic selectors like `button[role='tab']:has-text('...')`, `div[role='tablist'] button`, or `label:has-text('...')`.
   - **Mobile Viewports ([375, 812]) & Sidebar Drawer**:
     - In responsive mobile viewports, sidebars and navigation panels are collapsed by default into a drawer.
     - When starting a mobile journey at `/`: Note that previous journeys may have left the application on a downstream module.
     - To test Module 1 on mobile: Expand the drawer by clicking `CLICK [data-testid="stExpandSidebarButton"], button[aria-label="Expand sidebar"]` (or `[data-testid="collapsedControl"] button`), then click `CLICK label:has-text('1️⃣ Data Management')` to return to the initial module, and verify `ASSERT_TEXT 'Upload Data'` or `ASSERT_TEXT 'Data Management'`.
   - If navigating to new routes (e.g. /checkout), use standard, plausible CSS selectors.
6. **Use Realistic Input Values & Data Fixtures**:
   - Use available fixtures via UPLOAD_FILE to seed workflows when required.
   - Provide realistic data for form fields (e.g. valid numbers, real search terms).
   - Never use generic placeholder text like "test_value" when a specific domain format is expected.
7. **Include Grounded & Explicit Assertions (No Empty-State Pass & No Hallucinated Copy)**:
   - **Grounding Rule**: ONLY use ASSERT_TEXT for text strings that are directly observed in the Discovered Live Elements table or explicitly stated in Acceptance Criteria or Git Diff.
   - **Do NOT Invent Unverified Copy**: Never guess or hallucinate specific multi-word sentences (e.g. do NOT assert "Batch stress testing completed!" when the UI displays "✅ Completed 19 stress tests!"). Assert high-confidence keywords or short fragments (e.g. 'Completed', 'stress tests!', 'Analysis complete!', 'Score Overview', 'Model Detail', 'Robustness Score').
   - **Banned Empty-State Assertions**: NEVER use ASSERT_TEXT or ASSERT_ELEMENT to verify empty-state blocker warnings (such as '⚠️ No data loaded', '⚠️ No models trained', '⚠️ No stress test results available! Go to 4️⃣ Stress Testing', 'Please load and prepare data in Module 1 first'). Asserting that prerequisite data is missing is a test failure, not a functional validation pass.
   - **Inferred States**: If asserting a dynamic state where exact copy is unconfirmed, prefer ASSERT_ELEMENT (verifying alert containers, cards, badges) or a short high-confidence keyword (e.g. "Error", "Ready").
   - **Context Scoping**: For ASSERT_TEXT, use the optional `value` field to specify a scoping CSS selector (e.g. "main", "h1", "h2", "[role='main']", "[data-testid='stMain']") so that assertions check the intended view rather than persistent navigation menus.
8. **Supported Step Actions**:
   - NAVIGATE: target is path (e.g. "/" or "/cart") or full URL
   - CLICK: target is CSS selector (e.g. "#submit-btn", "button[type='submit']", "label:has-text('...')")
   - FILL: target is CSS selector, value is input text
   - UPLOAD_FILE: target is CSS selector of file input (e.g. "input[type='file']", "[data-testid='stFileUploaderDropzoneInput']"), value is fixture path or name
   - RESIZE: target is "viewport", value is "WIDTHxHEIGHT" (e.g. "375x812")
   - ASSERT_TEXT: target is expected text string; optional value is scoping CSS selector
   - ASSERT_ELEMENT: target is CSS selector expected to exist in rendered DOM

## Output Format
Respond ONLY with a single valid JSON object matching this exact schema:
```json
{{
  "journeys": [
    {{
      "id": "J-01",
      "title": "Short descriptive title",
      "description": "What this journey verifies",
      "priority": 1,
      "category": "functional",
      "viewport": [1440, 900],
      "steps": [
        {{
          "action": "NAVIGATE",
          "target": "/",
          "description": "Navigate to home page"
        }},
        {{
          "action": "FILL",
          "target": "#input-id",
          "value": "realistic_value",
          "description": "Enter value into input"
        }},
        {{
          "action": "CLICK",
          "target": "#btn-id",
          "description": "Click submit button"
        }},
        {{
          "action": "ASSERT_TEXT",
          "target": "Success message",
          "value": "main, [role='main']",
          "description": "Assert success message is displayed in main content"
        }}
      ]
    }}
  ]
}}
```
"""

    @classmethod
    def extract_json(cls, text: str) -> Dict[str, Any]:
        """Extract and parse JSON object from raw LLM output text."""
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("Adapter returned empty output.")

        # 1. Check for markdown code fence (```json ... ``` or ``` ... ```)
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
        if fence_match:
            candidate = fence_match.group(1).strip()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
                elif isinstance(parsed, list):
                    return {"journeys": parsed}
            except json.JSONDecodeError:
                pass  # Fall through to substring search

        # 2. Try direct json.loads
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
            elif isinstance(parsed, list):
                return {"journeys": parsed}
        except json.JSONDecodeError:
            pass

        # 3. Locate outermost { ... }
        start_brace = cleaned.find("{")
        end_brace = cleaned.rfind("}")
        if start_brace != -1 and end_brace != -1 and end_brace > start_brace:
            candidate = cleaned[start_brace : end_brace + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # 4. Locate outermost [ ... ] (array of journeys)
        start_bracket = cleaned.find("[")
        end_bracket = cleaned.rfind("]")
        if start_bracket != -1 and end_bracket != -1 and end_bracket > start_bracket:
            candidate = cleaned[start_bracket : end_bracket + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return {"journeys": parsed}
            except json.JSONDecodeError:
                pass

        raise ValueError("Could not locate or parse valid JSON object in adapter response.")

    @classmethod
    def validate_and_sanitize_plan(
        cls,
        raw_data: Dict[str, Any],
        max_journeys: int = 5,
        max_steps_per_journey: int = 25,
        discovered_elements: Optional[List[InteractiveElement]] = None,
    ) -> List[Journey]:
        """Validate and sanitize raw journey plan data against domain constraints."""
        raw_journeys = raw_data.get("journeys")
        if not isinstance(raw_journeys, list) or not raw_journeys:
            raise ValueError("Plan contains no 'journeys' list or journeys is empty.")

        discovered_selectors: Set[str] = set()
        if discovered_elements:
            for el in discovered_elements:
                if el.selector:
                    discovered_selectors.add(el.selector.lower())

        validated_journeys: List[Journey] = []

        for idx, rj in enumerate(raw_journeys[:max_journeys]):
            if not isinstance(rj, dict):
                continue

            j_id = str(rj.get("id") or f"J-{idx + 1:02d}").strip()
            if not re.match(r"^J-[0-9]{2,}$", j_id):
                j_id = f"J-{idx + 1:02d}"

            title = str(rj.get("title") or f"Journey {j_id}").strip()[:100]
            desc = str(rj.get("description") or title).strip()

            raw_priority = rj.get("priority", 1)
            try:
                priority = int(raw_priority)
                if priority < 1 or priority > 4:
                    priority = 1
            except (ValueError, TypeError):
                priority = 1

            category = str(rj.get("category") or "functional").lower().strip()
            if category not in ("functional", "responsive", "smoke", "negative"):
                category = "functional"

            viewport: Tuple[int, int] = (1440, 900)
            raw_vp = rj.get("viewport")
            if isinstance(raw_vp, (list, tuple)) and len(raw_vp) >= 2:
                try:
                    w, h = int(raw_vp[0]), int(raw_vp[1])
                    if w > 0 and h > 0:
                        viewport = (w, h)
                except (ValueError, TypeError):
                    viewport = (1440, 900)

            # Process and validate steps
            raw_steps = rj.get("steps")
            if not isinstance(raw_steps, list):
                continue

            validated_steps: List[JourneyStep] = []
            has_navigated = False
            journey_valid = True
            validation_error = None

            for s_idx, rs in enumerate(raw_steps[:max_steps_per_journey]):
                if not isinstance(rs, dict):
                    continue

                act_raw = str(rs.get("action", "")).upper().strip()
                try:
                    action = ActionType(act_raw)
                except ValueError:
                    journey_valid = False
                    validation_error = f"Step {s_idx + 1} has unsupported action '{act_raw}'"
                    logger.debug("Discarding step %d with unsupported action '%s'", s_idx + 1, act_raw)
                    continue

                if action not in SUPPORTED_WEB_ACTIONS:
                    journey_valid = False
                    validation_error = f"Step {s_idx + 1} action '{action.value}' not supported for WEB_SPA"
                    logger.debug("Discarding step %d with action '%s' not supported for WEB_SPA", s_idx + 1, action.value)
                    continue

                target = str(rs.get("target") or "").strip()
                if not target:
                    journey_valid = False
                    validation_error = f"Step {s_idx + 1} target is empty"
                    continue

                # Basic safety check against script/command injection
                if any(bad in target.lower() for bad in ("<script", "javascript:", "data:text/html", "<svg")):
                    journey_valid = False
                    validation_error = f"Step {s_idx + 1} target '{target}' rejected for security"
                    logger.warning("Rejecting unsafe target in step %d: %s", s_idx + 1, target)
                    continue

                # Sanitize clicks on hallucinated trigger buttons for reactive views
                if action == ActionType.CLICK:
                    target_lower = target.lower()
                    nonexistent_reactive_triggers = (
                        "compare all models",
                        "calculate reliability scores",
                        "generate comparison",
                        "compute reliability",
                        "compute scores",
                        "calculate scores",
                    )
                    matching_triggers = [trig for trig in nonexistent_reactive_triggers if trig in target_lower]
                    if matching_triggers:
                        is_present_in_dom = False
                        if discovered_elements:
                            for el in discovered_elements:
                                el_sel = el.selector.lower() if el.selector else ""
                                el_txt = el.text.lower() if el.text else ""
                                if any(trig in el_sel or trig in el_txt for trig in matching_triggers):
                                    is_present_in_dom = True
                                    break
                                if target_lower and (target_lower in el_sel or el_sel in target_lower):
                                    is_present_in_dom = True
                                    break
                        if not is_present_in_dom:
                            logger.info("Omitting nonexistent trigger button click '%s' for reactive module; outputs render automatically.", target)
                            continue
                        else:
                            logger.debug("Preserving trigger button click '%s' because it is present in discovered DOM.", target)

                    # Normalize model training button and tab in Module 2
                    if "tab" in target_lower or "role='tab'" in target_lower or 'role="tab"' in target_lower:
                        if any(k in target_lower for k in ("train model", "train models", "training", "model selection")):
                            target = "button[role='tab']:has-text('Model Selection'), button[role='tab']:has-text('Training')"
                    elif any(k in target_lower for k in ("train baseline model", "train baseline models", "train models", "train model")) or target_lower == "button:has-text('train')":
                        if "button" in target_lower and "tab" not in target_lower:
                            target = "button:has-text('Train')"

                    # Normalize tabs in Module 7 and Module 2
                    if "robustness & calibration" in target_lower or "robustness and calibration" in target_lower:
                        target = "button[role='tab']:has-text('Robustness')"
                    elif any(k in target_lower for k in ("composite ranking", "composite score", "composite breakdown", "model ranking", "ranking tab", "best model & recommendations")):
                        if "tab" in target_lower or "role='tab'" in target_lower or 'role="tab"' in target_lower:
                            target = "button[role='tab']:has-text('Best Model')"
                    elif any(k in target_lower for k in ("model comparison", "compare models")) and ("tab" in target_lower or "role='tab'" in target_lower):
                        target = "button[role='tab']:has-text('Model Evaluation'), button[role='tab']:has-text('Best Model')"

                    # Normalize mobile drawer expand control selector
                    if "collapsedcontrol" in target_lower or "expandsidebar" in target_lower:
                        target = '[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"] button, [data-testid="collapsedControl"] button, [data-testid="collapsedControl"], button[aria-label="Expand sidebar"]'

                if action == ActionType.ASSERT_ELEMENT:
                    target_lower = target.lower()
                    if "collapsedcontrol" in target_lower or "expandsidebar" in target_lower:
                        target = '[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"], button[aria-label="Expand sidebar"]'

                if action == ActionType.ASSERT_TEXT:
                    target_low = target.lower().strip()
                    if target_low in (
                        "dataset loaded successfully!",
                        "dataset loaded successfully",
                        "dataset uploaded successfully!",
                        "dataset uploaded successfully",
                    ):
                        target = "Dataset is loaded and ready"

                value = rs.get("value")
                val_str = str(value) if value is not None else None

                step_desc = str(rs.get("description") or f"{action.value} {target}").strip()
                exp_state = rs.get("expected_state")
                exp_str = str(exp_state).strip() if exp_state else None

                # Specific action validation
                if action == ActionType.NAVIGATE:
                    has_navigated = True
                elif action in (ActionType.CLICK, ActionType.FILL, ActionType.UPLOAD_FILE, ActionType.ASSERT_ELEMENT):
                    # Validate selector syntax with Unicode and Playwright selector support
                    if not is_valid_selector(target):
                        journey_valid = False
                        validation_error = f"Step {s_idx + 1} selector '{target}' is malformed or invalid syntax"
                        logger.debug("Discarding step with invalid CSS selector: %s", target)
                        continue

                    # If on initial page and discovered elements exist, check whether selector is known or plausible
                    if not has_navigated and discovered_selectors:
                        # Allow known selectors, standard tag selectors, or common input attributes
                        target_clean = target.lower().strip()
                        is_known = (
                            target_clean in discovered_selectors
                            or target_clean in ("button", "a", "input", "select", "textarea")
                            or any(kw in target_clean for kw in ("#", ".", "[name=", "[type=", "[role=", ":has-text(", ":text("))
                        )
                        if not is_known:
                            logger.debug("Selector '%s' not present in initial DOM, keeping as dynamic", target)

                elif action == ActionType.RESIZE:
                    if not val_str or "x" not in val_str:
                        val_str = "1440x900"

                validated_steps.append(
                    JourneyStep(
                        action=action,
                        target=target,
                        value=val_str,
                        description=step_desc,
                        expected_state=exp_str,
                    )
                )

            if validated_steps:
                validated_journeys.append(
                    Journey(
                        id=j_id,
                        title=title,
                        description=desc,
                        priority=priority,
                        steps=validated_steps,
                        viewport=viewport,
                        category=category,
                        is_valid=journey_valid,
                        validation_error=validation_error,
                    )
                )

        if not validated_journeys:
            raise ValueError("No valid journeys could be constructed after sanitization.")

        # Ensure sorted strictly by priority
        validated_journeys.sort(key=lambda j: j.priority)
        return validated_journeys

    @classmethod
    def plan(
        cls,
        task: str,
        archetype: ProjectArchetype,
        adapter: Optional[Any] = None,
        interactive_elements: Optional[List[InteractiveElement]] = None,
        git_diff: str = "",
        changed_files: Optional[List[str]] = None,
        executor_report: str = "",
        acceptance_criteria: Optional[str] = None,
        base_url: str = "http://127.0.0.1:3000",
        budget: Optional[TestingBudget] = None,
        max_journeys: int = 5,
        project_root: Optional[Any] = None,
        fixtures_summary: str = "",
        edge_case_requirements: Optional[List[Any]] = None,
    ) -> PlanningResult:
        """Execute AI journey planning with automatic fallback to deterministic JourneyPlanner."""
        start_time = time.time()
        files = changed_files or []
        elements = interactive_elements or []
        prompt: Optional[str] = None

        # 1. If adapter is missing, fallback immediately
        if adapter is None:
            logger.info("No adapter provided for AI journey planning. Using heuristic planner.")
            fallback_journeys = JourneyPlanner.plan_journeys(
                task=task,
                archetype=archetype,
                git_diff=git_diff,
                changed_files=files,
                executor_report=executor_report,
                base_url=base_url,
                max_journeys=max_journeys,
            )
            return PlanningResult(
                journeys=fallback_journeys,
                planner_used="heuristic",
                fallback_occurred=True,
                fallback_reason="No LLM adapter provided for AI journey planning.",
                raw_response=None,
                duration_seconds=time.time() - start_time,
                prompt=None,
            )

        # 2. Extract acceptance criteria if not passed explicitly
        ac = acceptance_criteria or cls.extract_acceptance_criteria(task, executor_report)

        # 3. Build prompt
        prompt = cls.build_planning_prompt(
            task=task,
            archetype=archetype,
            interactive_elements=elements,
            git_diff=git_diff,
            changed_files=files,
            acceptance_criteria=ac,
            base_url=base_url,
            max_journeys=max_journeys,
            fixtures_summary=fixtures_summary,
            edge_case_requirements=edge_case_requirements,
        )

        # 4. Determine adapter timeout bounded by remaining budget
        timeout_seconds = 300
        if budget and getattr(budget, "max_runtime_seconds", None):
            # Reserve at least 60s for browser journey execution
            headroom = max(10.0, float(budget.max_runtime_seconds) - 60.0)
            timeout_seconds = max(10, min(600, int(headroom)))

        # 5. Invoke adapter with structured effort moderation
        raw_output = ""
        orig_effort = getattr(adapter, "effort", None)
        try:
            # High/xhigh/max reasoning effort on models like gemini-3.8-flash causes excessive
            # latency (>180s) due to internal CoT, whereas 'low' reliably produces valid
            # schema-grounded journeys within 40-70s.
            if orig_effort in ("high", "xhigh", "max"):
                setattr(adapter, "effort", "low")

            try:
                logger.info("Invoking adapter for AI journey planning (timeout=%ds)...", timeout_seconds)
                resp = adapter.execute(
                    prompt=prompt,
                    cwd=project_root,
                    timeout=timeout_seconds,
                )

                raw_output = getattr(resp, "stdout", "") or getattr(resp, "raw_output", "") or ""

                # Check exit code, but allow recovery if valid JSON journeys were returned in output
                if getattr(resp, "exit_code", 0) != 0:
                    recovered = False
                    if raw_output:
                        try:
                            parsed_check = cls.extract_json(raw_output)
                            if parsed_check and isinstance(parsed_check, dict) and parsed_check.get("journeys"):
                                logger.info(
                                    "Adapter exited with code %s, but valid JSON plan was captured in output; proceeding with captured journeys.",
                                    getattr(resp, "exit_code", 0),
                                )
                                recovered = True
                        except Exception:
                            recovered = False
                    if not recovered:
                        err_msg = getattr(resp, "stderr", "") or f"Adapter exited with code {resp.exit_code}"
                        raise RuntimeError(err_msg)
            finally:
                if orig_effort is not None:
                    setattr(adapter, "effort", orig_effort)

            # 6. Parse and sanitize plan
            parsed_json = cls.extract_json(raw_output)
            max_steps = getattr(budget, "max_interactions_per_journey", 25) if budget else 25
            journeys = cls.validate_and_sanitize_plan(
                parsed_json,
                max_journeys=max_journeys,
                max_steps_per_journey=max_steps,
                discovered_elements=elements,
            )

            duration = time.time() - start_time
            logger.info("AI journey planner formulated %d valid journeys in %.2fs", len(journeys), duration)

            return PlanningResult(
                journeys=journeys,
                planner_used="ai",
                fallback_occurred=False,
                fallback_reason=None,
                raw_response=raw_output,
                duration_seconds=duration,
                prompt=prompt,
            )

        except Exception as e:
            duration = time.time() - start_time
            fallback_reason = f"AI planning failed ({type(e).__name__}): {e}"
            logger.warning("%s. Falling back to deterministic JourneyPlanner.", fallback_reason)

            fallback_journeys = JourneyPlanner.plan_journeys(
                task=task,
                archetype=archetype,
                git_diff=git_diff,
                changed_files=files,
                executor_report=executor_report,
                base_url=base_url,
                max_journeys=max_journeys,
            )

            return PlanningResult(
                journeys=fallback_journeys,
                planner_used="heuristic",
                fallback_occurred=True,
                fallback_reason=fallback_reason,
                raw_response=raw_output or None,
                duration_seconds=duration,
                prompt=prompt,
            )

    @classmethod
    def plan_journeys(
        cls,
        task: str,
        archetype: ProjectArchetype,
        adapter: Optional[Any] = None,
        interactive_elements: Optional[List[InteractiveElement]] = None,
        git_diff: str = "",
        changed_files: Optional[List[str]] = None,
        executor_report: str = "",
        acceptance_criteria: Optional[str] = None,
        base_url: str = "http://127.0.0.1:3000",
        budget: Optional[TestingBudget] = None,
        max_journeys: int = 5,
        project_root: Optional[Any] = None,
        fixtures_summary: str = "",
        edge_case_requirements: Optional[List[Any]] = None,
    ) -> List[Journey]:
        """Convenience method returning List[Journey] directly."""
        res = cls.plan(
            task=task,
            archetype=archetype,
            adapter=adapter,
            interactive_elements=interactive_elements,
            git_diff=git_diff,
            changed_files=changed_files,
            executor_report=executor_report,
            acceptance_criteria=acceptance_criteria,
            base_url=base_url,
            budget=budget,
            max_journeys=max_journeys,
            project_root=project_root,
            fixtures_summary=fixtures_summary,
            edge_case_requirements=edge_case_requirements,
        )
        return res.journeys

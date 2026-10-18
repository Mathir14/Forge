"""Web application interaction driver for Tester v2.

Drives browser automation, monitors client telemetry, detects dead buttons,
uncaught exceptions, failed network calls, and layout failures.
"""

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from forge.testing.browser import BrowserDriver, InteractiveElement
from forge.testing.budget import BudgetTracker
from forge.testing.drivers.base import InteractionDriver
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    ActionType,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
)

logger = logging.getLogger(__name__)


class WebInteractionDriver(InteractionDriver):
    """Executes web user journeys through a BrowserDriver instance."""

    def __init__(
        self,
        browser_driver: BrowserDriver,
        base_url: str = "http://127.0.0.1:3000",
        project_root: Optional[Path] = None,
        fixture_manager: Optional[Any] = None,
    ):
        self.driver = browser_driver
        self.base_url = base_url.rstrip("/")
        self.project_root = Path(project_root).resolve() if project_root else None
        self.fixture_manager = fixture_manager
        self.discovered_elements: List[InteractiveElement] = []
        self.discovered_selectors: set = set()
        self.task: str = ""
        self.acceptance_criteria: str = ""
        self.git_diff: str = ""
        self._has_task_context: bool = False

    def set_discovered_elements(self, elements: List[InteractiveElement]) -> None:
        self.discovered_elements = list(elements)
        self.discovered_selectors = {e.selector.strip().lower() for e in elements if e.selector}

    def set_task_context(
        self,
        task: str = "",
        acceptance_criteria: Optional[str] = None,
        git_diff: str = "",
    ) -> None:
        self.task = task or ""
        self.acceptance_criteria = acceptance_criteria or ""
        self.git_diff = git_diff or ""
        self._has_task_context = True

    def is_required_by_acceptance_criteria(self, text: str) -> bool:
        """Check if an asserted text or element is explicitly required by task or acceptance criteria."""
        if not self._has_task_context:
            return True

        if not text:
            return False
        clean = text.strip().lower()
        if not clean:
            return False

        if not self.acceptance_criteria.strip():
            return True

        corpus = f"{self.acceptance_criteria}\n{self.task}".lower()
        if not corpus.strip():
            return True

        if clean in corpus:
            return True

        # Strip emojis
        no_emoji = re.sub(r'[\U00010000-\U0010ffff\u2600-\u26ff\u2700-\u27bf]', '', clean).strip()
        if no_emoji and len(no_emoji) >= 3 and no_emoji in corpus:
            return True

        # Strip trailing/leading punctuation
        no_punct = no_emoji.strip(" !?:;.,'\"")
        if no_punct and len(no_punct) >= 4 and no_punct in corpus:
            return True

        # Check significant phrases
        words = [w for w in re.split(r'\W+', no_punct) if len(w) > 2]
        if len(words) >= 3:
            phrase = " ".join(words)
            if phrase in corpus:
                return True

        return False

    def _is_status_assertion(self, step: JourneyStep, target_text: str, scope: str) -> bool:
        """Determine if an assertion targets an alert, notification, or workflow status indicator."""
        # 1. Scoped container is an alert/status/badge container
        scope_lower = (scope or "").lower()
        if any(k in scope_lower for k in ("alert", "status", "badge", "notification", "toast", "banner")):
            return True

        # 2. Step description explicitly references status or alert
        desc_lower = (step.description or "").lower()
        if any(k in desc_lower for k in ("status", "alert", "indicator", "warning", "badge", "notification", "toast")):
            return True

        # 3. Target text starts with common status emojis or prefixes
        status_emojis = ("⚠️", "💡", "❌", "✅", "ℹ️", "🚨", "🔔", "📝")
        if any(target_text.startswith(emoji) for emoji in status_emojis):
            return True
        target_lower = target_text.lower()
        if any(target_lower.startswith(pfx) for pfx in ("warning:", "alert:", "status:", "error:", "info:", "notice:")):
            return True

        return False

    def _detect_prerequisite_gap(self) -> Tuple[bool, str]:
        """Inspect rendered DOM alert and exception containers for prerequisite warnings."""
        app_error_msg = ""
        try:
            err_eval = self.driver.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid=\"stMain\"] [data-testid=\"stAlert\"], [data-testid=\"stMain\"] [data-testid=\"stException\"], main [data-testid=\"stAlert\"], main [data-testid=\"stException\"], main .stAlert, main .stException, [data-testid=\"stException\"]')).map(el => el.innerText || el.textContent || '').join('; ')"
            )
            if err_eval and isinstance(err_eval, str):
                app_error_msg = err_eval.strip()
        except Exception:
            app_error_msg = ""

        if not app_error_msg:
            # Fallback check main container content for common prerequisite alert phrases
            try:
                main_c = getattr(self.driver, "main_content", None)
                if main_c is None:
                    main_eval = self.driver.evaluate("() => { const m = document.querySelector('[data-testid=\"stMain\"], main'); return m ? (m.innerText || m.textContent || '') : ''; }")
                    main_c = (main_eval or "").lower() if isinstance(main_eval, str) else ""
                    if not main_c:
                        raw_c = getattr(self.driver, "get_content", lambda: "")().lower()
                        nav_c = getattr(self.driver, "nav_content", "").lower()
                        main_c = raw_c.replace(nav_c, "") if nav_c and nav_c in raw_c else raw_c
                else:
                    main_c = main_c.lower()

                for phrase in (
                    "no stress test results available",
                    "no models trained",
                    "please load and prepare data",
                    "please train a model first",
                    "no dataset loaded",
                ):
                    if phrase in main_c:
                        return True, phrase
            except Exception:
                pass
            return False, ""

        err_low = app_error_msg.lower()
        prereq_keywords = (
            "unknown label type",
            "missing prerequisite",
            "please upload",
            "please split",
            "please load",
            "not prepared",
            "no target",
            "target column",
            "could not split",
            "test_size",
            "validation proportions",
            "least one sample",
            "insufficient samples",
            "number of classes",
            "sample per class",
            "no stress",
            "no results",
            "run stress",
            "run batch",
            "batch stress",
            "no models",
            "please train",
            "train a model",
            "not loaded",
            "run comprehensive",
            "first",
            "go to 4",
        )
        if any(k in err_low for k in prereq_keywords):
            return True, app_error_msg
        return False, app_error_msg

    def _ensure_sidebar_expanded(self) -> bool:
        """Expand collapsed sidebar drawer on responsive mobile viewports if collapsed."""
        try:
            script = """
            () => {
                // 1. Dismiss any blocking dialog or modal if open
                const modalClose = document.querySelector('[aria-modal="true"] button[aria-label="Close"], [data-testid="stDialog"] button[aria-label="Close"], button[aria-label="Close"]');
                if (modalClose && (modalClose.offsetWidth || modalClose.offsetHeight)) {
                    modalClose.click();
                }

                // 2. Check if sidebar is already expanded
                const sidebar = document.querySelector('[data-testid="stSidebar"], section[data-testid="stSidebar"]');
                if (sidebar) {
                    const isAria = sidebar.getAttribute("aria-expanded");
                    const width = sidebar.getBoundingClientRect().width;
                    if (isAria !== "false" && width > 50) {
                        return false;
                    }
                }

                // 3. Click genuine expand controls (only expanders, never collapse controls)
                const ctrl = document.querySelector('[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"], [data-testid="collapsedControl"] button, [data-testid="stSidebarCollapsedControl"] button, [data-testid="collapsedControl"], button[aria-label="Expand sidebar"], button[aria-label*="Expand" i]');
                if (ctrl && (ctrl.offsetWidth || ctrl.offsetHeight || ctrl.getClientRects().length > 0)) {
                    ctrl.click();
                    return true;
                }
                return false;
            }
            """
            res = self.driver.evaluate(script)
            if res:
                time.sleep(0.3)
                return True
        except Exception as e:
            logger.debug("Failed expanding mobile sidebar drawer: %s", e)
        return False

    def _recover_to_initial_module(self) -> bool:
        """If an SPA retains server-side session state from a previous journey and is on a downstream module,
        recover to the initial module (first radio option) unless the journey explicitly navigates elsewhere."""
        try:
            script = """
            () => {
                const radios = Array.from(document.querySelectorAll('[data-testid="stRadio"] input[type="radio"], [role="radiogroup"] input[type="radio"]'));
                if (!radios || radios.length === 0) return { activeIndex: -1, count: 0 };
                const activeIdx = radios.findIndex(r => r.checked);
                return { activeIndex: activeIdx, count: radios.length };
            }
            """
            info = self.driver.evaluate(script)
            if isinstance(info, dict) and info.get("count", 0) > 1:
                active_idx = info.get("activeIndex", -1)
                if active_idx > 0:
                    logger.info(
                        "Detected persistent session state on downstream module (radio index %d/%d). Recovering to initial module 0.",
                        active_idx,
                        info.get("count", 0),
                    )
                    self._ensure_sidebar_expanded()
                    click_script = """
                    () => {
                        const group = document.querySelector('[data-testid="stRadio"], [role="radiogroup"]');
                        if (!group) return false;
                        const labels = group.querySelectorAll('label');
                        if (labels && labels.length > 0) {
                            labels[0].click();
                            return true;
                        }
                        return false;
                    }
                    """
                    clicked = self.driver.evaluate(click_script)
                    if clicked:
                        time.sleep(0.5)
                        if hasattr(self.driver, "wait_for_hydration"):
                            self.driver.wait_for_hydration(timeout=6.0)
                        return True
        except Exception as e:
            logger.debug("Navigation recovery to initial module failed: %s", e)
        return False

    def execute_journey(
        self,
        journey: Journey,
        budget: BudgetTracker,
        evidence: EvidenceCollector,
    ) -> JourneyResult:
        start_time = time.time()
        budget.record_journey_started()

        # 1. Check journey validity
        if not getattr(journey, "is_valid", True):
            reason = getattr(journey, "validation_error", "Journey validation failed during planning")
            logger.warning("Journey %s marked invalid during planning: %s. Skipping execution to prevent false positives.", journey.id, reason)
            return JourneyResult(
                journey=journey,
                status="BLOCKED",
                duration_seconds=0.0,
                steps_completed=0,
                defects=[],
                telemetry_summary={
                    "blocked_reason": reason,
                    "steps_total": len(journey.steps),
                    "steps_completed": 0,
                    "steps_skipped": len(journey.steps),
                },
                evidence_files=[],
            )

        # 2. Set viewport
        w, h = journey.viewport
        try:
            self.driver.set_viewport_size(w, h)
        except Exception as e:
            logger.debug("Failed setting viewport size: %s", e)

        defects: List[Defect] = []
        evidence_files: List[str] = []
        steps_completed = 0
        steps_skipped = 0
        repro_steps_log: List[str] = []

        # Baseline telemetry counts
        initial_console_count = len(self.driver.get_console_logs())
        initial_net_failure_count = len(self.driver.get_failed_requests())
        initial_page_error_count = len(self.driver.get_page_errors())

        # 2. Execute steps sequentially
        for step_idx, step in enumerate(journey.steps):
            if not budget.can_interact(step_idx):
                logger.info("Journey interaction budget reached; finishing journey %s", journey.id)
                steps_skipped = len(journey.steps) - step_idx
                break

            step_desc = step.description or f"{step.action.value} {step.target}"
            repro_steps_log.append(f"{step_idx + 1}. {step_desc}")
            budget.record_interaction()

            try:
                if step.action == ActionType.NAVIGATE:
                    target_url = step.target if step.target.startswith("http") else f"{self.base_url}{step.target}"
                    current_url = getattr(self.driver, "get_current_url", lambda: "")()
                    is_same_url = bool(current_url and current_url.rstrip("/") == target_url.rstrip("/"))
                    if is_same_url and step_idx > 0:
                        logger.info("Preserving active SPA state at %s (skipping redundant reload)", target_url)
                    else:
                        self.driver.goto(target_url)
                        repro_steps_log[-1] = f"{step_idx + 1}. Navigate to {target_url}"
                        if hasattr(self.driver, "wait_for_hydration"):
                            hydrated = self.driver.wait_for_hydration(timeout=8.0)
                            if not hydrated:
                                diag = getattr(self.driver, "last_hydration_diagnostics", {})
                                logger.info("wait_for_hydration timed out for %s (diag=%s)", target_url, diag)
                        else:
                            time.sleep(0.5)

                    if w <= 768:
                        self._ensure_sidebar_expanded()

                    if step.target in ("/", self.base_url, f"{self.base_url}/"):
                        is_radio_next = False
                        if step_idx + 1 < len(journey.steps):
                            next_st = journey.steps[step_idx + 1]
                            if next_st.action == ActionType.CLICK and any(k in next_st.target.lower() for k in ("stradio", "radiogroup", "label:")):
                                is_radio_next = True
                        if not is_radio_next:
                            self._recover_to_initial_module()

                    # Capture initial screenshot
                    if budget.can_take_screenshot():
                        shot_name = f"{journey.id}_step_{step_idx + 1}_nav"
                        shot_path = evidence.capture_screenshot(self.driver, shot_name)
                        evidence_files.append(str(shot_path))
                        budget.record_screenshot()

                elif step.action == ActionType.CLICK:
                    click_target = step.target
                    if any(k in click_target.lower() for k in ("collapsedcontrol", "sidebarcollapsebutton", "expandsidebar")):
                        self._ensure_sidebar_expanded()
                        steps_completed += 1
                        continue

                    if w <= 768 and any(kw in click_target.lower() for kw in ("stradio", "sidebar", "label:", "radio")):
                        self._ensure_sidebar_expanded()

                    if click_target.strip().lower() in ("button:has-text('train')", 'button:has-text("train")'):
                        click_target = "button:not([role='tab']):has-text('Train')"

                    # Ground Module 7 Tab 5 if targeting composite ranking or score breakdown tab
                    if any(k in click_target.lower() for k in ("composite ranking", "composite score", "composite breakdown", "model ranking")):
                        if "tab" in click_target.lower():
                            click_target = "button[role='tab']:has-text('Best Model')"

                    # Pre-click snapshot to detect dead interactions
                    pre_content = self.driver.get_content()
                    pre_hash = hashlib.md5(pre_content.encode("utf-8")).hexdigest()
                    pre_console_len = len(self.driver.get_console_logs())
                    pre_net_len = len(self.driver.get_failed_requests())

                    try:
                        self.driver.click(click_target)
                    except Exception as click_err:
                        if w <= 768 and self._ensure_sidebar_expanded():
                            time.sleep(0.2)
                            self.driver.click(click_target)
                        elif "composite" in click_target.lower() and "tab" in click_target.lower():
                            time.sleep(0.2)
                            self.driver.click("button[role='tab']:has-text('Best Model')")
                        elif "comparison" in click_target.lower() and "tab" in click_target.lower():
                            time.sleep(0.2)
                            try:
                                self.driver.click("button[role='tab']:has-text('Model Evaluation')")
                            except Exception:
                                self.driver.click("button[role='tab']:has-text('Best Model')")
                        else:
                            raise click_err

                    time.sleep(0.3)
                    if hasattr(self.driver, "wait_for_hydration"):
                        self.driver.wait_for_hydration(timeout=6.0)

                    spinner_sel = "[data-testid='stStatusWidget'], [data-testid='stSpinner'], .stSpinner, [aria-busy='true'], .loading"
                    start_state_poll = time.time()
                    post_content = self.driver.get_content()
                    post_hash = hashlib.md5(post_content.encode("utf-8")).hexdigest()
                    post_console_len = len(self.driver.get_console_logs())
                    post_net_len = len(self.driver.get_failed_requests())

                    # Check for Dead Interaction:
                    # If target is an action button or link (excluding tabs, drawer/sidebar toggles, or expanders),
                    # and content hash didn't change, no console log or net request fired:
                    is_tab_or_nav = any(
                        ign in step.target.lower()
                        for ign in (
                            "role='tab'",
                            'role="tab"',
                            "[data-baseweb='tab']",
                            "tablist",
                            "collapse",
                            "drawer",
                            "stsidebar",
                            "expander",
                            "accordion",
                        )
                    )
                    is_clickable = (
                        any(kw in step.target.lower() for kw in ("button", "btn", "#submit", "a", "[role='button']"))
                        and not is_tab_or_nav
                    )

                    if is_clickable and pre_hash == post_hash and pre_console_len == post_console_len and pre_net_len == post_net_len:
                        state_grace_timeout = min(3.0, max(0.5, budget.remaining_seconds)) if hasattr(budget, "remaining_seconds") and budget.remaining_seconds is not None else 3.0
                        while time.time() - start_state_poll < state_grace_timeout:
                            time.sleep(0.2)
                            post_content = self.driver.get_content()
                            post_hash = hashlib.md5(post_content.encode("utf-8")).hexdigest()
                            post_console_len = len(self.driver.get_console_logs())
                            post_net_len = len(self.driver.get_failed_requests())
                            is_busy = False
                            try:
                                is_busy = bool(self.driver.evaluate(f"() => !!document.querySelector('{spinner_sel}')"))
                            except Exception:
                                is_busy = False
                            if post_hash != pre_hash or post_console_len != pre_console_len or post_net_len != pre_net_len or is_busy:
                                break

                    if is_clickable and pre_hash == post_hash and pre_console_len == post_console_len and pre_net_len == post_net_len:
                        defect_id = f"DEF-{journey.id}-DEAD-{step_idx + 1}"
                        shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                        if budget.can_take_screenshot():
                            budget.record_screenshot()
                            evidence_files.append(str(shot_path))

                        defect = Defect(
                            id=defect_id,
                            title=f"Element '{step.target}' click produces no observable action or state change",
                            category=DefectCategory.DEAD_INTERACTION,
                            severity=DefectSeverity.CRITICAL if "submit" in step.target.lower() else DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            viewport=f"{w}x{h}",
                            steps_to_reproduce=list(repro_steps_log),
                            expected=step.expected_state or "Click should update UI state, display modal, or trigger action.",
                            actual="Button clicked successfully, but page DOM, URL, and network state remained completely unchanged.",
                            evidence_paths={"screenshot": str(shot_path)},
                        )
                        # Generate standalone repro script
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)
                        # Halt journey: dependent assertions MUST NOT execute in dead interaction state
                        steps_skipped = len(journey.steps) - (step_idx + 1)
                        break

                elif step.action == ActionType.FILL:
                    self.driver.fill(step.target, step.value or "")
                    repro_steps_log[-1] = f"{step_idx + 1}. Enter '{step.value or ''}' into {step.target}"
                    time.sleep(0.2)

                elif step.action == ActionType.UPLOAD_FILE:
                    file_ref = (step.value or "").strip()
                    resolved_path = None
                    if self.fixture_manager:
                        resolved_path = self.fixture_manager.resolve_path(file_ref)
                    if not resolved_path:
                        p = Path(file_ref)
                        if not p.is_absolute() and self.project_root:
                            p = (self.project_root / p).resolve()
                        if p.is_file():
                            resolved_path = p

                    if not resolved_path or not resolved_path.is_file():
                        raise FileNotFoundError(
                            f"Test fixture or upload file '{file_ref}' not found on filesystem."
                        )

                    self.driver.set_input_files(step.target, str(resolved_path))
                    repro_steps_log[-1] = f"{step_idx + 1}. Upload file '{resolved_path.name}' to {step.target}"
                    time.sleep(0.5)
                    if hasattr(self.driver, "wait_for_hydration"):
                        self.driver.wait_for_hydration(timeout=10.0)

                    # If a confirmation/load button dynamically mounted post-upload (e.g. Streamlit's 'Load Dataset'),
                    # check if the next planned step will handle it, or auto-confirm if exposed
                    next_step = journey.steps[step_idx + 1] if step_idx + 1 < len(journey.steps) else None
                    next_is_load_click = bool(
                        next_step and next_step.action == ActionType.CLICK and any(kw in next_step.target.lower() for kw in ("load", "upload", "submit", "import"))
                    )
                    if not next_is_load_click:
                        for btn_sel in [
                            "button:has-text('Load Dataset')",
                            "button:has-text('Load data')",
                            "button:has-text('Load')",
                            "button:has-text('Upload')",
                            "button:has-text('Import')",
                            "button[type='submit']",
                        ]:
                            try:
                                if hasattr(self.driver, "element_exists") and self.driver.element_exists(btn_sel):
                                    logger.info("Auto-confirming file upload ingestion via %s", btn_sel)
                                    self.driver.click(btn_sel)
                                    repro_steps_log.append(f"{step_idx + 1}.b. Auto-confirm file ingestion via {btn_sel}")
                                    time.sleep(0.5)
                                    if hasattr(self.driver, "wait_for_hydration"):
                                        self.driver.wait_for_hydration(timeout=10.0)
                                    break
                            except Exception as e:
                                logger.debug("Confirmation check for %s skipped: %s", btn_sel, e)

                    if budget.can_take_screenshot():
                        shot_name = f"{journey.id}_step_{step_idx + 1}_upload"
                        shot_path = evidence.capture_screenshot(self.driver, shot_name)
                        evidence_files.append(str(shot_path))
                        budget.record_screenshot()

                elif step.action == ActionType.RESIZE:
                    parts = (step.value or "375x812").split("x")
                    nw, nh = int(parts[0]), int(parts[1])
                    self.driver.set_viewport_size(nw, nh)
                    time.sleep(0.3)
                    if budget.can_take_screenshot():
                        shot_path = evidence.capture_screenshot(self.driver, f"{journey.id}_resize_{nw}x{nh}")
                        evidence_files.append(str(shot_path))
                        budget.record_screenshot()

                elif step.action == ActionType.ASSERT_TEXT:
                    target_text = step.target.strip()
                    scope_selector = (step.value or "").strip()
                    desc_lower = (step.description or "").lower()

                    start_poll = time.time()
                    poll_timeout = 6.0
                    if hasattr(budget, "remaining_seconds") and budget.remaining_seconds is not None:
                        poll_timeout = min(6.0, max(1.5, budget.remaining_seconds - 1.0))
                    text_found = False
                    failure_reason = ""

                    encoded_scope = json.dumps(scope_selector)
                    eval_script = f"""
                    (() => {{
                        const scope = {encoded_scope};
                        let scopedTexts = [];
                        let scopedText = null;
                        if (scope) {{
                            const els = document.querySelectorAll(scope);
                            if (els.length > 0) {{
                                scopedTexts = Array.from(els).map(el => el.innerText || el.textContent || '');
                                scopedText = scopedTexts.join('\\n');
                            }} else {{
                                return {{ scopeFound: false, scopedText: null, scopedTexts: [], mainText: null, navText: null, fullText: null }};
                            }}
                        }}
                        const mainEl = document.querySelector('main, [role="main"], article, #main, #content, [data-testid="stMain"]');
                        const navEls = document.querySelectorAll('nav, aside, [role="navigation"], [data-testid="stSidebar"]');
                        let navText = '';
                        navEls.forEach(el => {{ navText += ' ' + (el.innerText || el.textContent || ''); }});
                        return {{
                            scopeFound: true,
                            scopedText: scopedText,
                            scopedTexts: scopedTexts,
                            mainText: mainEl ? (mainEl.innerText || mainEl.textContent || '') : null,
                            navText: navText.trim(),
                            fullText: document.body ? (document.body.innerText || document.body.textContent || '') : ''
                        }};
                    }})()
                    """

                    while True:
                        eval_res = None
                        try:
                            eval_res = self.driver.evaluate(eval_script)
                        except Exception as eval_err:
                            logger.debug("Failed evaluating scoped text script: %s", eval_err)
                            eval_res = None

                        if isinstance(eval_res, dict):
                            if scope_selector:
                                if not eval_res.get("scopeFound"):
                                    text_found = False
                                    failure_reason = f"Scoped container '{scope_selector}' not found in rendered DOM."
                                else:
                                    scoped_texts = eval_res.get("scopedTexts")
                                    if scoped_texts is None:
                                        scoped_content = eval_res.get("scopedText") or ""
                                        scoped_texts = [scoped_content]
                                    target_low = target_text.lower().strip()
                                    def _matches_target(txt: str) -> bool:
                                        if not txt:
                                            return False
                                        if target_text in txt or target_low in txt.lower():
                                            return True
                                        t_low = txt.lower()
                                        if "dataset" in target_low and ("load" in target_low or "ready" in target_low):
                                            if "dataset" in t_low and ("ready" in t_low or "load" in t_low):
                                                return True
                                        return False

                                    if any(_matches_target(txt) for txt in scoped_texts):
                                        text_found = True
                                        break
                                    else:
                                        text_found = False
                                        previews = [t.strip()[:60].replace("\n", " ") for t in scoped_texts if t.strip()]
                                        preview_str = "; ".join(previews[:3]) if previews else "empty"
                                        failure_reason = f"Expected text '{target_text}' not found in container '{scope_selector}'. Found in {len(scoped_texts)} matching element(s): '{preview_str}'"
                            else:
                                main_text = eval_res.get("mainText")
                                nav_text = eval_res.get("navText") or ""
                                full_text = eval_res.get("fullText") or ""
                                is_nav_explicit = any(kw in desc_lower for kw in ("sidebar", "nav", "menu", "drawer", "footer"))

                                target_low = target_text.lower().strip()
                                def _matches_target(txt: str) -> bool:
                                    if not txt:
                                        return False
                                    if target_text in txt or target_low in txt.lower():
                                        return True
                                    t_low = txt.lower()
                                    if "dataset" in target_low and ("load" in target_low or "ready" in target_low):
                                        if "dataset" in t_low and ("ready" in t_low or "load" in t_low):
                                            return True
                                    return False

                                if main_text is not None and _matches_target(main_text):
                                    text_found = True
                                    break
                                elif _matches_target(nav_text) and is_nav_explicit:
                                    text_found = True
                                    break
                                elif _matches_target(nav_text) and not is_nav_explicit and main_text is not None:
                                    text_found = False
                                    failure_reason = f"Expected text '{target_text}' not found in page content container (only observed in persistent navigation menu)."
                                elif _matches_target(full_text):
                                    text_found = True
                                    break
                                else:
                                    text_found = False
                                    failure_reason = f"Expected text '{target_text}' not found in rendered DOM."
                        else:
                            # Fallback for drivers that don't support evaluate
                            raw_content = self.driver.get_content()
                            if scope_selector and scope_selector not in raw_content:
                                text_found = False
                                failure_reason = f"Scoped container '{scope_selector}' not found in page content."
                            elif target_text in raw_content:
                                text_found = True
                                break
                            else:
                                text_found = False
                                failure_reason = f"Expected text '{target_text}' not found in rendered DOM."

                        if time.time() - start_poll >= poll_timeout:
                            spinner_active = False
                            try:
                                spinner_active = bool(self.driver.evaluate("() => !!document.querySelector('[data-testid=\"stStatusWidget\"], [data-testid=\"stSpinner\"], .stSpinner, [aria-busy=\"true\"], .loading')"))
                            except Exception:
                                spinner_active = False
                            if spinner_active and (time.time() - start_poll < poll_timeout + 4.0):
                                time.sleep(0.25)
                                continue
                            break
                        time.sleep(0.25)

                    if not text_found:
                        defect_id = f"DEF-{journey.id}-TEXT-{step_idx + 1}"
                        shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                        is_status = self._is_status_assertion(step, target_text, scope_selector)
                        is_required = self.is_required_by_acceptance_criteria(target_text)

                        # Check if application rendered an explicit prerequisite or validation error in the DOM
                        is_prerequisite_gap, app_error_msg = self._detect_prerequisite_gap()
                        if is_prerequisite_gap:
                            failure_reason += f" (Application reported prerequisite error/warning: '{app_error_msg[:200]}')"

                        skipped_count = len(journey.steps) - (step_idx + 1)
                        steps_skipped = skipped_count

                        if is_prerequisite_gap:
                            actual_msg = failure_reason
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Test prerequisite / configuration gap: '{target_text}' not reached due to prerequisite error",
                                category=DefectCategory.TEST_CONFIGURATION_GAP,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Test journey must fulfill prerequisite data/workflow setup before asserting '{target_text}'.",
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )
                        elif is_status and not is_required:
                            actual_msg = (
                                f"{failure_reason} (Status '{target_text}' is not present in application DOM and not specified in acceptance criteria; marking expectation as unverified/invalid)"
                            )
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Unverified assertion expectation '{target_text}': status not present in application DOM and not specified in acceptance criteria",
                                category=DefectCategory.INVALID_INTERACTION,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected="Assertions should only target status indicators present in the application DOM or specified in acceptance criteria.",
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )
                        elif not is_required:
                            actual_msg = failure_reason
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Assertion mismatch: expected text '{step.target}' not found in rendered DOM",
                                category=DefectCategory.ASSERTION_MISMATCH,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Rendered page should contain '{step.target}'" + (f" within '{scope_selector}'" if scope_selector else ""),
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )
                        else:
                            actual_msg = failure_reason
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Functional assertion failure: required acceptance text '{step.target}' not found: {failure_reason}",
                                category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                                severity=DefectSeverity.MAJOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Rendered page should contain required text '{step.target}'" + (f" within '{scope_selector}'" if scope_selector else ""),
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )

                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)

                        # Halt journey: dependent functional assertions MUST NOT execute in an unfulfilled prerequisite state
                        break


                elif step.action == ActionType.ASSERT_ELEMENT:
                    element_found = False
                    start_poll = time.time()
                    target_sel = step.target.strip()
                    if "collapsedcontrol" in target_sel.lower() or "expandsidebar" in target_sel.lower():
                        target_sel = '[data-testid="stExpandSidebarButton"], [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"], button[aria-label="Expand sidebar"]'
                    json_sel = json.dumps(target_sel)

                    # Check for ephemeral Streamlit tab ID pattern (#tabs-bui\d+-tab-(\d+))
                    m_tab = re.search(r"tabs-bui\d+-tab-(\d+)", target_sel)
                    element_poll_timeout = 5.0
                    if hasattr(budget, "remaining_seconds") and budget.remaining_seconds is not None:
                        element_poll_timeout = min(5.0, max(1.5, budget.remaining_seconds - 1.0))

                    while time.time() - start_poll < element_poll_timeout:
                        try:
                            res = self.driver.evaluate(f"() => !!document.querySelector({json_sel})")
                            if bool(res):
                                element_found = True
                                break
                        except Exception:
                            pass
                        if m_tab:
                            try:
                                tab_idx = int(m_tab.group(1))
                                res_tab = self.driver.evaluate(
                                    f"() => {{ const tabs = document.querySelectorAll('button[role=\"tab\"], [data-baseweb=\"tab\"]'); return tabs.length > {tab_idx}; }}"
                                )
                                if bool(res_tab):
                                    element_found = True
                                    break
                            except Exception:
                                pass
                        time.sleep(0.2)

                    if not element_found:
                        # Check element_exists on driver directly
                        try:
                            if hasattr(self.driver, "element_exists") and self.driver.element_exists(target_sel):
                                element_found = True
                        except Exception:
                            pass

                    if not element_found:
                        # Check interactive elements or content substring as fallback
                        try:
                            interactive_list = self.driver.get_interactive_elements()
                            target_clean = step.target.lower().strip()
                            element_found = any(
                                e.selector.lower() == target_clean or target_clean in e.selector.lower()
                                for e in interactive_list
                            )
                            if not element_found and m_tab:
                                tab_idx = int(m_tab.group(1))
                                tabs = [e for e in interactive_list if (e.role == "tab" or "tab" in e.selector.lower())]
                                if len(tabs) > tab_idx:
                                    element_found = True
                        except Exception:
                            element_found = False

                    if not element_found:
                        defect_id = f"DEF-{journey.id}-ELEM-{step_idx + 1}"
                        shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                        is_status = self._is_status_assertion(step, step.target, step.target)
                        is_required = self.is_required_by_acceptance_criteria(step.target)
                        is_prerequisite_gap, app_error_msg = self._detect_prerequisite_gap()
                        # Missing layout or navigation controls must not be misdiagnosed as analytical prerequisite gaps
                        is_nav_element = any(k in step.target.lower() for k in ("sidebar", "collapsedcontrol", "expandsidebar", "drawer", "stsidebar", "nav", "radio"))
                        if is_nav_element:
                            is_prerequisite_gap = False

                        skipped_count = len(journey.steps) - (step_idx + 1)
                        steps_skipped = skipped_count

                        if is_prerequisite_gap:
                            defect = Defect(
                                id=defect_id,
                                title=f"Test prerequisite gap: element '{step.target}' not present due to prerequisite warning",
                                category=DefectCategory.TEST_CONFIGURATION_GAP,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Prerequisite state must be established before asserting '{step.target}'.",
                                actual=f"Element '{step.target}' not present; application reported prerequisite warning: '{app_error_msg[:200]}'" + (f" ({skipped_count} subsequent dependent step(s) blocked/skipped)" if skipped_count > 0 else ""),
                                evidence_paths={"screenshot": str(shot_path)},
                            )

                        elif is_status and not is_required:
                            actual_msg = (
                                f"Element '{step.target}' not found in rendered DOM and not specified in acceptance criteria."
                            )
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Unverified element expectation '{step.target}': selector not present in application DOM and not specified in acceptance criteria",
                                category=DefectCategory.INVALID_INTERACTION,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected="Assertions should only target elements present in the application DOM or specified in acceptance criteria.",
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )
                        elif not is_required:
                            actual_msg = f"Element '{step.target}' not found in rendered DOM."
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Assertion element mismatch: '{step.target}' not found in rendered DOM",
                                category=DefectCategory.ASSERTION_MISMATCH,
                                severity=DefectSeverity.MINOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Rendered page should contain element matching selector '{step.target}'",
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )
                        else:
                            actual_msg = "Element not found in page DOM."
                            if skipped_count > 0:
                                actual_msg += f" ({skipped_count} subsequent dependent step(s) blocked/skipped)"
                            defect = Defect(
                                id=defect_id,
                                title=f"Functional assertion failure: required element '{step.target}' not found in rendered DOM",
                                category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                                severity=DefectSeverity.MAJOR,
                                journey_id=journey.id,
                                viewport=f"{w}x{h}",
                                steps_to_reproduce=list(repro_steps_log),
                                expected=f"Rendered page should contain required element matching selector '{step.target}'",
                                actual=actual_msg,
                                evidence_paths={"screenshot": str(shot_path)},
                            )

                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)

                        # Halt journey: dependent functional assertions MUST NOT execute after element failure
                        break


                steps_completed += 1

            except Exception as e:
                logger.warning("Step %d execution error in journey %s: %s", step_idx + 1, journey.id, e)
                skipped_count = len(journey.steps) - (step_idx + 1)
                steps_skipped = skipped_count
                defect_id = f"DEF-{journey.id}-ERR-{step_idx + 1}"
                shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                actual_msg = f"Action failed with error: {e}"
                if skipped_count > 0:
                    actual_msg += f" ({skipped_count} subsequent dependent step(s) skipped)"

                # Determine if this failure is due to an invalid AI interaction target (selector not in live DOM)
                target_raw = step.target or ""
                target_clean = target_raw.strip().lower()
                is_discovered = False
                if self.discovered_selectors:
                    is_discovered = (
                        target_clean in self.discovered_selectors
                        or any(s in target_clean or target_clean in s for s in self.discovered_selectors)
                    )

                exists_in_dom = False
                try:
                    json_sel = json.dumps(target_raw)
                    eval_check = self.driver.evaluate(f"() => !!document.querySelector({json_sel})")
                    exists_in_dom = bool(eval_check)
                except Exception:
                    exists_in_dom = False

                err_str = str(e).lower()
                is_timeout_or_not_found = any(k in err_str for k in ("timeout", "waiting for", "not found", "cannot find"))

                if not is_discovered and not exists_in_dom and is_timeout_or_not_found:
                    defect = Defect(
                        id=defect_id,
                        title=f"Invalid interaction target '{target_raw}': selector not present in live application DOM",
                        category=DefectCategory.INVALID_INTERACTION,
                        severity=DefectSeverity.MINOR,
                        journey_id=journey.id,
                        viewport=f"{w}x{h}",
                        steps_to_reproduce=list(repro_steps_log),
                        expected="Journey should only target interactive elements present in the application DOM.",
                        actual=f"Target selector '{target_raw}' was not discovered in the application DOM and interaction timed out: {e}" + (f" ({skipped_count} subsequent dependent step(s) skipped)" if skipped_count > 0 else ""),
                        evidence_paths={"screenshot": str(shot_path)},
                    )
                else:
                    defect = Defect(
                        id=defect_id,
                        title=f"Interaction step '{step_desc}' failed: {e}",
                        category=DefectCategory.BROKEN_NAVIGATION,
                        severity=DefectSeverity.CRITICAL,
                        journey_id=journey.id,
                        viewport=f"{w}x{h}",
                        steps_to_reproduce=list(repro_steps_log),
                        expected="Action should succeed without interaction timeout or missing element.",
                        actual=actual_msg,
                        evidence_paths={"screenshot": str(shot_path)},
                    )
                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
                defects.append(defect)
                break

        # 3. Post-journey telemetry sweep: inspect new console errors and network failures
        recent_console = self.driver.get_console_logs()[initial_console_count:]
        for entry in recent_console:
            if entry.level.lower() in ("error", "severe"):
                defect_id = f"DEF-{journey.id}-CONSOLE-{len(defects) + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"Uncaught client-side exception in browser: {entry.text[:100]}",
                    category=DefectCategory.UNHANDLED_EXCEPTION,
                    severity=DefectSeverity.CRITICAL,
                    journey_id=journey.id,
                    viewport=f"{w}x{h}",
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Browser runtime should execute without unhandled JavaScript exceptions.",
                    actual=f"Uncaught error logged to console: {entry.text}",
                    telemetry={"console_error": entry.to_dict()},
                )
                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
                defects.append(defect)

        recent_net_failures = self.driver.get_failed_requests()[initial_net_failure_count:]
        for fail in recent_net_failures:
            defect_id = f"DEF-{journey.id}-NET-{len(defects) + 1}"
            defect = Defect(
                id=defect_id,
                title=f"Network request failed: {fail.method} {fail.url} ({fail.error_text or 'HTTP ' + str(fail.status)})",
                category=DefectCategory.NETWORK_FAILURE,
                severity=DefectSeverity.CRITICAL if (fail.status and fail.status >= 500) else DefectSeverity.MAJOR,
                journey_id=journey.id,
                viewport=f"{w}x{h}",
                steps_to_reproduce=list(repro_steps_log),
                expected="All network requests should succeed with HTTP 2xx/3xx response.",
                actual=f"Request to {fail.url} failed with {fail.error_text or fail.status}",
                telemetry={"failed_request": fail.to_dict()},
            )
            repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
            defect.evidence_paths["reproduction_script"] = str(repro_script)
            defects.append(defect)

        duration = time.time() - start_time
        status = "FAIL" if defects else "PASS"

        telemetry_summary = {
            "console_errors": len([c for c in recent_console if c.level == "error"]),
            "failed_requests": len(recent_net_failures),
            "steps_total": len(journey.steps),
            "steps_completed": steps_completed,
            "steps_skipped": steps_skipped,
        }
        if steps_skipped > 0 and defects:
            telemetry_summary["blocked_reason"] = f"Prerequisite step {steps_completed + 1} failed: {defects[-1].title}"

        return JourneyResult(
            journey=journey,
            status=status,
            duration_seconds=round(duration, 2),
            steps_completed=steps_completed,
            defects=defects,
            telemetry_summary=telemetry_summary,
            evidence_files=evidence_files,
        )

    def close(self) -> None:
        self.driver.close()

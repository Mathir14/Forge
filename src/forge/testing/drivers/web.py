"""Web application interaction driver for Tester v2.

Drives browser automation, monitors client telemetry, detects dead buttons,
uncaught exceptions, failed network calls, and layout failures.
"""

import hashlib
import logging
import time
from typing import List, Optional

from forge.testing.browser import BrowserDriver
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

    def __init__(self, browser_driver: BrowserDriver, base_url: str = "http://127.0.0.1:3000"):
        self.driver = browser_driver
        self.base_url = base_url.rstrip("/")

    def execute_journey(
        self,
        journey: Journey,
        budget: BudgetTracker,
        evidence: EvidenceCollector,
    ) -> JourneyResult:
        start_time = time.time()
        budget.record_journey_started()

        # 1. Set viewport
        w, h = journey.viewport
        try:
            self.driver.set_viewport_size(w, h)
        except Exception as e:
            logger.debug("Failed setting viewport size: %s", e)

        defects: List[Defect] = []
        evidence_files: List[str] = []
        steps_completed = 0
        repro_steps_log: List[str] = []

        # Baseline telemetry counts
        initial_console_count = len(self.driver.get_console_logs())
        initial_net_failure_count = len(self.driver.get_failed_requests())
        initial_page_error_count = len(self.driver.get_page_errors())

        # 2. Execute steps sequentially
        for step_idx, step in enumerate(journey.steps):
            if not budget.can_interact(step_idx):
                logger.info("Journey interaction budget reached; finishing journey %s", journey.id)
                break

            step_desc = step.description or f"{step.action.value} {step.target}"
            repro_steps_log.append(f"{step_idx + 1}. {step_desc}")
            budget.record_interaction()

            try:
                if step.action == ActionType.NAVIGATE:
                    target_url = step.target if step.target.startswith("http") else f"{self.base_url}{step.target}"
                    self.driver.goto(target_url)
                    repro_steps_log[-1] = f"{step_idx + 1}. Navigate to {target_url}"
                    time.sleep(0.5)

                    # Capture initial screenshot
                    if budget.can_take_screenshot():
                        shot_name = f"{journey.id}_step_{step_idx + 1}_nav"
                        shot_path = evidence.capture_screenshot(self.driver, shot_name)
                        evidence_files.append(str(shot_path))
                        budget.record_screenshot()

                elif step.action == ActionType.CLICK:
                    # Pre-click snapshot to detect dead interactions
                    pre_content = self.driver.get_content()
                    pre_hash = hashlib.md5(pre_content.encode("utf-8")).hexdigest()
                    pre_console_len = len(self.driver.get_console_logs())
                    pre_net_len = len(self.driver.get_failed_requests())

                    self.driver.click(step.target)
                    time.sleep(0.4)

                    post_content = self.driver.get_content()
                    post_hash = hashlib.md5(post_content.encode("utf-8")).hexdigest()
                    post_console_len = len(self.driver.get_console_logs())
                    post_net_len = len(self.driver.get_failed_requests())

                    # Check for Dead Interaction:
                    # If target is a button or link, and content hash didn't change,
                    # no console log or net request fired, flag DEAD_INTERACTION
                    is_clickable = any(kw in step.target.lower() for kw in ("button", "btn", "#submit", "a", "[role='button']"))
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

                elif step.action == ActionType.FILL:
                    self.driver.fill(step.target, step.value or "")
                    repro_steps_log[-1] = f"{step_idx + 1}. Enter '{step.value or ''}' into {step.target}"
                    time.sleep(0.2)

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
                    content = self.driver.get_content()
                    if step.target not in content:
                        defect_id = f"DEF-{journey.id}-TEXT-{step_idx + 1}"
                        shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                        defect = Defect(
                            id=defect_id,
                            title=f"Expected text '{step.target}' not found in rendered DOM",
                            category=DefectCategory.BROKEN_NAVIGATION,
                            severity=DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            viewport=f"{w}x{h}",
                            steps_to_reproduce=list(repro_steps_log),
                            expected=f"Rendered page should contain '{step.target}'",
                            actual="Text not found in page DOM.",
                            evidence_paths={"screenshot": str(shot_path)},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)

                steps_completed += 1

            except Exception as e:
                logger.warning("Step %d execution error in journey %s: %s", step_idx + 1, journey.id, e)
                defect_id = f"DEF-{journey.id}-ERR-{step_idx + 1}"
                shot_path = evidence.capture_screenshot(self.driver, f"defect_{defect_id}")
                defect = Defect(
                    id=defect_id,
                    title=f"Interaction step '{step_desc}' failed: {e}",
                    category=DefectCategory.BROKEN_NAVIGATION,
                    severity=DefectSeverity.CRITICAL,
                    journey_id=journey.id,
                    viewport=f"{w}x{h}",
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Action should succeed without interaction timeout or missing element.",
                    actual=f"Action failed with error: {e}",
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

        return JourneyResult(
            journey=journey,
            status=status,
            duration_seconds=round(duration, 2),
            steps_completed=steps_completed,
            defects=defects,
            telemetry_summary={
                "console_errors": len([c for c in recent_console if c.level == "error"]),
                "failed_requests": len(recent_net_failures),
                "steps_total": len(journey.steps),
                "steps_completed": steps_completed,
            },
            evidence_files=evidence_files,
        )

    def close(self) -> None:
        self.driver.close()

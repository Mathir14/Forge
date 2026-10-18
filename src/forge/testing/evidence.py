"""Evidence Collector for Tester v2.

Manages structured empirical evidence directories for every run:
evidence/
    screenshots/
    telemetry/
    repro/

Produces standalone executable reproduction scripts so the Executor can
reproduce and verify defect fixes directly.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from forge.testing.browser import BrowserDriver
from forge.testing.models import ConsoleEntry, Defect, DefectCategory, Journey, NetworkFailure

logger = logging.getLogger(__name__)


class EvidenceCollector:
    """Collects and stores multi-modal empirical evidence for test runs."""

    def __init__(self, run_dir: Path, attempt: Optional[int] = None):
        self.run_dir = Path(run_dir)
        self.evidence_dir = self.run_dir / "evidence"
        self.screenshots_dir = self.evidence_dir / "screenshots"
        self.telemetry_dir = self.evidence_dir / "telemetry"
        self.repro_dir = self.evidence_dir / "repro"
        self.attempts_dir = self.evidence_dir / "attempts"

        # Determine attempt number if not explicitly provided or invalid
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
            self.attempt = attempt
        else:
            self.attempt = self._detect_attempt_number()

        # If this is a retry attempt (attempt > 1), isolate prior attempt's evidence
        if self.attempt > 1:
            self._archive_prior_evidence(prior_attempt=self.attempt - 1)

        # Ensure canonical directories exist
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry_dir.mkdir(parents=True, exist_ok=True)
        self.repro_dir.mkdir(parents=True, exist_ok=True)

    def _detect_attempt_number(self) -> int:
        """Detect current attempt number from existing run artifacts."""
        existing_attempts = list(self.run_dir.glob("*_tester_attempt_*.json"))
        if existing_attempts:
            max_num = 1
            for p in existing_attempts:
                m = re.search(r"_attempt_(\d+)\.json", p.name)
                if m:
                    max_num = max(max_num, int(m.group(1)))
            return max_num + 1

        if self.attempts_dir.exists():
            att_dirs = [d for d in self.attempts_dir.iterdir() if d.is_dir() and d.name.startswith("attempt_")]
            if att_dirs:
                max_att = 1
                for d in att_dirs:
                    try:
                        num = int(d.name.split("_")[1])
                        max_att = max(max_att, num)
                    except (IndexError, ValueError):
                        pass
                return max_att + 1

        has_existing_evidence = any(
            p.exists() and any(p.iterdir())
            for p in (self.screenshots_dir, self.repro_dir, self.telemetry_dir)
        )
        if has_existing_evidence:
            return 2

        return 1

    def _archive_prior_evidence(self, prior_attempt: int) -> Optional[Path]:
        """Move existing un-archived evidence to evidence/attempts/attempt_{prior_attempt}/."""
        has_files = any(
            p.exists() and any(f.is_file() for f in p.iterdir())
            for p in (self.screenshots_dir, self.repro_dir, self.telemetry_dir)
        )
        if not has_files:
            return None

        target_dir = self.attempts_dir / f"attempt_{prior_attempt}"
        target_dir.mkdir(parents=True, exist_ok=True)

        for sub in ("screenshots", "telemetry", "repro"):
            src_sub = self.evidence_dir / sub
            if src_sub.exists():
                dst_sub = target_dir / sub
                dst_sub.mkdir(parents=True, exist_ok=True)
                for f in src_sub.iterdir():
                    if f.is_file():
                        shutil.move(str(f), str(dst_sub / f.name))

        logger.info("Archived attempt %d evidence to %s", prior_attempt, target_dir)
        return target_dir

    def archive_current_attempt(self, attempt_number: int) -> Path:
        """Explicitly archive current attempt's evidence into evidence/attempts/attempt_{attempt_number}/."""
        target_dir = self.attempts_dir / f"attempt_{attempt_number}"
        target_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("screenshots", "telemetry", "repro"):
            src_sub = self.evidence_dir / sub
            if src_sub.exists():
                dst_sub = target_dir / sub
                dst_sub.mkdir(parents=True, exist_ok=True)
                for f in src_sub.iterdir():
                    if f.is_file():
                        shutil.copy2(str(f), str(dst_sub / f.name))
        return target_dir

    def capture_screenshot(
        self,
        driver: BrowserDriver,
        name: str,
        full_page: bool = False,
    ) -> Path:
        """Capture screenshot from browser driver and persist to evidence directory."""
        filename = f"{name}.png"
        target_path = self.screenshots_dir / filename
        try:
            driver.screenshot(target_path, full_page=full_page)
            logger.info("Captured screenshot: %s", target_path)
            return target_path
        except Exception as e:
            logger.warning("Failed capturing screenshot '%s': %s", name, e)
            return target_path

    def save_telemetry(
        self,
        console_logs: List[ConsoleEntry],
        failed_requests: List[NetworkFailure],
        page_errors: List[str],
        process_stdout: str = "",
        process_stderr: str = "",
    ) -> Dict[str, str]:
        """Save telemetry streams into structured JSON and log files."""
        saved_paths = {}

        # 1. Browser console log
        if console_logs:
            console_file = self.telemetry_dir / "browser_console.log"
            lines = [f"[{entry.level.upper()}] {entry.text} ({entry.location or 'unknown'})" for entry in console_logs]
            console_file.write_text("\n".join(lines), encoding="utf-8")
            saved_paths["browser_console"] = str(console_file)

        # 2. Network failures
        if failed_requests:
            net_file = self.telemetry_dir / "network_failures.json"
            net_data = [item.to_dict() for item in failed_requests]
            net_file.write_text(json.dumps(net_data, indent=2), encoding="utf-8")
            saved_paths["network_failures"] = str(net_file)

        # 3. Unhandled page exceptions
        if page_errors:
            err_file = self.telemetry_dir / "page_errors.json"
            err_file.write_text(json.dumps(page_errors, indent=2), encoding="utf-8")
            saved_paths["page_errors"] = str(err_file)

        # 4. Process logs
        if process_stdout or process_stderr:
            proc_file = self.telemetry_dir / "process_logs.log"
            content = f"=== STDOUT ===\n{process_stdout}\n=== STDERR ===\n{process_stderr}\n"
            proc_file.write_text(content, encoding="utf-8")
            saved_paths["process_logs"] = str(proc_file)

        return saved_paths

    def generate_reproduction_script(
        self,
        defect: Defect,
        journey: Optional[Journey] = None,
        base_url: str = "http://127.0.0.1:3000",
    ) -> Path:
        """Generate a standalone executable reproduction script for a defect."""
        safe_id = defect.id.replace("-", "_").lower()

        if defect.category in (
            DefectCategory.DEAD_INTERACTION,
            DefectCategory.UNHANDLED_EXCEPTION,
            DefectCategory.NETWORK_FAILURE,
            DefectCategory.LAYOUT_REGRESSION,
            DefectCategory.BROKEN_NAVIGATION,
            DefectCategory.INVALID_INTERACTION,
        ):
            # Generate Playwright Python reproduction script
            repro_file = self.repro_dir / f"repro_{safe_id}.py"
            steps_code = []
            if journey:
                for step in journey.steps:
                    if step.action.value == "NAVIGATE":
                        url = f"{base_url}{step.target}" if step.target.startswith("/") else step.target
                        steps_code.append(f'        page.goto("{url}")')
                    elif step.action.value == "CLICK":
                        steps_code.append(f'        page.click("{step.target}")')
                    elif step.action.value == "FILL":
                        val_str = step.value or ""
                        steps_code.append(f'        page.fill("{step.target}", "{val_str}")')
            else:
                for s in defect.steps_to_reproduce:
                    steps_code.append(f'        # {s}')

            repro_content = f'''"""Standalone reproduction script for {defect.id}: {defect.title}."""

import sys
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Please install playwright: pip install playwright && playwright install chromium")
    sys.exit(1)

def run():
    print("Reproducing {defect.id}: {defect.title}...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        # Telemetry hooks
        page.on("console", lambda msg: print("[CONSOLE] " + msg.text))
        page.on("pageerror", lambda err: print("[CRASH] " + str(err)))

        # Steps to reproduce
{chr(10).join(steps_code) if steps_code else "        # No automated steps recorded"}

        print("Expected: {defect.expected}")
        print("Actual:   {defect.actual}")
        page.wait_for_timeout(3000)
        browser.close()

if __name__ == "__main__":
    run()
'''
            repro_file.write_text(repro_content, encoding="utf-8")
            return repro_file

        elif defect.category == DefectCategory.CLI_CRASH:
            # Shell reproduction script
            repro_file = self.repro_dir / f"repro_{safe_id}.sh"
            repro_content = f'''#!/usr/bin/env bash
# Reproduction script for {defect.id}: {defect.title}
set -x

echo "Reproducing {defect.id}..."
{' '.join(defect.steps_to_reproduce)}

echo "Expected: {defect.expected}"
echo "Actual:   {defect.actual}"
'''
            repro_file.write_text(repro_content, encoding="utf-8")
            try:
                repro_file.chmod(0o755)
            except OSError:
                pass
            return repro_file

        else:
            # Generic reproduction notes
            repro_file = self.repro_dir / f"repro_{safe_id}.txt"
            repro_content = f"""Reproduction Steps for {defect.id}: {defect.title}
--------------------------------------------------
{chr(10).join(defect.steps_to_reproduce)}

Expected: {defect.expected}
Actual:   {defect.actual}
"""
            repro_file.write_text(repro_content, encoding="utf-8")
            return repro_file

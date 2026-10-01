"""CLI interaction driver for Tester v2.

Executes command-line journeys, testing flag combinations, exit codes,
stdout/stderr separation, and catching unhandled stack traces.
"""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from forge.testing.budget import BudgetTracker
from forge.testing.drivers.base import InteractionDriver
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
)

logger = logging.getLogger(__name__)


class CliInteractionDriver(InteractionDriver):
    """Executes CLI command journeys in an isolated working directory."""

    def __init__(self, cwd: Path):
        self.cwd = cwd

    def execute_journey(
        self,
        journey: Journey,
        budget: BudgetTracker,
        evidence: EvidenceCollector,
    ) -> JourneyResult:
        start_time = time.time()
        budget.record_journey_started()

        defects: List[Defect] = []
        steps_completed = 0
        repro_steps_log: List[str] = []

        for step_idx, step in enumerate(journey.steps):
            if not budget.can_interact(step_idx):
                break

            budget.record_interaction()
            cmd = step.target
            step_desc = step.description or f"Run '{cmd}'"
            repro_steps_log.append(cmd)

            try:
                res = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(self.cwd),
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                )

                # Check for Python / Node traceback in stderr or stdout
                combined = f"{res.stdout}\n{res.stderr}"
                has_traceback = "Traceback (most recent call last):" in combined or "TypeError:" in combined or "ReferenceError:" in combined

                # Check exit code
                expected_exit = 0
                if "invalid" in cmd.lower() or "--bad" in cmd.lower() or "error" in step.description.lower():
                    expected_exit = None  # Non-zero allowed for negative tests

                if has_traceback or (expected_exit == 0 and res.returncode != 0):
                    defect_id = f"DEF-{journey.id}-CLI-{step_idx + 1}"
                    actual_desc = f"Process exited with code {res.returncode}.\nStderr: {res.stderr[:300]}"
                    if has_traceback:
                        actual_desc = f"Unhandled crash/traceback detected:\n{combined[:400]}"

                    defect = Defect(
                        id=defect_id,
                        title=f"CLI command '{cmd}' crashed with exit code {res.returncode}",
                        category=DefectCategory.CLI_CRASH,
                        severity=DefectSeverity.CRITICAL if has_traceback else DefectSeverity.MAJOR,
                        journey_id=journey.id,
                        steps_to_reproduce=list(repro_steps_log),
                        expected=step.expected_state or "Command should exit with 0 and clean output.",
                        actual=actual_desc,
                        telemetry={"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr},
                    )
                    repro_script = evidence.generate_reproduction_script(defect, journey)
                    defect.evidence_paths["reproduction_script"] = str(repro_script)
                    defects.append(defect)

                steps_completed += 1

            except subprocess.TimeoutExpired:
                defect_id = f"DEF-{journey.id}-TIMEOUT-{step_idx + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"CLI command '{cmd}' timed out after 15s (hung/deadlocked)",
                    category=DefectCategory.CLI_CRASH,
                    severity=DefectSeverity.CRITICAL,
                    journey_id=journey.id,
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Command should complete within 15s.",
                    actual="Process hung waiting for input or deadlock.",
                )
                repro_script = evidence.generate_reproduction_script(defect, journey)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
                defects.append(defect)
                break
            except Exception as e:
                defect_id = f"DEF-{journey.id}-EXC-{step_idx + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"Failed invoking CLI command '{cmd}': {e}",
                    category=DefectCategory.CLI_CRASH,
                    severity=DefectSeverity.MAJOR,
                    journey_id=journey.id,
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Command invocation should succeed.",
                    actual=str(e),
                )
                defects.append(defect)
                break

        duration = time.time() - start_time
        status = "FAIL" if defects else "PASS"

        return JourneyResult(
            journey=journey,
            status=status,
            duration_seconds=round(duration, 2),
            steps_completed=steps_completed,
            defects=defects,
            telemetry_summary={
                "steps_total": len(journey.steps),
                "steps_completed": steps_completed,
                "defects_count": len(defects),
            },
        )

    def close(self) -> None:
        pass

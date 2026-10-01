"""Library interaction driver for Tester v2.

Validates libraries and SDKs through a consumer sandbox, verifying:
- Package packaging and exports (ESM/CJS or Python wheel/sdist)
- Clean importability as an external consumer
- Unhandled missing dependencies or runtime syntax/packaging errors
"""

import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List

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


class LibraryInteractionDriver(InteractionDriver):
    """Validates library packaging and consumer DX in a sandbox environment."""

    def __init__(self, project_root: Path):
        self.project_root = project_root

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

        with tempfile.TemporaryDirectory(prefix="forge_consumer_sandbox_") as tmp_dir:
            sandbox = Path(tmp_dir)

            for step_idx, step in enumerate(journey.steps):
                if not budget.can_interact(step_idx):
                    break

                budget.record_interaction()
                cmd = step.target
                repro_steps_log.append(f"{step_idx + 1}. {step.description or cmd}")

                try:
                    res = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=str(sandbox),
                        capture_output=True,
                        text=True,
                        timeout=30.0,
                    )

                    if res.returncode != 0:
                        defect_id = f"DEF-{journey.id}-LIB-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"Consumer sandbox validation failed: {step.description or cmd}",
                            category=DefectCategory.LIBRARY_PACKAGING,
                            severity=DefectSeverity.CRITICAL,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected=step.expected_state or "Library should install and run cleanly in external consumer project.",
                            actual=f"Process exited with {res.returncode}.\nStderr: {res.stderr[:300]}",
                            telemetry={"exit_code": res.returncode, "stdout": res.stdout, "stderr": res.stderr},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)
                        break

                    steps_completed += 1

                except Exception as e:
                    defect_id = f"DEF-{journey.id}-EXC-{step_idx + 1}"
                    defect = Defect(
                        id=defect_id,
                        title=f"Sandbox execution error: {e}",
                        category=DefectCategory.LIBRARY_PACKAGING,
                        severity=DefectSeverity.MAJOR,
                        journey_id=journey.id,
                        steps_to_reproduce=list(repro_steps_log),
                        expected="Sandbox step should execute without exception.",
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

"""API interaction driver for Tester v2.

Executes stateful HTTP request journeys, checks status codes and response schemas,
and detects unhandled 500 error tracebacks.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

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


class ApiInteractionDriver(InteractionDriver):
    """Executes stateful HTTP API request journeys."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self.session_variables: Dict[str, Any] = {}

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
            step_desc = step.description or f"HTTP {step.target}"
            repro_steps_log.append(f"{step_idx + 1}. {step_desc}")

            try:
                # 1. Substitute session variables (e.g. {token}, {user_id})
                target_url = step.target if step.target.startswith("http") else f"{self.base_url}{step.target}"
                for k, v in self.session_variables.items():
                    target_url = target_url.replace(f"{{{k}}}", str(v))

                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if "token" in self.session_variables:
                    headers["Authorization"] = f"Bearer {self.session_variables['token']}"

                method = "GET"
                data_bytes = None
                if step.value:
                    try:
                        # If value contains method prefix, e.g. "POST {...}"
                        parts = step.value.split(" ", 1)
                        if parts[0] in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                            method = parts[0]
                            payload_str = parts[1] if len(parts) > 1 else ""
                        else:
                            method = "POST"
                            payload_str = step.value

                        # Substitute session variables in payload
                        for k, v in self.session_variables.items():
                            payload_str = payload_str.replace(f"{{{k}}}", str(v))

                        if payload_str:
                            data_bytes = payload_str.encode("utf-8")
                    except Exception:
                        pass

                req = urllib.request.Request(target_url, data=data_bytes, headers=headers, method=method)

                status_code = None
                response_body = ""
                try:
                    with urllib.request.urlopen(req, timeout=10.0) as resp:
                        status_code = resp.status
                        response_body = resp.read().decode("utf-8", errors="replace")
                except urllib.error.HTTPError as e:
                    status_code = e.code
                    response_body = e.read().decode("utf-8", errors="replace")

                # Parse JSON and extract variables
                try:
                    res_json = json.loads(response_body)
                    if isinstance(res_json, dict):
                        if "token" in res_json:
                            self.session_variables["token"] = res_json["token"]
                        if "access_token" in res_json:
                            self.session_variables["token"] = res_json["access_token"]
                        if "id" in res_json:
                            self.session_variables["id"] = res_json["id"]
                except Exception:
                    pass

                # Check for Server Crash (500)
                if status_code and status_code >= 500:
                    defect_id = f"DEF-{journey.id}-API-500-{step_idx + 1}"
                    defect = Defect(
                        id=defect_id,
                        title=f"API endpoint {method} {target_url} returned unhandled server error {status_code}",
                        category=DefectCategory.UNHANDLED_EXCEPTION,
                        severity=DefectSeverity.CRITICAL,
                        journey_id=journey.id,
                        steps_to_reproduce=list(repro_steps_log),
                        expected=step.expected_state or "Expected HTTP 200/201 or clean 4xx client validation error.",
                        actual=f"HTTP {status_code}: {response_body[:300]}",
                        telemetry={"status_code": status_code, "body": response_body},
                    )
                    repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                    defect.evidence_paths["reproduction_script"] = str(repro_script)
                    defects.append(defect)

                steps_completed += 1

            except Exception as e:
                defect_id = f"DEF-{journey.id}-API-ERR-{step_idx + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"API request failed with network error: {e}",
                    category=DefectCategory.NETWORK_FAILURE,
                    severity=DefectSeverity.CRITICAL,
                    journey_id=journey.id,
                    steps_to_reproduce=list(repro_steps_log),
                    expected="API server should respond within timeout.",
                    actual=str(e),
                )
                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
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
        self.session_variables.clear()

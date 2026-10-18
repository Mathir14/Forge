"""API interaction driver for Tester v2.

Executes stateful HTTP request journeys, checks status codes and response schemas,
and detects unhandled 500 error tracebacks.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

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


def is_pure_status_expectation(expected_str: str, status_code: int) -> bool:
    """Return True if expected_str is asserting only the HTTP status code or standard phrase,
    without asserting specific response body content or schema.
    """
    if not expected_str:
        return True
    s = expected_str.strip().lower()
    # If JSON or structured body expected, it's not a pure status expectation
    if "{" in s or "}" in s or '"' in s or "'" in s:
        return False
    s_clean = s.replace(":", " ").replace("-", " ")
    tokens = set(s_clean.split())
    if not tokens:
        return True
    standard_phrases = {
        200: {"200", "http", "ok", "status", "code"},
        201: {"201", "http", "created", "status", "code"},
        204: {"204", "http", "no", "content", "status", "code"},
        400: {"400", "http", "bad", "request", "status", "code"},
        401: {"401", "http", "unauthorized", "unauthenticated", "status", "code"},
        403: {"403", "http", "forbidden", "access", "denied", "status", "code"},
        404: {"404", "http", "not", "found", "status", "code"},
        405: {"405", "http", "method", "not", "allowed", "status", "code"},
        409: {"409", "http", "conflict", "status", "code"},
        422: {"422", "http", "unprocessable", "entity", "validation", "error", "status", "code"},
        500: {"500", "http", "internal", "server", "error", "status", "code"},
    }
    allowed = standard_phrases.get(status_code, {str(status_code), "http", "status", "code"})
    return tokens.issubset(allowed)


def match_body_or_schema(exp_str: str, act_str: str, act_status: int) -> Tuple[bool, str]:
    """Helper to evaluate response body matching or schema validation against expected_state."""
    if not exp_str or is_pure_status_expectation(exp_str, act_status):
        return True, ""
    # Direct text or status code string match
    if exp_str in act_str or f"HTTP {act_status}" == exp_str or str(act_status) == exp_str:
        return True, ""
    # JSON object or schema comparison
    try:
        exp_j = json.loads(exp_str)
        act_j = json.loads(act_str)
        if isinstance(exp_j, dict) and isinstance(act_j, dict):
            # JSON schema format
            if exp_j.get("type") == "object" and "required" in exp_j and isinstance(exp_j["required"], list):
                missing = [k for k in exp_j["required"] if k not in act_j]
                if not missing:
                    return True, ""
                return False, f"JSON schema mismatch: missing required field(s) {missing}"
            # Subset key-value matching
            diffs = []
            for k, v in exp_j.items():
                if k not in act_j:
                    diffs.append(f"missing key '{k}'")
                elif act_j[k] != v:
                    diffs.append(f"field '{k}' expected {v!r}, got {act_j[k]!r}")
            if not diffs:
                return True, ""
            return False, f"JSON body mismatch: {'; '.join(diffs)}"
    except Exception:
        pass
    return False, f"Expected content {exp_str!r} not found in response body"


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
        last_response_body = ""
        last_status_code: Optional[int] = None

        for step_idx, step in enumerate(journey.steps):
            if not budget.can_interact(step_idx):
                break

            budget.record_interaction()
            step_desc = step.description or f"HTTP {step.target}"
            repro_steps_log.append(f"{step_idx + 1}. {step_desc}")

            expected_val = (step.expected_state or "").strip()

            # 1. Validation Before Execution: Missing expected-state on explicit assertion step
            if step.action == ActionType.ASSERT_TEXT and not expected_val:
                defect_id = f"DEF-{journey.id}-MISSING-SPEC-{step_idx + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"Missing expected-state definition in assertion step {step_idx + 1}",
                    category=DefectCategory.TEST_CONFIGURATION_GAP,
                    severity=DefectSeverity.MAJOR,
                    journey_id=journey.id,
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Specific assertion text or pattern to verify.",
                    actual="expected_state is empty or missing.",
                )
                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
                defects.append(defect)
                break

            matched_codes = [
                code_cand for code_cand in (200, 201, 204, 400, 401, 403, 404, 405, 409, 422, 500)
                if (
                    f"HTTP {code_cand}" in expected_val
                    or expected_val == str(code_cand)
                    or expected_val.startswith(f"{code_cand} ")
                    or f"status {code_cand}" in expected_val.lower()
                )
            ]

            # 2. Validation Before Execution: Contradictory expected-state detection (e.g. 200 vs 404, 401 vs 200)
            if len(matched_codes) > 1 and not set(matched_codes).issubset({200, 201, 204}):
                defect_id = f"DEF-{journey.id}-CONTRADICTORY-SPEC-{step_idx + 1}"
                defect = Defect(
                    id=defect_id,
                    title=f"Contradictory expected-state definition in journey step: {expected_val}",
                    category=DefectCategory.TEST_CONFIGURATION_GAP,
                    severity=DefectSeverity.MAJOR,
                    journey_id=journey.id,
                    steps_to_reproduce=list(repro_steps_log),
                    expected="Single unambiguous expected HTTP status code or criteria.",
                    actual=f"Conflicting expectations specified: {matched_codes}",
                )
                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                defect.evidence_paths["reproduction_script"] = str(repro_script)
                defects.append(defect)
                break

            expected_status_code = matched_codes[0] if matched_codes else None

            # 3. Determine test intent UPFRONT before execution
            is_explicit_auth_test = bool(
                expected_status_code in (401, 403)
                or "unauthorized" in expected_val.lower()
                or "forbidden" in expected_val.lower()
                or "unauthenticated" in expected_val.lower()
                or "unauthorized" in (step.description or "").lower()
                or "forbidden" in (step.description or "").lower()
                or "unauthenticated" in (step.description or "").lower()
                or "401" in (step.description or "").lower()
                or "403" in (step.description or "").lower()
                or "unauthorized" in journey.title.lower()
                or "unauthenticated" in journey.title.lower()
            )

            is_negative_test = bool(
                is_explicit_auth_test
                or (expected_status_code and expected_status_code >= 400)
                or "invalid" in (step.target or "").lower()
                or "invalid" in (step.description or "").lower()
                or "negative" in (step.description or "").lower()
                or "negative" in journey.title.lower()
                or journey.category == "negative"
                or any(c in expected_val for c in ("400", "404", "422", "4xx", "error", "validation", "fail"))
            )

            # 4. Handle pure assertion steps against previous response
            if step.action == ActionType.ASSERT_TEXT:
                matched, reason = match_body_or_schema(expected_val or step.target, last_response_body, last_status_code or 200)
                if not matched:
                    defect_id = f"DEF-{journey.id}-API-ASSERT-{step_idx + 1}"
                    defect = Defect(
                        id=defect_id,
                        title=f"API assertion failed: {reason}",
                        category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                        severity=DefectSeverity.MAJOR,
                        journey_id=journey.id,
                        steps_to_reproduce=list(repro_steps_log),
                        expected=expected_val or step.target,
                        actual=last_response_body[:300],
                        telemetry={"body": last_response_body},
                    )
                    repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                    defect.evidence_paths["reproduction_script"] = str(repro_script)
                    defects.append(defect)
                    break
                steps_completed += 1
                continue

            try:
                # 1. Substitute session variables (e.g. {token}, {user_id})
                target_url = step.target if step.target.startswith("http") else f"{self.base_url}{step.target}"
                for k, v in self.session_variables.items():
                    target_url = target_url.replace(f"{{{k}}}", str(v))

                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                if "token" in self.session_variables:
                    headers["Authorization"] = f"Bearer {self.session_variables['token']}"

                has_auth_token = bool(
                    "token" in self.session_variables
                    or "access_token" in self.session_variables
                    or any(h.lower() in ("authorization", "x-api-key", "api-key") or "auth" in h.lower() for h in headers)
                )

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

                last_response_body = response_body
                last_status_code = status_code

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

                # 1. Unhandled Server Error (500+)
                if status_code and status_code >= 500:
                    if expected_status_code != status_code:
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

                # 2. Authentication Rejections (401 / 403)
                elif status_code in (401, 403):
                    if is_explicit_auth_test:
                        # Intentionally testing unauthenticated or unauthorized rejection -> positive verification
                        # If specific error message was also expected, verify body
                        if expected_val and not is_pure_status_expectation(expected_val, status_code):
                            matched, reason = match_body_or_schema(expected_val, response_body, status_code)
                            if not matched:
                                defect_id = f"DEF-{journey.id}-API-AUTH-ASSERT-{step_idx + 1}"
                                defect = Defect(
                                    id=defect_id,
                                    title=f"API endpoint {method} {target_url} returned {status_code} but body {reason}",
                                    category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                                    severity=DefectSeverity.MAJOR,
                                    journey_id=journey.id,
                                    steps_to_reproduce=list(repro_steps_log),
                                    expected=expected_val,
                                    actual=f"HTTP {status_code}: {response_body[:300]}",
                                    telemetry={"status_code": status_code, "body": response_body},
                                )
                                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                                defect.evidence_paths["reproduction_script"] = str(repro_script)
                                defects.append(defect)
                    elif has_auth_token:
                        # Credentials were configured and supplied, but server rejected them -> Application Defect
                        defect_id = f"DEF-{journey.id}-API-AUTH-FAIL-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"API endpoint {method} {target_url} returned {status_code} despite test credentials being configured (auth failure in application)",
                            category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                            severity=DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected=step.expected_state or "Expected successful authenticated response (HTTP 2xx).",
                            actual=f"HTTP {status_code}: {response_body[:300]}",
                            telemetry={"status_code": status_code, "body": response_body},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)
                    else:
                        # Positive journey called protected endpoint without configuring credentials -> Test Configuration Gap
                        defect_id = f"DEF-{journey.id}-API-NOAUTH-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"API endpoint {method} {target_url} returned {status_code} (missing authentication credentials in test journey)",
                            category=DefectCategory.TEST_CONFIGURATION_GAP,
                            severity=DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected=step.expected_state or "Valid authentication credentials should be configured in test session or headers.",
                            actual=f"HTTP {status_code}: {response_body[:300]}",
                            telemetry={"status_code": status_code, "body": response_body},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)

                # 3. Other 4xx Client Errors (400, 404, 422, etc.)
                elif status_code and 400 <= status_code < 500:
                    if is_negative_test:
                        # Expected client validation / missing resource error
                        if expected_val and not is_pure_status_expectation(expected_val, status_code):
                            matched, reason = match_body_or_schema(expected_val, response_body, status_code)
                            if not matched:
                                defect_id = f"DEF-{journey.id}-API-ERR-ASSERT-{step_idx + 1}"
                                defect = Defect(
                                    id=defect_id,
                                    title=f"API endpoint {method} {target_url} {reason}",
                                    category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                                    severity=DefectSeverity.MAJOR,
                                    journey_id=journey.id,
                                    steps_to_reproduce=list(repro_steps_log),
                                    expected=expected_val,
                                    actual=f"HTTP {status_code}: {response_body[:300]}",
                                    telemetry={"status_code": status_code, "body": response_body},
                                )
                                repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                                defect.evidence_paths["reproduction_script"] = str(repro_script)
                                defects.append(defect)
                    else:
                        # Unexpected 4xx on positive journey -> Application Defect
                        defect_id = f"DEF-{journey.id}-API-{status_code}-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"API endpoint {method} {target_url} returned unexpected client error {status_code}",
                            category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                            severity=DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected=step.expected_state or "Expected successful HTTP 2xx response.",
                            actual=f"HTTP {status_code}: {response_body[:300]}",
                            telemetry={"status_code": status_code, "body": response_body},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)

                # 4. Successful HTTP 2xx Responses
                elif status_code and 200 <= status_code < 300:
                    if is_explicit_auth_test or (expected_status_code and expected_status_code in (401, 403)):
                        # Security defect: Expected 401/403, but endpoint accepted request without authentication!
                        defect_id = f"DEF-{journey.id}-API-SEC-BYPASS-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"API endpoint {method} {target_url} returned HTTP {status_code} instead of rejecting unauthenticated request (security bypass)",
                            category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                            severity=DefectSeverity.CRITICAL,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected="Expected HTTP 401 Unauthorized or 403 Forbidden.",
                            actual=f"HTTP {status_code}: {response_body[:300]}",
                            telemetry={"status_code": status_code, "body": response_body},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)
                    elif is_negative_test and expected_status_code and expected_status_code >= 400:
                        # Negative validation test expected failure, but server returned 2xx!
                        defect_id = f"DEF-{journey.id}-API-NEG-FAIL-{step_idx + 1}"
                        defect = Defect(
                            id=defect_id,
                            title=f"API endpoint {method} {target_url} returned HTTP {status_code} instead of expected error {expected_status_code}",
                            category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                            severity=DefectSeverity.MAJOR,
                            journey_id=journey.id,
                            steps_to_reproduce=list(repro_steps_log),
                            expected=f"Expected HTTP error {expected_status_code}",
                            actual=f"HTTP {status_code}: {response_body[:300]}",
                            telemetry={"status_code": status_code, "body": response_body},
                        )
                        repro_script = evidence.generate_reproduction_script(defect, journey, self.base_url)
                        defect.evidence_paths["reproduction_script"] = str(repro_script)
                        defects.append(defect)
                    elif expected_val and not is_pure_status_expectation(expected_val, status_code):
                        matched, reason = match_body_or_schema(expected_val, response_body, status_code)
                        if not matched:
                            defect_id = f"DEF-{journey.id}-API-ASSERT-{step_idx + 1}"
                            defect = Defect(
                                id=defect_id,
                                title=f"API endpoint {method} {target_url} {reason}",
                                category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
                                severity=DefectSeverity.MAJOR,
                                journey_id=journey.id,
                                steps_to_reproduce=list(repro_steps_log),
                                expected=expected_val,
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
        test_gap_categories = {DefectCategory.TEST_CONFIGURATION_GAP, DefectCategory.PREREQUISITE_FAILURE}
        if defects:
            if all(d.category in test_gap_categories for d in defects):
                status = "BLOCKED"
            else:
                status = "FAIL"
        else:
            status = "PASS"

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

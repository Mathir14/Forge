"""Domain models for Tester v2: journeys, defects, coverage, and telemetry."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ProjectArchetype(str, Enum):
    WEB_SPA = "WEB_SPA"          # Next.js, React, Vue, Vite, HTML/CSS/JS with dev server
    API = "API"                  # FastAPI, Express, Flask, REST/GraphQL daemon
    CLI = "CLI"                  # Command line application / tool
    LIBRARY = "LIBRARY"          # Python / Node library / SDK package
    UNKNOWN = "UNKNOWN"          # Generic repository


class DefectCategory(str, Enum):
    DEAD_INTERACTION = "DEAD_INTERACTION"                # Button, link, or element clicked with zero observable state change
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"          # Uncaught JS error or unhandled 500 traceback
    NETWORK_FAILURE = "NETWORK_FAILURE"                  # API request returned 4xx/5xx or failed/aborted
    BROKEN_NAVIGATION = "BROKEN_NAVIGATION"              # Dead end, unexpected 404, or blank screen
    LAYOUT_REGRESSION = "LAYOUT_REGRESSION"              # Visual overlap, element clipping, responsive collapse
    CLI_CRASH = "CLI_CRASH"                              # Traceback, missing option, or non-zero exit code
    LIBRARY_PACKAGING = "LIBRARY_PACKAGING"              # Import failure, missing export, or invalid types
    INVALID_INTERACTION = "INVALID_INTERACTION"          # AI-planned step targeted nonexistent/undiscovered control
    PREREQUISITE_FAILURE = "PREREQUISITE_FAILURE"        # Required data, state, or workflow prerequisite was unfulfilled
    TEST_CONFIGURATION_GAP = "TEST_CONFIGURATION_GAP"    # Test journey omitted necessary form/target setup or prerequisite step
    ASSERTION_MISMATCH = "ASSERTION_MISMATCH"            # Assertion text or element expectation did not match rendered DOM content on error-free page
    FUNCTIONAL_ASSERTION_FAILURE = "FUNCTIONAL_ASSERTION_FAILURE"  # Required functional output, metric, or calculation missing or incorrect despite fulfilled prerequisites


class DefectSeverity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"


@dataclass
class ConsoleEntry:
    level: str  # "error", "warning", "info", "log"
    text: str
    location: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "text": self.text,
            "location": self.location,
            "timestamp": self.timestamp,
        }


@dataclass
class NetworkFailure:
    url: str
    method: str
    status: Optional[int] = None
    error_text: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "error_text": self.error_text,
            "timestamp": self.timestamp,
        }


@dataclass
class InteractiveElement:
    selector: str
    tag_name: str
    text: str
    role: Optional[str] = None
    is_visible: bool = True
    is_enabled: bool = True


@dataclass
class Defect:
    id: str
    title: str
    category: DefectCategory
    severity: DefectSeverity
    journey_id: str
    steps_to_reproduce: List[str]
    expected: str
    actual: str
    viewport: Optional[str] = None
    telemetry: Dict[str, Any] = field(default_factory=dict)
    evidence_paths: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ID": self.id,
            "TITLE": self.title,
            "DESCRIPTION": self.title,
            "CATEGORY": self.category.value if isinstance(self.category, DefectCategory) else str(self.category),
            "SEVERITY": self.severity.value if isinstance(self.severity, DefectSeverity) else str(self.severity),
            "JOURNEY_ID": self.journey_id,
            "VIEWPORT": self.viewport,
            "STEPS_TO_REPRODUCE": self.steps_to_reproduce,
            "EXPECTED": self.expected,
            "ACTUAL": self.actual,
            "TELEMETRY": self.telemetry,
            "EVIDENCE": self.evidence_paths,
        }


class ActionType(str, Enum):
    NAVIGATE = "NAVIGATE"
    CLICK = "CLICK"
    FILL = "FILL"
    RESIZE = "RESIZE"
    HTTP_REQUEST = "HTTP_REQUEST"
    CLI_COMMAND = "CLI_COMMAND"
    ASSERT_TEXT = "ASSERT_TEXT"
    ASSERT_ELEMENT = "ASSERT_ELEMENT"
    UPLOAD_FILE = "UPLOAD_FILE"


@dataclass
class JourneyStep:
    action: ActionType
    target: str                  # selector, URL path, command line, or endpoint
    value: Optional[str] = None  # text to fill, payload, query, or fixture file path
    description: str = ""
    expected_state: Optional[str] = None


@dataclass
class Journey:
    id: str                      # e.g. "J-01"
    title: str
    description: str
    priority: int                # 1=modified feature, 2=adjacent workflow, 3=smoke, 4=exploratory
    steps: List[JourneyStep]
    viewport: Tuple[int, int] = (1440, 900)
    category: str = "functional"  # "functional", "responsive", "smoke", "negative"
    is_valid: bool = True
    validation_error: Optional[str] = None


@dataclass
class JourneyResult:
    journey: Journey
    status: str                  # "PASS", "FAIL", "BLOCKED"
    duration_seconds: float
    steps_completed: int
    defects: List[Defect] = field(default_factory=list)
    telemetry_summary: Dict[str, Any] = field(default_factory=dict)
    evidence_files: List[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Explicit coverage reporting: planned, executed, blocked journeys, coverage depth, and confidence."""
    planned_journeys: int
    executed_journeys: int
    passed_journeys: int
    failed_journeys: int
    blocked_journeys: int
    confidence: str              # "HIGH", "MEDIUM", "LOW"
    summary: str
    categories_covered: List[str] = field(default_factory=list)
    coverage_depth: str = "FUNCTIONAL_VERIFIED"  # "FUNCTIONAL_VERIFIED", "SHALLOW_SMOKE", "UNTESTED", "BLOCKED_PREREQUISITE"
    exercised_changed_components: List[str] = field(default_factory=list)
    visited_without_meaningful_assertions: List[str] = field(default_factory=list)
    verified_edge_cases: List[str] = field(default_factory=list)
    unit_test_verified_branches: List[str] = field(default_factory=list)
    untested_changed_branches: List[str] = field(default_factory=list)
    failed_verification_components: List[str] = field(default_factory=list)
    blocked_by_prerequisite_components: List[str] = field(default_factory=list)
    unexercised_changed_components: List[str] = field(default_factory=list)
    validation_gaps: List[str] = field(default_factory=list)
    unexecuted_journeys: int = 0
    skipped_journeys: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "planned_journeys": self.planned_journeys,
            "executed_journeys": self.executed_journeys,
            "passed_journeys": self.passed_journeys,
            "failed_journeys": self.failed_journeys,
            "blocked_journeys": self.blocked_journeys,
            "unexecuted_journeys": self.unexecuted_journeys,
            "skipped_journeys": self.skipped_journeys,
            "confidence": self.confidence,
            "summary": self.summary,
            "categories_covered": list(self.categories_covered),
            "coverage_depth": self.coverage_depth,
            "exercised_changed_components": list(self.exercised_changed_components),
            "visited_without_meaningful_assertions": list(self.visited_without_meaningful_assertions),
            "verified_edge_cases": list(self.verified_edge_cases),
            "unit_test_verified_branches": list(self.unit_test_verified_branches),
            "untested_changed_branches": list(self.untested_changed_branches),
            "failed_verification_components": list(self.failed_verification_components),
            "blocked_by_prerequisite_components": list(self.blocked_by_prerequisite_components),
            "unexercised_changed_components": list(self.unexercised_changed_components),
            "validation_gaps": list(self.validation_gaps),
        }

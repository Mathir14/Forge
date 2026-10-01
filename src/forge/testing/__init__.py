"""Forge Testing Package (Tester v2).

Provides black-box empirical verification:
- Runtime Supervision (background dev server / daemon lifecycle)
- Interaction Engine & Pluggable Browser Drivers
- Intelligent Journey Planning & Prioritization
- Multi-modal Telemetry & Evidence Collection
- Explicit Coverage & Actionable QA Reporting
"""

from forge.testing.archetypes import ArchetypeDetector
from forge.testing.browser import (
    BrowserDriver,
    BrowserDriverFactory,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
)
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.engine import TesterEngine
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.planner import JourneyPlanner
from forge.testing.report import TesterReportGenerator
from forge.testing.supervisor import RuntimeSupervisor

__all__ = [
    "ArchetypeDetector",
    "BrowserDriver",
    "BrowserDriverFactory",
    "BudgetTracker",
    "CoverageReport",
    "Defect",
    "DefectCategory",
    "DefectSeverity",
    "EvidenceCollector",
    "Journey",
    "JourneyPlanner",
    "JourneyResult",
    "JourneyStep",
    "MockBrowserDriver",
    "PlaywrightBrowserDriver",
    "ProjectArchetype",
    "RuntimeSupervisor",
    "TesterEngine",
    "TesterReportGenerator",
    "TestingBudget",
]

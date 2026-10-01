"""Execution budget controls for Tester v2.

Enforces deterministic, cost-controlled exploratory and journey testing by capping:
- Maximum runtime (seconds)
- Maximum total journeys
- Maximum interactions per journey
- Maximum screenshots captured
- Maximum navigation depth
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TestingBudget:
    """Configurable testing budget limits."""
    __test__ = False
    max_runtime_seconds: float = 120.0
    max_journeys: int = 5
    max_interactions_per_journey: int = 15
    max_screenshots: int = 10
    max_navigation_depth: int = 4

    @classmethod
    def from_dict(cls, data: Optional[dict] = None) -> "TestingBudget":
        if not data:
            return cls()
        return cls(
            max_runtime_seconds=float(data.get("max_runtime_seconds", 120.0)),
            max_journeys=int(data.get("max_journeys", 5)),
            max_interactions_per_journey=int(data.get("max_interactions_per_journey", 15)),
            max_screenshots=int(data.get("max_screenshots", 10)),
            max_navigation_depth=int(data.get("max_navigation_depth", 4)),
        )


class BudgetTracker:
    """Tracks consumption against TestingBudget limits during execution."""

    def __init__(self, budget: Optional[TestingBudget] = None):
        self.budget = budget or TestingBudget()
        self.start_time = time.time()
        self.journeys_executed = 0
        self.total_interactions = 0
        self.screenshots_taken = 0
        self.navigation_depth = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def is_time_exhausted(self) -> bool:
        return self.elapsed_seconds >= self.budget.max_runtime_seconds

    @property
    def is_journeys_exhausted(self) -> bool:
        return self.journeys_executed >= self.budget.max_journeys

    def can_start_journey(self) -> bool:
        return not self.is_time_exhausted and not self.is_journeys_exhausted

    def can_interact(self, journey_interactions: int) -> bool:
        if self.is_time_exhausted:
            return False
        if journey_interactions >= self.budget.max_interactions_per_journey:
            return False
        return True

    def can_take_screenshot(self) -> bool:
        if self.is_time_exhausted:
            return False
        return self.screenshots_taken < self.budget.max_screenshots

    def can_navigate(self, depth: int) -> bool:
        if self.is_time_exhausted:
            return False
        return depth <= self.budget.max_navigation_depth

    def record_journey_started(self) -> None:
        self.journeys_executed += 1

    def record_interaction(self) -> None:
        self.total_interactions += 1

    def record_screenshot(self) -> None:
        self.screenshots_taken += 1

    def to_dict(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "journeys_executed": self.journeys_executed,
            "total_interactions": self.total_interactions,
            "screenshots_taken": self.screenshots_taken,
            "budget": {
                "max_runtime_seconds": self.budget.max_runtime_seconds,
                "max_journeys": self.budget.max_journeys,
                "max_interactions_per_journey": self.budget.max_interactions_per_journey,
                "max_screenshots": self.budget.max_screenshots,
                "max_navigation_depth": self.budget.max_navigation_depth,
            },
        }

"""Base interaction driver interface for Tester v2."""

from abc import ABC, abstractmethod
from typing import List

from forge.testing.budget import BudgetTracker
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import Journey, JourneyResult


class InteractionDriver(ABC):
    """Abstract interface for executing journeys on a target application archetype."""

    @abstractmethod
    def execute_journey(
        self,
        journey: Journey,
        budget: BudgetTracker,
        evidence: EvidenceCollector,
    ) -> JourneyResult:
        """Execute a single planned user journey against the live application."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Release underlying driver resources."""
        pass

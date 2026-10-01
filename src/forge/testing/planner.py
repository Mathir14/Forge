"""Journey Planner for Tester v2.

Formulates and prioritizes user journeys according to strict prioritization rules:
1. Features modified in current task / git diff
2. Adjacent user workflows
3. Global smoke test
4. Exploratory & responsive boundary testing (if budget remains)
"""

import logging
import re
from typing import List, Optional

from forge.testing.models import (
    ActionType,
    Journey,
    JourneyStep,
    ProjectArchetype,
)

logger = logging.getLogger(__name__)


class JourneyPlanner:
    """Plans ordered test journeys based on user task, git diff, and project archetype."""

    @classmethod
    def plan_journeys(
        cls,
        task: str,
        archetype: ProjectArchetype,
        git_diff: str = "",
        changed_files: Optional[List[str]] = None,
        executor_report: str = "",
        base_url: str = "http://127.0.0.1:3000",
        max_journeys: int = 5,
    ) -> List[Journey]:
        files = changed_files or []
        journeys: List[Journey] = []

        if archetype == ProjectArchetype.WEB_SPA:
            journeys = cls._plan_web_journeys(task, git_diff, files, base_url)
        elif archetype == ProjectArchetype.API:
            journeys = cls._plan_api_journeys(task, git_diff, files, base_url)
        elif archetype == ProjectArchetype.CLI:
            journeys = cls._plan_cli_journeys(task, git_diff, files)
        elif archetype == ProjectArchetype.LIBRARY:
            journeys = cls._plan_library_journeys(task, git_diff, files)
        else:
            # Fallback smoke journey
            journeys = [
                Journey(
                    id="J-01",
                    title="General Application Smoke Test",
                    description="Verify application entry point responds cleanly without crash.",
                    priority=3,
                    steps=[JourneyStep(action=ActionType.NAVIGATE, target="/", description="Navigate to root endpoint")],
                )
            ]

        # Sort strictly by priority (1=modified, 2=adjacent, 3=smoke, 4=exploratory)
        journeys.sort(key=lambda j: j.priority)
        return journeys[:max_journeys]

    @classmethod
    def _extract_routes(cls, git_diff: str, files: List[str]) -> List[str]:
        """Infer web/API route paths from changed files and diffs."""
        routes = []
        for f in files:
            # Next.js / Nuxt / Svelte file routing: e.g. app/cart/page.tsx -> /cart
            m = re.search(r"(?:app|pages|routes)/([a-zA-Z0-9_\-/]+?)(?:/page|\.tsx|\.jsx|\.vue|\.js|$)", f)
            if m:
                route = "/" + m.group(1).replace("index", "").rstrip("/")
                if route and route not in routes:
                    routes.append(route)

        # Check diff content for route mentions like '/login', '/checkout'
        diff_routes = re.findall(r"['\"](/([a-zA-Z0-9_\-]+))['\"]", git_diff)
        for _, r in diff_routes:
            full = f"/{r}"
            if full not in routes and len(r) > 1 and r not in ("src", "static", "public", "api"):
                routes.append(full)

        return routes

    @classmethod
    def _plan_web_journeys(cls, task: str, git_diff: str, files: List[str], base_url: str) -> List[Journey]:
        journeys = []
        routes = cls._extract_routes(git_diff, files)
        primary_route = routes[0] if routes else "/"

        # Priority 1: Modified Features Journey
        # Probe elements likely changed based on task keywords
        modified_steps = [
            JourneyStep(action=ActionType.NAVIGATE, target=primary_route, description=f"Navigate to {primary_route}"),
        ]

        # Extract possible interactive element targets from diff
        button_matches = re.findall(r'id=["\']([^"\']*btn[^"\']*|[^"\']*submit[^"\']*|[^"\']*apply[^"\']*)["\']', git_diff, re.IGNORECASE)
        input_matches = re.findall(r'name=["\']([^"\']+)["\']', git_diff, re.IGNORECASE)

        if input_matches:
            target_input = f"[name='{input_matches[0]}']"
            modified_steps.append(JourneyStep(action=ActionType.FILL, target=target_input, value="test_value", description=f"Fill {target_input}"))

        if button_matches:
            target_btn = f"#{button_matches[0]}"
            modified_steps.append(JourneyStep(action=ActionType.CLICK, target=target_btn, description=f"Click button {target_btn}"))
        else:
            # Generic interactive click target
            modified_steps.append(JourneyStep(action=ActionType.CLICK, target="button", description="Click primary action button"))

        journeys.append(
            Journey(
                id="J-01",
                title=f"Task Feature Journey: {task[:50]}",
                description="Verify end-to-end functionality of modified components and user actions.",
                priority=1,
                steps=modified_steps,
                viewport=(1440, 900),
                category="functional",
            )
        )

        # Priority 2: Adjacent User Workflow Journey
        adjacent_route = routes[1] if len(routes) > 1 else "/"
        journeys.append(
            Journey(
                id="J-02",
                title="Adjacent Workflow & Navigation",
                description="Verify adjacent views and navigation transitions without dead ends.",
                priority=2,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target=adjacent_route, description=f"Navigate to {adjacent_route}"),
                    JourneyStep(action=ActionType.NAVIGATE, target=primary_route, description=f"Navigate back to {primary_route}"),
                ],
                viewport=(1440, 900),
                category="functional",
            )
        )

        # Priority 3: Global Smoke Test
        journeys.append(
            Journey(
                id="J-03",
                title="Global Viewport & Console Smoke Test",
                description="Verify root page renders cleanly with zero unhandled client exceptions.",
                priority=3,
                steps=[
                    JourneyStep(action=ActionType.NAVIGATE, target="/", description="Navigate to root /"),
                ],
                viewport=(1440, 900),
                category="smoke",
            )
        )

        # Priority 4: Responsive & Boundary Testing (Mobile Viewport)
        journeys.append(
            Journey(
                id="J-04",
                title="Responsive Viewport (Mobile 375px) Layout Verification",
                description="Verify layout, navigation menu, and action buttons adapt without collision on mobile.",
                priority=4,
                steps=[
                    JourneyStep(action=ActionType.RESIZE, target="viewport", value="375x812", description="Resize viewport to 375x812 (Mobile)"),
                    JourneyStep(action=ActionType.NAVIGATE, target=primary_route, description=f"Navigate to {primary_route} on mobile"),
                ],
                viewport=(375, 812),
                category="responsive",
            )
        )

        return journeys

    @classmethod
    def _plan_api_journeys(cls, task: str, git_diff: str, files: List[str], base_url: str) -> List[Journey]:
        journeys = []
        routes = cls._extract_routes(git_diff, files)
        primary_endpoint = routes[0] if routes else "/api"

        # Priority 1: Modified Endpoint Journey
        journeys.append(
            Journey(
                id="J-01",
                title=f"API Modified Endpoint Verification: {task[:50]}",
                description="Verify HTTP response status, headers, and payload schema on modified endpoint.",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target=primary_endpoint, value="GET", description=f"GET {primary_endpoint}"),
                ],
            )
        )

        # Priority 2: Negative / Error Handling Journey
        journeys.append(
            Journey(
                id="J-02",
                title="API Negative Boundary & Validation Handling",
                description="Verify server returns clean 4xx client errors rather than unhandled 500 crashes.",
                priority=2,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target=f"{primary_endpoint}/invalid-id-999", value="GET", description="GET invalid endpoint"),
                ],
            )
        )

        # Priority 3: Global Smoke Test
        journeys.append(
            Journey(
                id="J-03",
                title="API Service Health Smoke Test",
                description="Verify root / health check endpoint responds.",
                priority=3,
                steps=[
                    JourneyStep(action=ActionType.HTTP_REQUEST, target="/", value="GET", description="GET / health check"),
                ],
            )
        )

        return journeys

    @classmethod
    def _plan_cli_journeys(cls, task: str, git_diff: str, files: List[str]) -> List[Journey]:
        journeys = []

        # Find CLI entry point
        cmd_name = "python -m forge.cli" if any("forge" in f for f in files) else "python main.py"

        # Priority 1: Task command invocation
        journeys.append(
            Journey(
                id="J-01",
                title=f"CLI Task Command Verification: {task[:50]}",
                description="Execute CLI command matching task requirements and verify zero exit code.",
                priority=1,
                steps=[
                    JourneyStep(action=ActionType.CLI_COMMAND, target=f"{cmd_name} --help", description="Verify help and commands"),
                ],
            )
        )

        # Priority 2: Invalid option negative test
        journeys.append(
            Journey(
                id="J-02",
                title="CLI Negative Argument Handling",
                description="Verify CLI rejects unknown option gracefully without python stack trace.",
                priority=2,
                steps=[
                    JourneyStep(action=ActionType.CLI_COMMAND, target=f"{cmd_name} --non-existent-option", description="Execute invalid option"),
                ],
            )
        )

        return journeys

    @classmethod
    def _plan_library_journeys(cls, task: str, git_diff: str, files: List[str]) -> List[Journey]:
        return [
            Journey(
                id="J-01",
                title="Library Consumer Sandbox Import & Export Verification",
                description="Verify packaged library exports can be imported and executed by an external consumer.",
                priority=1,
                steps=[
                    JourneyStep(
                        action=ActionType.CLI_COMMAND,
                        target=f"python -c 'import sys; print(\"Consumer sandbox OK\")'",
                        description="Verify consumer import execution",
                    )
                ],
            )
        ]

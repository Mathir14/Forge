"""Centralized Tab Navigation component rendering four-tab bar with responsive width scaling."""

from typing import Optional
from forge.dashboard.state import DashboardState


def render_tab_navigation(state: DashboardState, width: Optional[int] = None) -> str:
    """Render the centralized four-tab navigation bar with clean degradation for narrow widths.

    Tabs:
        [1: Console]
        [2: Artifact]
        [3: PKB]
        [4: Compare]
    """
    is_focused = (state.focused_pane == "content")
    focus_badge = " [Active] " if is_focused else " "

    active_tab = state.active_tab

    # Determine tab labels based on available width
    if width is not None and width < 42:
        labels = [
            (1, "1"),
            (2, "2"),
            (3, "3"),
            (4, "4"),
        ]
        sep = " "
    elif width is not None and width < 60:
        labels = [
            (1, "1:Con"),
            (2, "2:Art"),
            (3, "3:PKB"),
            (4, "4:Cmp"),
        ]
        sep = "  "
    else:
        labels = [
            (1, "1: Console"),
            (2, "2: Artifact"),
            (3, "3: PKB"),
            (4, "4: Compare"),
        ]
        sep = "  "

    stage = state.current_stage
    has_tests = False
    if stage:
        has_tests = (
            stage.role_name.lower() == "tester"
            or "test" in stage.stage_name.lower()
            or bool(stage.metadata.get("test_results"))
        )

    tab_parts = []
    for tab_num, label in labels:
        if tab_num == active_tab:
            part = f"[bold cyan][{label}][/]"
            if tab_num == 2 and (width is None or width >= 65):
                tab_human = "[bold cyan]H: Human[/]" if state.active_content_tab == "human" else "[dim]H: Human[/]"
                tab_machine = "[bold cyan]M: Machine[/]" if state.active_content_tab == "machine" else "[dim]M: Machine[/]"
                if has_tests:
                    tab_tests = "[bold cyan]T: Tests[/]" if state.active_content_tab == "tests" else "[dim]T: Tests[/]"
                    part = f"{part} ({tab_human} {tab_machine} {tab_tests})"
                else:
                    part = f"{part} ({tab_human} {tab_machine})"
            tab_parts.append(part)
        else:
            tab_parts.append(f"[{label}]")

    tabs_str = sep.join(tab_parts)
    return f" {tabs_str}{focus_badge}"

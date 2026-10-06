"""Centralized Tab Navigation component rendering five-tab bar with responsive width scaling."""

from typing import Optional
from forge.dashboard.state import DashboardState


def render_tab_navigation(state: DashboardState, width: Optional[int] = None) -> str:
    """Render the centralized five-tab navigation bar with clean degradation for narrow widths.

    Tabs:
        [1: Console]
        [2: Artifact]
        [3: Tester]
        [4: PKB]
        [5: Compare]
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
            (5, "5"),
        ]
        sep = " "
    elif width is not None and width < 60:
        labels = [
            (1, "1:Con"),
            (2, "2:Art"),
            (3, "3:Tst"),
            (4, "4:PKB"),
            (5, "5:Cmp"),
        ]
        sep = "  "
    else:
        labels = [
            (1, "1: Console"),
            (2, "2: Artifact"),
            (3, "3: Tester"),
            (4, "4: PKB"),
            (5, "5: Compare"),
        ]
        sep = "  "

    tab_parts = []
    for tab_num, label in labels:
        if tab_num == active_tab:
            part = f"[bold cyan][{label}][/]"
            if tab_num == 2 and (width is None or width >= 75):
                tab_human = "[bold cyan]H: Human[/]" if state.active_content_tab == "human" else "[dim]H: Human[/]"
                tab_machine = "[bold cyan]M: Machine[/]" if state.active_content_tab == "machine" else "[dim]M: Machine[/]"
                part = f"{part} ({tab_human} {tab_machine})"
            tab_parts.append(part)
        else:
            tab_parts.append(f"[{label}]")

    tabs_str = sep.join(tab_parts)
    return f" {tabs_str}{focus_badge}"

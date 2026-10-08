"""Footer component rendering keybinding hints and navigation cues."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState
from forge.stages.definition import StageOrder


def render_footer(state: DashboardState) -> Panel:
    """Render the bottom bar with keybinding hints and focus status."""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=1, justify="right")

    hints = Text()
    hints.append("[1-5] ", style="bold cyan")
    hints.append("Tabs  ", style="dim")

    hints.append("[Tab] ", style="bold cyan")
    hints.append("Focus  ", style="dim")

    hints.append("[↑/↓] ", style="bold cyan")
    if state.focused_pane == "timeline":
        hints.append("Stage  ", style="dim")
    else:
        hints.append("Scroll  ", style="dim")

    hints.append("[PgUp/Dn] ", style="bold cyan")
    hints.append("Page  ", style="dim")

    hints.append("[Q] ", style="bold cyan")
    hints.append("Quit", style="dim")

    status = Text()
    pane_name = "SIDEBAR" if state.focused_pane == "timeline" else "CONTENT"
    status.append(f"Focus: {pane_name}  ", style="bold yellow")
    if state.terminal_status:
        term_stat = state.terminal_status.upper()
        if StageOrder.is_success_status(term_stat):
            mode_text = " COMPLETED "
            mode_style = "bold white on green"

        elif term_stat in ("CANCELLED", "ABORTED"):
            mode_text = " CANCELLED "
            mode_style = "bold white on yellow"
        elif term_stat == "INCOMPLETE":
            mode_text = " INCOMPLETE "
            mode_style = "bold white on red"
        else:
            mode_text = " HALTED "
            mode_style = "bold white on red"
    else:
        mode_text = " LIVE ATTACH " if state.run.is_active else " OBSERVER "
        mode_style = "bold black on green" if state.run.is_active else "bold white on blue"
    status.append(mode_text, style=mode_style)

    grid.add_row(hints, status)

    return Panel(
        grid,
        style="dim white",
        border_style="dim",
        padding=(0, 1),
    )

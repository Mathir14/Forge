"""Footer component rendering keybinding hints and navigation cues."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState


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

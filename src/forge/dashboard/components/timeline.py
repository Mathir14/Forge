"""Timeline component rendering stage progression and status badges."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState
from forge.stages.definition import StageOrder


def render_timeline(state: DashboardState) -> Panel:
    """Render the stage progression timeline in the left pane."""
    table = Table(
        show_header=False,
        box=None,
        padding=(0, 0),
        expand=True,
    )
    table.add_column("Indicator", justify="left", width=2)
    table.add_column("Stage", ratio=1, no_wrap=True)
    table.add_column("Duration", justify="right", width=6)

    for idx, stage in enumerate(state.run.stages):
        is_selected = (idx == state.selected_stage_index)
        status = stage.display_status

        # Determine icon and color
        if StageOrder.is_success_status(status, stage.role_name):
            icon = Text("✓", style="bold green")
            status_style = "green"
        elif status in ("FAILED", "CHANGES_REQUIRED", "BLOCKED", "ERROR", "REJECTED", "FAIL"):
            icon = Text("!", style="bold red")
            status_style = "red"
        elif status in ("RUNNING", "IN_PROGRESS"):
            icon = Text("▶", style="bold yellow")
            status_style = "yellow"
        else:
            icon = Text("○", style="dim")
            status_style = "dim"

        # Format duration
        if stage.duration_seconds > 0:
            dur_str = f"{int(stage.duration_seconds)}s"
        else:
            dur_str = "--"

        # Stage label
        stage_text = Text()
        if is_selected:
            prefix = "> " if state.focused_pane == "timeline" else "• "
            stage_text.append(prefix, style="bold cyan")
            stage_text.append(stage.stage_name, style=f"bold {status_style} underline")
        else:
            stage_text.append("  ")
            stage_text.append(stage.stage_name, style=status_style)

        if stage.attempts:
            stage_text.append(f" ({len(stage.attempts)} att)", style="dim")

        table.add_row(icon, stage_text, Text(dur_str, style="dim"))

    border_color = "cyan" if state.focused_pane == "timeline" else "dim"
    title_text = " Stages [Active] " if state.focused_pane == "timeline" else " Stages "

    return Panel(
        table,
        title=title_text,
        title_align="left",
        border_style=border_color,
        style="white",
    )

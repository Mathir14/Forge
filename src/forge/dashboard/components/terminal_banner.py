"""Terminal banner component displayed when the autonomous pipeline finishes or halts."""

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState


def render_terminal_banner(state: DashboardState) -> Panel:
    """Render a prominent banner indicating final pipeline state and the pipeline stage summary."""
    status = (state.terminal_status or state.run.status or "UNKNOWN").upper()
    from forge.stages.definition import StageOrder
    is_success = StageOrder.is_success_status(status)
    is_cancelled = status in ("CANCELLED", "ABORTED")

    grid = Table.grid(expand=True)
    grid.add_column(ratio=4)
    grid.add_column(ratio=1, justify="right")

    left = Text()
    if is_success:
        left.append("  ✅ PIPELINE COMPLETED  ", style="bold white on green")
        left.append("  ")
        left.append("All stages completed successfully.", style="bold green")
    elif is_cancelled:
        left.append("  ⚠️ PIPELINE CANCELLED  ", style="bold white on yellow")
        left.append("  ")
        left.append("Execution was cancelled by user.", style="bold yellow")
    else:
        left.append("  ⛔ PIPELINE FAILED  ", style="bold white on red")
        left.append("  ")
        stage_label = state.terminal_stage or "Stage"
        reason = state.terminal_reason or f"Halted with status '{status}'"
        left.append(f"{stage_label} — ", style="bold red")
        left.append(reason, style="bold yellow")

    right = Text()
    right.append("Press ", style="dim")
    right.append("[Q]", style="bold white on cyan")
    right.append(" to exit", style="bold cyan")

    grid.add_row(left, right)

    summary = state.get_run_summary()
    summary_renderable = summary.format_rich()

    panel_content = Group(
        grid,
        Text(""),
        summary_renderable,
    )

    border_style = "green" if is_success else ("yellow" if is_cancelled else "red")
    return Panel(
        panel_content,
        style="white",
        border_style=border_style,
        padding=(0, 1),
    )

"""Header component rendering run metadata, lifecycle status, and metrics."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState
from forge.stages.definition import StageOrder


def render_header(state: DashboardState) -> Panel:
    """Render the top banner displaying run ID, task, status badge, and duration."""
    run = state.run

    # Determine status badge styling
    status = (run.status or "UNKNOWN").upper()
    if StageOrder.is_success_status(status):
        status_text = Text(f" {status} ", style="bold white on green")
    elif status in ("FAILED", "CHANGES_REQUIRED", "BLOCKED", "ERROR", "REJECTED", "FAIL"):
        status_text = Text(f" {status} ", style="bold white on red")
    elif status in ("IN_PROGRESS", "RUNNING"):
        status_text = Text(" RUNNING ", style="bold black on yellow")
    else:
        status_text = Text(f" {status} ", style="bold white on blue")

    # Format timing and stage counts
    mins = int(run.total_duration_seconds // 60)
    secs = int(run.total_duration_seconds % 60)
    duration_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"

    completed_stages = sum(
        1 for s in run.stages
        if StageOrder.is_success_status(s.status, s.role_name) or s.status in ("CHANGES_REQUIRED", "FAILED", "BLOCKED", "REJECTED", "FAIL")
    )
    total_stages = len(run.stages)
    stages_str = f"{completed_stages}/{total_stages} completed"

    # Truncate task
    task_clean = (run.task or "No task description").strip().splitlines()[0]
    if len(task_clean) > 85:
        task_clean = task_clean[:82] + "..."

    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(ratio=1, justify="right")

    left_text = Text()
    left_text.append("⚡ FORGE ", style="bold cyan")
    left_text.append(f"[{run.run_id}] ", style="bold yellow")
    left_text.append(f"\"{task_clean}\"", style="italic")

    right_text = Text()
    right_text.append(f"⏳ {duration_str}  ", style="dim")
    right_text.append(f"📊 {stages_str}  ", style="dim")
    right_text.append_text(status_text)

    grid.add_row(left_text, right_text)

    return Panel(
        grid,
        style="dim white",
        border_style="cyan",
        padding=(0, 1),
    )

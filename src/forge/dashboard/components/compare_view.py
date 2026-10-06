from pathlib import Path
from typing import Optional, List
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.model import RunModel
from forge.dashboard.state import DashboardState
from forge.dashboard.components.navigation import render_tab_navigation
from forge.storage.run_manager import RunManager


def render_compare_view(
    state: DashboardState,
    project_root: Optional[Path] = None,
    max_lines: int = 35,
) -> Panel:
    """Render side-by-side run comparison of stage durations, retry counts, and statuses."""
    is_focused = (state.focused_pane == "content")
    border_color = "cyan" if is_focused else "dim"
    title_markup = render_tab_navigation(state)

    run_a = state.run

    # Resolve Run B (comparison target) from actual project root
    if not state.compare_run_model:
        root = project_root or getattr(state, "project_root", None)
        if root is None:
            rd = run_a.run_dir
            if rd.parent.name == "runs" and rd.parent.parent.name == ".forge":
                root = rd.parent.parent.parent
            elif rd.parent.name == ".forge":
                root = rd.parent.parent
            else:
                root = rd.parent.parent
        run_mgr = RunManager(root)
        available_runs = run_mgr.list_runs()
        # Pick the most recent run that is not Run A
        for r in reversed(available_runs):
            if r.run_id != run_a.run_id:
                try:
                    state.compare_run_model = RunModel.from_dir(r.run_dir)
                    state.compare_run_id = r.run_id
                    break
                except Exception:
                    continue

    run_b = state.compare_run_model
    if not run_b:
        body = Text(
            f"\nNo other historical runs found to compare with '{run_a.run_id}'.\n"
            "At least two runs are required for comparative analysis.\n",
            style="dim italic",
        )
        return Panel(
            body,
            title=title_markup,
            title_align="left",
            border_style=border_color,
            style="white",
        )


    # 1. High-level Summary Comparison Table
    summary_table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 2),
        expand=True,
    )
    summary_table.add_column("Metric", style="bold white", width=18)
    summary_table.add_column(f"Run A ({run_a.run_id})", ratio=1)
    summary_table.add_column(f"Run B ({run_b.run_id})", ratio=1)
    summary_table.add_column("Delta (A vs B)", justify="right", width=16)

    dur_a = run_a.total_duration_seconds
    dur_b = run_b.total_duration_seconds
    delta_dur = dur_a - dur_b
    delta_sign = "+" if delta_dur > 0 else ""
    delta_style = "red" if delta_dur > 0 else "green"

    summary_table.add_row(
        "Status",
        Text(run_a.status.upper(), style="bold green" if run_a.status == "APPROVED" else "bold red"),
        Text(run_b.status.upper(), style="bold green" if run_b.status == "APPROVED" else "bold red"),
        Text("DIVERGENT" if run_a.status != run_b.status else "IDENTICAL", style="yellow" if run_a.status != run_b.status else "dim"),
    )
    summary_table.add_row(
        "Total Duration",
        f"{dur_a:.1f}s",
        f"{dur_b:.1f}s",
        Text(f"{delta_sign}{delta_dur:.1f}s", style=delta_style),
    )
    summary_table.add_row(
        "Stages Completed",
        str(len(run_a.stages)),
        str(len(run_b.stages)),
        f"{len(run_a.stages) - len(run_b.stages):+d}",
    )

    # 2. Stage Breakdown Table
    stage_table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 1),
        expand=True,
    )
    stage_table.add_column("Stage", ratio=2)
    stage_table.add_column("Run A Status", width=14)
    stage_table.add_column("Run B Status", width=14)
    stage_table.add_column("Run A Dur", justify="right", width=10)
    stage_table.add_column("Run B Dur", justify="right", width=10)
    stage_table.add_column("Time Delta", justify="right", width=12)

    # Union of stage names
    all_stage_names = sorted(list(set(run_a.stage_names + run_b.stage_names)))
    for s_name in all_stage_names[:max_lines]:
        sa = run_a.get_stage(s_name)
        sb = run_b.get_stage(s_name)

        sa_status = sa.display_status if sa else "MISSING"
        sb_status = sb.display_status if sb else "MISSING"

        sa_dur = sa.duration_seconds if sa else 0.0
        sb_dur = sb.duration_seconds if sb else 0.0
        delta = sa_dur - sb_dur
        delta_str = f"{delta:+.1f}s" if (sa and sb) else "--"
        d_color = "red" if delta > 2.0 else ("green" if delta < -2.0 else "dim")

        stage_table.add_row(
            s_name,
            Text(sa_status, style="green" if sa_status == "APPROVED" else ("dim" if sa_status == "MISSING" else "yellow")),
            Text(sb_status, style="green" if sb_status == "APPROVED" else ("dim" if sb_status == "MISSING" else "yellow")),
            f"{sa_dur:.1f}s" if sa else "--",
            f"{sb_dur:.1f}s" if sb else "--",
            Text(delta_str, style=d_color),
        )

    content_grid = Table.grid(expand=True)
    content_grid.add_column()
    content_grid.add_row(summary_table)
    content_grid.add_row(Text("─" * 60, style="dim"))
    content_grid.add_row(stage_table)

    return Panel(
        content_grid,
        title=title_markup,
        title_align="left",
        border_style=border_color,
        style="white",
    )

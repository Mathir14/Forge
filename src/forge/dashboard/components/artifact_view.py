"""Artifact View component rendering Human and Machine Reports."""

from typing import List, Optional
from rich.console import RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState
from forge.dashboard.components.navigation import render_tab_navigation
from forge.dashboard.components.viewport import VirtualViewport


def render_artifact_view(state: DashboardState, max_lines: int = 40) -> Panel:
    """Render the central artifact viewport (Human Report or Machine Report)."""
    title_markup = render_tab_navigation(state)
    stage = state.current_stage
    if not stage:
        return Panel(
            Text("No stage selected.", style="dim italic"),
            title=title_markup,
            title_align="left",
            border_style="dim",
            style="white",
        )

    is_focused = (state.focused_pane == "content")
    border_color = "cyan" if is_focused else "dim"

    if state.active_content_tab == "human":
        body = _render_human_report(stage, state.scroll_offset, max_lines, state=state)
    elif state.active_content_tab == "tests":
        from forge.dashboard.components.tester_view import render_tester_content
        body = render_tester_content(stage, max_lines=max_lines, state=state)
    else:
        body = _render_machine_report(stage, state.scroll_offset, max_lines, state=state)

    return Panel(
        body,
        title=title_markup,
        title_align="left",
        border_style=border_color,
        style="white",
    )



def _render_human_report(stage, scroll_offset: int = 0, max_lines: int = 40, state: Optional[DashboardState] = None) -> RenderableType:
    content = stage.human_report or stage.raw_content
    if not content or not content.strip():
        if stage.status == "PENDING":
            return Text("\nStage has not executed yet. Waiting for upstream handoff...\n", style="dim italic")
        return Text("\nNo markdown artifact found for this stage.\n", style="yellow italic")

    try:
        renderable = Markdown(content)
    except Exception:
        renderable = Text(content)

    return VirtualViewport(
        renderable,
        scroll_offset=scroll_offset,
        max_lines=max_lines,
        state=state,
        cache_key=f"artifact_human_{stage.stage_name}_{id(content)}",
        indicator_label="Lines",
    )


def _render_machine_report(stage, scroll_offset: int = 0, max_lines: int = 40, state: Optional[DashboardState] = None) -> RenderableType:
    mr = stage.machine_report
    if not mr:
        return Text(
            f"\nNo Machine Report found for stage '{stage.stage_name}'.\n"
            "The stage may be pending or completed without emitting structured YAML protocol data.",
            style="yellow italic",
        )

    # 1. Properties Table
    grid = Table.grid(padding=(0, 2))
    grid.add_column("Key", style="bold cyan", width=20)
    grid.add_column("Value", ratio=1)

    status_style = "bold green" if (mr.status in ("APPROVED", "VERIFIED", "SUCCESS", "PASS", "READY", "CRITIQUE_COMPLETE") and mr.is_valid) else "bold red"
    grid.add_row("ROLE:", Text(stage.role_name.upper()))
    grid.add_row("STATUS:", Text(mr.status, style=status_style))
    if not mr.is_valid and mr.validation_errors:
        grid.add_row("VALIDATION ERRORS:", Text("; ".join(mr.validation_errors), style="bold red"))
    grid.add_row("HANDOFF:", Text(mr.handoff or "NONE", style="yellow"))
    if mr.confidence:
        grid.add_row("CONFIDENCE:", Text(mr.confidence))
    if stage.exit_code is not None:
        exit_style = "green" if stage.exit_code == 0 else "bold red"
        grid.add_row("EXIT CODE:", Text(str(stage.exit_code), style=exit_style))
    if stage.duration_seconds > 0:
        grid.add_row("DURATION:", Text(f"{stage.duration_seconds:.2f}s"))

    if mr.reason:
        grid.add_row("REASON:", Text(mr.reason, style="italic"))
    if mr.next_action:
        grid.add_row("NEXT ACTION:", Text(mr.next_action))

    # Issues breakdown
    if mr.issues:
        issues_text = Text()
        for severity, items in mr.issues.items():
            color = "red" if severity.upper() == "CRITICAL" else ("yellow" if severity.upper() == "MAJOR" else "dim")
            for item in items:
                issues_text.append(f"• [{severity}] {item}\n", style=color)
        grid.add_row("ISSUES:", issues_text)

    # 2. Syntax-highlighted YAML block
    outer_table = Table.grid(expand=True)
    outer_table.add_column()
    outer_table.add_row(grid)

    if mr.raw_yaml:
        outer_table.add_row(Text("\nRaw Machine Protocol Block:", style="bold dim"))
        syntax = Syntax(mr.raw_yaml, "yaml", theme="monokai", line_numbers=True)
        outer_table.add_row(syntax)

    return VirtualViewport(
        outer_table,
        scroll_offset=scroll_offset,
        max_lines=max_lines,
        state=state,
        indicator_label="Lines",
    )

"""Console View component rendering live AgentEvent streams and console logs."""

from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState


def render_console_view(state: DashboardState, max_lines: int = 35) -> Panel:
    """Render the live streaming console log and tool execution events."""
    logs = state.run.console_logs
    is_focused = (state.focused_pane == "content")
    border_color = "cyan" if is_focused else "dim"

    if not logs:
        body = Text(
            "\nNo live streaming events received yet.\n"
            "Waiting for active stage execution or provider stream chunks...\n",
            style="dim italic",
        )
    else:
        total_lines = len(logs)
        # Auto-scroll to tail if scroll_offset == 0, else show slice from tail - offset
        if state.scroll_offset == 0:
            start = max(0, total_lines - max_lines)
            end = total_lines
        else:
            end = max(max_lines, total_lines - state.scroll_offset)
            start = max(0, end - max_lines)

        visible_logs = logs[start:end]
        text = Text()

        # Position cue if scrolled
        if total_lines > max_lines:
            text.append(f"[Lines {start + 1}–{end} of {total_lines}] (Auto-scroll: {'OFF' if state.scroll_offset > 0 else 'ON'})\n\n", style="dim")

        for line in visible_logs:
            if line.startswith("▶"):
                text.append(line + "\n", style="bold cyan")
            elif line.startswith("✓"):
                text.append(line + "\n", style="bold green")
            elif line.startswith("✗"):
                text.append(line + "\n", style="bold red")
            elif "TOOL_START" in line:
                text.append(line + "\n", style="bold yellow")
            elif "TOOL_FINISH" in line:
                text.append(line + "\n", style="green")
            else:
                text.append(line + "\n")
        body = text

    focus_badge = " [Active] " if is_focused else " "
    title_markup = f" [bold cyan][1: Console][/]  [2: Artifact]  [3: Tester]  [4: PKB]  [5: Compare]{focus_badge}"

    return Panel(
        body,
        title=title_markup,
        title_align="left",
        border_style=border_color,
        style="white",
    )

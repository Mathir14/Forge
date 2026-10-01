"""Tester View component rendering test matrix, journeys, behavioral evidence, and stack traces."""

import re
from typing import List, Dict, Any, Optional
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax
from forge.dashboard.state import DashboardState


def render_tester_view(state: DashboardState, max_lines: int = 35) -> Panel:
    """Render the test execution matrix, behavioral evidence, and failure inspector."""
    is_focused = (state.focused_pane == "content")
    border_color = "cyan" if is_focused else "dim"

    # Find tester stage
    tester_stage = state.run.get_stage("tester") or state.run.get_stage("04_tester")
    if not tester_stage:
        for s in state.run.stages:
            if "test" in s.stage_name.lower():
                tester_stage = s
                break

    if not tester_stage or tester_stage.status == "PENDING":
        body = Text(
            "\nNo tester evidence available for this run.\n"
            "The Tester stage has not executed yet or was skipped.\n",
            style="dim italic",
        )
        return Panel(
            body,
            title=" [3: Tester & Evidence] ",
            title_align="left",
            border_style=border_color,
            style="white",
        )

    # Parse test matrix and journeys from tester metadata and markdown
    summary, journeys, failure_details = _extract_tester_data(tester_stage)

    # 1. Summary Cards Table
    summary_table = Table.grid(padding=(0, 2), expand=True)
    summary_table.add_column(ratio=1)
    summary_table.add_column(ratio=1)
    summary_table.add_column(ratio=1)
    summary_table.add_column(ratio=1)

    status = tester_stage.status.upper()
    status_style = "bold green" if status in ("APPROVED", "VERIFIED", "PASSED", "SUCCESS") else "bold red"

    summary_table.add_row(
        Text(f"STATUS: {status}", style=status_style),
        Text(f"TOTAL: {summary['total']}", style="bold white"),
        Text(f"PASSED: {summary['passed']}", style="green"),
        Text(f"FAILED: {summary['failed']}", style="red" if summary["failed"] > 0 else "dim"),
    )

    # 2. Journeys Table
    matrix_table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 1),
        expand=True,
    )
    matrix_table.add_column("Result", width=8)
    matrix_table.add_column("Test / Journey", ratio=3)
    matrix_table.add_column("Duration", justify="right", width=8)

    if journeys:
        for j in journeys[:max_lines]:
            j_status = j["status"].upper()
            if j_status in ("PASS", "PASSED"):
                res_text = Text("✓ PASS", style="bold green")
            elif j_status in ("FAIL", "FAILED"):
                res_text = Text("✗ FAIL", style="bold red")
            else:
                res_text = Text("○ SKIP", style="dim")
            matrix_table.add_row(res_text, j["name"], f"{j.get('duration', 0):.2f}s")
    else:
        # Fall back to raw report excerpt
        excerpt = (tester_stage.human_report or tester_stage.raw_content)[:1200]
        matrix_table.add_row(Text("REPORT", style="bold yellow"), excerpt, f"{tester_stage.duration_seconds:.1f}s")

    # Combine into view
    content_grid = Table.grid(expand=True)
    content_grid.add_column()
    content_grid.add_row(summary_table)
    content_grid.add_row(Text("─" * 60, style="dim"))
    content_grid.add_row(matrix_table)

    if failure_details:
        content_grid.add_row(Text("\nFailure Inspector & Evidence:", style="bold red"))
        content_grid.add_row(Text(failure_details[:1000], style="dim red"))

    focus_badge = " [Active] " if is_focused else " "
    title_markup = f" [1: Console]  [2: Artifact]  [bold cyan][3: Tester][/]  [4: PKB]  [5: Compare]{focus_badge}"

    return Panel(
        content_grid,
        title=title_markup,
        title_align="left",
        border_style=border_color,
        style="white",
    )


def _extract_tester_data(stage) -> tuple[Dict[str, Any], List[Dict[str, Any]], str]:
    """Extract structured test summary and journey items from stage artifacts."""
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    journeys = []
    failure_details = ""

    # Check stage metadata
    test_meta = stage.metadata.get("test_results") or stage.metadata.get("data", {}).get("test_results")
    if isinstance(test_meta, dict):
        summary["total"] = test_meta.get("total", 0)
        summary["passed"] = test_meta.get("passed", 0)
        summary["failed"] = test_meta.get("failed", 0)
        summary["skipped"] = test_meta.get("skipped", 0)
        for item in test_meta.get("tests", []):
            if isinstance(item, dict):
                journeys.append({
                    "name": item.get("name", "Unknown test"),
                    "status": item.get("status", "PASS"),
                    "duration": item.get("duration", 0.0),
                })

    # Heuristic extraction from markdown if structured json tests are empty
    content = stage.human_report or stage.raw_content
    if not journeys and content:
        # Match lines like "test_something PASSED [0.12s]" or "PASSED tests/test_foo.py::test_bar"
        pass_matches = re.findall(r"(?:PASSED\s+([a-zA-Z0-9_\-\.\:\/]+)|([a-zA-Z0-9_\-\.\:\/]+)\s+PASSED)", content)
        for m in pass_matches:
            name = m[0] or m[1]
            journeys.append({"name": name, "status": "PASS", "duration": 0.05})
            summary["passed"] += 1

        fail_matches = re.findall(r"(?:FAILED\s+([a-zA-Z0-9_\-\.\:\/]+)|([a-zA-Z0-9_\-\.\:\/]+)\s+FAILED)", content)
        for m in fail_matches:
            name = m[0] or m[1]
            journeys.append({"name": name, "status": "FAIL", "duration": 0.05})
            summary["failed"] += 1

        summary["total"] = summary["passed"] + summary["failed"]

        # Extract FAILURES block if present
        if "=== FAILURES ===" in content or "FAILURES" in content:
            parts = content.split("FAILURES")
            if len(parts) > 1:
                failure_details = parts[1][:1500]

    return summary, journeys, failure_details

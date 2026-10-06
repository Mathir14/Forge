"""Project Knowledge Base (PKB) View component rendering repository facts, filters, and diffs."""

from pathlib import Path
from typing import Optional, Dict, Any, List
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from forge.dashboard.state import DashboardState
from forge.dashboard.components.navigation import render_tab_navigation
from forge.storage.knowledge import KnowledgeStore
from forge.core.knowledge import KnowledgeFact


def render_pkb_view(state: DashboardState, project_root: Optional[Path] = None, max_lines: int = 35) -> Panel:
    """Render the Project Knowledge Base facts, filters, and status badges."""
    is_focused = (state.focused_pane == "content")
    border_color = "cyan" if is_focused else "dim"
    title_markup = render_tab_navigation(state)

    root = project_root or getattr(state, "project_root", None) or Path.cwd()
    store = KnowledgeStore(root)

    try:
        all_facts = store.load_all()
    except Exception:
        all_facts = {}

    if not all_facts:
        body = Text(
            "\nNo Project Knowledge Base (PKB) facts found in .forge/knowledge/.\n"
            "Facts are populated automatically during pipeline audits and verified stages.\n",
            style="dim italic",
        )
        return Panel(
            body,
            title=title_markup,
            title_align="left",
            border_style=border_color,
            style="white",
        )


    # Apply filters
    facts = list(all_facts.values())
    if state.pkb_type_filter:
        facts = [f for f in facts if f.type.lower() == state.pkb_type_filter.lower()]
    if state.pkb_status_filter:
        facts = [f for f in facts if f.status.upper() == state.pkb_status_filter.upper()]

    # Filter bar
    filter_bar = Text()
    filter_bar.append("Type: ", style="bold cyan")
    filter_bar.append(f"[{state.pkb_type_filter or 'ALL'}]  ", style="bold yellow")
    filter_bar.append("Status: ", style="bold cyan")
    filter_bar.append(f"[{state.pkb_status_filter or 'ALL'}]  ", style="bold yellow")
    filter_bar.append(f"Total: {len(facts)}/{len(all_facts)} facts", style="dim")

    # Facts Table
    table = Table(
        show_header=True,
        header_style="bold cyan",
        box=None,
        padding=(0, 1),
        expand=True,
    )
    table.add_column("Status", width=14)
    table.add_column("Type", width=14)
    table.add_column("ID / Title", ratio=2)
    table.add_column("Confidence", justify="right", width=10)

    # Slicing
    start = min(state.scroll_offset, max(0, len(facts) - 1))
    end = min(len(facts), start + max_lines)

    for fact in facts[start:end]:
        # Status styling
        st = fact.status.upper()
        if st == "VERIFIED":
            st_text = Text("● VERIFIED", style="bold green")
        elif st == "PROVISIONAL":
            st_text = Text("○ PROVISIONAL", style="bold yellow")
        elif st == "DISPUTED":
            st_text = Text("▲ DISPUTED", style="bold red")
        elif st == "HUMAN_LOCKED":
            st_text = Text("🔒 LOCKED", style="bold magenta")
        else:
            st_text = Text(f"  {st}", style="dim")

        # Type badge
        type_str = f"[{fact.type.upper()}]"

        # Title
        title_text = Text()
        title_text.append(fact.id, style="bold cyan")
        title_text.append(f": {fact.title}", style="white")

        # Confidence from provenance
        conf = fact.provenance.get("confidence") if isinstance(fact.provenance, dict) else None
        conf_str = f"{float(conf):.1f}" if conf is not None else "--"

        table.add_row(st_text, Text(type_str, style="dim yellow"), title_text, conf_str)

    content_grid = Table.grid(expand=True)
    content_grid.add_column()
    content_grid.add_row(filter_bar)
    content_grid.add_row(Text("─" * 60, style="dim"))
    content_grid.add_row(table)

    return Panel(
        content_grid,
        title=title_markup,
        title_align="left",
        border_style=border_color,
        style="white",
    )

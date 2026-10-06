"""UI Components for the Forge Terminal Dashboard."""

from forge.dashboard.components.header import render_header
from forge.dashboard.components.timeline import render_timeline
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.console_view import render_console_view
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.components.pkb_view import render_pkb_view
from forge.dashboard.components.compare_view import render_compare_view
from forge.dashboard.components.footer import render_footer
from forge.dashboard.components.navigation import render_tab_navigation

__all__ = [
    "render_header",
    "render_timeline",
    "render_artifact_view",
    "render_console_view",
    "render_tester_view",
    "render_pkb_view",
    "render_compare_view",
    "render_footer",
    "render_tab_navigation",
]

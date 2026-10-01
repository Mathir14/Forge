"""DashboardState: In-memory navigation, selection, and scroll state."""

from dataclasses import dataclass, field
from typing import Optional, List
from forge.dashboard.model import RunModel, StageModel


@dataclass
class DashboardState:
    """Manages active navigation, scroll offsets, and view state."""
    run: RunModel
    selected_stage_index: int = 0
    focused_pane: str = "timeline"  # "timeline" or "content"
    active_tab: int = 2  # 1: Console, 2: Artifact, 3: Tester, 4: PKB, 5: Compare
    active_content_tab: str = "human"  # "human" or "machine" (for Artifact tab)
    scroll_offset: int = 0
    should_exit: bool = False
    pkb_type_filter: Optional[str] = None
    pkb_status_filter: Optional[str] = None
    compare_run_id: Optional[str] = None
    compare_run_model: Optional[RunModel] = None

    @property
    def current_stage(self) -> Optional[StageModel]:
        if not self.run.stages:
            return None
        idx = max(0, min(self.selected_stage_index, len(self.run.stages) - 1))
        return self.run.stages[idx]

    def select_next_stage(self) -> None:
        if not self.run.stages:
            return
        if self.selected_stage_index < len(self.run.stages) - 1:
            self.selected_stage_index += 1
            self.scroll_offset = 0

    def select_prev_stage(self) -> None:
        if not self.run.stages:
            return
        if self.selected_stage_index > 0:
            self.selected_stage_index -= 1
            self.scroll_offset = 0

    def toggle_focus(self) -> None:
        self.focused_pane = "content" if self.focused_pane == "timeline" else "timeline"

    def set_tab(self, tab_num: int) -> None:
        if 1 <= tab_num <= 5:
            self.active_tab = tab_num
            self.scroll_offset = 0

    def next_tab(self) -> None:
        self.active_tab = (self.active_tab % 5) + 1
        self.scroll_offset = 0

    def prev_tab(self) -> None:
        self.active_tab = 5 if self.active_tab == 1 else self.active_tab - 1
        self.scroll_offset = 0

    def toggle_content_tab(self) -> None:
        self.active_content_tab = "machine" if self.active_content_tab == "human" else "human"
        self.scroll_offset = 0

    def set_content_tab(self, tab: str) -> None:
        if tab in ("human", "machine"):
            self.active_content_tab = tab
            self.scroll_offset = 0

    def scroll_up(self, lines: int = 1) -> None:
        self.scroll_offset = max(0, self.scroll_offset - lines)

    def scroll_down(self, lines: int = 1, max_lines: int = 1000) -> None:
        self.scroll_offset = min(max_lines, self.scroll_offset + lines)

    def page_up(self, page_size: int = 15) -> None:
        self.scroll_up(lines=page_size)

    def page_down(self, page_size: int = 15, max_lines: int = 1000) -> None:
        self.scroll_down(lines=page_size, max_lines=max_lines)

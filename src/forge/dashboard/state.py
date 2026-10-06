"""DashboardState: In-memory navigation, selection, and scroll state."""

from dataclasses import dataclass, field
from typing import Optional, List, Any
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
    terminal_status: Optional[str] = None
    terminal_stage: Optional[str] = None
    terminal_reason: Optional[str] = None
    user_has_selected_stage: bool = False
    project_root: Optional[Any] = None

    def __post_init__(self) -> None:
        if not self.user_has_selected_stage:
            if self.run.active_stage_name:
                self.follow_active_stage(self.run.active_stage_name)
            else:
                for idx, s in enumerate(self.run.stages):
                    if s.status in ("RUNNING", "IN_PROGRESS"):
                        self.selected_stage_index = idx
                        break

    def follow_active_stage(self, stage_name: Optional[str] = None) -> None:
        """Update selected_stage_index to follow active stage unless user manually navigated."""
        if self.user_has_selected_stage:
            return
        target = (stage_name or self.run.active_stage_name or "").lower().strip()
        if not target:
            return
        for idx, s in enumerate(self.run.stages):
            if s.stage_name.lower() == target or s.role_name.lower() == target:
                self.selected_stage_index = idx
                self.scroll_offset = 0
                break

    @property
    def current_stage(self) -> Optional[StageModel]:
        if not self.run.stages:
            return None
        if not self.user_has_selected_stage and self.run.active_stage_name:
            self.follow_active_stage(self.run.active_stage_name)
        idx = max(0, min(self.selected_stage_index, len(self.run.stages) - 1))
        return self.run.stages[idx]

    def select_next_stage(self) -> None:
        self.user_has_selected_stage = True
        if not self.run.stages:
            return
        if self.selected_stage_index < len(self.run.stages) - 1:
            self.selected_stage_index += 1
            self.scroll_offset = 0

    def select_prev_stage(self) -> None:
        self.user_has_selected_stage = True
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

    def get_run_summary(self) -> Any:
        """Return the canonical RunSummary, ensuring terminal status and reason are synchronized."""
        from forge.core.summary import RunSummary
        if getattr(self.run, "summary", None) is not None:
            summary = self.run.summary
        else:
            summary = RunSummary(run_id=self.run.run_id)
            self.run.summary = summary

        if self.terminal_status:
            summary.final_status = self.terminal_status
        elif self.run.status and self.run.status not in ("PENDING", "UNKNOWN", "IN_PROGRESS", "RUNNING"):
            summary.final_status = self.run.status

        if self.terminal_reason:
            summary.reason = self.terminal_reason

        return summary

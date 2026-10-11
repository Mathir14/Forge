"""Virtual scroll viewport for Rich renderables.

Renders child renderables into formatted terminal lines, slices the rendered lines
to the visible window, updates state scroll boundaries, and provides an exact,
flicker-free visual position indicator.
"""

from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console, ConsoleOptions, RenderResult, RenderableType, Segment
from rich.text import Text


class VirtualViewport:
    """Virtual scroll container that slices rendered terminal lines with exact row precision."""

    _LINE_CACHE: Dict[Tuple[str, int], List[List[Segment]]] = {}
    _MAX_CACHE_ENTRIES = 50

    def __init__(
        self,
        renderable: RenderableType,
        scroll_offset: int = 0,
        max_lines: int = 35,
        show_indicator: bool = True,
        cache_key: Optional[str] = None,
        state: Optional[Any] = None,
        indicator_label: str = "Lines",
    ):
        self.renderable = renderable
        self.scroll_offset = max(0, scroll_offset)
        self.max_lines = max(1, max_lines)
        self.show_indicator = show_indicator
        self.cache_key = cache_key
        self.state = state
        self.indicator_label = indicator_label

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the rendered line cache."""
        cls._LINE_CACHE.clear()

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width or (console.size.width or 80)
        all_lines: Optional[List[List[Segment]]] = None

        if self.cache_key is not None:
            cache_lookup = (self.cache_key, width)
            all_lines = self._LINE_CACHE.get(cache_lookup)

        if all_lines is None:
            child_opts = options.update(height=None)
            all_lines = console.render_lines(self.renderable, child_opts)
            if self.cache_key is not None:
                if len(self._LINE_CACHE) >= self._MAX_CACHE_ENTRIES:
                    self._LINE_CACHE.clear()
                self._LINE_CACHE[(self.cache_key, width)] = all_lines

        total_lines = len(all_lines)

        # Budget lines for content and position indicator
        if self.show_indicator and total_lines > self.max_lines:
            content_budget = max(1, self.max_lines - 1)
        else:
            content_budget = self.max_lines

        max_scroll = max(0, total_lines - content_budget)

        # Synchronize boundary with DashboardState
        if self.state is not None:
            self.state.max_scroll_offset = max_scroll
            if self.state.scroll_offset > max_scroll:
                self.state.scroll_offset = max_scroll
            offset = self.state.scroll_offset
        else:
            offset = max(0, min(self.scroll_offset, max_scroll))

        start = offset
        end = min(total_lines, start + content_budget)
        slice_lines = all_lines[start:end]

        # Render position indicator if content exceeds viewport
        if self.show_indicator and total_lines > self.max_lines:
            ind_text = Text(
                f"({self.indicator_label} {start + 1}–{end} of {total_lines})",
                style="dim",
            )
            for seg in console.render(ind_text, options):
                yield seg
            yield Segment.line()

        # Render visible visual lines
        for line in slice_lines:
            yield from line
            yield Segment.line()

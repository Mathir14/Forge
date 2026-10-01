"""DashboardApp: Main terminal application orchestrating state and layout rendering."""

import os
import queue
import select
import signal
import sys
import time
from pathlib import Path
from typing import Optional, List, Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live

from forge.dashboard.model import RunModel
from forge.dashboard.state import DashboardState
from forge.dashboard.components.header import render_header
from forge.dashboard.components.timeline import render_timeline
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.console_view import render_console_view
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.components.pkb_view import render_pkb_view
from forge.dashboard.components.compare_view import render_compare_view
from forge.dashboard.components.footer import render_footer


class DashboardApp:
    """Terminal UI application for inspecting Forge runs in post-mortem and live attach modes."""

    def __init__(
        self,
        run_dir: Path,
        console: Optional[Console] = None,
        live_queue: Optional[Any] = None,
        stop_event: Optional[Any] = None,
        abort_event: Optional[Any] = None,
        project_root: Optional[Path] = None,
    ):
        self.run_dir = run_dir.resolve()
        is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        out_file = sys.__stdout__ if (is_tty and sys.__stdout__ is not None) else sys.stdout
        self.console = console or Console(file=out_file)
        self.live_queue = live_queue
        self.stop_event = stop_event
        self.abort_event = abort_event
        self.project_root = project_root or self.run_dir.parent.parent
        self.run_model = RunModel.from_dir(self.run_dir)
        initial_tab = 1 if live_queue is not None else 2
        self.state = DashboardState(run=self.run_model, active_tab=initial_tab)

    def create_layout(self) -> Layout:
        """Construct the 3-tier split layout with contextual operational views."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="sidebar", ratio=1, minimum_size=28),
            Layout(name="content", ratio=3),
        )

        term_height = self.console.size.height or 24
        content_lines = max(4, term_height - 8)

        # Route viewport to active tab component
        if self.state.active_tab == 1:
            content_panel = render_console_view(self.state, max_lines=content_lines)
        elif self.state.active_tab == 2:
            content_panel = render_artifact_view(self.state, max_lines=content_lines)
        elif self.state.active_tab == 3:
            content_panel = render_tester_view(self.state, max_lines=content_lines)
        elif self.state.active_tab == 4:
            content_panel = render_pkb_view(self.state, project_root=self.project_root, max_lines=content_lines)
        elif self.state.active_tab == 5:
            content_panel = render_compare_view(self.state, max_lines=content_lines)
        else:
            content_panel = render_artifact_view(self.state, max_lines=content_lines)

        layout["header"].update(render_header(self.state))
        layout["sidebar"].update(render_timeline(self.state))
        layout["content"].update(content_panel)
        layout["footer"].update(render_footer(self.state))

        return layout

    def handle_key(self, key: str) -> None:
        """Process keyboard input actions."""
        if not key:
            return

        clean_key = key.lower()

        # Exit
        if clean_key == "q":
            self.state.should_exit = True
            return

        if clean_key in ("\x03", "ctrl+c"):
            self.state.should_exit = True
            if self.abort_event is not None:
                self.abort_event.set()
            return

        # Pane switching
        if key in ("tab", "\t"):
            self.state.toggle_focus()
            return

        # Top-level Tab switching (1: Console, 2: Artifact, 3: Tester, 4: PKB, 5: Compare)
        if clean_key == "1":
            if self.live_queue is None and self.state.active_tab == 2:
                self.state.set_content_tab("human")
            else:
                self.state.set_tab(1)
            return
        elif clean_key == "2":
            if self.state.active_tab == 2:
                self.state.set_content_tab("machine")
            else:
                self.state.set_tab(2)
            return
        elif clean_key == "3":
            self.state.set_tab(3)
            return
        elif clean_key == "4":
            self.state.set_tab(4)
            return
        elif clean_key == "5":
            self.state.set_tab(5)
            return

        # Artifact Sub-tab switching
        if clean_key == "h":
            if self.state.active_tab == 2:
                self.state.set_content_tab("human")
            return
        elif clean_key == "m":
            if self.state.active_tab == 2:
                self.state.set_content_tab("machine")
            return

        # PKB Filter cycling ([ and ])
        if clean_key == "[":
            if self.state.active_tab == 4:
                self._cycle_pkb_filter(forward=False)
            return
        elif clean_key == "]":
            if self.state.active_tab == 4:
                self._cycle_pkb_filter(forward=True)
            return

        # Navigation & Scrolling
        if key in ("up", "k"):
            if self.state.focused_pane == "timeline":
                self.state.select_prev_stage()
            else:
                self.state.scroll_up(1)
        elif key in ("down", "j"):
            if self.state.focused_pane == "timeline":
                self.state.select_next_stage()
            else:
                self.state.scroll_down(1)
        elif key in ("page_up", "pgup"):
            self.state.page_up(15)
        elif key in ("page_down", "pgdn"):
            self.state.page_down(15)

    def _cycle_pkb_filter(self, forward: bool = True) -> None:
        """Cycle through fact types for PKB view."""
        types = [None, "architecture", "feature", "decision", "unresolved"]
        curr = self.state.pkb_type_filter
        idx = types.index(curr) if curr in types else 0
        new_idx = (idx + 1) % len(types) if forward else (idx - 1) % len(types)
        self.state.pkb_type_filter = types[new_idx]
        self.state.scroll_offset = 0

    def render_once(self) -> str:
        """Render a single static snapshot to string (useful for tests and non-interactive scripts)."""
        layout = self.create_layout()
        with self.console.capture() as capture:
            self.console.print(layout)
        return capture.get()

    def run(self) -> int:
        """Run the interactive dashboard loop. Falls back to static render if stdin is not a TTY."""
        if not sys.stdin.isatty():
            if self.live_queue is not None:
                while True:
                    while True:
                        try:
                            ev = self.live_queue.get_nowait()
                            self.run_model.apply_event(ev)
                        except Exception:
                            break
                    if self.stop_event is not None and self.stop_event.is_set() and self.live_queue.empty():
                        break
                    time.sleep(0.05)
            self.console.print(self.create_layout())
            return 0

        # POSIX terminal raw/cbreak setup
        import termios
        import tty
        import io

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        target_out = sys.__stdout__ if sys.__stdout__ is not None else old_stdout

        try:
            if self.live_queue is not None:
                sys.stdout = io.StringIO()
                sys.stderr = io.StringIO()

            tty.setcbreak(fd)
            # Enter alternate buffer and hide cursor
            target_out.write("\x1b[?1049h\x1b[H\x1b[?25l")
            target_out.flush()

            layout = self.create_layout()
            last_size = self.console.size
            with Live(layout, console=self.console, screen=False, auto_refresh=False) as live:
                live.update(self.create_layout(), refresh=True)

                while not self.state.should_exit:
                    # 1. Drain live event queue if attached to running pipeline
                    if self.live_queue is not None:
                        drained = False
                        while True:
                            try:
                                ev = self.live_queue.get_nowait()
                                self.run_model.apply_event(ev)
                                drained = True
                            except (queue.Empty, Exception):
                                break
                        if drained:
                            live.update(self.create_layout(), refresh=True)
                        if self.stop_event is not None and self.stop_event.is_set() and self.live_queue.empty():
                            self.run_model.is_active = False
                            live.update(self.create_layout(), refresh=True)
                            time.sleep(0.2)
                            self.state.should_exit = True
                            break

                    # 2. Live duration timer update for active stage
                    if self.run_model.is_active and self.run_model.active_stage_name:
                        active_st = self.run_model.get_stage(self.run_model.active_stage_name)
                        st_t = self.run_model.stage_start_times.get(self.run_model.active_stage_name)
                        if active_st and st_t:
                            active_st.duration_seconds = time.time() - st_t
                            self.run_model.total_duration_seconds = sum(s.duration_seconds for s in self.run_model.stages)
                            live.update(self.create_layout(), refresh=True)

                    # 3. Check terminal resize
                    current_size = self.console.size
                    if current_size != last_size:
                        last_size = current_size
                        live.update(self.create_layout(), refresh=True)

                    # 4. Non-blocking poll for user input (50ms interval)
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if r:
                        key = self._read_key(fd)
                        self.handle_key(key)
                        live.update(self.create_layout(), refresh=True)

        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            try:
                # Restore cursor and original screen buffer
                target_out.write("\x1b[?25h\x1b[?1049l")
                target_out.flush()
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        return 0

    @staticmethod
    def _read_key(fd: int) -> str:
        """Read a character or ANSI escape sequence from file descriptor."""
        try:
            ch = os.read(fd, 1).decode("utf-8", errors="ignore")
        except Exception:
            return ""

        if ch == "\x1b":
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                seq = os.read(fd, 8).decode("utf-8", errors="ignore")
                full = ch + seq
                if full == "\x1b[A":
                    return "up"
                elif full == "\x1b[B":
                    return "down"
                elif full == "\x1b[C":
                    return "right"
                elif full == "\x1b[D":
                    return "left"
                elif full in ("\x1b[5~", "\x1b[V"):
                    return "page_up"
                elif full in ("\x1b[6~", "\x1b[U"):
                    return "page_down"
                return full
            return "esc"
        elif ch == "\t":
            return "tab"
        elif ch in ("\r", "\n"):
            return "enter"
        return ch

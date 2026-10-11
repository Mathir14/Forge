"""Comprehensive regression tests for Forge dashboard scrolling responsiveness,
motion smoothness, mouse-wheel routing, boundary clamping, and input tokenization.
"""

from pathlib import Path
import queue
import threading
import time
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from forge.core.platform import parse_key_buffer, PosixTerminalSession
from forge.dashboard.app import DashboardApp
from forge.dashboard.components.viewport import VirtualViewport
from forge.dashboard.components.artifact_view import render_artifact_view
from forge.dashboard.components.console_view import render_console_view
from forge.dashboard.components.tester_view import render_tester_view
from forge.dashboard.components.pkb_view import render_pkb_view
from forge.dashboard.components.compare_view import render_compare_view
from forge.dashboard.model import RunModel, StageModel
from forge.dashboard.state import DashboardState
from forge.storage.knowledge import KnowledgeStore
from forge.core.knowledge import KnowledgeFact, FactStatus, FactSource


def test_parse_key_buffer_arrows_and_bursts():
    """Verify parse_key_buffer correctly tokenizes single keys and rapid multi-key bursts."""
    # Single arrow keys
    assert parse_key_buffer("\x1b[A") == ["up"]
    assert parse_key_buffer("\x1b[B") == ["down"]
    assert parse_key_buffer("\x1b[C") == ["right"]
    assert parse_key_buffer("\x1b[D") == ["left"]

    # SS3 cursor keys
    assert parse_key_buffer("\x1bOA") == ["up"]
    assert parse_key_buffer("\x1bOB") == ["down"]

    # Rapid burst of arrow keys in a single read buffer (the cause of dropped inputs!)
    assert parse_key_buffer("\x1b[B\x1b[B\x1b[B") == ["down", "down", "down"]
    assert parse_key_buffer("\x1b[A\x1b[B\x1b[A") == ["up", "down", "up"]

    # Page keys
    assert parse_key_buffer("\x1b[5~") == ["page_up"]
    assert parse_key_buffer("\x1b[6~") == ["page_down"]

    # Home and End
    assert parse_key_buffer("\x1b[H") == ["home"]
    assert parse_key_buffer("\x1b[F") == ["end"]
    assert parse_key_buffer("\x1b[1~") == ["home"]
    assert parse_key_buffer("\x1b[4~") == ["end"]

    # Standard keys
    assert parse_key_buffer("q") == ["q"]
    assert parse_key_buffer("\t") == ["tab"]
    assert parse_key_buffer("\r") == ["enter"]
    assert parse_key_buffer("\n") == ["enter"]
    assert parse_key_buffer("\x1b") == ["esc"]
    assert parse_key_buffer("\x03") == ["\x03"]

    # Mixed burst: arrow followed by character
    assert parse_key_buffer("\x1b[Bq") == ["down", "q"]


def test_parse_key_buffer_mouse_events():
    """Verify SGR and legacy mouse escape sequences are parsed into structured tokens."""
    # SGR Wheel Up: button 64, X=45, Y=12
    assert parse_key_buffer("\x1b[<64;45;12M") == ["wheel_up:45:12"]

    # SGR Wheel Down: button 65, X=80, Y=20
    assert parse_key_buffer("\x1b[<65;80;20M") == ["wheel_down:80:20"]

    # Multiple wheel events in rapid succession
    burst = "\x1b[<64;50;10M\x1b[<64;50;10M\x1b[<64;50;10M"
    assert parse_key_buffer(burst) == ["wheel_up:50:10", "wheel_up:50:10", "wheel_up:50:10"]

    # SGR Click (button 0 press)
    assert parse_key_buffer("\x1b[<0;15;5M") == ["click:15:5"]

    # Legacy X10 mouse wheel up: button 64 -> chr(64 + 32) = '`'
    legacy_up = "\x1b[M" + chr(64 + 32) + chr(20 + 32) + chr(10 + 32)
    assert parse_key_buffer(legacy_up) == ["wheel_up:20:10"]

    # Legacy X10 mouse wheel down: button 65 -> chr(65 + 32) = 'a'
    legacy_down = "\x1b[M" + chr(65 + 32) + chr(20 + 32) + chr(10 + 32)
    assert parse_key_buffer(legacy_down) == ["wheel_down:20:10"]


def test_virtual_viewport_exact_row_slicing():
    """Verify VirtualViewport slices pre-rendered lines with row precision, avoiding markdown freezes."""
    VirtualViewport.clear_cache()
    # Create 15 paragraphs of markdown with blank lines
    md_content = "\n\n".join(f"Paragraph {i}\nSome details for paragraph {i}." for i in range(15))
    md = Markdown(md_content)

    console = Console(width=80)

    # Render with offset 0
    vp0 = VirtualViewport(md, scroll_offset=0, max_lines=5, show_indicator=False)
    lines0 = list(console.render_lines(vp0, console.options))

    # Render with offset 1
    vp1 = VirtualViewport(md, scroll_offset=1, max_lines=5, show_indicator=False)
    lines1 = list(console.render_lines(vp1, console.options))

    # With rendered row slicing, offset 0 and offset 1 MUST differ by shifting exactly 1 terminal row!
    assert len(lines0) == 5
    assert len(lines1) == 5
    # The first line of lines1 must match the second line of lines0
    seg_texts0 = "".join(s.text for s in lines0[1])
    seg_texts1 = "".join(s.text for s in lines1[0])
    assert seg_texts0 == seg_texts1


def test_virtual_viewport_indicator_and_state_bounds(tmp_path: Path):
    """Verify VirtualViewport synchronizes max_scroll_offset and renders correct position cues."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    state = DashboardState(run=model)

    lines = [f"Line {i}" for i in range(50)]
    text = Text("\n".join(lines))

    console = Console(width=80)
    vp = VirtualViewport(text, max_lines=10, show_indicator=True, state=state)

    # Render through console
    rendered_str = ""
    with console.capture() as capture:
        console.print(vp)
    rendered_str = capture.get()

    # Content has 50 lines, budget is 9 lines + 1 indicator line
    assert state.max_scroll_offset == 50 - 9  # 41
    assert "(Lines 1–9 of 50)" in rendered_str

    # Now scroll state to 10
    state.scroll_offset = 10
    with console.capture() as capture:
        console.print(vp)
    rendered_str = capture.get()
    assert "(Lines 11–19 of 50)" in rendered_str


def test_dashboard_state_scroll_clamping(tmp_path: Path):
    """Verify DashboardState bounds scrolling within [0, max_scroll_offset]."""
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=tmp_path)
    state = DashboardState(run=model)
    state.max_scroll_offset = 20

    # Down within bounds
    state.scroll_down(5)
    assert state.scroll_offset == 5

    # Down up to bounds
    state.scroll_down(30)
    assert state.scroll_offset == 20  # Clamped to max_scroll_offset

    # Up within bounds
    state.scroll_up(8)
    assert state.scroll_offset == 12

    # Up beyond 0
    state.scroll_up(50)
    assert state.scroll_offset == 0  # Clamped to 0

    # Page up and page down
    state.page_down(page_size=15)
    assert state.scroll_offset == 15
    state.page_up(page_size=10)
    assert state.scroll_offset == 5

    # Jump to top / bottom
    state.scroll_to_bottom()
    assert state.scroll_offset == 20
    state.scroll_to_top()
    assert state.scroll_offset == 0


def test_dashboard_app_mouse_wheel_pane_routing(tmp_path: Path):
    """Verify mouse wheel events route to timeline when over sidebar and content when over content."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    (run_dir / "01_architect.md").write_text("# Architect Report\n" + "\n".join(f"Line {i}" for i in range(50)))
    (run_dir / "02_planner.md").write_text("# Planner Report\n" + "\n".join(f"Line {i}" for i in range(50)))

    app = DashboardApp(run_dir)
    console_width = app.console.size.width or 80
    sidebar_width = max(28, console_width // 4)

    # 1. Wheel over sidebar (X = 10 <= sidebar_width)
    assert app.state.selected_stage_index == 0
    # Wheel down over sidebar should select next stage
    app.handle_key(f"wheel_down:10:10")
    assert app.state.selected_stage_index == 1
    # Wheel up over sidebar should select prev stage
    app.handle_key(f"wheel_up:10:10")
    assert app.state.selected_stage_index == 0

    # 2. Wheel over content area (X = 50 > sidebar_width)
    assert app.state.scroll_offset == 0
    # Wheel down over content should scroll content by 3 rows
    app.handle_key(f"wheel_down:50:10")
    assert app.state.scroll_offset == 3
    # Wheel down again
    app.handle_key(f"wheel_down:50:10")
    assert app.state.scroll_offset == 6
    # Wheel up over content should scroll content up by 3 rows
    app.handle_key(f"wheel_up:50:10")
    assert app.state.scroll_offset == 3

    # Stage index remains untouched
    assert app.state.selected_stage_index == 0


def test_dashboard_app_mouse_click_focus(tmp_path: Path):
    """Verify mouse click switches focus between sidebar and content pane."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    (run_dir / "01_architect.md").write_text("# Architect")

    app = DashboardApp(run_dir)
    console_width = app.console.size.width or 80
    sidebar_width = max(28, console_width // 4)

    # Initial focus is timeline
    assert app.state.focused_pane == "timeline"

    # Click on content pane (X > sidebar_width)
    app.handle_key(f"click:{sidebar_width + 10}:5")
    assert app.state.focused_pane == "content"

    # Click on sidebar (X <= sidebar_width)
    app.handle_key(f"click:10:5")
    assert app.state.focused_pane == "timeline"


def test_console_view_scrolling_consistency(tmp_path: Path):
    """Verify Console view scroll directions and auto-scroll state."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    app = DashboardApp(run_dir)
    app.state.active_tab = 1  # Console view
    app.state.focused_pane = "content"

    # Add 100 console logs
    app.run_model.console_logs = [f"Log line {i}" for i in range(100)]

    # Initial state: scroll_offset is 0, auto-scroll is ON
    assert app.state.scroll_offset == 0

    # Up arrow in console view scrolls UP into older logs (increases offset from tail)
    app.handle_key("up")
    assert app.state.scroll_offset == 1

    app.handle_key("up")
    assert app.state.scroll_offset == 2

    # Wheel up in console view scrolls UP into older logs by 3 rows
    app.handle_key("wheel_up:50:10")
    assert app.state.scroll_offset == 5

    # Down arrow in console view scrolls DOWN toward newer logs (decreases offset)
    app.handle_key("down")
    assert app.state.scroll_offset == 4

    # Wheel down scrolls DOWN toward tail by 3 rows
    app.handle_key("wheel_down:50:10")
    assert app.state.scroll_offset == 1

    # Down arrow back to tail (offset 0)
    app.handle_key("down")
    assert app.state.scroll_offset == 0

    # Down arrow at tail stays clamped at 0
    app.handle_key("down")
    assert app.state.scroll_offset == 0


def test_home_and_end_keys_navigation(tmp_path: Path):
    """Verify Home and End keys jump to top/bottom in content and first/last stage in timeline."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()
    (run_dir / "01_architect.md").write_text("# Architect\n" + "\n".join(f"L{i}" for i in range(100)))
    (run_dir / "02_planner.md").write_text("# Planner\n" + "\n".join(f"L{i}" for i in range(100)))

    app = DashboardApp(run_dir)
    app.state.max_scroll_offset = 80

    # In timeline: Home selects 0, End selects last stage
    app.state.focused_pane = "timeline"
    last_stage_idx = len(app.run_model.stages) - 1
    app.handle_key("end")
    assert app.state.selected_stage_index == last_stage_idx
    app.handle_key("home")
    assert app.state.selected_stage_index == 0

    # In content: Home jumps to 0, End jumps to max_scroll_offset
    app.state.focused_pane = "content"
    app.handle_key("end")
    assert app.state.scroll_offset == 80
    app.handle_key("home")
    assert app.state.scroll_offset == 0


def test_tester_pkb_compare_views_support_virtual_scrolling(tmp_path: Path):
    """Verify Tester, PKB, and Compare views render properly through VirtualViewport."""
    run_dir = tmp_path / "run-test"
    run_dir.mkdir()

    # 1. Tester view with 50 tests
    model = RunModel(run_id="run-test", task="task", status="RUNNING", created_at="2026-01-01", run_dir=run_dir)
    tests_list = [{"name": f"test_{i}", "status": "PASS" if i % 2 == 0 else "FAIL", "duration": 0.05} for i in range(50)]
    stage = StageModel(
        stage_name="04_tester",
        role_name="tester",
        sequence_number=4,
        status="APPROVED",
        metadata={"test_results": {"total": 50, "passed": 25, "failed": 25, "skipped": 0, "tests": tests_list}},
    )
    model.stages.append(stage)
    state = DashboardState(run=model, active_tab=3)

    panel_tester = render_tester_view(state, max_lines=15)
    assert panel_tester is not None
    # Render through console
    console = Console(width=80)
    with console.capture() as capture:
        console.print(panel_tester)
    out_tester = capture.get()
    assert "(Rows" in out_tester or "test_0" in out_tester
    assert state.max_scroll_offset > 0

    # 2. PKB view with 40 facts
    root = tmp_path / "proj"
    store = KnowledgeStore(root)
    for i in range(40):
        fact = KnowledgeFact(
            id=f"fact-{i}",
            title=f"Fact Title {i}",
            type="feature",
            status=FactStatus.VERIFIED.value,
            summary=f"Summary {i}",
        )
        store.save_fact(fact)

    state_pkb = DashboardState(run=model, active_tab=4, project_root=root)
    panel_pkb = render_pkb_view(state_pkb, project_root=root, max_lines=15)
    with console.capture() as capture:
        console.print(panel_pkb)
    out_pkb = capture.get()
    assert "(Facts" in out_pkb or "fact-0" in out_pkb
    assert state_pkb.max_scroll_offset > 0

    # 3. Compare view
    state_cmp = DashboardState(run=model, active_tab=5, project_root=root)
    state_cmp.compare_run_model = model
    panel_cmp = render_compare_view(state_cmp, project_root=root, max_lines=15)
    with console.capture() as capture:
        console.print(panel_cmp)
    assert capture.get() is not None

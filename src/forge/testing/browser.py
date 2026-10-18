"""Browser automation abstraction for Tester v2.

Defines the pluggable BrowserDriver interface, preventing hard-coded dependencies
on any single automation engine, and provides:
- PlaywrightBrowserDriver (first-party implementation)
- MockBrowserDriver (in-memory simulator for fast unit tests)
- BrowserDriverFactory (registry and discovery)
"""

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type, Union

from forge.testing.models import ConsoleEntry, InteractiveElement, NetworkFailure

logger = logging.getLogger(__name__)


class BrowserDriver(ABC):
    """Abstract browser automation driver interface."""

    @abstractmethod
    def start(self) -> None:
        """Launch the browser instance and context."""
        pass

    @abstractmethod
    def goto(self, url: str, timeout: float = 30.0) -> None:
        """Navigate to the target URL and wait for DOM readiness."""
        pass

    @abstractmethod
    def click(self, selector: str, timeout: float = 10.0) -> None:
        """Click on the element matching the selector."""
        pass

    @abstractmethod
    def fill(self, selector: str, text: str, timeout: float = 10.0) -> None:
        """Clear and fill an input element with text."""
        pass

    @abstractmethod
    def set_viewport_size(self, width: int, height: int) -> None:
        """Resize the browser viewport."""
        pass

    @abstractmethod
    def screenshot(self, path: Union[Path, str], full_page: bool = False) -> Path:
        """Capture screenshot and write PNG to path."""
        pass

    @abstractmethod
    def get_content(self) -> str:
        """Retrieve current page HTML content."""
        pass

    @abstractmethod
    def evaluate(self, script: str) -> Any:
        """Evaluate JavaScript expression in page context."""
        pass

    @abstractmethod
    def get_console_logs(self) -> List[ConsoleEntry]:
        """Return all captured console logs (log, warn, error)."""
        pass

    @abstractmethod
    def get_failed_requests(self) -> List[NetworkFailure]:
        """Return all failed HTTP requests (status >= 400 or aborted)."""
        pass

    @abstractmethod
    def get_page_errors(self) -> List[str]:
        """Return all unhandled page exceptions / crashes."""
        pass

    @abstractmethod
    def wait_for_hydration(self, timeout: float = 8.0) -> bool:
        """Wait for SPA client-side hydration and DOM stabilization."""
        pass

    @abstractmethod
    def set_input_files(self, selector: str, file_path: str) -> None:
        """Set uploaded file on an input[type='file'] element."""
        pass

    @abstractmethod
    def get_interactive_elements(self) -> List[InteractiveElement]:
        """Return all interactive elements (buttons, inputs, links)."""
        pass

    def get_current_url(self) -> str:
        """Return the current page URL."""
        return ""

    def element_exists(self, selector: str, timeout: float = 1.0) -> bool:
        """Check if an element exists and is visible on page."""
        return False

    @abstractmethod
    def close(self) -> None:
        """Close page, context, and browser."""
        pass


class PlaywrightBrowserDriver(BrowserDriver):
    """Playwright-backed browser automation driver."""

    def __init__(self, headless: bool = True, viewport: Tuple[int, int] = (1440, 900)):
        self.headless = headless
        self.viewport = viewport
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._console_logs: List[ConsoleEntry] = []
        self._failed_requests: List[NetworkFailure] = []
        self._page_errors: List[str] = []
        self.last_hydration_diagnostics: Dict[str, Any] = {}

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright is not installed in the current environment. "
                "Install it via 'pip install playwright && playwright install chromium' "
                "or configure a different BrowserDriver backend."
            ) from e

        try:
            self._playwright = sync_playwright().start()
            # Launch chromium with headless shell
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            self._context = self._browser.new_context(
                viewport={"width": self.viewport[0], "height": self.viewport[1]},
                ignore_https_errors=True,
            )
            self._page = self._context.new_page()

            # Telemetry event hooks
            self._page.on(
                "console",
                lambda msg: self._console_logs.append(
                    ConsoleEntry(
                        level=msg.type,
                        text=msg.text,
                        location=f"{msg.location.get('url', '')}:{msg.location.get('lineNumber', '')}" if msg.location else None,
                        timestamp=time.time(),
                    )
                ),
            )
            self._page.on(
                "pageerror",
                lambda exc: self._page_errors.append(str(exc)),
            )
            self._page.on(
                "requestfailed",
                lambda req: self._failed_requests.append(
                    NetworkFailure(
                        url=req.url,
                        method=req.method,
                        status=None,
                        error_text=str(req.failure) if req.failure else "Request failed",
                        timestamp=time.time(),
                    )
                ),
            )
            self._page.on(
                "response",
                lambda resp: self._failed_requests.append(
                    NetworkFailure(
                        url=resp.url,
                        method=resp.request.method,
                        status=resp.status,
                        error_text=f"HTTP {resp.status}",
                        timestamp=time.time(),
                    )
                ) if resp.status >= 400 else None,
            )
        except Exception:
            self.close()
            raise

    def goto(self, url: str, timeout: float = 30.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self._page.goto(url, timeout=int(timeout * 1000), wait_until="load")

    def _ensure_sidebar_expanded(self, timeout_ms: int = 1500) -> bool:
        """If Streamlit sidebar is collapsed on mobile viewports, auto-expand it."""
        if not self._page:
            return False

        # Dismiss blocking modal or dialog if open
        try:
            modal_close = self._page.locator('[aria-modal="true"] button[aria-label="Close"], [data-testid="stDialog"] button[aria-label="Close"], button[aria-label="Close"]').first
            if modal_close.count() > 0 and modal_close.is_visible(timeout=300):
                modal_close.click(timeout=500)
                time.sleep(0.2)
        except Exception:
            pass

        # Check if sidebar is already expanded
        try:
            is_open = self._page.evaluate("""
                () => {
                    const sidebar = document.querySelector('[data-testid="stSidebar"], section[data-testid="stSidebar"]');
                    if (sidebar) {
                        const isAria = sidebar.getAttribute("aria-expanded");
                        const w = sidebar.getBoundingClientRect().width;
                        if (isAria !== "false" && w > 50) return true;
                    }
                    return false;
                }
            """)
            if is_open:
                return False
        except Exception:
            pass

        for toggle_sel in (
            '[data-testid="stExpandSidebarButton"]',
            '[data-testid="stSidebarCollapseButton"]',
            '[data-testid="stSidebarCollapsedControl"] button',
            '[data-testid="stSidebarCollapsedControl"]',
            '[data-testid="collapsedControl"] button',
            '[data-testid="collapsedControl"]',
            'button[aria-label="Expand sidebar"]',
            'button[aria-label*="Expand" i]',
        ):
            try:
                btn = self._page.locator(toggle_sel).first
                if btn.count() > 0 and btn.is_visible(timeout=timeout_ms):
                    btn.click(timeout=timeout_ms)
                    time.sleep(0.3)
                    return True
            except Exception:
                continue
        return False

    def click(self, selector: str, timeout: float = 10.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        timeout_ms = int(timeout * 1000)

        # Fallback for ephemeral Streamlit tab ID (#tabs-bui\d+-tab-(\d+))
        m_tab = re.match(r"^#tabs-bui\d+-tab-(\d+)$", selector.strip())
        if m_tab:
            tab_idx = int(m_tab.group(1))
            try:
                tab_locator = self._page.locator("button[role='tab'], [data-baseweb='tab']").nth(tab_idx)
                if tab_locator.count() > 0:
                    tab_locator.click(timeout=timeout_ms)
                    return
            except Exception:
                pass

        # Mobile viewport check: if clicking sidebar elements while drawer is collapsed
        if self.viewport and self.viewport[0] <= 768:
            is_sidebar_target = any(k in selector.lower() for k in ("sidebar", "radio", "nav", "option"))
            if is_sidebar_target:
                self._ensure_sidebar_expanded(timeout_ms=1000)

        # If targeting outer Streamlit selectbox container directly, click inner select control
        if selector.startswith('[data-testid="stSelectbox"]') and not any(k in selector for k in ("div", "input", "role", "label", "button")):
            try:
                self._page.click(f"{selector} div[data-baseweb='select'], {selector} [role='combobox']", timeout=min(timeout_ms, 2000))
                return
            except Exception:
                pass
        try:
            self._page.click(selector, timeout=timeout_ms)
        except Exception as e:
            err_str = str(e).lower()
            if self.viewport and self.viewport[0] <= 768 and any(k in err_str for k in ("timeout", "not visible", "hidden")):
                if self._ensure_sidebar_expanded(timeout_ms=1500):
                    try:
                        self._page.click(selector, timeout=min(timeout_ms, 3000))
                        return
                    except Exception:
                        pass
            if any(k in err_str for k in ("intercepts pointer events", "outside of the viewport", "outside viewport")):
                logger.info("Pointer issue or viewport clipping for selector %s; retrying click with force=True", selector)
                try:
                    self._page.click(selector, timeout=min(timeout_ms, 3000), force=True)
                    return
                except Exception:
                    pass

            # Fallback for button:has-text('Train...') if specific phrasing differed (e.g. 'Train Baseline Models' vs '🚀 Train Logistic Regression')
            if "button:has-text('train" in selector.lower() and selector.strip() != "button:has-text('Train')":
                try:
                    self._page.click("button:has-text('Train')", timeout=min(timeout_ms, 4000))
                    return
                except Exception:
                    pass

            # Fallback for tab selectors if compound phrasing differed (e.g. 'Robustness & Calibration' vs '🛡️ Robustness')
            if ("role='tab'" in selector or 'role="tab"' in selector) and "robustness" in selector.lower():
                try:
                    self._page.click("button[role='tab']:has-text('Robustness')", timeout=min(timeout_ms, 4000))
                    return
                except Exception:
                    pass

            raise

    def fill(self, selector: str, text: str, timeout: float = 10.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        timeout_ms = int(timeout * 1000)
        try:
            self._page.fill(selector, text, timeout=timeout_ms)
        except Exception as e:
            err_msg = str(e).lower()
            if "not an <input>" in err_msg or "not an input" in err_msg:
                locator = self._page.locator(selector)
                try:
                    inner_input = locator.locator("input, textarea").first
                    if inner_input.count() > 0:
                        inner_input.fill(text, timeout=timeout_ms)
                        inner_input.press("Enter")
                        return
                except Exception:
                    pass
                try:
                    inner_select = locator.locator("select").first
                    if inner_select.count() > 0:
                        inner_select.select_option(label=text, timeout=timeout_ms)
                        return
                except Exception:
                    pass
            raise

    def set_viewport_size(self, width: int, height: int) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self.viewport = (width, height)
        self._page.set_viewport_size({"width": width, "height": height})

    def screenshot(self, path: Any, full_page: bool = False) -> Path:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(target), full_page=full_page)
        return target

    def get_content(self) -> str:
        if not self._page:
            return ""
        return self._page.content()

    def evaluate(self, script: str) -> Any:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        return self._page.evaluate(script)

    def get_console_logs(self) -> List[ConsoleEntry]:
        return list(self._console_logs)

    def get_failed_requests(self) -> List[NetworkFailure]:
        return list(self._failed_requests)

    def get_page_errors(self) -> List[str]:
        return list(self._page_errors)

    def set_input_files(self, selector: str, file_path: str) -> None:
        """Set uploaded file on an input[type='file'] element."""
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self._page.set_input_files(selector, file_path)

    def get_current_url(self) -> str:
        """Return the current page URL."""
        if self._page:
            try:
                return self._page.url or ""
            except Exception:
                return ""
        return ""

    def element_exists(self, selector: str, timeout: float = 1.0) -> bool:
        """Check if an element exists and is visible on page."""
        if not self._page:
            return False
        try:
            loc = self._page.locator(selector).first
            return loc.is_visible(timeout=int(timeout * 1000))
        except Exception:
            return False

    def wait_for_hydration(self, timeout: float = 8.0) -> bool:
        """Wait for SPA client-side hydration and DOM stabilization."""
        if not self._page:
            return False

        hydration_script = """
        () => {
            const isStreamlit = !!document.querySelector('.stApp, [data-testid="stAppViewContainer"]');

            // 1. Resolve content containers (explicitly rejecting body alone as proof of readiness)
            let mainContainer = null;
            let sidebarContainer = null;
            if (isStreamlit) {
                mainContainer = document.querySelector('[data-testid="stMain"], [data-testid="stAppViewBlockContainer"]');
                sidebarContainer = document.querySelector('[data-testid="stSidebar"]');
            } else {
                mainContainer = document.querySelector('main, [role="main"], #root > *:not(header), #app > *:not(header), #__next > *:not(header), article, .main-content, #main, #content');
                sidebarContainer = document.querySelector('nav, aside, [role="navigation"]');
            }

            const hasValidContainer = !!(mainContainer || sidebarContainer);

            // 2. Detect busy indicators and transient loading states
            let isBusy = false;
            let busyReason = null;
            if (isStreamlit) {
                const runningWidget = document.querySelector('[data-testid="stStatusWidget"][aria-label*="Running"]');
                const scriptRunning = !!document.querySelector('.stApp[data-test-script-state="running"]');
                const spinner = !!document.querySelector('[data-testid="stSpinner"], .stSpinner, [data-testid="stSkeleton"]');
                const ariaBusy = !!document.querySelector('[aria-busy="true"]');
                if (runningWidget) { isBusy = true; busyReason = 'stStatusWidget_running'; }
                else if (scriptRunning) { isBusy = true; busyReason = 'script_state_running'; }
                else if (spinner) { isBusy = true; busyReason = 'spinner_or_skeleton'; }
                else if (ariaBusy) { isBusy = true; busyReason = 'aria_busy'; }
            } else {
                const ariaBusy = !!document.querySelector('[aria-busy="true"]');
                const genericSpinner = !!document.querySelector('.spinner, .loading, [data-loading="true"], [role="progressbar"]');
                if (ariaBusy) { isBusy = true; busyReason = 'aria_busy'; }
                else if (genericSpinner) { isBusy = true; busyReason = 'generic_spinner'; }
            }

            // 3. Count interactive widgets and extract substantive text, strictly excluding platform header chrome
            const interactiveQuery = 'button, a, input, select, textarea, [role="button"], [role="radio"], [data-testid="stRadio"] label, [data-baseweb="radio"], [role="tab"], h1, h2, h3, [role="heading"], [role="alert"]';
            let appElements = [];
            if (mainContainer) {
                appElements.push(...Array.from(mainContainer.querySelectorAll(interactiveQuery)));
            }
            if (sidebarContainer) {
                appElements.push(...Array.from(sidebarContainer.querySelectorAll(interactiveQuery)));
            }
            // Filter out static platform header controls (e.g. Deploy button, options menu in Streamlit or banner header)
            appElements = appElements.filter(el => !el.closest('header, [data-testid="stHeader"], [role="banner"]'));

            const mainText = mainContainer ? (mainContainer.innerText || '').trim() : '';
            const sidebarText = sidebarContainer ? (sidebarContainer.innerText || '').trim() : '';
            const combinedText = (mainText + ' ' + sidebarText).trim();

            // 4. Substantive content verification
            let hasSubstantiveContent = false;
            if (isStreamlit) {
                hasSubstantiveContent = combinedText.length >= 10 && appElements.length >= 1;
            } else {
                hasSubstantiveContent = combinedText.length > 0 && appElements.length >= 1;
            }

            // 5. Readiness determination
            let isReady = false;
            if (isStreamlit) {
                const notRunning = !!document.querySelector('.stApp[data-test-script-state="not_running"]');
                isReady = hasValidContainer && !isBusy && (notRunning || !isBusy) && hasSubstantiveContent;
            } else {
                isReady = hasValidContainer && !isBusy && hasSubstantiveContent;
            }

            return {
                hasContainer: hasValidContainer,
                busy: isBusy,
                busyReason: busyReason,
                count: appElements.length,
                textLength: combinedText.length,
                isStreamlit: isStreamlit,
                ready: isReady
            };
        }
        """
        start = time.time()
        last_count = -1
        stable_cycles = 0
        min_stable_cycles = 2
        last_diag: Dict[str, Any] = {}

        while (time.time() - start) < timeout:
            try:
                res = self._page.evaluate(hydration_script)
                if isinstance(res, dict):
                    last_diag = res
                    if res.get("ready") and not res.get("busy"):
                        count = res.get("count", 0)
                        if count == last_count:
                            stable_cycles += 1
                            if stable_cycles >= min_stable_cycles:
                                self.last_hydration_diagnostics = last_diag
                                return True
                        else:
                            last_count = count
                            stable_cycles = 0
                    else:
                        stable_cycles = 0
            except Exception as e:
                logger.debug("Evaluation error during wait_for_hydration: %s", e)

            time.sleep(0.25)

        self.last_hydration_diagnostics = last_diag
        logger.info("wait_for_hydration timed out after %.1fs (diag=%s)", timeout, last_diag)
        return False

    def get_interactive_elements(self) -> List[InteractiveElement]:
        if not self._page:
            return []
        script = """
        () => {
            const results = [];
            const seen = new Set();

            function add(sel, tag, role, text, isVisible, isEnabled) {
                if (!sel) return;
                const cleanText = (text || '').trim().replace(/\\s+/g, ' ');
                const key = sel + '::' + cleanText;
                if (seen.has(key)) return;
                seen.add(key);
                results.push({
                    selector: sel,
                    tag_name: tag,
                    role: role || null,
                    text: cleanText.slice(0, 60),
                    is_visible: !!isVisible,
                    is_enabled: !!isEnabled
                });
            }

            // 1. Radio groups (Streamlit, BaseWeb, Material, standard radiogroups)
            const radioLabels = document.querySelectorAll(
                '[data-testid="stRadio"] label[data-baseweb="radio"], ' +
                '[data-testid="stRadio"] label:has(input[type="radio"]), ' +
                '[role="radiogroup"] label:has(input[type="radio"]), ' +
                '[role="radiogroup"] [role="radio"]'
            );
            radioLabels.forEach(lbl => {
                const text = (lbl.innerText || lbl.textContent || '').trim().replace(/\\s+/g, ' ');
                if (text && text.length > 0) {
                    const group = lbl.closest('[data-testid="stRadio"]') || lbl.closest('[role="radiogroup"]');
                    const groupSel = group && group.getAttribute('data-testid')
                        ? `[data-testid="${group.getAttribute('data-testid')}"]`
                        : (group && group.getAttribute('role') ? `[role="${group.getAttribute('role')}"]` : '');
                    const sel = groupSel ? `${groupSel} label:has-text("${text}")` : `label:has-text("${text}")`;
                    const isVisible = !!(lbl.offsetWidth || lbl.offsetHeight || lbl.getClientRects().length);
                    const isEnabled = !lbl.querySelector('input:disabled');
                    add(sel, 'label', 'radio', text, isVisible, isEnabled);
                }
            });

            // 2. Tabs ([role="tab"], [data-testid="stTabs"] button)
            const tabs = document.querySelectorAll('[role="tab"], [data-testid="stTabs"] button');
            tabs.forEach(tab => {
                const text = (tab.innerText || tab.textContent || '').trim().replace(/\\s+/g, ' ');
                let sel = tab.id ? '#' + tab.id : '';
                if (!sel && tab.getAttribute('data-testid')) {
                    sel = `[data-testid="${tab.getAttribute('data-testid')}"]`;
                }
                if (!sel && text) {
                    sel = `[data-testid="stTabs"] button:has-text("${text}")`;
                }
                if (sel) {
                    const isVisible = !!(tab.offsetWidth || tab.offsetHeight || tab.getClientRects().length);
                    add(sel, 'button', 'tab', text, isVisible, !tab.disabled);
                }
            });

            // 3. Standard interactive controls & semantic elements
            const standardQuery = 'button, a, input:not([type="radio"]), select, textarea, [role="button"], h1, h2, h3, [role="heading"], [role="alert"]';
            const elements = Array.from(document.querySelectorAll(standardQuery));
            elements.forEach(el => {
                const tag = el.tagName.toLowerCase();
                const text = (el.innerText || el.value || el.placeholder || '').trim().replace(/\\s+/g, ' ');
                const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const isEnabled = !el.disabled;

                // Skip invisible anchor links without text (e.g. heading anchor links)
                if (tag === 'a' && !text && el.getAttribute('aria-label') === 'Link to heading') return;

                let sel = el.id ? '#' + el.id : '';
                if (!sel && el.getAttribute('data-testid')) {
                    sel = `[data-testid="${el.getAttribute('data-testid')}"]`;
                }
                if (!sel && el.name) {
                    sel = `[name="${el.name}"]`;
                }
                if (!sel && el.getAttribute('aria-label')) {
                    sel = `[aria-label="${el.getAttribute('aria-label')}"]`;
                }
                if (!sel && tag === 'button' && text && text.length < 30) {
                    sel = `button:has-text("${text}")`;
                }
                if (!sel) {
                    sel = tag;
                }

                add(sel, tag, el.getAttribute('role'), text, isVisible, isEnabled);
            });

            return results.slice(0, 60);
        }
        """
        try:
            raw = self._page.evaluate(script)
            return [InteractiveElement(**item) for item in raw]
        except Exception:
            return []

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.debug("Error closing PlaywrightBrowserDriver: %s", e)
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None


class MockBrowserDriver(BrowserDriver):
    """Deterministic in-memory browser driver for fast testing and fallback."""

    # 1x1 transparent PNG binary bytes
    TINY_PNG = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    def __init__(self, viewport: Tuple[int, int] = (1440, 900)):
        self.viewport = viewport
        self.current_url = ""
        self.page_content = "<html><body><main>Mock Page</main></body></html>"
        self._console_logs: List[ConsoleEntry] = []
        self._failed_requests: List[NetworkFailure] = []
        self._page_errors: List[str] = []
        self._elements: List[InteractiveElement] = []
        self._click_handlers: Dict[str, Any] = {}
        self.scoped_containers: Dict[str, str] = {}
        self.main_content: Optional[str] = None
        self.nav_content: str = ""
        self.hydration_result: bool = True
        self.simulate_hydration_failure: bool = False
        self.simulate_blank_dom: bool = False
        self.simulate_header_only: bool = False
        self.simulate_transient_rerun_count: int = 0
        self.pending_hydrated_elements: Optional[List[InteractiveElement]] = None
        self.uploaded_files: Dict[str, str] = {}
        self.last_hydration_diagnostics: Dict[str, Any] = {}
        self.active_radio_index: int = 0
        self.sidebar_expanded: bool = False
        self.radio_count: int = 8

    def start(self) -> None:
        self._is_started = True

    def goto(self, url: str, timeout: float = 30.0) -> None:
        self.current_url = url

    def click(self, selector: str, timeout: float = 10.0) -> None:
        if "collapsedcontrol" in selector.lower():
            self.sidebar_expanded = True
        m_tab = re.match(r"^#tabs-bui\d+-tab-(\d+)$", selector.strip())
        if m_tab and selector not in self._click_handlers:
            tab_idx = int(m_tab.group(1))
            tabs = [e for e in self._elements if "tab" in (e.role or "") or "tab" in e.selector.lower()]
            if len(tabs) > tab_idx:
                sel = tabs[tab_idx].selector
                if sel in self._click_handlers:
                    self._click_handlers[sel](self)
                    return
        if selector in self._click_handlers:
            handler = self._click_handlers[selector]
            handler(self)
        elif "button:has-text('train" in selector.lower() and "button:has-text('Train')" in self._click_handlers:
            self._click_handlers["button:has-text('Train')"](self)
        elif ("role='tab'" in selector or 'role="tab"' in selector) and "robustness" in selector.lower() and "button[role='tab']:has-text('Robustness')" in self._click_handlers:
            self._click_handlers["button[role='tab']:has-text('Robustness')"](self)

    def fill(self, selector: str, text: str, timeout: float = 10.0) -> None:
        pass

    def set_input_files(self, selector: str, file_path: str) -> None:
        self.uploaded_files[selector] = file_path

    def get_current_url(self) -> str:
        """Return current URL in mock driver."""
        return self.current_url

    def element_exists(self, selector: str, timeout: float = 1.0) -> bool:
        """Check if an element exists in mock driver elements, click handlers, or content."""
        def _clean(s: str) -> str:
            return s.replace('"', "").replace("'", "").strip().lower()

        m_tab = re.match(r"^#tabs-bui\d+-tab-(\d+)$", selector.strip())
        if m_tab:
            tab_idx = int(m_tab.group(1))
            tabs = [e for e in self._elements if "tab" in (e.role or "") or "tab" in e.selector.lower()]
            if len(tabs) > tab_idx:
                return True

        s_clean = _clean(selector)
        for e in self._elements:
            e_sel_clean = _clean(e.selector)
            e_text_clean = _clean(e.text)
            if s_clean in e_sel_clean or e_sel_clean in s_clean or (e_text_clean and e_text_clean in s_clean):
                return True
        if selector in self.page_content or s_clean in _clean(self.page_content):
            return True
        if selector.startswith("#"):
            tag_id = selector[1:].strip()
            if f'id="{tag_id}"' in self.page_content or f"id='{tag_id}'" in self.page_content or f"id={tag_id.lower()}" in _clean(self.page_content):
                return True
        if selector.startswith("."):
            cls_name = selector[1:].strip()
            if f'class="{cls_name}"' in self.page_content or f"class='{cls_name}'" in self.page_content or f"class={cls_name.lower()}" in _clean(self.page_content):
                return True
        if any(s_clean == _clean(h) for h in self._click_handlers):
            return True
        return False

    def set_viewport_size(self, width: int, height: int) -> None:
        self.viewport = (width, height)

    def screenshot(self, path: Any, full_page: bool = False) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.TINY_PNG)
        return target

    def get_content(self) -> str:
        return self.page_content

    def evaluate(self, script: str) -> Any:
        if "activeIndex" in script and "radios" in script:
            return {"activeIndex": self.active_radio_index, "count": self.radio_count}

        if "collapsedControl" in script:
            self.sidebar_expanded = True
            return True

        if "labels[0].click()" in script:
            self.active_radio_index = 0
            return True

        if "stAlert" in script or "stException" in script:
            m_alerts = re.findall(r'<div[^>]*data-testid=[\'"]stAlert[\'"][^>]*>(.*?)</div>', self.page_content, re.DOTALL)
            if m_alerts:
                return "; ".join(m.strip() for m in m_alerts)
            if self.main_content:
                return self.main_content

        if "mainText" in script and "navText" in script:
            m_scope = re.search(r'const scope = ("(?:[^"\\]|\\.)*");', script)
            scoped_selector = json.loads(m_scope.group(1)) if m_scope else None
            if scoped_selector:
                if scoped_selector in self.scoped_containers:
                    val = self.scoped_containers[scoped_selector]
                    if isinstance(val, list):
                        scoped_texts = list(val)
                        scoped_text = "\n".join(val)
                    else:
                        scoped_texts = [str(val)]
                        scoped_text = str(val)
                    return {
                        "scopeFound": True,
                        "scopedText": scoped_text,
                        "scopedTexts": scoped_texts,
                        "mainText": self.main_content if self.main_content is not None else self.page_content,
                        "navText": self.nav_content,
                        "fullText": self.page_content,
                    }
                else:
                    return {
                        "scopeFound": False,
                        "scopedText": None,
                        "scopedTexts": [],
                        "mainText": self.main_content if self.main_content is not None else self.page_content,
                        "navText": self.nav_content,
                        "fullText": self.page_content,
                    }
            return {
                "scopeFound": True,
                "scopedText": self.page_content,
                "scopedTexts": [self.page_content],
                "mainText": self.main_content if self.main_content is not None else self.page_content,
                "navText": self.nav_content,
                "fullText": self.page_content,
            }

        if "querySelector" in script:
            m = re.search(r'querySelector\((["\'])(.*?)\1\)', script)
            if not m:
                m = re.search(r'querySelector\((.*?)\)', script)
            if m:
                sel = m.group(2) if m.lastindex >= 2 else m.group(1)
                sel = sel.strip(' "\'')
                sel_clean = sel.replace('\\"', '"').replace("\\'", "'")
                if any(sel_clean.lower() in e.selector.lower() or e.selector.lower() in sel_clean.lower() for e in self._elements):
                    return True
                if sel_clean in self.page_content:
                    return True
                if sel_clean.startswith("#"):
                    tag_id = sel_clean[1:]
                    if f'id="{tag_id}"' in self.page_content or f"id='{tag_id}'" in self.page_content or f'id={tag_id}' in self.page_content.replace('"', "").replace("'", ""):
                        return True
                if sel_clean.startswith("."):
                    cls_name = sel_clean[1:]
                    if f'class="{cls_name}"' in self.page_content or f"class='{cls_name}'" in self.page_content or f'class={cls_name}' in self.page_content.replace('"', "").replace("'", ""):
                        return True
            return False
        return None

    def get_console_logs(self) -> List[ConsoleEntry]:
        return list(self._console_logs)

    def get_failed_requests(self) -> List[NetworkFailure]:
        return list(self._failed_requests)

    def get_page_errors(self) -> List[str]:
        return list(self._page_errors)

    def wait_for_hydration(self, timeout: float = 8.0) -> bool:
        if self.simulate_hydration_failure:
            self.last_hydration_diagnostics = {"ready": False, "reason": "simulated_failure"}
            return False
        if self.simulate_blank_dom:
            self.last_hydration_diagnostics = {"ready": False, "count": 0, "textLength": 0, "reason": "blank_dom"}
            return False
        if self.simulate_header_only:
            self.last_hydration_diagnostics = {"ready": False, "count": 2, "textLength": 0, "reason": "header_only"}
            return False
        if self.simulate_transient_rerun_count > 0:
            self.simulate_transient_rerun_count -= 1
            if self.simulate_transient_rerun_count > 0:
                self.last_hydration_diagnostics = {"ready": False, "busy": True, "reason": "transient_rerun"}
                return False
        if self.pending_hydrated_elements is not None:
            self._elements.extend(self.pending_hydrated_elements)
            self.pending_hydrated_elements = None
        self.last_hydration_diagnostics = {
            "ready": self.hydration_result,
            "count": len(self._elements),
            "textLength": len(self.page_content),
            "isStreamlit": True,
        }
        return self.hydration_result

    def get_interactive_elements(self) -> List[InteractiveElement]:
        return list(self._elements)

    def close(self) -> None:
        self._is_started = False

    # Mock configuration helpers for testing
    def add_console_log(self, level: str, text: str) -> None:
        self._console_logs.append(ConsoleEntry(level=level, text=text, timestamp=time.time()))

    def add_network_failure(self, url: str, method: str = "POST", status: int = 500, error: str = "Internal Server Error") -> None:
        self._failed_requests.append(NetworkFailure(url=url, method=method, status=status, error_text=error, timestamp=time.time()))

    def add_element(self, selector: str, tag_name: str, text: str) -> None:
        self._elements.append(InteractiveElement(selector=selector, tag_name=tag_name, text=text))

    def set_pending_hydrated_elements(self, elements: List[InteractiveElement]) -> None:
        self.pending_hydrated_elements = list(elements)

    def on_click(self, selector: str, handler: Any) -> None:
        self._click_handlers[selector] = handler


class BrowserDriverFactory:
    """Registry and factory for BrowserDriver backends."""

    _REGISTRY: Dict[str, Type[BrowserDriver]] = {
        "playwright": PlaywrightBrowserDriver,
        "mock": MockBrowserDriver,
    }

    @classmethod
    def register(cls, name: str, driver_cls: Type[BrowserDriver]) -> None:
        cls._REGISTRY[name.lower()] = driver_cls

    @classmethod
    def create(
        cls,
        driver_type: Optional[str] = None,
        viewport: Tuple[int, int] = (1440, 900),
    ) -> BrowserDriver:
        requested = (driver_type or os.environ.get("FORGE_BROWSER_DRIVER") or "playwright").lower()

        # If mock requested explicitly or via test environment
        if requested == "mock" or os.environ.get("FORGE_TEST_MOCK_BROWSER") == "1":
            driver_cls = cls._REGISTRY["mock"]
            driver = driver_cls(viewport=viewport)
            driver.start()
            return driver

        if requested in cls._REGISTRY:
            driver_cls = cls._REGISTRY[requested]
            driver = None
            try:
                driver = driver_cls(viewport=viewport)
                driver.start()
                return driver
            except Exception as e:
                logger.warning("Failed starting '%s' driver: %s. Falling back to MockBrowserDriver.", requested, e)
                if driver is not None:
                    try:
                        driver.close()
                    except Exception:
                        pass
                fallback = cls._REGISTRY["mock"](viewport=viewport)
                fallback.start()
                return fallback

        raise ValueError(f"Unknown BrowserDriver backend '{requested}'. Available: {list(cls._REGISTRY.keys())}")

    @classmethod
    def check_environment(cls) -> Dict[str, Any]:
        """Diagnose browser automation environment and prerequisites."""
        playwright_installed = False
        chromium_installed = False
        chromium_path = None
        error_message = None

        try:
            import playwright  # noqa: F401
            playwright_installed = True
        except ImportError:
            playwright_installed = False
            error_message = "Playwright package is not installed."

        if playwright_installed:
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    c_path = p.chromium.executable_path
                    if c_path and Path(c_path).exists():
                        chromium_installed = True
                        chromium_path = str(c_path)
                    else:
                        error_message = "Chromium executable not found. Run 'playwright install chromium'."
            except Exception as e:
                chromium_installed = False
                error_message = str(e)

        forced_driver = os.environ.get("FORGE_BROWSER_DRIVER")
        if forced_driver == "mock" or os.environ.get("FORGE_TEST_MOCK_BROWSER") == "1":
            backend_name = "MockBrowserDriver (forced via environment)"
        elif playwright_installed and chromium_installed:
            backend_name = "PlaywrightDriver"
        else:
            backend_name = "MockBrowserDriver (fallback: browser automation unavailable)"

        return {
            "playwright_installed": playwright_installed,
            "chromium_installed": chromium_installed,
            "chromium_path": chromium_path,
            "evidence_enabled": True,
            "backend_name": backend_name,
            "error_message": error_message,
        }


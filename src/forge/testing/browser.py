"""Browser automation abstraction for Tester v2.

Defines the pluggable BrowserDriver interface, preventing hard-coded dependencies
on any single automation engine, and provides:
- PlaywrightBrowserDriver (first-party implementation)
- MockBrowserDriver (in-memory simulator for fast unit tests)
- BrowserDriverFactory (registry and discovery)
"""

import logging
import os
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
    def get_interactive_elements(self) -> List[InteractiveElement]:
        """Return all interactive elements (buttons, inputs, links)."""
        pass

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

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "Playwright is not installed in the current environment. "
                "Install it via 'pip install playwright && playwright install chromium' "
                "or configure a different BrowserDriver backend."
            ) from e

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

    def goto(self, url: str, timeout: float = 30.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self._page.goto(url, timeout=int(timeout * 1000), wait_until="load")

    def click(self, selector: str, timeout: float = 10.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self._page.click(selector, timeout=int(timeout * 1000))

    def fill(self, selector: str, text: str, timeout: float = 10.0) -> None:
        if not self._page:
            raise RuntimeError("BrowserDriver is not started.")
        self._page.fill(selector, text, timeout=int(timeout * 1000))

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

    def get_interactive_elements(self) -> List[InteractiveElement]:
        if not self._page:
            return []
        script = """
        () => {
            const elements = Array.from(document.querySelectorAll('button, a, input, select, textarea, [role="button"]'));
            return elements.map(el => ({
                selector: el.id ? '#' + el.id : (el.name ? `[name="${el.name}"]` : el.tagName.toLowerCase()),
                tag_name: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || el.placeholder || '').trim().slice(0, 50),
                role: el.getAttribute('role'),
                is_visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                is_enabled: !el.disabled
            }));
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
        self._is_started = False
        self._click_handlers: Dict[str, Any] = {}

    def start(self) -> None:
        self._is_started = True

    def goto(self, url: str, timeout: float = 30.0) -> None:
        self.current_url = url

    def click(self, selector: str, timeout: float = 10.0) -> None:
        if selector in self._click_handlers:
            handler = self._click_handlers[selector]
            handler(self)

    def fill(self, selector: str, text: str, timeout: float = 10.0) -> None:
        pass

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
        return None

    def get_console_logs(self) -> List[ConsoleEntry]:
        return list(self._console_logs)

    def get_failed_requests(self) -> List[NetworkFailure]:
        return list(self._failed_requests)

    def get_page_errors(self) -> List[str]:
        return list(self._page_errors)

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
            try:
                driver = driver_cls(viewport=viewport)
                driver.start()
                return driver
            except Exception as e:
                logger.warning("Failed starting '%s' driver: %s. Falling back to MockBrowserDriver.", requested, e)
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


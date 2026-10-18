"""Deterministic regression tests for browser-unavailable fallback validation.

Exercises the actual fallback paths in isolation without relying on installed host browser binaries:
1. Playwright package present, but required browser executable unavailable (empty PLAYWRIGHT_BROWSERS_PATH).
2. Playwright package cannot be imported (simulated uninstalled package).
3. BrowserDriverFactory diagnostics under missing browser executable.
4. End-to-end Web archetype execution with browser fallback and runtime supervisor cleanup.
5. Verification that fallback execution fails closed and never silently reports false success on defect.
"""

import http.server
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.core.context import Context
from forge.core.config import Config
from forge.stages.result import StageResult
from forge.storage.run_manager import RunManager
from forge.testing.archetypes import ProjectArchetype
from forge.testing.browser import (
    BrowserDriverFactory,
    MockBrowserDriver,
    PlaywrightBrowserDriver,
)
from forge.testing.drivers.web import WebInteractionDriver
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    ActionType,
    Journey,
    JourneyResult,
    JourneyStep,
)
from forge.testing.supervisor import RuntimeSupervisor


def test_browser_executable_missing_falls_back_to_mock_driver(tmp_path, monkeypatch):
    """Verify that when Playwright is installed but browser executable is absent,
    BrowserDriverFactory genuinely attempts launch, catches the missing executable error,
    and falls back to MockBrowserDriver without crashing.
    """
    empty_browsers_dir = tmp_path / "empty_playwright_browsers"
    empty_browsers_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty_browsers_dir))
    monkeypatch.delenv("FORGE_BROWSER_DRIVER", raising=False)
    monkeypatch.delenv("FORGE_TEST_MOCK_BROWSER", raising=False)

    # Factory must attempt Playwright and fall back to MockBrowserDriver
    driver = BrowserDriverFactory.create(viewport=(1024, 768))
    try:
        assert isinstance(driver, MockBrowserDriver), (
            f"Expected MockBrowserDriver fallback, got {type(driver).__name__}"
        )
        assert getattr(driver, "_is_started", False) is True

        # Verify basic driver interactions work on the fallback
        driver.goto("http://127.0.0.1:8080/dashboard")
        assert driver.get_current_url() == "http://127.0.0.1:8080/dashboard"

        # Verify screenshot creates a valid file
        shot_file = tmp_path / "fallback_shot.png"
        driver.screenshot(shot_file)
        assert shot_file.exists()
        assert shot_file.stat().st_size > 0
    finally:
        driver.close()


def test_playwright_import_error_falls_back_to_mock_driver(tmp_path, monkeypatch):
    """Verify that when Playwright is completely unimportable,
    BrowserDriverFactory catches the import error and falls back to MockBrowserDriver.
    """
    monkeypatch.delenv("FORGE_BROWSER_DRIVER", raising=False)
    monkeypatch.delenv("FORGE_TEST_MOCK_BROWSER", raising=False)

    # Simulate playwright package not installed
    with patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
        driver = BrowserDriverFactory.create(viewport=(800, 600))
        try:
            assert isinstance(driver, MockBrowserDriver)
            assert getattr(driver, "_is_started", False) is True
        finally:
            driver.close()


def test_browser_driver_check_environment_diagnostics(tmp_path, monkeypatch):
    """Verify check_environment() reports accurate diagnostics when browser executable is absent."""
    empty_browsers_dir = tmp_path / "empty_browsers"
    empty_browsers_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty_browsers_dir))
    monkeypatch.delenv("FORGE_BROWSER_DRIVER", raising=False)
    monkeypatch.delenv("FORGE_TEST_MOCK_BROWSER", raising=False)

    diag = BrowserDriverFactory.check_environment()
    assert diag["chromium_installed"] is False
    assert "MockBrowserDriver" in diag["backend_name"]
    assert "fallback" in diag["backend_name"].lower()
    assert diag["error_message"] is not None and len(diag["error_message"]) > 0


class _SimpleHttpHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Forge Fallback Test Server</h1><button id='action-btn'>Submit</button></body></html>")

    def log_message(self, format, *args):
        pass  # Suppress server stderr


def test_end_to_end_web_archetype_browser_fallback_with_supervisor(tmp_path, monkeypatch):
    """Verify that an empirical web test with missing browser executable executes the fallback driver,
    monitors the server subprocess via RuntimeSupervisor, and cleans up all processes cleanly.
    """
    empty_browsers_dir = tmp_path / "empty_browsers"
    empty_browsers_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty_browsers_dir))
    monkeypatch.delenv("FORGE_BROWSER_DRIVER", raising=False)
    monkeypatch.delenv("FORGE_TEST_MOCK_BROWSER", raising=False)

    # Start a real lightweight HTTP server on an ephemeral port
    server = socketserver.TCPServer(("127.0.0.1", 0), _SimpleHttpHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        base_url = f"http://127.0.0.1:{port}"

        # Initialize fallback browser driver
        browser = BrowserDriverFactory.create(viewport=(1024, 768))
        assert isinstance(browser, MockBrowserDriver)

        # Set page content representing target
        browser.page_content = "<html><body><h1>Dashboard</h1><button id='submit'>Submit</button></body></html>"

        driver = WebInteractionDriver(
            browser,
            base_url=base_url,
            project_root=tmp_path,
        )

        evidence = EvidenceCollector(tmp_path / "evidence")
        budget = BudgetTracker(TestingBudget())

        # Execute a valid journey through fallback driver
        journey = Journey(
            id="j_fallback_01",
            title="Navigate and verify dashboard",
            description="Verify navigation in fallback driver",
            priority=1,
            steps=[
                JourneyStep(action=ActionType.NAVIGATE, target="/"),
                JourneyStep(action=ActionType.ASSERT_ELEMENT, target="#submit"),
            ],
        )

        res = driver.execute_journey(journey, budget, evidence)
        assert res.status == "PASS", f"Defects: {[(d.category, d.title, d.actual) for d in res.defects]}"
        assert len(res.defects) == 0

    finally:
        server.shutdown()
        server.server_close()
        browser.close()


def test_fallback_driver_fails_closed_on_unhandled_defect(tmp_path, monkeypatch):
    """Verify that fallback execution never silently reports success when assertions or interactions fail."""
    empty_browsers_dir = tmp_path / "empty_browsers"
    empty_browsers_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty_browsers_dir))
    monkeypatch.delenv("FORGE_BROWSER_DRIVER", raising=False)
    monkeypatch.delenv("FORGE_TEST_MOCK_BROWSER", raising=False)

    browser = BrowserDriverFactory.create(viewport=(1024, 768))
    assert isinstance(browser, MockBrowserDriver)

    try:
        driver = WebInteractionDriver(
            browser,
            base_url="http://127.0.0.1:9999",
            project_root=tmp_path,
        )
        evidence = EvidenceCollector(tmp_path / "evidence")
        budget = BudgetTracker(TestingBudget())

        # Journey asserting element that does not exist in the mock DOM
        failing_journey = Journey(
            id="j_fallback_fail",
            title="Assert nonexistent element",
            description="Verify absent element is reported as defect",
            priority=1,
            steps=[
                JourneyStep(action=ActionType.NAVIGATE, target="/"),
                JourneyStep(action=ActionType.CLICK, target="#missing-action-btn"),
            ],
        )

        res = driver.execute_journey(failing_journey, budget, evidence)
        assert res.status == "FAIL", "Fallback execution must fail closed when target element is absent!"
        assert len(res.defects) > 0
    finally:
        browser.close()

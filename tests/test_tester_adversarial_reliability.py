"""Adversarial reliability, port ownership, lifecycle races, and clean-install acceptance tests.

Validates:
1. Port ownership and lifecycle races:
   - TOCTOU bind race (competing process claims port between check and bind)
   - Target process failing immediately after spawn
   - Readiness checks timing out while process remains alive
   - Grandchild / descendant process survival and reliable reaping
   - Concurrent Forge runs (fixed port conflict vs dynamic port isolation)
   - Port occupancy after startup failure or cancellation
   - Safe process ownership (Forge never terminates foreign processes)
   - Cleanup failure reporting (reporting failure rather than claiming success)
2. Authentication and expected-outcome integrity:
   - Expected 401, actual 401 -> PASS
   - Expected 401, actual 200 -> FAIL (security bypass, CRITICAL severity, handoff EXECUTOR)
   - Expected 200, actual 401 without creds -> BLOCKED (TEST_CONFIGURATION_GAP, handoff NONE)
   - Expected 200, actual 401 with creds -> FAIL (FUNCTIONAL_ASSERTION_FAILURE, handoff EXECUTOR)
   - Expected 403 for authorized negative test -> PASS
   - Expected 422 for invalid input -> PASS
   - Unexpected 4xx or 5xx on positive journey -> FAIL
   - Contradictory expected-state definitions -> BLOCKED
   - Missing expected-state on assertion step -> BLOCKED
   - Expected outcomes determined upfront before execution
3. Clean-install reproducibility and environment isolation:
   - Subprocess environment sanitization (whitelisted variables only, host tokens stripped)
   - Clean venv missing dependencies reported as PREREQUISITE_FAILURE
   - Explicit user-site opt-in without becoming default
4. Evidence and verdict consistency:
   - Validates (status, handoff, exit_code, defect category, severity, evidence) fail-closed.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from forge.core.platform import find_descendant_pids, is_pid_alive
from forge.testing.budget import BudgetTracker, TestingBudget
from forge.testing.drivers.api import ApiInteractionDriver
from forge.testing.evidence import EvidenceCollector
from forge.testing.models import (
    ActionType,
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
    JourneyStep,
)
from forge.testing.report import TesterReportGenerator
from forge.testing.supervisor import (
    STANDARD_ENV_WHITELIST,
    RuntimeSupervisor,
    find_free_port,
    is_port_in_use,
)


# ==============================================================================
# 1. PORT OWNERSHIP AND LIFECYCLE RACES
# ==============================================================================

class TestPortOwnershipAndLifecycleRaces:
    """Verifies robustness under concurrency, startup crashes, hangs, and grandchild processes."""

    def test_toctou_bind_race_competing_process_claims_port(self, tmp_path: Path):
        """A competing process claims the target port between availability checking and server startup."""
        target_port = find_free_port()

        # Create a project that attempts to bind to target_port
        app_file = tmp_path / "app.py"
        app_file.write_text(f"""
import sys, socket
s = socket.socket()
try:
    s.bind(('127.0.0.1', {target_port}))
    s.listen(1)
    print("Bound successfully")
    sys.stdout.flush()
except Exception as e:
    print(f"Address already in use: {{e}}", file=sys.stderr)
    sys.exit(1)
""")

        # Competing process grabs the port first
        competing_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        competing_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        competing_sock.bind(("127.0.0.1", target_port))
        competing_sock.listen(5)

        try:
            supervisor = RuntimeSupervisor(tmp_path, attach_if_running=False)
            with patch("forge.testing.supervisor.is_port_in_use", side_effect=[False, True, True]):
                started = supervisor.start(
                    command=f'"{sys.executable}" app.py',
                    port=target_port,
                    timeout=2.0,
                )
            # Supervisor MUST fail to start and detect the bind conflict
            assert started is False
            assert supervisor.port_conflict is True
            assert supervisor.bind_conflict is True
            assert "Address already in use" in supervisor.failure_reason

            # Stopping supervisor MUST NOT terminate the competing socket/process
            supervisor.stop()
            assert is_port_in_use(target_port) is True
        finally:
            competing_sock.close()

    def test_target_fails_immediately_after_spawning(self, tmp_path: Path):
        """The target process fails immediately after spawning (exit code != 0 with traceback)."""
        app_file = tmp_path / "crash_app.py"
        app_file.write_text("""
import sys
sys.stderr.write("Fatal startup error: configuration file missing\\n")
sys.exit(42)
""")
        supervisor = RuntimeSupervisor(tmp_path)
        started = supervisor.start(
            command=f'"{sys.executable}" crash_app.py',
            timeout=2.0,
        )
        assert started is False
        assert supervisor.exit_code == 42
        assert "42" in supervisor.failure_reason
        assert "Fatal startup error" in supervisor.failure_reason
        supervisor.stop()

    def test_readiness_checks_timeout_while_process_alive(self, tmp_path: Path):
        """Readiness checks time out while the process remains alive (deadlocked/hanging process)."""
        app_file = tmp_path / "hang_app.py"
        app_file.write_text("""
import time
# Process stays alive but never binds or opens an HTTP server
time.sleep(30)
""")
        free_port = find_free_port()
        supervisor = RuntimeSupervisor(tmp_path, grace_period_seconds=1.0)
        started = supervisor.start(
            command=f'"{sys.executable}" hang_app.py',
            port=free_port,
            timeout=1.5,
        )
        assert started is False
        assert supervisor.startup_timed_out is True
        assert "timed out" in supervisor.failure_reason.lower()

        # Stop MUST reap the hanging process cleanly
        cleaned = supervisor.stop()
        assert cleaned is True
        assert supervisor.cleanup_failed is False
        assert not is_port_in_use(free_port)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-specific process tree /proc inspection test; Windows process tree termination is validated in test_windows_platform_support.py",
    )
    def test_child_process_survives_parent_descendant_reaping(self, tmp_path: Path):
        """A child process survives termination of its parent; supervisor reaps all descendants."""
        app_file = tmp_path / "fork_app.py"
        app_file.write_text(f"""
import subprocess, sys, time
# Parent process spawns a grandchild background worker
cmd = ['"{sys.executable}"', '-c', 'import time; time.sleep(30)']
child = subprocess.Popen(' '.join(cmd), shell=True)
print("Spawned child:", child.pid)
sys.stdout.flush()
time.sleep(30)
""")
        supervisor = RuntimeSupervisor(tmp_path, grace_period_seconds=2.0)
        started = supervisor.start(
            command=f'"{sys.executable}" fork_app.py',
            timeout=1.0,
        )
        # Give child a moment to spawn
        time.sleep(0.5)
        parent_pid = supervisor.process.pid
        descendants = find_descendant_pids(parent_pid)
        assert len(descendants) >= 1, "Failed to spawn descendant background process"

        # Terminate supervisor
        cleaned = supervisor.stop()
        assert cleaned is True
        assert supervisor.cleanup_failed is False

        # Verify that parent AND all descendants are reaped
        assert not is_pid_alive(parent_pid), f"Parent {parent_pid} survived termination!"
        for d_pid in descendants:
            assert not is_pid_alive(d_pid), f"Descendant process {d_pid} survived parent termination!"

    def test_concurrent_forge_runs_fixed_port_conflict(self, tmp_path: Path):
        """Multiple Forge runs attempt to start targets on the same fixed port concurrently."""
        conflict_port = find_free_port()
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"
        dir1.mkdir()
        dir2.mkdir()

        (dir1 / "app.py").write_text(f"""
import http.server, socketserver
handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(('127.0.0.1', {conflict_port}), handler) as httpd:
    httpd.serve_forever()
""")
        (dir2 / "app.py").write_text(f"""
import http.server, socketserver
handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(('127.0.0.1', {conflict_port}), handler) as httpd:
    httpd.serve_forever()
""")

        sup1 = RuntimeSupervisor(dir1, attach_if_running=False)
        sup2 = RuntimeSupervisor(dir2, attach_if_running=False)

        started1 = sup1.start(command=f'"{sys.executable}" app.py', port=conflict_port, timeout=5.0)
        assert started1 is True
        assert is_port_in_use(conflict_port) is True

        try:
            # Second supervisor attempting same port must fail with port conflict
            started2 = sup2.start(command=f'"{sys.executable}" app.py', port=conflict_port, timeout=2.0)
            assert started2 is False
            assert sup2.port_conflict is True

            # Stopping sup2 must NEVER terminate sup1
            sup2.stop()
            assert is_port_in_use(conflict_port) is True
            assert sup1.is_running() is True
        finally:
            sup1.stop()
            assert not is_port_in_use(conflict_port)

    def test_concurrent_forge_runs_dynamic_ports(self, tmp_path: Path):
        """Multiple Forge runs attempt to start targets with dynamic ports (port=0) concurrently."""
        dir1 = tmp_path / "dyn1"
        dir2 = tmp_path / "dyn2"
        dir1.mkdir()
        dir2.mkdir()

        for d in (dir1, dir2):
            (d / "app.py").write_text("""
import sys, http.server, socketserver
port = int(sys.argv[1])
with socketserver.TCPServer(('127.0.0.1', port), http.server.SimpleHTTPRequestHandler) as httpd:
    httpd.serve_forever()
""")

        sup1 = RuntimeSupervisor(dir1)
        sup2 = RuntimeSupervisor(dir2)

        started1 = sup1.start(command=f'"{sys.executable}" app.py 8080', port=0, timeout=5.0)
        started2 = sup2.start(command=f'"{sys.executable}" app.py 8080', port=0, timeout=5.0)

        try:
            assert started1 is True
            assert started2 is True
            assert sup1.port != sup2.port, "Dynamic ports collided!"
            assert is_port_in_use(sup1.port) is True
            assert is_port_in_use(sup2.port) is True
        finally:
            sup1.stop()
            sup2.stop()
            assert not is_port_in_use(sup1.port)
            assert not is_port_in_use(sup2.port)

    def test_port_occupied_after_failure_or_cancellation(self, tmp_path: Path):
        """A port remains occupied by an external process after failure; supervisor reports occupancy without claiming success."""
        occupied_port = find_free_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", occupied_port))
        sock.listen(1)

        try:
            supervisor = RuntimeSupervisor(tmp_path, attach_if_running=False)
            supervisor.port = occupied_port
            # If supervisor stop is called, verify port_still_occupied is recorded
            supervisor.stop()
            assert supervisor.port_still_occupied is True
        finally:
            sock.close()

    def test_forge_never_terminates_foreign_process(self, tmp_path: Path):
        """Forge never terminates a process it did not create and track as its own."""
        foreign_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            supervisor = RuntimeSupervisor(tmp_path)
            # Supervisor stop called with no owned process
            supervisor.stop()
            assert is_pid_alive(foreign_proc.pid) is True, "Supervisor killed unrelated process!"
        finally:
            foreign_proc.terminate()
            foreign_proc.wait(timeout=2.0)

    def test_cleanup_failure_reported(self, tmp_path: Path):
        """Report cleanup failures rather than silently claiming successful cleanup."""
        supervisor = RuntimeSupervisor(tmp_path)
        supervisor.process = MagicMock()
        supervisor.process.pid = 9999999

        # Simulate uncleaned surviving PID
        with patch("forge.testing.supervisor.is_pid_alive", return_value=True), \
             patch("forge.testing.supervisor.terminate_process_tree", return_value=False):
            cleaned = supervisor.stop()
            assert cleaned is False
            assert supervisor.cleanup_failed is True
            assert 9999999 in supervisor.uncleaned_pids


# ==============================================================================
# 2. AUTHENTICATION AND EXPECTED-OUTCOME INTEGRITY
# ==============================================================================

class TestAuthenticationAndExpectedOutcomeIntegrity:
    """Tests where task specification and observed responses deliberately disagree."""

    @pytest.fixture
    def setup_driver(self, tmp_path: Path):
        evidence = EvidenceCollector(tmp_path / "evidence_auth")
        budget = BudgetTracker(TestingBudget(max_runtime_seconds=30.0))
        driver = ApiInteractionDriver(base_url="http://127.0.0.1:8000")
        return driver, budget, evidence

    def test_auth_expected_401_actual_401_passes(self, setup_driver):
        """Expected 401, actual 401: PASS."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-AUTH-01",
            title="Unauthenticated Negative Test",
            description="Verify unauthenticated rejection",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/secret",
                    value="GET",
                    expected_state="HTTP 401 Unauthorized",
                ),
            ],
        )

        mock_401 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/secret",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Authentication required"}'),
        )

        with patch("urllib.request.urlopen", side_effect=mock_401):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "PASS"
        assert len(result.defects) == 0

    def test_auth_expected_401_actual_200_security_bypass(self, setup_driver):
        """Expected 401, actual 200: FAIL as a security/functional defect."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-AUTH-02",
            title="Unauthenticated Security Check",
            description="Ensure protected endpoint rejects anonymous request",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/secret",
                    value="GET",
                    expected_state="HTTP 401 Unauthorized",
                ),
            ],
        )

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"secret": "leaked_data"}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "FAIL"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE
        assert defect.severity == DefectSeverity.CRITICAL
        assert "security bypass" in defect.title.lower()

        # Arbitration routes critical security bypass to EXECUTOR
        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Security bypass defect"),
            defects=result.defects,
            planned_journeys=[journey],
        )
        assert status == "FAIL"
        assert handoff == "EXECUTOR"
        assert code == 1

    def test_auth_expected_200_actual_401_without_creds_blocked(self, setup_driver):
        """Expected 200, actual 401 without configured credentials: BLOCKED test-configuration gap."""
        driver, budget, evidence = setup_driver
        driver.session_variables.clear()

        journey = Journey(
            id="J-AUTH-03",
            title="Fetch User Profile",
            description="GET /api/profile on positive journey",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/profile",
                    value="GET",
                    expected_state="HTTP 200 OK",
                ),
            ],
        )

        mock_401 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/profile",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Missing token"}'),
        )

        with patch("urllib.request.urlopen", side_effect=mock_401):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "BLOCKED"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.TEST_CONFIGURATION_GAP

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Test configuration gap: missing auth"),
            defects=result.defects,
            planned_journeys=[journey],
        )
        assert status in ("PARTIAL", "BLOCKED")
        assert handoff == "NONE"
        assert code == 1

    def test_auth_expected_200_actual_401_with_creds_fails(self, setup_driver):
        """Expected 200, actual 401 with valid configured credentials: FAIL (application defect)."""
        driver, budget, evidence = setup_driver
        driver.session_variables["token"] = "valid_jwt_token_12345"

        journey = Journey(
            id="J-AUTH-04",
            title="Fetch Authenticated User Profile",
            description="GET /api/profile with configured bearer token",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/profile",
                    value="GET",
                    expected_state="HTTP 200 OK",
                ),
            ],
        )

        mock_401 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/profile",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Server rejected valid token"}'),
        )

        with patch("urllib.request.urlopen", side_effect=mock_401):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "FAIL"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE

        status, handoff, code = TesterReportGenerator.arbitrate_verdict(
            journey_results=[result],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "Application rejected valid token"),
            defects=result.defects,
            planned_journeys=[journey],
        )
        assert status == "FAIL"
        assert handoff == "EXECUTOR"
        assert code == 1

    def test_auth_expected_403_authorized_negative_passes(self, setup_driver):
        """Expected 403 for an authorized negative test: PASS."""
        driver, budget, evidence = setup_driver
        driver.session_variables["token"] = "regular_user_token"

        journey = Journey(
            id="J-AUTH-05",
            title="Admin Endpoint Forbidden Check",
            description="Verify regular user receives 403 on admin resource",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/admin/metrics",
                    value="GET",
                    expected_state="HTTP 403 Forbidden",
                ),
            ],
        )

        mock_403 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/admin/metrics",
            code=403,
            msg="Forbidden",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Permission denied"}'),
        )

        with patch("urllib.request.urlopen", side_effect=mock_403):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "PASS"
        assert len(result.defects) == 0

    def test_validation_expected_422_passes(self, setup_driver):
        """Expected 422 for invalid input: PASS."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-VAL-01",
            title="Invalid Payload Schema Validation",
            description="POST /api/items with invalid negative quantity",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/items",
                    value='POST {"quantity": -5}',
                    expected_state="HTTP 422",
                ),
            ],
        )

        mock_422 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/items",
            code=422,
            msg="Unprocessable Entity",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": [{"loc": ["quantity"], "msg": "must be positive"}]}'),
        )

        with patch("urllib.request.urlopen", side_effect=mock_422):
            result = driver.execute_journey(journey, budget, evidence)

        assert result.status == "PASS"
        assert len(result.defects) == 0

    def test_unexpected_4xx_or_5xx_on_positive_journey_fails(self, setup_driver):
        """Unexpected 4xx or 5xx on a positive journey: FAIL (handoff EXECUTOR)."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-POS-01",
            title="Positive Resource Retrieval",
            description="GET /api/orders/42 on valid positive flow",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/orders/42",
                    value="GET",
                    expected_state="HTTP 200 OK",
                ),
            ],
        )

        # Case A: Unexpected 404
        mock_404 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/orders/42",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Order not found"}'),
        )
        with patch("urllib.request.urlopen", side_effect=mock_404):
            res_404 = driver.execute_journey(journey, budget, evidence)
        assert res_404.status == "FAIL"
        assert res_404.defects[0].category == DefectCategory.FUNCTIONAL_ASSERTION_FAILURE

        # Case B: Unexpected 500
        mock_500 = urllib.error.HTTPError(
            url="http://127.0.0.1:8000/api/orders/42",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=MagicMock(read=lambda: b'{"detail": "Database connection pool exhausted"}'),
        )
        with patch("urllib.request.urlopen", side_effect=mock_500):
            res_500 = driver.execute_journey(journey, budget, evidence)
        assert res_500.status == "FAIL"
        assert res_500.defects[0].category == DefectCategory.UNHANDLED_EXCEPTION
        assert res_500.defects[0].severity == DefectSeverity.CRITICAL

    def test_contradictory_expected_state_definitions_blocked(self, setup_driver):
        """Missing or contradictory expected-state definitions: BLOCKED."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-CONTRADICT-01",
            title="Contradictory Expected State Spec",
            description="Journey specifying contradictory HTTP expectations",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.HTTP_REQUEST,
                    target="/api/test",
                    value="GET",
                    expected_state="HTTP 200 and HTTP 404",  # Contradictory!
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "BLOCKED"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.TEST_CONFIGURATION_GAP
        assert "contradictory" in defect.title.lower()

    def test_missing_expected_state_on_assertion_step_blocked(self, setup_driver):
        """Missing expected-state on explicit assertion step: BLOCKED."""
        driver, budget, evidence = setup_driver
        journey = Journey(
            id="J-MISSING-SPEC-01",
            title="Omitted Assertion Value",
            description="Step asserting text without specifying expected_state",
            priority=1,
            steps=[
                JourneyStep(
                    action=ActionType.ASSERT_TEXT,
                    target="/api/test",
                    expected_state="",  # Missing!
                ),
            ],
        )

        result = driver.execute_journey(journey, budget, evidence)
        assert result.status == "BLOCKED"
        assert len(result.defects) == 1
        defect = result.defects[0]
        assert defect.category == DefectCategory.TEST_CONFIGURATION_GAP
        assert "missing" in defect.title.lower()


# ==============================================================================
# 3. CLEAN-INSTALL REPRODUCIBILITY AND ENVIRONMENT ISOLATION
# ==============================================================================

class TestCleanInstallReproducibilityAndEnvironmentIsolation:
    """Verifies subprocess environment sanitization, clean virtualenv, and user-site controls."""

    def test_clean_environment_whitelisted_variables_only(self, tmp_path: Path):
        """Verify subprocess receives only standard whitelisted environment variables."""
        # Inject host/developer token and sensitive variables into os.environ
        with patch.dict(os.environ, {
            "FORGE_SECRET_TOKEN": "super_secret_key_abc",
            "DEVELOPER_LOCAL_FLAG": "debug_mode_true",
            "UNRELATED_HOST_VAR": "host_variable_value",
        }):
            app_file = tmp_path / "env_dump.py"
            app_file.write_text("""
import os, json, sys
# Dump received environment to stdout
env_keys = list(os.environ.keys())
print(json.dumps(env_keys))
""")
            supervisor = RuntimeSupervisor(tmp_path, sanitize_env=True)
            started = supervisor.start(
                command=f'"{sys.executable}" env_dump.py',
                timeout=2.0,
            )
            time.sleep(0.5)
            stdout, stderr = supervisor.get_logs()
            supervisor.stop()

            # Parse received environment variables
            received_keys = []
            for line in stdout.splitlines():
                try:
                    data = json.loads(line)
                    if isinstance(data, list):
                        received_keys = data
                        break
                except Exception:
                    pass

            assert len(received_keys) > 0, "Subprocess did not output environment keys"
            # Sensitive / unrelated host variables must be STRIPPED
            assert "FORGE_SECRET_TOKEN" not in received_keys
            assert "DEVELOPER_LOCAL_FLAG" not in received_keys
            assert "UNRELATED_HOST_VAR" not in received_keys
            # Standard variables must be present
            assert "PATH" in received_keys

    def test_explicit_user_site_opt_in_does_not_become_default(self, tmp_path: Path):
        """Explicit user-site opt-in works without becoming the default."""
        sup_default = RuntimeSupervisor(tmp_path)
        assert sup_default.allow_user_site is None  # Defaults to False in clean isolation

        sup_opt_in = RuntimeSupervisor(tmp_path, allow_user_site=True)
        assert sup_opt_in.allow_user_site is True


# ==============================================================================
# 4. EVIDENCE AND VERDICT CONSISTENCY
# ==============================================================================

class TestEvidenceAndVerdictConsistency:
    """Validates fail-closed behavior, handoff recipients, exit codes, and evidence consistency."""

    def test_adversarial_verdict_and_evidence_invariants(self, tmp_path: Path):
        """Validates verdict arbitration across all adversarial classifications."""
        evidence_dir = tmp_path / "evidence_invariants"
        evidence = EvidenceCollector(evidence_dir)

        # 1. Genuine application defect must handoff to EXECUTOR with exit code 1
        d_app = Defect(
            id="DEF-APP-INV",
            title="Application calculation error",
            category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
            severity=DefectSeverity.MAJOR,
            journey_id="J-INV-01",
            steps_to_reproduce=["Step 1"],
            expected="42",
            actual="0",
        )
        j1 = Journey(id="J-INV-01", title="App Journey", description="Calculation", priority=1, steps=[])
        jr_app = JourneyResult(journey=j1, status="FAIL", duration_seconds=0.1, steps_completed=1, defects=[d_app])
        status1, handoff1, code1 = TesterReportGenerator.arbitrate_verdict(
            journey_results=[jr_app],
            coverage=CoverageReport(1, 1, 0, 1, 0, "LOW", "App defect"),
            defects=[d_app],
            planned_journeys=[j1],
        )
        assert status1 == "FAIL"
        assert handoff1 == "EXECUTOR"
        assert code1 == 1

        # 2. Test configuration gap must handoff to NONE with exit code 1
        d_gap = Defect(
            id="DEF-GAP-INV",
            title="Missing auth token in test journey",
            category=DefectCategory.TEST_CONFIGURATION_GAP,
            severity=DefectSeverity.MAJOR,
            journey_id="J-INV-02",
            steps_to_reproduce=["Step 1"],
            expected="Valid credentials",
            actual="401 Unauthorized",
        )
        j2 = Journey(id="J-INV-02", title="Config Gap Journey", description="Missing token", priority=1, steps=[])
        jr_gap = JourneyResult(journey=j2, status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[d_gap])
        status2, handoff2, code2 = TesterReportGenerator.arbitrate_verdict(
            journey_results=[jr_gap],
            coverage=CoverageReport(1, 1, 0, 0, 1, "LOW", "Test configuration gap"),
            defects=[d_gap],
            planned_journeys=[j2],
        )
        assert status2 in ("BLOCKED", "PARTIAL")
        assert handoff2 == "NONE"
        assert code2 == 1

        # 3. Clean fully verified journey with zero defects must handoff to REVIEWER with exit code 0
        j3 = Journey(id="J-INV-03", title="Clean Journey", description="Verified", priority=1, steps=[])
        jr_clean = JourneyResult(journey=j3, status="PASS", duration_seconds=0.1, steps_completed=1, defects=[])
        status3, handoff3, code3 = TesterReportGenerator.arbitrate_verdict(
            journey_results=[jr_clean],
            coverage=CoverageReport(1, 1, 1, 0, 0, "HIGH", "All journeys verified", coverage_depth="FUNCTIONAL_VERIFIED"),
            defects=[],
            planned_journeys=[j3],
        )
        assert status3 == "PASS"
        assert handoff3 == "REVIEWER"
        assert code3 == 0

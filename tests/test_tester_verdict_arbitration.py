"""Tests for Tester verdict arbitration, coverage confidence, and defect taxonomy.

Verifies:
1. Centralized deterministic verdict arbitration:
   - PASS (exit code 0, handoff REVIEWER)
   - PARTIAL (exit code 1, handoff NONE)
   - FAIL (exit code 1, handoff EXECUTOR)
   - BLOCKED (exit code 1, handoff NONE)
   - NOT_TESTABLE (exit code 0, handoff REVIEWER)
2. Run-041 reproduction: journeys with INVALID_INTERACTION and TEST_CONFIGURATION_GAP
   result in status PARTIAL, exit code 1, handoff NONE (never PASS).
3. Protocol and MachineReportValidator accept PARTIAL.
4. Unit-test verified branches separation from live UI verified and untested branches.
5. Visited without meaningful assertions classification.
"""

from pathlib import Path
import json
from unittest.mock import patch, MagicMock
import pytest
from click.testing import CliRunner

from forge.cli import main
from forge.adapters.base import AdapterResponse
from forge.storage.run_manager import RunManager
from forge.protocol.validator import MachineReportValidator
from forge.testing.models import (
    ActionType,
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyResult,
    JourneyStep,
    ProjectArchetype,
)
from forge.testing.report import TesterReportGenerator
from forge.testing.edge_cases import EdgeCaseRequirement
from tests.conftest import configure_automated_execution_environment


def test_validator_accepts_partial_status():
    """Verify that MachineReportValidator recognizes 'PARTIAL' as an allowed status for TESTER."""
    valid_partial_report = {
        "ROLE": "TESTER",
        "STATUS": "PARTIAL",
        "HANDOFF": "NONE",
        "EXIT_CODE": 1,
        "JOURNEYS": {
            "planned": 2,
            "executed": 2,
            "passed": 1,
            "failed": 1,
            "blocked": 0,
        },
        "DEFECTS": [],
        "COVERAGE": {
            "confidence": "MEDIUM",
            "coverage_depth": "SHALLOW_SMOKE",
            "summary": "1 journey failed due to invalid interaction",
        },
    }
    validated = MachineReportValidator.validate(valid_partial_report, expected_role="TESTER")
    assert validated.status == "PARTIAL"
    assert validated.exit_code == 1
    assert validated.handoff == "NONE"


def test_arbitrate_verdict_case_1_all_five_pass():
    """Case 1: All five required journeys are recorded and pass -> PASS, REVIEWER, 0."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    results = [JourneyResult(journey=j, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]) for j in journeys]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=5,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="HIGH",
        summary="All 5 journeys passed",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        planned_journeys=journeys,
        required_journey_ids=[j.id for j in journeys],
    )
    assert status == "PASS"
    assert handoff == "REVIEWER"
    assert code == 0


def test_arbitrate_verdict_case_2_zero_journeys_planned():
    """Case 2: Zero journeys are planned -> NOT_TESTABLE, NONE, exit code 1."""
    cov = CoverageReport(
        planned_journeys=0,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="LOW",
        summary="Zero journeys planned",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[],
        coverage=cov,
        defects=[],
        planned_journeys=[],
        required_journey_ids=[],
    )
    assert status == "NOT_TESTABLE"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_3_planned_five_zero_results():
    """Case 3: Five journeys are planned, but zero results are recorded -> BLOCKED, NONE, exit code 1."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=5,
        unexecuted_journeys=5,
        confidence="LOW",
        summary="Zero of 5 journeys recorded",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[],
        coverage=cov,
        defects=[],
        planned_journeys=journeys,
        required_journey_ids=[j.id for j in journeys],
    )
    assert status == "BLOCKED"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_4_planned_five_only_three_recorded():
    """Case 4: Five journeys are planned, only three successful results are recorded -> PARTIAL, NONE, exit code 1."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    results = [JourneyResult(journey=journeys[i], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]) for i in range(3)]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=3,
        passed_journeys=3,
        failed_journeys=0,
        blocked_journeys=0,
        unexecuted_journeys=2,
        confidence="MEDIUM",
        summary="Only 3 of 5 journeys recorded",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        planned_journeys=journeys,
        required_journey_ids=[j.id for j in journeys],
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_5_three_pass_two_blocked():
    """Case 5: Five journeys are planned, three pass and two are blocked -> PARTIAL, NONE, exit code 1."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    r1 = JourneyResult(journey=journeys[0], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r2 = JourneyResult(journey=journeys[1], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r3 = JourneyResult(journey=journeys[2], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r4 = JourneyResult(journey=journeys[3], status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[])
    r5 = JourneyResult(journey=journeys[4], status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[])
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=3,
        passed_journeys=3,
        failed_journeys=0,
        blocked_journeys=2,
        confidence="MEDIUM",
        summary="3 passed, 2 blocked",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1, r2, r3, r4, r5],
        coverage=cov,
        defects=[],
        planned_journeys=journeys,
        required_journey_ids=[j.id for j in journeys],
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_6_three_pass_two_test_gaps():
    """Case 6: Five journeys are planned, three pass and two fail because of test gaps -> PARTIAL, NONE, exit code 1."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    d4 = Defect(
        id="DEF-GAP-1",
        title="Invalid selector",
        category=DefectCategory.INVALID_INTERACTION,
        severity=DefectSeverity.MINOR,
        journey_id="J-04",
        steps_to_reproduce=["Click tab"],
        expected="Tab clicked",
        actual="Tab not found",
    )
    d5 = Defect(
        id="DEF-GAP-2",
        title="Fixture unsuitability",
        category=DefectCategory.TEST_CONFIGURATION_GAP,
        severity=DefectSeverity.MINOR,
        journey_id="J-05",
        steps_to_reproduce=["Upload file"],
        expected="Uploaded",
        actual="Format mismatch",
    )
    r1 = JourneyResult(journey=journeys[0], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r2 = JourneyResult(journey=journeys[1], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r3 = JourneyResult(journey=journeys[2], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r4 = JourneyResult(journey=journeys[3], status="FAIL", duration_seconds=1.0, steps_completed=1, defects=[d4])
    r5 = JourneyResult(journey=journeys[4], status="FAIL", duration_seconds=1.0, steps_completed=1, defects=[d5])
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=3,
        failed_journeys=2,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="3 passed, 2 failed due to test gaps",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1, r2, r3, r4, r5],
        coverage=cov,
        defects=[d4, d5],
        planned_journeys=journeys,
        required_journey_ids=[j.id for j in journeys],
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_7_confirmed_functional_defect():
    """Case 7: A confirmed functional application defect exists -> FAIL, EXECUTOR, exit code 1."""
    j1 = Journey(id="J-01", title="Model Training", description="Model training", priority=1, steps=[])
    d_bug = Defect(
        id="DEF-APP-1",
        title="Accuracy calculation crashed with ZeroDivisionError",
        category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-01",
        steps_to_reproduce=["Train model"],
        expected="Accuracy score displayed",
        actual="ZeroDivisionError in console",
    )
    r1 = JourneyResult(journey=j1, status="FAIL", duration_seconds=1.0, steps_completed=1, defects=[d_bug])
    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Functional defect discovered",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1],
        coverage=cov,
        defects=[d_bug],
        planned_journeys=[j1],
        required_journey_ids=["J-01"],
    )
    assert status == "FAIL"
    assert handoff == "EXECUTOR"
    assert code == 1


def test_arbitrate_verdict_case_8_required_journey_omitted():
    """Case 8: All recorded journeys pass, but one required journey was never planned or executed -> PARTIAL, NONE, exit code 1."""
    required_ids = ["J-01", "J-02", "J-03", "J-04", "J-05"]
    planned = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 5)]
    results = [JourneyResult(journey=j, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]) for j in planned]
    cov = CoverageReport(
        planned_journeys=4,
        executed_journeys=4,
        passed_journeys=4,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="4 passed, but J-05 required journey was omitted",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        planned_journeys=planned,
        required_journey_ids=required_ids,
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_9_duplicate_concealing_missing_journey():
    """Case 9: Duplicate results exist for one journey while another required journey is missing -> PARTIAL, NONE, exit code 1."""
    required_ids = ["J-01", "J-02", "J-03", "J-04", "J-05"]
    j1 = Journey(id="J-01", title="Journey 1", description="Test 1", priority=1, steps=[])
    j2 = Journey(id="J-02", title="Journey 2", description="Test 2", priority=1, steps=[])
    j3 = Journey(id="J-03", title="Journey 3", description="Test 3", priority=1, steps=[])
    j4 = Journey(id="J-04", title="Journey 4", description="Test 4", priority=1, steps=[])
    # J-01 is duplicated (e.g. from a retry), while J-05 is completely missing
    r1_attempt1 = JourneyResult(journey=j1, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r1_attempt2 = JourneyResult(journey=j1, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r2 = JourneyResult(journey=j2, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r3 = JourneyResult(journey=j3, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r4 = JourneyResult(journey=j4, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    
    results = [r1_attempt1, r1_attempt2, r2, r3, r4]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=5,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="5 result items but J-01 duplicated and J-05 missing",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        required_journey_ids=required_ids,
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_case_10_total_prerequisite_blockage():
    """Case 10: A total prerequisite blockage prevents meaningful execution -> BLOCKED, NONE, exit code 1."""
    j1 = Journey(id="J-01", title="Database Dependent", description="Prereq test", priority=1, steps=[])
    d_prereq = Defect(
        id="DEF-PREREQ-1",
        title="Database daemon unreachable",
        category=DefectCategory.PREREQUISITE_FAILURE,
        severity=DefectSeverity.CRITICAL,
        journey_id="J-01",
        steps_to_reproduce=["Connect DB"],
        expected="Connected",
        actual="Connection refused",
    )
    r1 = JourneyResult(journey=j1, status="BLOCKED", duration_seconds=0.1, steps_completed=0, defects=[d_prereq])
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=5,
        confidence="LOW",
        summary="Execution blocked by prerequisite failure",
        coverage_depth="BLOCKED_PREREQUISITE",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1],
        coverage=cov,
        defects=[d_prereq],
    )
    assert status == "BLOCKED"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_run_041_reproduction():
    """Reproduction of run-041:
    3 journeys pass, 1 fails with INVALID_INTERACTION, 1 fails with TEST_CONFIGURATION_GAP.
    Must arbitrate to PARTIAL, handoff NONE, exit code 1 (NEVER PASS).
    """
    j1 = Journey(id="J-01", title="Overview", description="Overview test", priority=1, steps=[])
    j2 = Journey(id="J-02", title="Stress Test", description="Stress test", priority=1, steps=[])
    j3 = Journey(id="J-03", title="Comparison", description="Comparison test", priority=1, steps=[])
    j4 = Journey(id="J-04", title="Model Detail", description="Model detail test", priority=1, steps=[])
    j5 = Journey(id="J-05", title="Mobile Drawer", description="Mobile drawer test", priority=1, steps=[])

    d3 = Defect(
        id="DEF-J-03-ERR-18",
        title="Selector 'Composite Ranking' tab not found in live DOM",
        category=DefectCategory.INVALID_INTERACTION,
        severity=DefectSeverity.MINOR,
        journey_id="J-03",
        steps_to_reproduce=["Click composite ranking tab"],
        expected="Tab clicked",
        actual="Tab selector not found",
    )
    d5 = Defect(
        id="DEF-J-05-ELEM-4",
        title="Mobile drawer button selector not discovered",
        category=DefectCategory.TEST_CONFIGURATION_GAP,
        severity=DefectSeverity.MINOR,
        journey_id="J-05",
        steps_to_reproduce=["Assert drawer element"],
        expected="Drawer visible",
        actual="Drawer not discovered",
    )

    r1 = JourneyResult(journey=j1, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r2 = JourneyResult(journey=j2, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r3 = JourneyResult(journey=j3, status="FAIL", duration_seconds=1.0, steps_completed=2, defects=[d3])
    r4 = JourneyResult(journey=j4, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[])
    r5 = JourneyResult(journey=j5, status="FAIL", duration_seconds=1.0, steps_completed=1, defects=[d5])

    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=3,
        failed_journeys=2,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="2 journeys failed due to selector / test configuration gaps",
    )

    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1, r2, r3, r4, r5],
        coverage=cov,
        defects=[d3, d5],
        planned_journeys=[j1, j2, j3, j4, j5],
    )

    # Must be PARTIAL, not PASS
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1

    # Now verify via full generate()
    md_content, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[r1, r2, r3, r4, r5],
        coverage=cov,
        duration_seconds=10.0,
        evidence_dir="/tmp/evidence",
        planned_journeys=[j1, j2, j3, j4, j5],
    )

    assert json_dict["STATUS"] == "PARTIAL"
    assert json_dict["EXIT_CODE"] == 1
    assert json_dict["HANDOFF"] == "NONE"
    assert "PARTIAL" in md_content


def test_coverage_report_includes_unit_test_and_visited_without_assertions():
    """Verify CoverageReport schema captures unit_test_verified_branches and visited_without_meaningful_assertions."""
    cov = CoverageReport(
        planned_journeys=3,
        executed_journeys=3,
        passed_journeys=2,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="MEDIUM",
        summary="Partial coverage",
        exercised_changed_components=["views/module_02_model.py"],
        unexercised_changed_components=["views/module_07_comparison.py"],
        visited_without_meaningful_assertions=["views/module_07_comparison.py"],
        verified_edge_cases=["EC-EMPTY-DATASET"],
        unit_test_verified_branches=["EC-MODEL-NAN-HANDLING: modules/model_module.py#calculate_metrics"],
        untested_changed_branches=["EC-INFINITY-LOSS: modules/stress_module.py#stress_evaluate"],
    )
    d = cov.to_dict()
    assert "unit_test_verified_branches" in d
    assert "visited_without_meaningful_assertions" in d
    assert d["unit_test_verified_branches"] == ["EC-MODEL-NAN-HANDLING: modules/model_module.py#calculate_metrics"]
    assert d["visited_without_meaningful_assertions"] == ["views/module_07_comparison.py"]


def test_auto_pipeline_tester_partial_halts_immediately_without_repair(tmp_path):
    """Verify auto_pipeline halts immediately without launching an auto-repair iteration when Tester returns PARTIAL."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_partial = AdapterResponse(
            stdout="""# Tester Report
Selector missing in DOM.
```yaml
ROLE: TESTER
STATUS: PARTIAL
HANDOFF: NONE
EXIT_CODE: 1
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="PARTIAL",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_partial]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp) as mock_exec:

            res = runner.invoke(main, ["auto", "Build feature", "--max-retries", "3"])
            assert res.exit_code == 1
            assert "Tester halted" in res.output or "PARTIAL" in res.output
            # Must NOT launch an auto-repair iteration for Executor
            assert mock_exec.call_count == 1

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "PARTIAL"


def test_auto_pipeline_tester_v2_not_testable_halts_fail_closed(tmp_path):
    """Verify auto_pipeline halts fail-closed when Tester v2 returns NOT_TESTABLE with EXIT_CODE 1 and HANDOFF NONE."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_not_testable = AdapterResponse(
            stdout="""# Tester Evaluation
No interactive UI components planned or found.
```yaml
ROLE: TESTER
STATUS: NOT_TESTABLE
HANDOFF: NONE
EXIT_CODE: 1
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="NOT_TESTABLE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_not_testable]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp) as mock_exec:

            res = runner.invoke(main, ["auto", "Update config file", "--max-retries", "3"])
            assert res.exit_code == 1
            assert "Tester halted" in res.output or "NOT_TESTABLE" in res.output
            # Must NOT launch an auto-repair iteration
            assert mock_exec.call_count == 1

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "NOT_TESTABLE"


def test_arbitrate_verdict_zero_passed_journeys_never_pass():
    """Verify that a run with 0 passed journeys can NEVER return PASS under any circumstances."""
    j1 = Journey(id="J-01", title="Skipped Test", description="Skipped test", priority=1, steps=[])
    r1 = JourneyResult(journey=j1, status="SKIPPED", duration_seconds=0.01, steps_completed=0, defects=[])
    cov = CoverageReport(
        planned_journeys=0,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=0,
        skipped_journeys=1,
        confidence="LOW",
        summary="No journeys passed",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1],
        coverage=cov,
        defects=[],
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_retries_do_not_inflate_passed_counts():
    """Verify that retries of a single journey (5 results of J-01) do not satisfy 5 planned journeys."""
    j1 = Journey(id="J-01", title="Login Journey", description="Login", priority=1, steps=[])
    results = [
        JourneyResult(journey=j1, status="PASS", duration_seconds=1.0, steps_completed=2, defects=[])
        for _ in range(5)
    ]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=1,
        passed_journeys=1,
        failed_journeys=0,
        blocked_journeys=0,
        unexecuted_journeys=4,
        confidence="MEDIUM",
        summary="J-01 passed 5 times, J-02..J-05 unexecuted",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        required_journey_ids=["J-01", "J-02", "J-03", "J-04", "J-05"],
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_skipped_and_unexecuted_prevent_pass():
    """Verify that unexecuted or skipped journeys strictly prevent a PASS verdict."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    results = [
        JourneyResult(journey=journeys[0], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]),
        JourneyResult(journey=journeys[1], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]),
        JourneyResult(journey=journeys[2], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]),
        JourneyResult(journey=journeys[3], status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]),
        JourneyResult(journey=journeys[4], status="SKIPPED", duration_seconds=0.01, steps_completed=0, defects=[]),
    ]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=4,
        passed_journeys=4,
        failed_journeys=0,
        blocked_journeys=0,
        skipped_journeys=1,
        confidence="MEDIUM",
        summary="4 passed, 1 skipped",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=results,
        coverage=cov,
        defects=[],
        planned_journeys=journeys,
    )
    assert status == "PARTIAL"
    assert handoff == "NONE"
    assert code == 1


def test_arbitrate_verdict_actionable_categories_trigger_fail():
    """Verify that actionable application defect categories (DEAD_INTERACTION, BROKEN_NAVIGATION) return FAIL, EXECUTOR, 1."""
    j1 = Journey(id="J-01", title="Navigation Flow", description="Nav flow", priority=1, steps=[])
    d_dead = Defect(
        id="DEF-APP-DEAD",
        title="Save button unresponsive / dead click",
        category=DefectCategory.DEAD_INTERACTION,
        severity=DefectSeverity.MAJOR,
        journey_id="J-01",
        steps_to_reproduce=["Click Save"],
        expected="Form saved",
        actual="No DOM state change observed",
    )
    r1 = JourneyResult(journey=j1, status="FAIL", duration_seconds=1.0, steps_completed=1, defects=[d_dead])
    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="LOW",
        summary="Dead interaction encountered",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[r1],
        coverage=cov,
        defects=[d_dead],
        planned_journeys=[j1],
    )
    assert status == "FAIL"
    assert handoff == "EXECUTOR"
    assert code == 1


def test_arbitrate_verdict_untested_or_blocked_prereq_depth_never_pass():
    """Verify that coverage depth UNTESTED or BLOCKED_PREREQUISITE cannot qualify for PASS."""
    cov_untested = CoverageReport(
        planned_journeys=0,
        executed_journeys=0,
        passed_journeys=0,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="LOW",
        summary="Untested",
        coverage_depth="UNTESTED",
    )
    status, handoff, code = TesterReportGenerator.arbitrate_verdict(
        journey_results=[],
        coverage=cov_untested,
        defects=[],
    )
    assert status == "NOT_TESTABLE"
    assert handoff == "NONE"
    assert code == 1


def test_stage_order_not_testable_not_in_global_success_statuses():
    """Verify StageOrder.is_success_status('NOT_TESTABLE') is False for run-level checks."""
    from forge.stages.definition import StageOrder
    assert StageOrder.is_success_status("NOT_TESTABLE") is False
    assert StageOrder.is_success_status("PASS") is True
    assert StageOrder.is_success_status("APPROVED") is True


def test_generate_report_metric_counts_agreement():
    """Verify that markdown table, JSON coverage dict, and DATA agree on journey counts."""
    journeys = [Journey(id=f"J-0{i}", title=f"Journey {i}", description=f"Test {i}", priority=1, steps=[]) for i in range(1, 6)]
    results = [JourneyResult(journey=j, status="PASS", duration_seconds=1.0, steps_completed=3, defects=[]) for j in journeys]
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=5,
        failed_journeys=0,
        blocked_journeys=0,
        unexecuted_journeys=0,
        skipped_journeys=0,
        confidence="HIGH",
        summary="All passed",
        coverage_depth="FUNCTIONAL_VERIFIED",
    )
    md, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=results,
        coverage=cov,
        duration_seconds=5.0,
        planned_journeys=journeys,
    )
    assert json_dict["STATUS"] == "PASS"
    assert json_dict["EXIT_CODE"] == 0
    assert json_dict["HANDOFF"] == "REVIEWER"
    assert json_dict["DATA"]["journeys_total"] == 5
    assert json_dict["COVERAGE"]["planned_journeys"] == 5
    assert json_dict["COVERAGE"]["passed_journeys"] == 5
    assert "| Planned Journeys | 5 |" in md
    assert "| Passed Journeys | 5 |" in md


# =========================================================================
# NOT_TESTABLE Protocol Boundary & Discriminator Regression Tests
# =========================================================================

def test_doc_only_not_testable_bypass_allowed_when_no_code_changed():
    """Verify StageOrder.is_doc_only_not_testable_bypass allows ADR-002 doc-only task when no application code changed."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=["README.md", "docs/index.md"])
    assert can_bypass is True
    assert reason is None


def test_not_testable_rejected_when_application_code_changed():
    """Verify StageOrder.is_doc_only_not_testable_bypass strictly rejects bypass when application code was modified."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=["README.md", "app.py"])
    assert can_bypass is False
    assert "Application code changes detected (app.py)" in reason


def test_empirical_not_testable_never_bypasses_even_if_exit_code_zero():
    """Verify StageOrder.is_doc_only_not_testable_bypass rejects empirical verification with COVERAGE even if exit code is 0."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(
        role="TESTER",
        status="NOT_TESTABLE",
        handoff="REVIEWER",
        exit_code=0,
        is_valid=True,
        data={"COVERAGE": {"planned_journeys": 0, "coverage_depth": "UNTESTED"}}
    )
    resp = AdapterResponse(stdout="Empirical", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Empirical")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Empirical", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=["README.md"])
    assert can_bypass is False
    assert "Empirical verification telemetry detected" in reason


def test_not_testable_rejected_when_handoff_is_none():
    """Verify StageOrder.is_doc_only_not_testable_bypass rejects NOT_TESTABLE when handoff is NONE."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="NONE", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=["README.md"])
    assert can_bypass is False
    assert "Tester handoff is 'NONE', expected 'REVIEWER'" in reason


def test_not_testable_rejected_when_exit_code_nonzero():
    """Verify StageOrder.is_doc_only_not_testable_bypass rejects NOT_TESTABLE when exit code is 1."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=1, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=1, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=False)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=["README.md"])
    assert can_bypass is False
    assert "Non-zero exit code (1) indicates verification failure" in reason


def test_auto_pipeline_not_testable_halts_fail_closed_when_application_code_changed(tmp_path):
    """Verify auto_pipeline halts fail-closed when Tester emits NOT_TESTABLE on application code changes."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        # Executor creates an application code file: app.py
        Path("app.py").write_text("def hello(): pass\n", encoding="utf-8")
        exec_resp = AdapterResponse(
            stdout="""# Executor Report
## Files Changed
- app.py: created

```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
HANDOFF: TESTER
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS"
        )
        test_not_testable = AdapterResponse(
            stdout="""# Tester Evaluation
Documentation update only; no runnable code.

```yaml
ROLE: TESTER
STATUS: NOT_TESTABLE
HANDOFF: REVIEWER
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="NOT_TESTABLE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_not_testable]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Implement feature"])
            assert res.exit_code == 1
            assert "Tester NOT_TESTABLE rejected: Application code changes detected" in res.output
            assert "Reviewer finished" not in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "NOT_TESTABLE"


def test_auto_pipeline_empirical_not_testable_halts_fail_closed(tmp_path):
    """Verify auto_pipeline halts fail-closed when Tester emits empirical NOT_TESTABLE with COVERAGE."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(main, ["init"])
        configure_automated_execution_environment()

        arch_resp = AdapterResponse(stdout="```yaml\nROLE: ARCHITECT\nSTATUS: APPROVED\nHANDOFF: PLANNER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="APPROVED")
        plan_resp = AdapterResponse(stdout="```yaml\nROLE: PLANNER\nSTATUS: READY\nHANDOFF: EXECUTOR\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="READY")
        exec_resp = AdapterResponse(stdout="```yaml\nROLE: EXECUTOR\nSTATUS: SUCCESS\nHANDOFF: TESTER\n```", stderr="", exit_code=0, duration_seconds=0.1, raw_output="SUCCESS")
        test_empirical_not_testable = AdapterResponse(
            stdout="""# Tester Verification
```yaml
ROLE: TESTER
STATUS: NOT_TESTABLE
HANDOFF: REVIEWER
COVERAGE:
  planned_journeys: 0
  coverage_depth: UNTESTED
```""",
            stderr="", exit_code=0, duration_seconds=0.1, raw_output="NOT_TESTABLE",
        )

        with patch("forge.adapters.opencode.OpenCodeAdapter.is_available", return_value=True), \
             patch("forge.adapters.antigravity.AntigravityAdapter.is_available", return_value=True), \
             patch("forge.adapters.opencode.OpenCodeAdapter.execute", side_effect=[arch_resp, plan_resp, test_empirical_not_testable]), \
             patch("forge.adapters.antigravity.AntigravityAdapter.execute", return_value=exec_resp):

            res = runner.invoke(main, ["auto", "Update docs"])
            assert res.exit_code == 1
            assert "Tester NOT_TESTABLE rejected: Empirical verification telemetry detected" in res.output
            assert "Reviewer finished" not in res.output

            rm = RunManager(Path.cwd())
            run = rm.latest()
            assert run.status == "NOT_TESTABLE"


def test_verify_pipeline_completion_rejects_empirical_not_testable_deliverable():
    """Verify StageOrder.verify_pipeline_completion rejects empirical 04_tester.json deliverable with exit_code 1 or COVERAGE."""
    from forge.stages.definition import StageOrder
    art_data = {
        "role": "tester",
        "status": "NOT_TESTABLE",
        "handoff": "NONE",
        "exit_code": 1,
        "COVERAGE": {"planned_journeys": 0, "coverage_depth": "UNTESTED"},
    }
    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(art_data)
    assert can_bypass is False
    assert "Tester handoff is 'NONE', expected 'REVIEWER'" in reason


def test_verify_pipeline_completion_accepts_valid_doc_only_deliverable():
    """Verify StageOrder.verify_pipeline_completion accepts clean documentation-only 04_tester.json deliverable."""
    from forge.stages.definition import StageOrder
    art_data = {
        "role": "tester",
        "status": "NOT_TESTABLE",
        "handoff": "REVIEWER",
        "exit_code": 0,
    }
    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(art_data, changed_files=["README.md"])
    assert can_bypass is True
    assert reason is None


# =========================================================================
# Adversarial Regression Tests: Documentation-Only Bypass Boundary
# =========================================================================

def test_adversarial_case_a_untracked_application_files(tmp_path):
    """Case A: Untracked application files (Python or JS) strictly reject bypass even if tracked files are clean."""
    import subprocess
    from forge.stages.definition import StageOrder
    from forge.core.git import GitService
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt
    from forge.core.context import Context
    from forge.core.config import Config

    # Initialize a clean git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Readme\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    git = GitService(tmp_path)
    baseline = git.capture_baseline()

    # Create untracked application file without modifying tracked files
    untracked_py = tmp_path / "new_service.py"
    untracked_py.write_text("def run(): pass\n", encoding="utf-8")

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)
    ctx = Context(run=None, project_root=tmp_path, config=Config(), git=git, baseline=baseline)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx)
    assert can_bypass is False
    assert "Application code changes detected" in reason
    assert "new_service.py" in reason

    # Clean up and test untracked JS file
    untracked_py.unlink()
    untracked_js = tmp_path / "widget.js"
    untracked_js.write_text("console.log('hi');\n", encoding="utf-8")

    can_bypass_js, reason_js = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx)
    assert can_bypass_js is False
    assert "Application code changes detected" in reason_js
    assert "widget.js" in reason_js


def test_adversarial_case_b_mixed_documentation_and_application_changes():
    """Case B: Modifying documentation and application source in the same task strictly rejects bypass."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(
        res,
        changed_files=["README.md", "docs/architecture.md", "src/controller.py"],
    )
    assert can_bypass is False
    assert "Application code changes detected (src/controller.py)" in reason


def test_adversarial_case_c_reverted_source_changes_via_task_deliverables(tmp_path):
    """Case C: Reverted source changes recorded in Executor deliverable strictly reject bypass."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    # Working tree is clean, but 03_executor.md recorded an application code modification
    run_dir = tmp_path / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "03_executor.md").write_text(
        """# Executor Report
## Files Changed
- app.py: modified and later reverted in workspace
- docs/README.md: updated

```yaml
ROLE: EXECUTOR
STATUS: SUCCESS
HANDOFF: TESTER
```""",
        encoding="utf-8",
    )

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, run_dir=run_dir)
    assert can_bypass is False
    assert "Application code changes detected (app.py)" in reason

    # Test via 03_executor.json with files_changed list
    run_dir2 = tmp_path / "run-002"
    run_dir2.mkdir(parents=True, exist_ok=True)
    (run_dir2 / "03_executor.json").write_text(
        json.dumps({
            "role": "executor",
            "status": "SUCCESS",
            "files_changed": ["lib/network.ts", "README.md"],
        }),
        encoding="utf-8",
    )
    can_bypass2, reason2 = StageOrder.is_doc_only_not_testable_bypass(res, run_dir=run_dir2)
    assert can_bypass2 is False
    assert "Application code changes detected (lib/network.ts)" in reason2


def test_adversarial_case_c_reverted_source_changes_via_git_commit_history(tmp_path):
    """Case C: Reverted source changes preserved in task commit history strictly reject bypass."""
    import subprocess
    from forge.stages.definition import StageOrder
    from forge.core.git import GitService
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt
    from forge.core.context import Context
    from forge.core.config import Config

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Readme\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('original')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmp_path, check=True, capture_output=True)

    git = GitService(tmp_path)
    baseline = git.capture_baseline()

    # Commit 1: Task modifies app.py
    (tmp_path / "app.py").write_text("print('modified')\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Task commit 1: modify app.py"], cwd=tmp_path, check=True, capture_output=True)

    # Commit 2: Task reverts app.py back to original
    (tmp_path / "app.py").write_text("print('original')\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "Task commit 2: revert app.py"], cwd=tmp_path, check=True, capture_output=True)

    # Working tree is clean, and net diff between baseline and HEAD is empty
    assert git.status() == ""

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)
    ctx = Context(run=None, project_root=tmp_path, config=Config(), git=git, baseline=baseline)

    can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx)
    assert can_bypass is False
    assert "Application code changes detected (app.py)" in reason


def test_adversarial_case_d_nested_source_paths_and_substring_cases():
    """Case D: Source code in nested/doc-like directories or containing 'test' as a substring is strictly rejected."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    # Code files inside paths resembling docs/config must NOT be exempted
    doc_like_code_paths = [
        "docs/server.py",
        "doc/api/gateway.js",
        ".ai/tools/generator.py",
        ".github/scripts/deploy.py",
        "documentation/sub/helper.ts",
        # Non-test files with 'test' as substring must NOT be exempted
        "nested/contest.py",
        "services/latest.py",
        "fastest.py",
        "shortest.py",
        "attest.py",
        "protest.py",
    ]
    for p in doc_like_code_paths:
        can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=[p])
        assert can_bypass is False, f"Expected {p} to be rejected"
        assert "Application code changes detected" in reason
        assert p in reason

    # In contrast, legitimate nested documentation must be allowed
    legit_docs = [
        "docs/guide.md",
        "doc/architecture.rst",
        ".ai/project/decisions.md",
        "documentation/manual.txt",
        "docs/images/architecture.png",
    ]
    can_bypass_legit, reason_legit = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=legit_docs)
    assert can_bypass_legit is True
    assert reason_legit is None


def test_adversarial_case_e_git_inspection_failures_fail_closed(tmp_path):
    """Case E: Any Git inspection failure causes fail-closed rejection with clear diagnostic reason."""
    import subprocess
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt
    from forge.core.context import Context
    from forge.core.config import Config

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    # Subcase 1: git.is_git_repo() raises error
    mock_git1 = MagicMock()
    mock_git1.is_git_repo.side_effect = RuntimeError("Corrupted git worktree")
    ctx1 = Context(run=None, project_root=tmp_path, config=Config(), git=mock_git1)
    can_bypass1, reason1 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx1)
    assert can_bypass1 is False
    assert "Git inspection failed: Corrupted git worktree" in reason1

    # Subcase 2: git.changed_files() raises error
    mock_git2 = MagicMock()
    mock_git2.is_git_repo.return_value = True
    mock_git2._run.return_value = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    mock_git2.changed_files.side_effect = RuntimeError("git status timeout")
    ctx2 = Context(run=None, project_root=tmp_path, config=Config(), git=mock_git2)
    can_bypass2, reason2 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx2)
    assert can_bypass2 is False
    assert "Git inspection failed: git status timeout" in reason2

    # Subcase 3: git status subprocess failure
    mock_git3 = MagicMock()
    mock_git3.is_git_repo.return_value = True
    mock_git3._run.return_value = subprocess.CompletedProcess(
        args=["git", "status"], returncode=128, stdout="", stderr="fatal: index corrupted"
    )
    ctx3 = Context(run=None, project_root=tmp_path, config=Config(), git=mock_git3)
    can_bypass3, reason3 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx3)
    assert can_bypass3 is False
    assert "Git inspection failed: git status exited with code 128" in reason3

    # Subcase 4: git diff subprocess failure
    mock_git4 = MagicMock()
    mock_git4.is_git_repo.return_value = True
    def mock_run_cmd(args):
        if "diff" in args:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="diff failed")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
    mock_git4._run.side_effect = mock_run_cmd
    ctx4 = Context(run=None, project_root=tmp_path, config=Config(), git=mock_git4)
    can_bypass4, reason4 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx4)
    assert can_bypass4 is False
    assert "Git inspection failed: git diff exited with code 1" in reason4

    # Subcase 5: Corrupt baseline file on disk
    run_dir = tmp_path / "corrupt_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "git_baseline.json").write_text("{corrupt json data", encoding="utf-8")
    mock_git5 = MagicMock()
    mock_git5.is_git_repo.return_value = True
    mock_git5._run.return_value = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    ctx5 = Context(run=None, project_root=tmp_path, config=Config(), git=mock_git5)
    can_bypass5, reason5 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx5, run_dir=run_dir)
    assert can_bypass5 is False
    assert "Git baseline loading failed" in reason5

    # Subcase 6: Context has baseline_error
    ctx6 = Context(run=None, project_root=tmp_path, config=Config(), git=None)
    ctx6.baseline_error = "Repository locked by another process"
    can_bypass6, reason6 = StageOrder.is_doc_only_not_testable_bypass(res, context=ctx6)
    assert can_bypass6 is False
    assert "Git baseline loading failed: Repository locked by another process" in reason6

    # Subcase 7: Incomplete change information (no git, no run_dir, no changed_files)
    can_bypass7, reason7 = StageOrder.is_doc_only_not_testable_bypass(res)
    assert can_bypass7 is False
    assert "Insufficient change information" in reason7


def test_adversarial_case_f_file_extension_and_path_edge_cases():
    """Case F: Extensions, case sensitivity, Windows separators, and renames are properly handled."""
    from forge.stages.definition import StageOrder
    from forge.protocol.validator import MachineReport
    from forge.stages.result import StageResult
    from forge.stages.definition import StageDefinition
    from forge.prompts.compiler import RenderedPrompt

    role = StageDefinition(name="tester", sequence_number=4)
    mr = MachineReport(role="TESTER", status="NOT_TESTABLE", handoff="REVIEWER", exit_code=0, is_valid=True)
    resp = AdapterResponse(stdout="Doc only", stderr="", exit_code=0, duration_seconds=0.1, raw_output="Doc only")
    prompt = RenderedPrompt.from_text("")
    res = StageResult(role=role, prompt=prompt, response=resp, machine_report=mr, raw_markdown="Doc only", duration_seconds=0.1, success=True)

    # Edge cases: uppercase / mixed-case extensions
    for ext_file in ["src/App.PY", "src/View.TSX", "scripts/Build.SH", "models/query.SQL"]:
        can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=[ext_file])
        assert can_bypass is False, f"Expected {ext_file} to be rejected"
        assert "Application code changes detected" in reason

    # Edge cases: Windows backslashes
    for win_path in ["src\\components\\modal.tsx", "views\\dashboard.py", "scripts\\test.sh"]:
        can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=[win_path])
        assert can_bypass is False, f"Expected {win_path} to be rejected"
        assert "Application code changes detected" in reason

    # Edge cases: renamed files (either source or target is code)
    renames = [
        "README.md -> src/index.ts",
        "src/old_app.py -> docs/archived.md",
    ]
    for rename in renames:
        can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=[rename])
        assert can_bypass is False, f"Expected rename '{rename}' to be rejected"
        assert "Application code changes detected" in reason

    # Edge cases: diverse application extensions
    polyglot_files = [
        "service.go", "engine.rs", "worker.rb", "Main.java", "Worker.kt", "core.c", "kernel.cpp", "app.wasm", "index.mjs", "loader.cjs"
    ]
    for poly in polyglot_files:
        can_bypass, reason = StageOrder.is_doc_only_not_testable_bypass(res, changed_files=[poly])
        assert can_bypass is False, f"Expected {poly} to be rejected"
        assert "Application code changes detected" in reason


def test_adversarial_case_g_downstream_summary_and_pipeline_consistency(tmp_path):
    """Case G: StageSummaryItem icon, verify_pipeline_completion, and pipeline halts are consistent."""
    from forge.core.summary import StageSummaryItem
    from forge.stages.definition import StageOrder
    from forge.core.run import compute_task_fingerprint

    # 1. StageSummaryItem icon returns ✗ on rejected NOT_TESTABLE
    item_rejected = StageSummaryItem(
        name="Tester",
        status="NOT_TESTABLE",
        execution_state="COMPLETED",
        role_name="tester",
        reason="Tester NOT_TESTABLE rejected: Application code changes detected (app.py)",
    )
    assert item_rejected.icon == "✗"

    # StageSummaryItem icon returns ✓ on clean accepted NOT_TESTABLE bypass
    item_accepted = StageSummaryItem(
        name="Tester",
        status="NOT_TESTABLE",
        execution_state="COMPLETED",
        role_name="tester",
        reason=None,
    )
    assert item_accepted.icon == "✓"

    # 2. verify_pipeline_completion fails if Tester 04_tester.json has NOT_TESTABLE on a code change task
    run_dir = tmp_path / "run-003"
    run_dir.mkdir(parents=True, exist_ok=True)
    task_fp = compute_task_fingerprint("Test task")

    # Create 03_executor deliverable indicating app.py was modified
    (run_dir / "03_executor.md").write_text("# Executor Report\n## Files Changed\n- app.py: modified\n", encoding="utf-8")
    (run_dir / "03_executor.json").write_text(json.dumps({
        "role": "executor", "sequence_number": 3, "status": "COMPLETE",
        "task_fingerprint": task_fp, "run_id": "run-003",
    }), encoding="utf-8")

    # Create 04_tester deliverable attempting NOT_TESTABLE bypass
    (run_dir / "04_tester.json").write_text(json.dumps({
        "role": "tester", "sequence_number": 4, "status": "NOT_TESTABLE",
        "handoff": "REVIEWER", "exit_code": 0,
        "task_fingerprint": task_fp, "run_id": "run-003",
    }), encoding="utf-8")

    # Other required deliverables
    (run_dir / "01_architect.json").write_text(json.dumps({"role": "architect", "sequence_number": 1, "status": "APPROVED", "task_fingerprint": task_fp, "run_id": "run-003"}), encoding="utf-8")
    (run_dir / "02_planner.json").write_text(json.dumps({"role": "planner", "sequence_number": 2, "status": "READY", "task_fingerprint": task_fp, "run_id": "run-003"}), encoding="utf-8")
    (run_dir / "05_reviewer.json").write_text(json.dumps({"role": "reviewer", "sequence_number": 5, "status": "APPROVED", "task_fingerprint": task_fp, "run_id": "run-003"}), encoding="utf-8")

    is_complete, inc_stage, reason = StageOrder.verify_pipeline_completion(
        stages=[],
        run_dir=run_dir,
        task_fingerprint=task_fp,
        run_id="run-003",
        no_critic=True,
    )
    assert is_complete is False
    assert inc_stage == "Tester"
    assert "Application code changes detected (app.py)" in reason



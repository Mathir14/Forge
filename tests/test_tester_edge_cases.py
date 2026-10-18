"""Regression tests for Forge Tester Edge-Case Generation, Branch Tracking, and Defect Taxonomy."""
import math
import tempfile
from pathlib import Path
import pytest

from forge.testing.models import (
    ActionType,
    CoverageReport,
    Defect,
    DefectCategory,
    DefectSeverity,
    Journey,
    JourneyStep,
)
from forge.testing.edge_cases import EdgeCaseAnalyzer, EdgeCaseRequirement
from forge.testing.fixtures import TestingFixtureManager, TestFixture, analyze_fixture_suitability
from forge.testing.report import TesterReportGenerator
from forge.testing.models import ProjectArchetype


def test_edge_case_analyzer_detects_requirements():
    """Verify EdgeCaseAnalyzer extracts behavior-specific branches from git diff."""
    git_diff = """
diff --git a/modules/model_module.py b/modules/model_module.py
--- a/modules/model_module.py
+++ b/modules/model_module.py
@@ -740,6 +740,15 @@
+            elif math.isnan(best_score) and not math.isnan(score):
+                best_score = score
+                best_model = name
+            elif not math.isnan(score):
+                if invert:
+                    if score < best_score:
+                        best_score = score
+                        best_model = name
+                else:
+                    if score > best_score:
+                        best_score = score
+                        best_model = name
diff --git a/views/module_05_post_stress.py b/views/module_05_post_stress.py
+rec = post_stress_analyzer.policy.resolve_recommendation(score)
diff --git a/views/module_07_comparison.py b/views/module_07_comparison.py
+grade = comparator.weights.resolve_grade(s)
+table = composite_weights
diff --git a/views/module_08_reliability.py b/views/module_08_reliability.py
+scaled = f"{score}/{scorer.MAX_PERF}"
"""
    files = [
        "modules/model_module.py",
        "views/module_05_post_stress.py",
        "views/module_07_comparison.py",
        "views/module_08_reliability.py",
    ]
    reqs = EdgeCaseAnalyzer.analyze_diff(git_diff, relevant_files=files)
    req_ids = [r.id for r in reqs]

    assert "EC-MODEL-NAN-HANDLING" in req_ids
    assert "EC-MULTI-MODEL-COMPARISON" in req_ids
    assert "EC-POST-STRESS-RECOMMENDATION" in req_ids
    assert "EC-COMPARISON-COMPOSITE-POLICY" in req_ids
    assert "EC-RELIABILITY-SCALING" in req_ids

    nan_req = next(r for r in reqs if r.id == "EC-MODEL-NAN-HANDLING")
    assert not nan_req.testable_via_ui
    assert nan_req.untestable_reason is not None

    multi_req = next(r for r in reqs if r.id == "EC-MULTI-MODEL-COMPARISON")
    assert multi_req.testable_via_ui
    assert multi_req.ui_guidance is not None


def test_fixture_manager_temporary_fixtures_lifecycle():
    """Verify temporary edge-case fixtures are generated, indexed, and cleanly deleted."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        root = Path(tmp_dir)
        mgr = TestingFixtureManager(root)
        tmp_target = root / ".forge" / "tmp_fixtures"

        generated = mgr.generate_edge_case_fixtures(target_dir=tmp_target)
        assert len(generated) >= 3
        for f in generated:
            assert f.is_temporary
            assert f.is_edge_case
            assert f.path.is_file()
            assert f.name in mgr._fixtures

        # Suitability checks on generated fixtures
        nan_fix = next(f for f in generated if "missing_values" in f.name)
        assert nan_fix.suitability is not None
        assert nan_fix.suitability.missing_values_count > 0

        boundary_fix = next(f for f in generated if "boundary" in f.name)
        assert boundary_fix.suitability is not None
        assert not boundary_fix.suitability.is_split_compatible

        empty_fix = next(f for f in generated if "empty" in f.name)
        assert empty_fix.suitability is not None
        assert not empty_fix.suitability.is_suitable

        # Cleanup
        mgr.cleanup_temporary_fixtures()
        assert len(mgr.temporary_fixtures) == 0
        for f in generated:
            assert not f.path.exists()
            assert f.name not in mgr._fixtures


def test_defect_taxonomy_and_handoff_rules():
    """Verify FUNCTIONAL_ASSERTION_FAILURE maps to MAJOR severity and EXECUTOR handoff."""
    from forge.testing.report import TesterReportGenerator
    from forge.testing.models import ProjectArchetype, JourneyResult

    journey = Journey(
        id="J-01",
        title="Functional Model Workflow",
        description="Verify model output",
        priority=1,
        steps=[JourneyStep(action=ActionType.ASSERT_TEXT, target="Expected Result")],
    )

    # 1. Defect with FUNCTIONAL_ASSERTION_FAILURE
    defect_functional = Defect(
        id="DEF-01",
        title="Required Model Output Missing",
        category=DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
        severity=DefectSeverity.MAJOR,
        journey_id="J-01",
        steps_to_reproduce=["Navigate to page", "Check output"],
        expected="Expected Result",
        actual="Not found",
    )
    result_fail = JourneyResult(
        journey=journey,
        status="FAIL",
        duration_seconds=1.0,
        steps_completed=0,
        defects=[defect_functional],
    )
    cov = CoverageReport(
        planned_journeys=1,
        executed_journeys=1,
        passed_journeys=0,
        failed_journeys=1,
        blocked_journeys=0,
        confidence="HIGH",
        summary="Test summary",
        coverage_depth="FUNCTIONAL_VERIFIED",
    )
    md, report_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[result_fail],
        coverage=cov,
        duration_seconds=2.0,
    )

    assert report_dict["STATUS"] == "FAIL"
    assert report_dict["HANDOFF"] == "EXECUTOR"
    assert len(report_dict["ISSUES"]["MAJOR"]) == 1

    # 2. Defect with superficial ASSERTION_MISMATCH only
    defect_mismatch = Defect(
        id="DEF-02",
        title="Minor Copy Variance",
        category=DefectCategory.ASSERTION_MISMATCH,
        severity=DefectSeverity.MINOR,
        journey_id="J-01",
        steps_to_reproduce=["Navigate to page", "Check button text"],
        expected="Run Tests",
        actual="Run",
    )
    result_mismatch = JourneyResult(
        journey=journey,
        status="FAIL",
        duration_seconds=1.0,
        steps_completed=0,
        defects=[defect_mismatch],
    )
    md2, report_dict2 = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[result_mismatch],
        coverage=cov,
        duration_seconds=2.0,
    )
    assert report_dict2["HANDOFF"] == "NONE"
    assert len(report_dict2["ISSUES"]["MINOR"]) == 1
    assert len(report_dict2["ISSUES"]["MAJOR"]) == 0


def test_coverage_report_edge_cases_and_untested_branches_rendered():
    """Verify CoverageReport serializes and renders edge-case and untested-branch metadata."""
    cov = CoverageReport(
        planned_journeys=5,
        executed_journeys=5,
        passed_journeys=5,
        failed_journeys=0,
        blocked_journeys=0,
        confidence="HIGH",
        coverage_depth="FUNCTIONAL_VERIFIED",
        summary="Verified happy path and edge cases.",
        verified_edge_cases=[
            "Multiple Candidate Model Comparison (modules/model_module.py)",
            "Dynamic Recommendation Resolution (views/module_05_post_stress.py)",
        ],
        untested_changed_branches=[
            "modules/model_module.py: NaN/Non-Finite Metric Protection (math.isnan(best_score)) - UI unreachable"
        ],
    )
    cov_dict = cov.to_dict()
    assert len(cov_dict["verified_edge_cases"]) == 2
    assert len(cov_dict["untested_changed_branches"]) == 1

    md, json_dict = TesterReportGenerator.generate(
        archetype=ProjectArchetype.WEB_SPA,
        runtime_target="http://127.0.0.1:8501",
        journey_results=[],
        coverage=cov,
        duration_seconds=3.0,
    )
    assert "Verified Edge Cases & Boundary Workflows" in md
    assert "Multiple Candidate Model Comparison" in md
    assert "Changed Branches Not Exercised in Live UI Run" in md
    assert "NaN/Non-Finite Metric Protection" in md
    assert json_dict["COVERAGE"]["verified_edge_cases"] == cov_dict["verified_edge_cases"]
    assert json_dict["COVERAGE"]["untested_changed_branches"] == cov_dict["untested_changed_branches"]


def test_model_module_nan_branch_in_isolation():
    """Directly test get_best_model for multiple models and NaN fallback branch."""
    pytest.importorskip("numpy", reason="numpy required for Stress-Testing-Framework model_module test")
    import sys
    sys.path.insert(0, "/home/mathir/Stress-Testing-Framework")
    try:
        from modules.model_module import ModelTrainer

        trainer = ModelTrainer()
        # Case 1: Multiple models with real finite scores
        trainer.metrics = {
            "Logistic Regression": {"Test": {"accuracy": 0.85, "f1": 0.82}},
            "Random Forest": {"Test": {"accuracy": 0.92, "f1": 0.90}},
        }
        best_name, best_score = trainer.get_best_model("accuracy", "Test")
        assert best_name == "Random Forest"
        assert math.isclose(best_score, 0.92)

        # Case 2: Model 1 has NaN score, Model 2 has finite score (tests lines 744-746)
        trainer.metrics = {
            "Faulty Model": {"Test": {"accuracy": float("nan")}},
            "Valid Model": {"Test": {"accuracy": 0.78}},
        }
        best_name2, best_score2 = trainer.get_best_model("accuracy", "Test")
        assert best_name2 == "Valid Model"
        assert math.isclose(best_score2, 0.78)

        # Case 3: Invert metric (lower is better, e.g. loss)
        trainer.metrics = {
            "Model A": {"Test": {"loss": 0.45}},
            "Model B": {"Test": {"loss": 0.22}},
        }
        best_name3, best_score3 = trainer.get_best_model("loss", "Test")
        assert best_name3 == "Model B"
        assert math.isclose(best_score3, 0.22)
    finally:
        if "/home/mathir/Stress-Testing-Framework" in sys.path:
            sys.path.remove("/home/mathir/Stress-Testing-Framework")

"""Edge-case analysis and changed-branch test requirement extraction for Forge Tester."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EdgeCaseRequirement:
    """A behavior-specific branch or edge-case test requirement derived from git changes."""
    id: str
    component: str
    title: str
    description: str
    branch_condition: str
    testable_via_ui: bool
    ui_guidance: Optional[str] = None
    untestable_reason: Optional[str] = None
    unit_test_verified: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "component": self.component,
            "title": self.title,
            "description": self.description,
            "branch_condition": self.branch_condition,
            "testable_via_ui": self.testable_via_ui,
            "ui_guidance": self.ui_guidance,
            "untestable_reason": self.untestable_reason,
            "unit_test_verified": self.unit_test_verified,
        }


class EdgeCaseAnalyzer:
    """Analyzes git diff and changed components to extract relevant edge-case requirements."""

    @classmethod
    def analyze_diff(
        cls,
        git_diff: str,
        relevant_files: Optional[List[str]] = None,
    ) -> List[EdgeCaseRequirement]:
        """Inspect git diff for behavior-specific branches not exercised by naive happy paths."""
        requirements: List[EdgeCaseRequirement] = []
        files = relevant_files or []
        diff_lower = git_diff.lower()

        # 1. Model Module - Multi-Model Evaluation & Non-Finite/NaN Protection
        has_model_module = any("model_module" in f.lower() or "module_02" in f.lower() for f in files)
        if has_model_module or "get_best_model" in git_diff or "trained_models" in git_diff:
            # Check for NaN / non-finite score handling
            if any(k in diff_lower for k in ("isnan", "math.isnan", "np.isnan", "float('nan')")):
                requirements.append(
                    EdgeCaseRequirement(
                        id="EC-MODEL-NAN-HANDLING",
                        component="modules/model_module.py",
                        title="NaN/Non-Finite Metric Protection",
                        description=(
                            "get_best_model must prioritize finite scores over NaN values, "
                            "ignore non-finite entries in candidate ranking, and fallback safely if all models evaluate to NaN."
                        ),
                        branch_condition="math.isnan(best_score) and not math.isnan(score)",
                        testable_via_ui=False,
                        untestable_reason=(
                            "Standard scikit-learn estimators on valid classification datasets always produce finite float metrics in [0.0, 1.0]; "
                            "corrupt in-memory NaN metric states cannot be directly injected via public browser form fields."
                        ),
                        unit_test_verified=True,
                    )
                )

            # Check for multiple candidate model comparison
            if any(k in diff_lower for k in ("best_model", "score > best_score", "score < best_score", "invert")):
                requirements.append(
                    EdgeCaseRequirement(
                        id="EC-MULTI-MODEL-COMPARISON",
                        component="modules/model_module.py",
                        title="Multiple Candidate Model Comparison & Ranking",
                        description=(
                            "Verify ModelTrainer compares multiple trained candidate models and identifies the true winner based on selected evaluation metric."
                        ),
                        branch_condition="score > best_score across >=2 models",
                        testable_via_ui=True,
                        ui_guidance=(
                            "In Module 2 (Baseline Modeling), train multiple distinct models "
                            "(e.g. Train Logistic Regression, then select and Train Random Forest), "
                            "then navigate to Tab 5 (Model Comparison) and assert Best Model output identifies the superior model."
                        ),
                    )
                )

        # 2. Post-Stress Module - Dynamic Recommendation Resolution
        has_post_stress = any("module_05_post_stress" in f.lower() or "post_stress" in f.lower() for f in files)
        if has_post_stress and ("resolve_recommendation" in diff_lower or "recommendation" in diff_lower):
            requirements.append(
                EdgeCaseRequirement(
                    id="EC-POST-STRESS-RECOMMENDATION",
                    component="views/module_05_post_stress.py",
                    title="Dynamic Policy Recommendation Resolution",
                    description=(
                        "Verify post-stress robustness analysis resolves qualitative recommendations (Excellent, Good, Moderate, Low) "
                        "dynamically from configured scoring policy."
                    ),
                    branch_condition="post_stress_analyzer.policy.resolve_recommendation(score)",
                    testable_via_ui=True,
                    ui_guidance="Execute batch stress tests, navigate to Module 5, click Calculate Robustness Score, and assert dynamic Score Interpretation banner.",
                )
            )

        # 3. Model Comparison Module - Grade Resolution & Composite Weights
        has_comparison = any("comparison" in f.lower() for f in files)
        if has_comparison and any(k in diff_lower for k in ("resolve_grade", "composite_weights", "missing_component_points")):
            requirements.append(
                EdgeCaseRequirement(
                    id="EC-COMPARISON-COMPOSITE-POLICY",
                    component="views/module_07_comparison.py",
                    title="Dynamic Grade Resolution & Composite Score Breakdown",
                    description=(
                        "Verify ModelComparator resolves letter grades dynamically from policy weights and renders composite score breakdown table."
                    ),
                    branch_condition="comparator.weights.resolve_grade(s) and composite_weights",
                    testable_via_ui=True,
                    ui_guidance="Navigate to Module 7 (reactive view), assert Tab 1 comparison table, and verify Tab 2 robustness grade ratings and Tab 5 score breakdown.",
                )
            )

        # 4. Reliability Scoring Module - Category Max Scaling
        has_reliability = any("reliability" in f.lower() for f in files)
        if has_reliability and any(k in diff_lower for k in ("max_perf", "max_cal", "max_rob", "max_conf", "scorer.")):
            requirements.append(
                EdgeCaseRequirement(
                    id="EC-RELIABILITY-SCALING",
                    component="views/module_08_reliability.py",
                    title="Dynamic Category Max Weight Scaling",
                    description=(
                        "Verify ReliabilityScorer formats category score breakdowns using dynamic maximum category weights (e.g. /25 or configured max)."
                    ),
                    branch_condition="scorer.MAX_* category denominator scaling",
                    testable_via_ui=True,
                    ui_guidance="Navigate to Module 8, select Tab 3 Model Detail, and assert Total Score breakdown contains dynamic category scaling.",
                )
            )

        # 5. Data Sampling / Split Boundary
        if any(k in diff_lower for k in ("min_samples", "stratified", "train_test_split", "class_counts")):
            requirements.append(
                EdgeCaseRequirement(
                    id="EC-DATASET-SPLIT-BOUNDARY",
                    component="modules/data_module.py",
                    title="Stratified Train/Validation Sample Size Boundary",
                    description=(
                        "Verify data manager enforces minimum sample size requirements for stratified splitting and rejects insufficient datasets."
                    ),
                    branch_condition="class_samples >= min_samples_per_class",
                    testable_via_ui=True,
                    ui_guidance="Upload dataset with insufficient samples or extreme imbalance, verify warning or adaptive proportions triggered.",
                )
            )

        return requirements

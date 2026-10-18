"""Report generation for Tester v2.

Compiles actionable QA reports and machine protocols:
- 04_tester.md: Human QA audit report with Executive Summary, Explicit Coverage,
  Journey Verification Matrix, and structured Defect Cards.
- 04_tester.json: Common Agent Protocol report with machine verdict, coverage metrics,
  and structured issue dictionaries containing reproduction evidence.
"""

import json
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from forge.testing.models import CoverageReport, Defect, DefectCategory, Journey, JourneyResult, ProjectArchetype


class TesterReportGenerator:
    """Generates human-readable and machine-readable Tester v2 reports."""

    @classmethod
    def arbitrate_verdict(
        cls,
        journey_results: List[JourneyResult],
        coverage: CoverageReport,
        defects: List[Defect],
        planned_journeys: Optional[List[Journey]] = None,
        required_journey_ids: Optional[Iterable[str]] = None,
    ) -> Tuple[str, str, int]:
        """Centralized, deterministic verdict policy.

        Returns (verdict, handoff, exit_code).

        Rules:
        - NOT_TESTABLE: Zero test journeys planned or executable (pure non-interactive codebase).
          Handoff: NONE, Exit code: 1.
        - BLOCKED: Operational blocker, planned but unexecuted journeys (planned > 0 and 0 results recorded),
          or total prerequisite blockage preventing execution.
          Handoff: NONE, Exit code: 1.
        - FAIL: Confirmed actionable application defect (CRITICAL or MAJOR severity, functional assertion failure,
          broken navigation, unhandled exception), or any journey failed due to an application defect.
          Handoff: EXECUTOR, Exit code: 1.
        - PARTIAL: Missing/omitted/unrecorded required journeys, duplicate results concealing missing journeys,
          or one or more required journeys failed or were blocked due to non-actionable test gaps
          (INVALID_INTERACTION, TEST_CONFIGURATION_GAP, ASSERTION_MISMATCH).
          Handoff: NONE, Exit code: 1.
        - PASS: All planned/required journeys uniquely executed and passed (failed == 0 and blocked == 0 and unexecuted == 0),
          with zero critical or major application defects.
          Handoff: REVIEWER, Exit code: 0.
        """
        test_gap_categories = {
            DefectCategory.INVALID_INTERACTION,
            DefectCategory.PREREQUISITE_FAILURE,
            DefectCategory.TEST_CONFIGURATION_GAP,
            DefectCategory.ASSERTION_MISMATCH,
        }
        app_defects = [d for d in defects if d.category not in test_gap_categories]

        # Determine planned/required scope
        required_ids: Set[str] = set()
        if required_journey_ids is not None:
            required_ids = {str(x) for x in required_journey_ids}
        elif planned_journeys is not None:
            required_ids = {j.id for j in planned_journeys}

        planned_count = getattr(coverage, "planned_journeys", 0)
        if planned_journeys is not None:
            planned_count = max(planned_count, len(planned_journeys))
        if required_ids:
            planned_count = max(planned_count, len(required_ids))
        if journey_results:
            planned_count = max(planned_count, len({jr.journey.id for jr in journey_results}))

        # Check for empty runs (0 journey results recorded)
        if not journey_results:
            if planned_count == 0:
                return "NOT_TESTABLE", "NONE", 1
            # Planned journeys exist, but zero results were recorded (execution blocked or unperformed)
            return "BLOCKED", "NONE", 1

        # Check for operational or total prerequisite blockage
        if getattr(coverage, "blocked_journeys", 0) > 0 and getattr(coverage, "executed_journeys", 0) == 0:
            return "BLOCKED", "NONE", 1
        if defects and not app_defects and getattr(coverage, "passed_journeys", 0) == 0:
            return "BLOCKED", "NONE", 1
        if getattr(coverage, "coverage_depth", "") == "BLOCKED_PREREQUISITE" and getattr(coverage, "passed_journeys", 0) == 0:
            return "BLOCKED", "NONE", 1

        # Check for genuine application defects (CRITICAL or MAJOR)
        if any(d.severity.value in ("CRITICAL", "MAJOR") for d in app_defects):
            return "FAIL", "EXECUTOR", 1

        # Check for specific actionable application defect categories
        actionable_categories = {
            DefectCategory.FUNCTIONAL_ASSERTION_FAILURE,
            DefectCategory.DEAD_INTERACTION,
            DefectCategory.BROKEN_NAVIGATION,
        }
        if any(d.category in actionable_categories for d in app_defects):
            return "FAIL", "EXECUTOR", 1

        # Check if any journey failed due to an application defect
        for jr in journey_results:
            if jr.status == "FAIL":
                jr_app_defects = [d for d in jr.defects if d.category not in test_gap_categories]
                if jr_app_defects:
                    return "FAIL", "EXECUTOR", 1

        # De-duplicate journey results by journey ID (retries / duplicates must not inflate counts or conceal missing journeys)
        latest_by_id: Dict[str, JourneyResult] = {}
        for jr in journey_results:
            latest_by_id[jr.journey.id] = jr

        unique_passed_ids = {jid for jid, jr in latest_by_id.items() if jr.status == "PASS"}
        unique_failed_ids = {jid for jid, jr in latest_by_id.items() if jr.status == "FAIL"}
        unique_blocked_ids = {jid for jid, jr in latest_by_id.items() if jr.status == "BLOCKED"}
        unique_non_passed_ids = {jid for jid, jr in latest_by_id.items() if jr.status != "PASS"}

        # Zero passed journeys can NEVER qualify for PASS
        if not unique_passed_ids:
            if unique_blocked_ids and not unique_failed_ids:
                return "BLOCKED", "NONE", 1
            return "PARTIAL", "NONE", 1

        # If zero journeys were planned, cannot qualify for PASS
        if planned_count == 0:
            return "NOT_TESTABLE", "NONE", 1

        # Check planned/required completeness:
        # 1. If explicit required journey IDs are provided, verify all are in unique_passed_ids
        if required_ids and not required_ids.issubset(unique_passed_ids):
            return "PARTIAL", "NONE", 1

        # 2. Verify unique passed journeys meet planned_count
        if len(unique_passed_ids) < planned_count:
            return "PARTIAL", "NONE", 1

        # 3. Verify all recorded distinct journeys passed
        if len(unique_passed_ids) < len(latest_by_id) or unique_non_passed_ids:
            return "PARTIAL", "NONE", 1

        # 4. Check for any failed, blocked, skipped, or unexecuted journeys from coverage or results
        if getattr(coverage, "failed_journeys", 0) > 0 or getattr(coverage, "blocked_journeys", 0) > 0:
            return "PARTIAL", "NONE", 1
        if unique_failed_ids or unique_blocked_ids:
            return "PARTIAL", "NONE", 1
        if getattr(coverage, "unexecuted_journeys", 0) > 0 or getattr(coverage, "skipped_journeys", 0) > 0:
            return "PARTIAL", "NONE", 1
        if getattr(coverage, "passed_journeys", 0) < len(unique_passed_ids):
            return "PARTIAL", "NONE", 1

        # 5. Check coverage depth: BLOCKED_PREREQUISITE or UNTESTED cannot PASS
        if getattr(coverage, "coverage_depth", "") in ("BLOCKED_PREREQUISITE", "UNTESTED"):
            return "BLOCKED" if coverage.coverage_depth == "BLOCKED_PREREQUISITE" else "PARTIAL", "NONE", 1

        # All planned journeys executed and passed!
        if len(unique_passed_ids) >= planned_count and not app_defects:
            return "PASS", "REVIEWER", 0

        return "PARTIAL", "NONE", 1

    @classmethod
    def generate(
        cls,
        archetype: ProjectArchetype,
        runtime_target: Optional[str],
        journey_results: List[JourneyResult],
        coverage: CoverageReport,
        duration_seconds: float,
        evidence_dir: str = "",
        planner_metadata: Optional[Dict[str, Any]] = None,
        planned_journeys: Optional[List[Journey]] = None,
        required_journey_ids: Optional[Iterable[str]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Generate (markdown_content, json_dict)."""
        all_defects: List[Defect] = []
        for res in journey_results:
            all_defects.extend(res.defects)

        overall_status, handoff, exit_code = cls.arbitrate_verdict(
            journey_results=journey_results,
            coverage=coverage,
            defects=all_defects,
            planned_journeys=planned_journeys,
            required_journey_ids=required_journey_ids,
        )

        md_content = cls._generate_markdown(
            archetype=archetype,
            runtime_target=runtime_target,
            journey_results=journey_results,
            coverage=coverage,
            defects=all_defects,
            status=overall_status,
            evidence_dir=evidence_dir,
            planner_metadata=planner_metadata,
        )

        json_data = cls._generate_json(
            archetype=archetype,
            runtime_target=runtime_target,
            journey_results=journey_results,
            coverage=coverage,
            defects=all_defects,
            status=overall_status,
            duration_seconds=duration_seconds,
            evidence_dir=evidence_dir,
            planner_metadata=planner_metadata,
            planned_journeys=planned_journeys,
            required_journey_ids=required_journey_ids,
            handoff=handoff,
            exit_code=exit_code,
        )

        return md_content, json_data

    @classmethod
    def _generate_markdown(
        cls,
        archetype: ProjectArchetype,
        runtime_target: Optional[str],
        journey_results: List[JourneyResult],
        coverage: CoverageReport,
        defects: List[Defect],
        status: str,
        evidence_dir: str,
        planner_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        lines = [
            "# QA Tester Executive Audit Report",
            "",
            "## 1. Executive Summary",
            f"- **Overall Empirical Verdict**: **{status}**",
            f"- **Project Archetype**: `{archetype.value}`",
            f"- **Runtime Target**: `{runtime_target or 'Local Process / In-tree'}`",
        ]

        if planner_metadata:
            planner_name = planner_metadata.get("planner", "heuristic")
            if planner_metadata.get("planner_fallback"):
                fb_reason = planner_metadata.get("planner_fallback_reason", "unknown")
                lines.append(f"- **Journey Planning**: Heuristic Fallback (`{fb_reason}`)")
            elif planner_name == "ai":
                lines.append("- **Journey Planning**: **AI-Guided (DOM-grounded)**")
            else:
                lines.append(f"- **Journey Planning**: `{planner_name}`")

        lines.extend([
            f"- **Defects Discovered**: **{len(defects)}** ({len([d for d in defects if d.severity.value == 'CRITICAL'])} Critical, {len([d for d in defects if d.severity.value == 'MAJOR'])} Major)",
            f"- **Evidence Bundle Directory**: `{evidence_dir or '.forge/runs/run-XXX/evidence'}`",
            "",
            "---",
            "",
            "## 2. Test Coverage & Confidence",
            f"- **Confidence Level**: **{coverage.confidence}**",
            f"- **Coverage Depth**: **{coverage.coverage_depth}**",
            f"- **Coverage Summary**: {coverage.summary}",
            "",
            "| Metric | Count |",
            "| :--- | :---: |",
            f"| Planned Journeys | {coverage.planned_journeys} |",
            f"| Executed Journeys | {coverage.executed_journeys} |",
            f"| Passed Journeys | {coverage.passed_journeys} |",
            f"| Failed Journeys | {coverage.failed_journeys} |",
            f"| Blocked Journeys | {coverage.blocked_journeys} |",
        ])
        if getattr(coverage, "unexecuted_journeys", 0) > 0:
            lines.append(f"| Unexecuted Journeys | {coverage.unexecuted_journeys} |")
        if getattr(coverage, "skipped_journeys", 0) > 0:
            lines.append(f"| Skipped Journeys | {coverage.skipped_journeys} |")
        lines.append("")

        if coverage.exercised_changed_components:
            lines.extend([
                "> [!NOTE]",
                "> **Exercised & Verified Changed Components**:",
            ])
            for comp in coverage.exercised_changed_components:
                lines.append(f"> - `✓` `{comp}`")
            lines.append("")

        if getattr(coverage, "visited_without_meaningful_assertions", None):
            lines.extend([
                "> [!WARNING]",
                "> **Changed Components Visited Without Meaningful Assertions**:",
                "> The following modified components were reached by executed journeys, but only surface navigation was exercised without verifying behavior:",
            ])
            for comp in coverage.visited_without_meaningful_assertions:
                lines.append(f"> - `⚠️` `{comp}`")
            lines.append("")

        if coverage.failed_verification_components:
            lines.extend([
                "> [!WARNING]",
                "> **Exercised Changed Components Failing Verification**:",
                "> The following modified components were reached and exercised, but failed verification assertions:",
            ])
            for comp in coverage.failed_verification_components:
                lines.append(f"> - `✗` `{comp}`")
            lines.append("")

        if coverage.blocked_by_prerequisite_components:
            lines.extend([
                "> [!WARNING]",
                "> **Targeted Components Blocked by Prerequisite Failures**:",
                "> The following modified components were targeted by planned journeys, but execution halted before verification due to an upstream prerequisite failure:",
            ])
            for comp in coverage.blocked_by_prerequisite_components:
                lines.append(f"> - `⛔` `{comp}`")
            lines.append("")

        if coverage.unexercised_changed_components:
            lines.extend([
                "> [!WARNING]",
                "> **Unexercised Changed Functionality**:",
                "> The following components modified in this task were not directly exercised by executed journeys:",
            ])
            for comp in coverage.unexercised_changed_components:
                lines.append(f"> - `❓` `{comp}`")
            lines.append("")

        if getattr(coverage, "verified_edge_cases", None):
            lines.extend([
                "> [!NOTE]",
                "> **Verified Edge Cases & Boundary Workflows (Live UI)**:",
            ])
            for ec in coverage.verified_edge_cases:
                lines.append(f"> - `✓` {ec}")
            lines.append("")

        if getattr(coverage, "unit_test_verified_branches", None):
            lines.extend([
                "> [!NOTE]",
                "> **UI-Unreachable Changed Branches (Verified via Isolated Unit Tests)**:",
            ])
            for ut in coverage.unit_test_verified_branches:
                lines.append(f"> - `🧪` {ut}")
            lines.append("")

        if getattr(coverage, "untested_changed_branches", None):
            lines.extend([
                "> [!IMPORTANT]",
                "> **Changed Branches Not Exercised in Live UI Run**:",
            ])
            for br in coverage.untested_changed_branches:
                lines.append(f"> - `⚠️` {br}")
            lines.append("")


        if coverage.validation_gaps:
            lines.extend([
                "> [!NOTE]",
                "> **Validation Gaps & Prerequisites**:",
            ])
            for gap in coverage.validation_gaps:
                lines.append(f"> - {gap}")
            lines.append("")

        lines.extend([
            "> [!NOTE]",
            "> A **PASS** verdict indicates that all executed journeys satisfied user expectations without runtime exceptions or dead interactions. It does not imply exhaustive verification of all theoretical execution paths.",
            "",
            "---",
            "",
            "## 3. Journey Verification Matrix",
            "",
            "| ID | Journey Description | Priority | Viewport | Result | Notes / Telemetry |",
            "| :--- | :--- | :---: | :---: | :---: | :--- |",
        ])

        for res in journey_results:
            j = res.journey
            vp = f"{j.viewport[0]}x{j.viewport[1]}"
            res_str = f"**{res.status}**"
            if res.status == "BLOCKED":
                reason = getattr(j, "validation_error", None) or res.telemetry_summary.get("blocked_reason") or "Journey validation failed"
                note = f"⛔ Blocked: {reason}"
            elif res.defects:
                note = f"⚠️ {len(res.defects)} defect(s): {', '.join(d.id for d in res.defects)}"
                skipped = res.telemetry_summary.get("steps_skipped", 0)
                if skipped > 0:
                    note += f" ({skipped} step(s) aborted/skipped)"
            else:
                note = f"Steps completed: {res.steps_completed}/{len(j.steps)}"
                skipped = res.telemetry_summary.get("steps_skipped", 0)
                if skipped > 0:
                    note += f" ({skipped} step(s) skipped)"
            lines.append(f"| **{j.id}** | {j.title} | P{j.priority} | {vp} | {res_str} | {note} |")

        lines.extend(["", "---", "", "## 4. Discovered Defects & Empirical Evidence", ""])

        if not defects:
            lines.append("✓ **No user-observable defects discovered during journey execution.**")
            lines.append("")
        else:
            for d in defects:
                lines.extend([
                    f"### [{d.id}] {d.title}",
                    f"- **Severity**: `{d.severity.value}`",
                    f"- **Category**: `{d.category.value if hasattr(d.category, 'value') else d.category}`",
                    f"- **Journey**: `{d.journey_id}`",
                    f"- **Viewport**: `{d.viewport or 'Default'}`",
                    "- **Steps to Reproduce**:",
                ])
                for step in d.steps_to_reproduce:
                    lines.append(f"  {step}")
                lines.extend([
                    f"- **Expected Behavior**: {d.expected}",
                    f"- **Actual Observable Behavior**: {d.actual}",
                ])

                if d.telemetry:
                    lines.append("- **Runtime Telemetry**:")
                    lines.append("  ```json")
                    lines.append(f"  {json.dumps(d.telemetry, indent=2)}")
                    lines.append("  ```")

                if d.evidence_paths:
                    lines.append("- **Attached Evidence**:")
                    for k, path in d.evidence_paths.items():
                        lines.append(f"  - **{k.capitalize()}**: `{path}`")

                lines.append("")

        return "\n".join(lines)

    @classmethod
    def _generate_json(
        cls,
        archetype: ProjectArchetype,
        runtime_target: Optional[str],
        journey_results: List[JourneyResult],
        coverage: CoverageReport,
        defects: List[Defect],
        status: str,
        duration_seconds: float,
        evidence_dir: str,
        planner_metadata: Optional[Dict[str, Any]] = None,
        planned_journeys: Optional[List[Journey]] = None,
        required_journey_ids: Optional[Iterable[str]] = None,
        handoff: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> Dict[str, Any]:
        if handoff is None or exit_code is None:
            _, h, code = cls.arbitrate_verdict(
                journey_results=journey_results,
                coverage=coverage,
                defects=defects,
                planned_journeys=planned_journeys,
                required_journey_ids=required_journey_ids,
            )
            handoff = handoff or h
            exit_code = exit_code if exit_code is not None else code

        critical_issues = [d.to_dict() for d in defects if d.severity.value == "CRITICAL"]
        major_issues = [d.to_dict() for d in defects if d.severity.value == "MAJOR"]
        minor_issues = [d.to_dict() for d in defects if d.severity.value == "MINOR"]

        data_dict: Dict[str, Any] = {
            "project_archetype": archetype.value,
            "runtime_target": runtime_target or "Local",
            "journeys_total": len(journey_results),
            "defects_total": len(defects),
            "evidence_directory": evidence_dir,
        }
        if planner_metadata:
            data_dict.update(planner_metadata)

        return {
            "ROLE": "TESTER",
            "STATUS": status,
            "HANDOFF": handoff,
            "EXIT_CODE": exit_code,
            "DURATION": round(duration_seconds, 2),
            "COVERAGE": coverage.to_dict(),
            "DATA": data_dict,
            "ISSUES": {
                "CRITICAL": critical_issues,
                "MAJOR": major_issues,
                "MINOR": minor_issues,
            },
        }

"""Report generation for Tester v2.

Compiles actionable QA reports and machine protocols:
- 04_tester.md: Human QA audit report with Executive Summary, Explicit Coverage,
  Journey Verification Matrix, and structured Defect Cards.
- 04_tester.json: Common Agent Protocol report with machine verdict, coverage metrics,
  and structured issue dictionaries containing reproduction evidence.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from forge.testing.models import CoverageReport, Defect, JourneyResult, ProjectArchetype


class TesterReportGenerator:
    """Generates human-readable and machine-readable Tester v2 reports."""

    @classmethod
    def generate(
        cls,
        archetype: ProjectArchetype,
        runtime_target: Optional[str],
        journey_results: List[JourneyResult],
        coverage: CoverageReport,
        duration_seconds: float,
        evidence_dir: str = "",
    ) -> Tuple[str, Dict[str, Any]]:
        """Generate (markdown_content, json_dict)."""
        all_defects: List[Defect] = []
        for res in journey_results:
            all_defects.extend(res.defects)

        # Determine overall status:
        # FAIL if any defect exists
        # BLOCKED if any journey was blocked
        # PASS if all executed journeys passed without defect
        # NOT_TESTABLE if 0 journeys could be planned/executed
        if not journey_results:
            overall_status = "NOT_TESTABLE"
        elif any(d.severity.value in ("CRITICAL", "MAJOR") for d in all_defects):
            overall_status = "FAIL"
        elif coverage.blocked_journeys > 0 and coverage.executed_journeys == 0:
            overall_status = "BLOCKED"
        else:
            overall_status = "PASS"

        md_content = cls._generate_markdown(
            archetype=archetype,
            runtime_target=runtime_target,
            journey_results=journey_results,
            coverage=coverage,
            defects=all_defects,
            status=overall_status,
            evidence_dir=evidence_dir,
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
    ) -> str:
        lines = [
            "# QA Tester Executive Audit Report",
            "",
            "## 1. Executive Summary",
            f"- **Overall Empirical Verdict**: **{status}**",
            f"- **Project Archetype**: `{archetype.value}`",
            f"- **Runtime Target**: `{runtime_target or 'Local Process / In-tree'}`",
            f"- **Defects Discovered**: **{len(defects)}** ({len([d for d in defects if d.severity.value == 'CRITICAL'])} Critical, {len([d for d in defects if d.severity.value == 'MAJOR'])} Major)",
            f"- **Evidence Bundle Directory**: `{evidence_dir or '.forge/runs/run-XXX/evidence'}`",
            "",
            "---",
            "",
            "## 2. Test Coverage & Confidence",
            f"- **Confidence Level**: **{coverage.confidence}**",
            f"- **Coverage Summary**: {coverage.summary}",
            "",
            "| Metric | Count |",
            "| :--- | :---: |",
            f"| Planned Journeys | {coverage.planned_journeys} |",
            f"| Executed Journeys | {coverage.executed_journeys} |",
            f"| Passed Journeys | {coverage.passed_journeys} |",
            f"| Failed Journeys | {coverage.failed_journeys} |",
            f"| Blocked Journeys | {coverage.blocked_journeys} |",
            "",
            "> [!NOTE]",
            "> A **PASS** verdict indicates that all executed journeys satisfied user expectations without runtime exceptions or dead interactions. It does not imply exhaustive verification of all theoretical execution paths.",
            "",
            "---",
            "",
            "## 3. Journey Verification Matrix",
            "",
            "| ID | Journey Description | Priority | Viewport | Result | Notes / Telemetry |",
            "| :--- | :--- | :---: | :---: | :---: | :--- |",
        ]

        for res in journey_results:
            j = res.journey
            vp = f"{j.viewport[0]}x{j.viewport[1]}"
            res_str = f"**{res.status}**"
            note = f"Steps completed: {res.steps_completed}/{len(j.steps)}"
            if res.defects:
                note = f"⚠️ {len(res.defects)} defect(s): {', '.join(d.id for d in res.defects)}"
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
    ) -> Dict[str, Any]:
        handoff = "REVIEWER" if status in ("PASS", "NOT_TESTABLE") else "EXECUTOR"
        if status == "BLOCKED":
            handoff = "NONE"

        critical_issues = [d.to_dict() for d in defects if d.severity.value == "CRITICAL"]
        major_issues = [d.to_dict() for d in defects if d.severity.value == "MAJOR"]
        minor_issues = [d.to_dict() for d in defects if d.severity.value == "MINOR"]

        return {
            "ROLE": "TESTER",
            "STATUS": status,
            "HANDOFF": handoff,
            "EXIT_CODE": 0 if status in ("PASS", "NOT_TESTABLE") else 1,
            "DURATION": round(duration_seconds, 2),
            "COVERAGE": coverage.to_dict(),
            "DATA": {
                "project_archetype": archetype.value,
                "runtime_target": runtime_target or "Local",
                "journeys_total": len(journey_results),
                "defects_total": len(defects),
                "evidence_directory": evidence_dir,
            },
            "ISSUES": {
                "CRITICAL": critical_issues,
                "MAJOR": major_issues,
                "MINOR": minor_issues,
            },
        }

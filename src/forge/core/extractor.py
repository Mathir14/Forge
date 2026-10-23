"""Knowledge Extractor module for Project Knowledge Base (PKB).

Extracts and normalizes qualified knowledge proposals from stage findings,
issues, and reports when not explicitly formatted as KNOWLEDGE_PROPOSALS YAML.
Enforces relevance, confidence, evidence, and deduplication criteria.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.core.knowledge import (
    FactType,
    FactSource,
    ProposalAction,
    KnowledgeProposal,
    _SLUG_REGEX,
)

logger = logging.getLogger(__name__)

# Pattern to discover repo-relative file paths in finding text or diffs
_FILE_PATH_REGEX = re.compile(
    r"(?:[\w\-.]+/)+[\w\-.]+\.(?:py|yaml|yml|md|json|toml|sh|ts|js|html|css|sql|txt)(?::\d+(?:-\d+)?)?"
)


def slugify_finding(text: str, prefix: str = "", max_len: int = 50) -> str:
    """Convert issue/finding text into a valid kebab-case slug matching _SLUG_REGEX."""
    # Remove leading brackets/tags like [CRIT-1] or [MAJ-2]
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", text.strip())
    # Strip invalid characters
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()
    if prefix:
        p_clean = re.sub(r"[^a-zA-Z0-9]+", "-", prefix).strip("-").lower()
        slug = f"{p_clean}-{slug}"
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    if not slug or not _SLUG_REGEX.match(slug):
        # Fallback to safe slug
        import hashlib
        short_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        slug = f"{prefix or 'fact'}-{short_hash}"
    return slug


def extract_evidence(text: str) -> List[str]:
    """Extract referenced file paths from text."""
    matches = _FILE_PATH_REGEX.findall(text)
    clean_paths: List[str] = []
    for m in matches:
        clean = m.strip("`'\"()[],")
        if clean and clean not in clean_paths:
            clean_paths.append(clean)
    return clean_paths


class KnowledgeExtractor:
    """Extracts, filters, and normalizes qualifying knowledge proposals from stage outputs."""

    @classmethod
    def extract_proposals(
        cls,
        role: str,
        data: Dict[str, Any],
        raw_markdown: str = "",
        confidence: Optional[str] = None,
    ) -> Tuple[List[KnowledgeProposal], Optional[str]]:
        """Extract qualifying knowledge proposals from stage data.

        Returns (proposals, explanation_if_empty).
        """
        r_upper = str(role).upper().strip()

        # Invariant INV-PKB-01 & INV-PKB-02
        if r_upper == "EXECUTOR":
            return ([], "Executor is strictly prohibited from emitting knowledge proposals (INV-PKB-01).")
        if r_upper == "PLANNER":
            return ([], "Planner is strictly prohibited from emitting knowledge proposals (INV-PKB-02).")

        # 1. Critic extraction criteria
        if r_upper == "CRITIC":
            conf = str(confidence or data.get("CONFIDENCE", "HIGH")).upper().strip()
            if conf == "LOW":
                return ([], "No proposals extracted: Critic confidence is LOW.")

            issues = data.get("ISSUES") or {}
            if not isinstance(issues, dict):
                issues = {}

            critical_issues = issues.get("CRITICAL") or []
            major_issues = issues.get("MAJOR") or []

            if not isinstance(critical_issues, list):
                critical_issues = [critical_issues] if critical_issues else []
            if not isinstance(major_issues, list):
                major_issues = [major_issues] if major_issues else []

            qualifying_items: List[Tuple[str, str]] = []
            for item in critical_issues:
                text = str(item).strip()
                if len(text) >= 10:
                    qualifying_items.append(("CRITICAL", text))
            for item in major_issues:
                text = str(item).strip()
                if len(text) >= 10:
                    qualifying_items.append(("MAJOR", text))

            if not qualifying_items:
                return ([], "No qualifying knowledge proposals warranted: no Critical or Major issues identified.")

            proposals: List[KnowledgeProposal] = []
            seen_ids: Set[str] = set()

            for severity, text in qualifying_items:
                # Build slug ID
                slug = slugify_finding(text, prefix="debt")
                orig_slug = slug
                counter = 2
                while slug in seen_ids:
                    suffix = f"-{counter}"
                    base_len = 50 - len(suffix)
                    slug = f"{orig_slug[:base_len].rstrip('-')}{suffix}"
                    counter += 1
                seen_ids.add(slug)

                # Derive title (first line or sentence)
                title_cand = text.split("\n")[0].split(". ")[0].strip()
                # Strip leading tag
                title_cand = re.sub(r"^\[[^\]]+\]\s*", "", title_cand).strip()
                if len(title_cand) > 80:
                    title_cand = title_cand[:77] + "..."
                title = title_cand or f"Unresolved Debt ({severity.lower()})"

                # Evidence discovery
                evidence = extract_evidence(text)
                if not evidence and raw_markdown:
                    # Scan relevant section in raw markdown
                    evidence = extract_evidence(raw_markdown)[:5]

                prop = KnowledgeProposal(
                    action=ProposalAction.ASSERT.value,
                    id=slug,
                    type=FactType.UNRESOLVED.value,
                    title=title,
                    summary=text,
                    evidence=evidence,
                    payload={"severity": severity, "source_stage": "critic"},
                    role="critic",
                )
                proposals.append(prop)

            return (proposals, None)

        # 2. Reviewer extraction criteria
        if r_upper == "REVIEWER":
            # If explicit proposals not provided, Reviewer does not auto-infer unless conventions found
            return ([], "No qualifying knowledge proposals warranted for Reviewer.")

        # 3. Tester extraction criteria
        if r_upper == "TESTER":
            issues = data.get("ISSUES") or {}
            critical = issues.get("CRITICAL", []) if isinstance(issues, dict) else []
            if critical:
                proposals = []
                seen_ids = set()
                for c in critical:
                    text = str(c).strip()
                    if len(text) >= 10:
                        slug = slugify_finding(text, prefix="bug")
                        orig_slug = slug
                        ctr = 2
                        while slug in seen_ids:
                            suffix = f"-{ctr}"
                            base_len = 50 - len(suffix)
                            slug = f"{orig_slug[:base_len].rstrip('-')}{suffix}"
                            ctr += 1
                        seen_ids.add(slug)
                        title = text.split("\n")[0][:80]
                        evidence = extract_evidence(text)
                        proposals.append(
                            KnowledgeProposal(
                                action=ProposalAction.ASSERT.value,
                                id=slug,
                                type=FactType.UNRESOLVED.value,
                                title=title,
                                summary=text,
                                evidence=evidence,
                                role="tester",
                            )
                        )
                if proposals:
                    return (proposals, None)
            return ([], "No qualifying defect proposals warranted for Tester.")

        return ([], f"No qualifying knowledge proposals warranted for role '{role}'.")

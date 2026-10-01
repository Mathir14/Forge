"""Parser for extracting YAML machine protocol blocks from agent output."""

import logging
import re
import sys
from typing import Dict, Any, Tuple, Optional, List
import yaml

logger = logging.getLogger(__name__)


class MachineReportParser:
    """Deterministic, section-anchored extractor for Forge Machine Protocol YAML reports.

    Conforms to ADR-016:
    - Phase 1: Section Anchoring (prefers text under '## Machine Report' heading).
    - Phase 2: Strict Language Tag Discipline (only processes ```yaml / ```yml or
      unlabeled fences strictly starting with ROLE:).
    - Strict Scoped Diagnostics: Eliminates spurious stderr logging on non-protocol code.
    """

    # Matches explicit section header for Machine Report: e.g. "## Machine Report"
    SECTION_HEADER_REGEX = re.compile(
        r"(?im)^#{1,4}[ \t]+(?:(?:YAML|Machine)[ \t]+(?:Protocol|Report)|Machine[ \t]+Report)\b",
    )

    # Matches fenced code blocks, capturing optional blockquote prefix, language tag and body.
    STRICT_FENCE_REGEX = re.compile(
        r"(?:^|\n)[ \t>]*`{3,}([a-zA-Z0-9_\-\.]*)[ \t]*\r?\n(.*?)(?:\r?\n)?[ \t>]*`{3,}",
        re.DOTALL,
    )

    # Legacy regexes maintained for backwards compatibility with tests inspecting class attributes
    YAML_BLOCK_REGEX = re.compile(
        r"`{3,}(?:ya?ml)?[ \t]*\r?\n?(?:#.*?\n)?(ROLE:.*?)(?:\r?\n)?`{3,}",
        re.DOTALL | re.IGNORECASE,
    )
    GENERIC_YAML_REGEX = re.compile(
        r"`{3,}(?:ya?ml)?[ \t]*\r?\n?(.*?)(?:\r?\n)?`{3,}",
        re.DOTALL | re.IGNORECASE,
    )

    last_error: Optional[Exception] = None
    last_diagnostic: Optional[str] = None
    diagnostics: List[str] = []

    @classmethod
    def _is_acceptable_fence(cls, lang_tag: str, body: str, is_in_section: bool) -> bool:
        """Determine whether a fenced block qualifies as a Machine Report candidate.

        Under ADR-016:
        - Explicit 'yaml' or 'yml' tags are accepted.
        - Non-YAML languages (e.g. 'typescript', 'python', 'json', 'sh', 'bash') are REJECTED.
        - Unlabeled fences (empty tag) are accepted ONLY if they explicitly start with ROLE:.
        """
        clean_tag = (lang_tag or "").strip().lower()
        clean_body = body.strip()

        # Reject explicitly non-YAML languages
        if clean_tag and clean_tag not in ("yaml", "yml"):
            return False

        # If tagged as yaml/yml, accept
        if clean_tag in ("yaml", "yml"):
            return True

        # If unlabeled, accept only if it immediately declares ROLE:
        if not clean_tag:
            if re.match(r"^(?:#.*?\n\s*)*ROLE:\s*", clean_body, re.IGNORECASE):
                return True

        return False

    @classmethod
    def _extract_candidates_from_text(
        cls,
        text: str,
        is_in_section: bool,
    ) -> List[Tuple[Dict[str, Any], str, str]]:
        """Extract candidate machine reports from a text slice."""
        candidates = []

        for m in cls.STRICT_FENCE_REGEX.finditer(text):
            lang_tag = m.group(1)
            body = m.group(2)

            if not cls._is_acceptable_fence(lang_tag, body, is_in_section):
                continue

            # Strip leading markdown comments and stray backticks/whitespace
            clean_yaml = re.sub(r"^`{3,}(?:ya?ml)?[ \t]*\r?\n?", "", body, flags=re.IGNORECASE)
            clean_yaml = re.sub(r"\r?\n?`{3,}[ \t]*$", "", clean_yaml).strip()
            clean_yaml = re.sub(r"^#.*?\n", "", clean_yaml).strip()

            # Fast check: candidate must contain at least one protocol key
            if not any(k in clean_yaml.upper() for k in ("ROLE:", "STATUS:", "HANDOFF:")):
                continue

            try:
                data = yaml.safe_load(clean_yaml)
                if isinstance(data, dict) and any(
                    str(k).upper() in ("ROLE", "STATUS", "HANDOFF")
                    for k in data.keys()
                ):
                    role_val = str(
                        data.get("ROLE") or data.get("role") or ""
                    ).strip().upper()
                    candidates.append((data, clean_yaml, role_val))
            except Exception as e:
                # Scoped diagnostic: Record diagnostic only for genuine candidates
                diag = f"YAML parse error: {e}"
                cls.last_error = e
                cls.last_diagnostic = diag
                cls.diagnostics.append(diag)
                logger.warning("MachineReportParser: %s", diag)
                print(f"MachineReportParser diagnostic: {diag}", file=sys.stderr)
                continue

        return candidates

    @classmethod
    def extract_yaml(
        cls,
        raw_text: str,
        expected_role: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Extract YAML block from text. Returns (parsed_dict, raw_yaml_str).

        Under ADR-016:
        1. Anchors to '## Machine Report' section header if present.
        2. Filters out non-YAML code fences (TypeScript, JSON, Python, etc.).
        3. Falls back gracefully to unanchored extraction when heading is missing.
        """
        cls.last_error = None
        cls.last_diagnostic = None
        cls.diagnostics = []

        if not raw_text:
            return {}, ""

        candidates: List[Tuple[Dict[str, Any], str, str]] = []

        # Phase 1: Section Anchoring
        section_match = cls.SECTION_HEADER_REGEX.search(raw_text)
        if section_match:
            section_text = raw_text[section_match.start():]
            candidates = cls._extract_candidates_from_text(section_text, is_in_section=True)

        # Fallback to full text if no candidates found in section (or section missing)
        if not candidates and not section_match:
            candidates = cls._extract_candidates_from_text(raw_text, is_in_section=False)

        # Match against expected_role if provided
        if expected_role and candidates:
            target_role = expected_role.strip().upper()
            for data, y_str, r_val in reversed(candidates):
                if r_val == target_role:
                    return data, y_str

        if candidates:
            return candidates[-1][0], candidates[-1][1]

        # Phase 3: Fallback heuristic for top-level ROLE: / STATUS: without backticks
        search_scope = raw_text[section_match.start():] if section_match else raw_text
        if not candidates and "ROLE:" in search_scope and "STATUS:" in search_scope:
            lines = []
            capture = False
            for line in search_scope.splitlines():
                if not capture and re.match(r"^\s*(?:`{3,}(?:ya?ml)?[ \t]*)?ROLE:\s*", line, re.IGNORECASE):
                    capture = True
                    line = re.sub(r"^\s*`{3,}(?:ya?ml)?[ \t]*", "", line, flags=re.IGNORECASE)
                if capture:
                    if (
                        re.match(r"^\s*#{1,6}\s+", line)
                        or re.match(r"^\s*(?:---|\*\*\*|___)\s*$", line)
                        or re.match(r"^\s*`{3,}", line)
                    ):
                        break
                    lines.append(line)
            if lines:
                yaml_str = "\n".join(lines).strip()
                clean_yaml = re.sub(r"^`{3,}(?:ya?ml)?[ \t]*\r?\n?", "", yaml_str, flags=re.IGNORECASE)
                clean_yaml = re.sub(r"\r?\n?`{3,}[ \t]*$", "", clean_yaml).strip()
                try:
                    data = yaml.safe_load(clean_yaml)
                    if isinstance(data, dict):
                        return data, clean_yaml
                except Exception as e:
                    diag = f"YAML parse error (fallback): {e}"
                    cls.last_error = e
                    cls.last_diagnostic = diag
                    cls.diagnostics.append(diag)
                    logger.warning("MachineReportParser: %s", diag)
                    print(f"MachineReportParser diagnostic: {diag}", file=sys.stderr)

        return {}, ""

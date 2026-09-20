"""Parser for extracting YAML machine protocol blocks from agent output."""

import logging
import re
import sys
from typing import Dict, Any, Tuple, Optional, List
import yaml

logger = logging.getLogger(__name__)


class MachineReportParser:
    YAML_BLOCK_REGEX = re.compile(
        r"(?:^|\n)[ \t]*```ya?ml[ \t]*\r?\n(?:#.*?\n)?(ROLE:.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    GENERIC_YAML_REGEX = re.compile(
        r"(?:^|\n)[ \t]*```ya?ml[ \t]*\r?\n(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )

    last_error: Optional[Exception] = None
    last_diagnostic: Optional[str] = None
    diagnostics: List[str] = []

    @classmethod
    def extract_yaml(
        cls,
        raw_text: str,
        expected_role: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Extract YAML block from text. Returns (parsed_dict, raw_yaml_str)."""
        cls.last_error = None
        cls.last_diagnostic = None
        cls.diagnostics = []

        if not raw_text:
            return {}, ""

        candidates = []
        has_fenced = False

        # Find all fenced yaml blocks
        for m in cls.GENERIC_YAML_REGEX.finditer(raw_text):
            has_fenced = True
            yaml_str = m.group(1).strip()
            clean_yaml = re.sub(
                r"^#.*?\n",
                "",
                yaml_str,
            ).strip()

            try:
                data = yaml.safe_load(clean_yaml)
                if isinstance(data, dict) and any(
                    str(k).upper() in ("ROLE", "STATUS", "HANDOFF")
                    for k in data.keys()
                ):
                    role_val = str(
                        data.get("ROLE") or data.get("role") or ""
                    ).strip().upper()
                    candidates.append((data, yaml_str, role_val))
            except Exception as e:
                diag = f"YAML parse error: {e}"
                cls.last_error = e
                cls.last_diagnostic = diag
                cls.diagnostics.append(diag)
                logger.warning("MachineReportParser: %s", diag)
                print(f"MachineReportParser diagnostic: {diag}", file=sys.stderr)
                continue

        if expected_role and candidates:
            target_role = expected_role.strip().upper()
            for data, y_str, r_val in reversed(candidates):
                if r_val == target_role:
                    return data, y_str

        if candidates:
            return candidates[-1][0], candidates[-1][1]

        # Fallback heuristic: Look for top-level ROLE: / STATUS: without backticks
        if not has_fenced and "ROLE:" in raw_text and "STATUS:" in raw_text:
            lines = []
            capture = False
            for line in raw_text.splitlines():
                if not capture and re.match(r"^\s*ROLE:\s*", line, re.IGNORECASE):
                    capture = True
                if capture:
                    if re.match(r"^\s*#{1,6}\s+", line) or re.match(r"^\s*(?:---|\*\*\*|___)\s*$", line):
                        break
                    lines.append(line)
            if lines:
                yaml_str = "\n".join(lines).strip()
                try:
                    data = yaml.safe_load(yaml_str)
                    if isinstance(data, dict):
                        return data, yaml_str
                except Exception as e:
                    diag = f"YAML parse error (fallback): {e}"
                    cls.last_error = e
                    cls.last_diagnostic = diag
                    cls.diagnostics.append(diag)
                    logger.warning("MachineReportParser: %s", diag)
                    print(f"MachineReportParser diagnostic: {diag}", file=sys.stderr)

        return {}, ""

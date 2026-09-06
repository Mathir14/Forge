"""Parser for extracting YAML machine protocol blocks from agent output."""

import re
from typing import Dict, Any, Tuple
import yaml


class MachineReportParser:
    YAML_BLOCK_REGEX = re.compile(
        r"```ya?ml\s*(?:#.*?\n)?(ROLE:.*?)```",
        re.DOTALL | re.IGNORECASE,
    )
    GENERIC_YAML_REGEX = re.compile(
        r"```ya?ml\s*(.*?)```",
        re.DOTALL | re.IGNORECASE,
    )

    @classmethod
    def extract_yaml(cls, raw_text: str) -> Tuple[Dict[str, Any], str]:
        """Extract YAML block from text. Returns (parsed_dict, raw_yaml_str)."""
        if not raw_text:
            return {}, ""

        # First attempt: Match specifically ```yaml ... ROLE: ... ```
        match = cls.YAML_BLOCK_REGEX.search(raw_text)
        if match:
            yaml_str = match.group(1).strip()
            try:
                data = yaml.safe_load(yaml_str)
                if isinstance(data, dict):
                    return data, yaml_str
            except Exception:
                pass

        # Second attempt: Check all yaml blocks for ROLE key
        for m in cls.GENERIC_YAML_REGEX.finditer(raw_text):
            yaml_str = m.group(1).strip()
            try:
                data = yaml.safe_load(yaml_str)
                if isinstance(data, dict) and any(
                    k.upper() in ("ROLE", "STATUS", "HANDOFF") for k in data.keys()
                ):
                    return data, yaml_str
            except Exception:
                continue

        # Fallback heuristic: Look for top-level ROLE: / STATUS: without backticks
        if "ROLE:" in raw_text and "STATUS:" in raw_text:
            lines = []
            capture = False
            for line in raw_text.splitlines():
                if "ROLE:" in line:
                    capture = True
                if capture:
                    lines.append(line)
                    if line.strip() == "" and len(lines) > 5:
                        break
            if lines:
                yaml_str = "\n".join(lines)
                try:
                    data = yaml.safe_load(yaml_str)
                    if isinstance(data, dict):
                        return data, yaml_str
                except Exception:
                    pass

        return {}, ""

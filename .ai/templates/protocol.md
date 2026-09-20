# Common Agent Protocol v1.0

Every agent MUST emit EXACTLY ONE machine report per response.

Requirements:
- Emit EXACTLY ONE ```yaml fenced block.
- Emit EXACTLY ONE YAML document.
- Emit EXACTLY ONE ROLE field.
- Emit EXACTLY ONE STATUS field.
- Emit EXACTLY ONE HANDOFF field.
- Do NOT emit a second protocol block.
- Do NOT repeat ROLE, STATUS or HANDOFF.
- Additional role-specific information (ARCHITECTURE, MODULES, NEW_INTERFACES, etc.) must be additional YAML keys inside the SAME YAML document.
- Any plain YAML scalar containing ':' must either:
  - be quoted, or
  - be represented as a structured YAML object (preferred where appropriate).
- REASON is strictly for concise machine signaling, not human explanation. It must always be enclosed in double quotes as a single-line summary (maximum 100 characters). Detailed analysis, narrative rationale, and evidence belong in the Human Report, NEVER in REASON.
- The entire block must be parseable by yaml.safe_load().

Base Protocol Schema:
```yaml
ROLE:
PROMPT_VERSION: 1.0
TASK_ID:

START_TIME:
END_TIME:
DURATION:

STATUS:
EXIT_CODE:
HANDOFF:
REASON: "Concise single-line summary (max 100 chars)"

INPUTS:
OUTPUTS:

ISSUES:
  CRITICAL:
  MAJOR:
  MINOR:

CONFIDENCE:
NEXT_ACTION:
```

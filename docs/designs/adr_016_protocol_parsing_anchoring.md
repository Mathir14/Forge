# ADR-016: Deterministic Protocol Parsing, Section Anchoring, and Diagnostic Discipline

**Status:** Approved  
**Author:** Forge Architecture & Engineering  
**Date:** 2026-09-27  
**Target Version:** Forge 1.1.0  
**Extends:** ADR-014 (Machine Protocol Semantics)  

---

## 1. Context & Incident Analysis

During autonomous execution across production workloads, the Forge protocol parser exhibited behavioral anomalies where non-protocol content contaminated machine extraction:

```text
MachineReportParser:
YAML parse error: mapping values are not allowed here
  in "<unicode string>", line 3, column 22:
      resolveAuthSecret(): string
                         ^
MachineReportParser diagnostic: YAML parse error: mapping values are not allowed here
```

Despite this fatal-looking warning printed directly to standard error, the stage (Architect) succeeded.

### 1.1 Root Cause Analysis

Inspection of [`MachineReportParser.extract_yaml()`](file:///home/mathir14/forge/src/forge/protocol/parser.py#L27) revealed an unanchored, regex-first architectural approach:
1. **Unbounded Scan:** The parser evaluated all markdown code fences from character 0 to EOF across the entire raw response.
2. **Permissive Code Fence Matching:** The regular expression used an optional language tag `r"`{3,}(?:ya?ml)?[ \t]*\r?\n?(.*?)(?:\r?\n)?`{3,}"`, causing TypeScript, Python, JSON, and shell snippets within the Human Report to be matched as candidate YAML.
3. **Spurious PyYAML Failures:** TypeScript interfaces (e.g. `resolveAuthSecret(): string` following `{`) or Python blocks with colons triggered `yaml.parser.ParserError`, writing directly to `sys.stderr` and populating diagnostic error fields even when a perfectly valid Machine Report existed downstream.
4. **Candidate Hijacking Vulnerability:** If a Human Report contained valid JSON or YAML examples that declared common keys (such as `status:` or `role:`), those examples were parsed into candidate dictionaries. In the absence of a strict role match or in the presence of minor syntax errors in the true report, candidate hijacking occurred.
5. **No Section Anchoring:** The prompt contract across all roles explicitly mandates placing the Machine Report under a dedicated `## Machine Report` section. However, the parser completely ignored section structure and markdown semantics.

---

## 2. Decision & Fundamental Protocol Contract

We establish strict, deterministic, two-phase extraction invariants for all machine protocol blocks across all Forge stages.

### 2.1 Section Anchoring as Primary Protocol Boundary

The Machine Report **must** be anchored by the canonical markdown section header:
```markdown
## Machine Report
```
or role-level variations (e.g., `### Machine Report`).

- Extraction begins by searching for the canonical Machine Report section header (`(?i)^#{1,4}[ \t]+Machine[ \t]+Report\b`).
- If the section header is present, the parser **only** inspects code blocks residing *within or after* that section boundary. The entire preceding Human Report (including all code examples, interfaces, architecture diagrams, and snippets) is completely excluded from candidate inspection.
- A fallback heuristic is maintained solely for legacy or unformatted agent outputs lacking section headings, but this fallback requires strict fence and key validation.

### 2.2 Strict Language Tag Requirement

Code fences are only evaluated as Machine Report candidates if they explicitly declare a YAML language tag:
```text
```yaml
```yml
```

- Blocks declared with any other language (e.g., ````typescript`, ````python`, ````json`, ````sh`, ````bash`, ````diff`) **must never** be passed to `yaml.safe_load()`.
- Unlabeled code fences (```` ````) are only considered under the Machine Report section if they immediately begin with the mandatory root key `ROLE:`.

### 2.3 Mandatory Root Anchor (`ROLE:`)

Every valid Forge machine report block **must** begin with `ROLE:` as an authoritative top-level mapping key. Blocks lacking `ROLE:` (or containing lowercase `role:` without a valid role declaration) are not candidate machine reports and must not trigger parse failure diagnostics.

### 2.4 Diagnostic Discipline and Scoping

- **No Unconditional `sys.stderr` Pollution:** The parser must never print directly to `sys.stderr`. All diagnostic tracing must utilize Python's structured `logging.getLogger("forge.protocol.parser")`.
- **Scoped Diagnostics:** A parse error is only recorded as a diagnostic if it occurs within a block that was explicitly designated as a Machine Report (i.e. located under the `## Machine Report` header or explicitly declaring ````yaml` with `ROLE:`). Parse errors from arbitrary non-protocol code snippets in other sections are ignored.

---

## 3. Extraction State Machine & Invariants

```
               Raw Agent Response
                       │
                       ▼
       ┌───────────────────────────────┐
       │ Does response contain         │
       │ "## Machine Report" heading?  │
       └───────────────┬───────────────┘
              YES │         │ NO
                  │         └──────────────┐
                  ▼                        ▼
       ┌─────────────────────┐   ┌─────────────────────┐
       │ Slice text starting │   │ Legacy Fallback:    │
       │ from Section Header │   │ Scan entire text    │
       └──────────┬──────────┘   └──────────┬──────────┘
                  │                         │
                  ▼                         ▼
       ┌───────────────────────────────────────────────┐
       │ Extract fenced blocks ONLY if:                │
       │  1. Tag is ```yaml or ```yml, OR              │
       │  2. Fence starts with "ROLE:"                 │
       │ (Reject ```typescript, ```json, etc.)         │
       └──────────────────────┬────────────────────────┘
                              │
                              ▼
       ┌───────────────────────────────────────────────┐
       │ Parse candidates with yaml.safe_load()        │
       │ Filter by:                                    │
       │  - Dict type                                  │
       │  - Top-level key 'ROLE' == expected_role      │
       └──────────────────────┬────────────────────────┘
                              │
                              ▼
       ┌───────────────────────────────────────────────┐
       │ Return (report_dict, raw_yaml_str)            │
       │ If multiple match: take last candidate        │
       │ If none match: return ({}, "")                │
       └───────────────────────────────────────────────┘
```

### Invariant Rules Table

| Invariant | Rule | Violation Behavior |
|---|---|---|
| **Section Boundary** | Text preceding `## Machine Report` is completely shielded from candidate extraction. | Non-protocol code blocks are never fed to PyYAML. |
| **Language Discipline** | Fences with non-YAML identifiers (`typescript`, `json`, `python`, etc.) are ignored. | Zero syntax crashes or false parse errors. |
| **Role Verification** | Extracted candidate must match `ROLE: <expected_role>` at root level. | Example YAMLs and cross-role citations are rejected. |
| **Logging Discipline** | Output uses `logger.debug` / `logger.warning`. Zero raw `print(..., file=sys.stderr)`. | CLI standard error remains completely clean. |

---

## 4. Consequences & Compatibility

1. **Resolution of Issue A:** TypeScript interfaces in Architect reports, JSON schemas in Planner reports, and diff blocks in Executor reports will no longer produce noisy `sys.stderr` YAML parse warnings.
2. **Determinism:** Extraction becomes predictable and resistant to prompt injection or candidate hijacking.
3. **Backwards Compatibility:** Legacy runs or non-conformant model outputs that omit `## Machine Report` but provide ````yaml ROLE: ... ```` continue to parse through the secondary fallback path.
4. **Deterministic Validator Contract:** Downstream validation via [`MachineReportValidator`](file:///home/mathir14/forge/src/forge/protocol/validator.py#L12) remains unchanged and fully enforced as defined in ADR-014.

# Forge Decisions

## D1: CLI-First Architecture
**Decision:** Forge is a CLI tool, not a library or server.
**Rationale:** Simpler deployment, easier integration with existing workflows, no daemon needed.
**Impact:** All state is filesystem-based (.forge/runs/), no persistence layer required.

## D2: Adapter Pattern
**Decision:** Use adapter pattern for CLI tools (opencode, antigravity).
**Rationale:** Allows swapping LLM backends without changing orchestration logic.
**Impact:** New adapters implement `BaseAdapter.execute()`, registered via `AdapterRegistry`.

## D3: Machine Report Protocol
**Decision:** All agent responses must include structured YAML machine report.
**Rationale:** Enables programmatic status tracking and pipeline control.
**Impact:** Protocol parser/validator ensure consistent reporting across all agents.

## D4: Subprocess Execution
**Decision:** Execute CLI agents via `subprocess.run()` with timeout.
**Rationale:** Isolates agent failures, allows timeout enforcement, captures output cleanly.
**Impact:** All adapters use stdin for prompts (no shell injection), 300s timeout default.

## D5: Artifact-Based State
**Decision:** Each run produces .md and .json artifacts in `.forge/runs/{run_id}/`.
**Rationale:** Human-readable markdown for review, machine-readable JSON for parsing.
**Impact:** No database needed, runs are portable, diff-friendly.

## D6: Auto-Approve Default Off
**Decision:** `auto_approve` defaults to `False` everywhere.
**Rationale:** Security - prevents unintended permission bypass in executor.
**Impact:** Must explicitly opt-in via config or CLI flag.

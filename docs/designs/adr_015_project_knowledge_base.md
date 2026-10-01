# ADR-015: Repository Project Knowledge Base (PKB) Architecture

**Status:** Proposed  
**Author:** Forge Architecture & Engineering  
**Date:** 2026-09-25  
**Target Version:** Forge 1.2.0  
**Supersedes/Extends:** Extends ADR-002 (Tester Role), ADR-014 (Machine Protocol Invariants), and Core Storage Architecture  

---

## 1. Context & Incident Analysis (The Rediscovery Problem)

### 1.1 The Failure Mode: Ephemeral Intelligence Across Runs
Across dozens of autonomous execution runs (`run-001` through `run-020`), Forge exhibits a costly operational pathology: **Perpetual Amnesia and Repeated Rediscovery**.

Every run initializes with an empty cognitive state regarding repository structure, module boundaries, verified conventions, and historical bug patterns:
1. **Redundant Repository Scanning:** The pre-run Critic and Architect repeatedly spend 300–600 seconds and tens of thousands of tokens re-analyzing the AST, directory structure, module ownership, and conventions that have not changed since the previous run.
2. **Hallucinatory Inconsistency:** In `run-010`, the Architect correctly recognized that protocol machine reports require uppercase keys (`ROLE`, `STATUS`, `HANDOFF`). In `run-013`, a different model instance assumed lowercase keys (`role`, `status`), causing validation failures that required an auto-repair cycle.
3. **Repeated Rediscovery of Subtle Pitfalls:** In `run-012`, execution failed because `BLOCKED` status was emitted alongside an active handoff. While ADR-014 established the rule in code, other latent project nuances (e.g., mock process group handling under Linux vs POSIX signals, subshell escaping in custom CLI runners) are repeatedly rediscovered and re-debugged across runs.
4. **Behavioral Divergence Between Stages:** In `run-017`, the Architect assumed payments were fully encapsulated in `PaymentService`. During Stage 4, the Tester discovered that `CheckoutService` still performed payment retries directly. Because Forge had no structured medium to record this discrepancy, subsequent runs reverted to the Architect's idealized assumption.

### 1.2 Why "AI Memory" Fails
Existing industry solutions label this problem "AI Memory" and attempt to solve it using vector embeddings, external vector databases (Pinecone, Chroma), or opaque cloud-hosted memory services. In an engineering orchestrator like Forge, these approaches fail on fundamental grounds:
- **Non-Deterministic Retrieval:** Vector search returns probabilistic, top-$k$ fuzzy matches. An agent may retrieve a convention in run $N$ and miss it in run $N+1$ due to slight query phrasing differences.
- **Hidden Out-of-Band State:** Storing knowledge in external databases or user home directories violates **repository hermeticism**. A repository checked out on CI or a teammate's machine does not carry the memory, making runs non-reproducible.
- **Unreviewable & Non-Auditable:** Developers cannot inspect a vector embedding in a pull request. Hallucinations silently contaminate memory without Git history, commit hashes, or blame attribution.
- **Lack of Verification & Grounding:** Vector databases store arbitrary chunks of model outputs without checking whether the referenced files or functions still exist in Git HEAD.

### 1.3 The Core Architectural Principle
> **Project knowledge belongs to the repository, not the model.**  
> It must be deterministic, human-readable, schema-validated, role-governed, version-controlled in Git, grounded in filesystem evidence, and updated through explicit protocol transactions.

---

## 2. Decision: Repository-Local Project Knowledge Base (.forge/knowledge/)

We establish a first-class, repository-local **Project Knowledge Base (PKB)** housed in `.forge/knowledge/`.

The PKB is governed by eight fundamental engineering pillars:
1. **Git-Centric Persistence:** All knowledge is stored as human-readable, deterministic YAML files inside the repository. It is committed, diffed, branched, and reviewed using standard Git workflows.
2. **Strict Schema Validation:** Every knowledge file adheres to an explicit JSON/YAML schema. Free-form, unstructured model musings are rejected at the protocol boundary.
3. **Separation of Authoring vs Verification:** Authoring knowledge (asserting a fact) and verifying knowledge (confirming it against runtime tests or static code) are distinct protocol actions governed by role capabilities.
4. **Staged Proposals (No Direct In-Place Mutation):** Agent roles never write directly to `.forge/knowledge/` during execution. They emit structured `KnowledgeProposal` transactions inside their run artifacts. A deterministic **Reconciliation Engine** merges proposals at run completion.
5. **Empirical Primacy & Dispute Transparency:** Runtime evidence (Tester) takes precedence over theoretical architecture (Architect). When observations contradict specifications, Forge does not silently overwrite; it flags the item as `DISPUTED` and logs an actionable defect in `unresolved.yaml`.
6. **Bounded Confidence Mechanics:** Knowledge entries possess explicit, mathematically bounded confidence scores ($0.0 \le C \le 1.0$) that increase upon independent verification and decay upon code drift or dispute.
7. **Git Drift & Grounding Invariants:** Every knowledge assertion records the SHA-256 content hashes of the repository files it describes. If the underlying code changes, the knowledge is automatically flagged as `STALE` and scheduled for reverification.
8. **Developer Sovereignty (`HUMAN_LOCKED`):** Any knowledge item manually authored or locked by a human developer cannot be overwritten, downgraded, or deleted by any autonomous agent.

---

## 3. Directory Layout & Information Architecture

The knowledge base lives exclusively in `.forge/knowledge/`:

```
.forge/
└── knowledge/
    ├── manifest.yaml       # PKB metadata, schema version, integrity checksums, last run
    ├── architecture.yaml   # System topology, tiering, core data flows, tech stack
    ├── modules.yaml        # Module boundaries, directory ownership, export contracts, dependencies
    ├── features.yaml       # Functional feature inventory, entry points (CLI/HTTP/UI), user journeys
    ├── conventions.yaml    # Code style, error handling, design idioms, test patterns
    ├── decisions.yaml      # Architectural decisions, historical tradeoffs, immutable constraints
    ├── unresolved.yaml     # Known technical debt, recurring defects, knowledge disputes, flaky areas
    └── glossary.yaml       # Domain ubiquitous language, nomenclature taxonomy, anti-patterns
```

### 3.1 Domain Ownership and Content Boundaries

| File | Primary Purpose | Authoritative Role | Verifying Roles |
|---|---|---|---|
| `manifest.yaml` | PKB metadata, schema versioning, drift tracking | Orchestrator (Core) | Orchestrator |
| `architecture.yaml` | System archetype, layer boundaries, external dependencies | **Architect** | Critic, Tester |
| `modules.yaml` | Module boundaries, directory paths, public contracts, invariants | **Architect** | Reviewer, Tester |
| `features.yaml` | Feature catalog, CLI commands, HTTP routes, acceptance criteria | **Architect** (New) & **Critic** (Discovered) | Tester (Behavioral) |
| `conventions.yaml` | Code idioms, error handling conventions, testing rules | **Reviewer** | Critic, Architect |
| `decisions.yaml` | ADR summary, settled design choices, non-negotiable rules | **Architect** | Reviewer |
| `unresolved.yaml` | Bugs, recurring pitfalls, architectural disputes, tech debt | **Tester** & **Critic** | Critic, Reviewer |
| `glossary.yaml` | Ubiquitous domain terminology, canonical naming, alias mappings | **Architect** & **Reviewer** | All roles |

---

## 4. Uniform Entry Envelope Schema

Every entry across all knowledge files (except `manifest.yaml`) shares an identical structural envelope (`KnowledgeEnvelope`). This ensures uniform handling of provenance, verification, drift detection, and developer locks.

```yaml
# Uniform Knowledge Entry Schema
id: "auth-jwt-session-revocation"         # Unique kebab-case slug identifier
title: "Redis-backed JWT Denylist"        # Concise human-readable title
status: VERIFIED                          # PROPOSED | VERIFIED | DISPUTED | STALE | DEPRECATED | HUMAN_LOCKED
confidence: 0.88                          # Bounded float [0.0, 1.0]
verification_count: 4                     # Number of independent runs that verified this entry

provenance:
  originating_run: "run-008"              # First run that proposed this entry
  originating_role: "architect"           # Role that authored the entry
  source: "verified"                      # inferred | observed | verified | human
  created_at: "2026-09-20T14:22:10Z"      # ISO 8601 UTC timestamp
  last_verified_run: "run-019"            # Most recent run confirming this entry
  last_verified_role: "tester"            # Role that performed the latest verification
  last_verified_at: "2026-09-24T09:15:00Z"
  verification_source: "test_suite:tests/test_auth_revocation.py"

grounding:
  files:
    - path: "src/auth/revocation.py"
      sha256: "9f83c6b2e3...1a"           # Hash of file content when verified
    - path: "tests/test_auth_revocation.py"
      sha256: "4b82d1c9f0...3e"

disputes: []                              # Populated if contradictions are observed
tags: ["security", "auth", "redis"]
payload: {}                               # Domain-specific payload schema
```

---

## 5. Domain-Specific Payload Schemas

### 5.1 `architecture.yaml`
Records macro-level system architecture, boundaries, and components.

```yaml
schema_version: "1.0"
system_archetype: "cli_orchestrator"      # cli_orchestrator | monolith | microservices | library | spa
tiering:
  - name: "cli"
    path: "src/forge/cli.py"
    responsibilities: ["Command parsing", "User interaction", "Exit code propagation"]
  - name: "stages"
    path: "src/forge/stages/"
    responsibilities: ["Stage orchestration", "Auto-repair loops"]
  - name: "core"
    path: "src/forge/core/"
    responsibilities: ["Run state", "Git integration", "Process execution"]
  - name: "protocol"
    path: "src/forge/protocol/"
    responsibilities: ["Machine report parsing", "Contract validation"]
  - name: "storage"
    path: "src/forge/storage/"
    responsibilities: ["Run directories", "Lock files", "Artifact persistence"]

external_dependencies:
  - name: "git"
    type: "binary"
    minimum_version: "2.30.0"
    required: true
  - name: "antigravity"
    type: "cli_adapter"
    required: false

data_flow_invariants:
  - "Stages may depend on Core, Protocol, and Storage."
  - "Protocol must never depend on Stages or CLI."
  - "Storage must never import from Stages."
```

### 5.2 `modules.yaml`
Defines boundaries, interface contracts, and dependency rules for subpackages/modules.

```yaml
schema_version: "1.0"
modules:
  - id: "module-forge-protocol"
    title: "Common Agent Protocol Engine"
    status: VERIFIED
    confidence: 0.95
    verification_count: 12
    provenance: { ... }
    grounding:
      files:
        - path: "src/forge/protocol/report.py"
          sha256: "3d5f...12"
        - path: "src/forge/protocol/validator.py"
          sha256: "e78a...89"
    payload:
      root_path: "src/forge/protocol"
      primary_responsibility: "Defines MachineReport dataclass, YAML block extraction, and contract validation."
      public_exports:
        - "MachineReport"
        - "MachineReportValidator"
        - "ProtocolParser"
      allowed_inbound: ["src/forge/stages", "src/forge/cli.py"]
      allowed_outbound: []                # Leaf module: no internal dependencies allowed
      architectural_invariants:
        - "Must remain zero-dependency on external network or storage."
        - "Validator must enforce ADR-014 (BLOCKED <=> HANDOFF: NONE)."
```

### 5.3 `features.yaml`
Tracks functional capabilities of the system and their verification touchpoints.

```yaml
schema_version: "1.0"
features:
  - id: "feature-exclusive-run-locking"
    title: "Exclusive Run Ownership File Locking"
    status: VERIFIED
    confidence: 0.95
    verification_count: 8
    provenance: { ... }
    grounding:
      files:
        - path: "src/forge/storage/run_lock.py"
          sha256: "5a2c...10"
        - path: "tests/test_run_locking.py"
          sha256: "8e9f...77"
    payload:
      description: "Guarantees exactly one live Forge OS process owns a run directory using OS-level fcntl/msvcrt locks."
      entry_points:
        - type: "cli"
          invocation: "forge run <task>"
        - type: "internal_api"
          symbol: "forge.storage.run_lock.RunLock"
      owning_modules: ["module-forge-storage"]
      acceptance_criteria:
        - "Second process attempting lock on same run_id immediately raises RunOwnershipError."
        - "Stale locks from crashed processes (SIGKILL) are recovered deterministically."
      test_suite: "tests/test_run_locking.py"
```

### 5.4 `conventions.yaml`
Defines idiomatic conventions, patterns, and style rules observed and enforced in the repository.

```yaml
schema_version: "1.0"
conventions:
  - id: "conv-python-subprocess-handling"
    title: "Process Group Subprocess Execution and Signal Handling"
    status: VERIFIED
    confidence: 0.90
    verification_count: 6
    provenance: { ... }
    grounding:
      files:
        - path: "src/forge/adapters/base.py"
          sha256: "1b4e...33"
    payload:
      category: "error_handling"          # error_handling | architecture | testing | typing | formatting
      rule: "Always spawn external CLI adapters in their own process group via preexec_fn=os.setsid on POSIX."
      rationale: "Prevents orphaned child processes when Forge times out or handles SIGINT."
      bad_example: "subprocess.Popen(['agy', ...], timeout=300)"
      good_example: "subprocess.Popen(['agy', ...], preexec_fn=os.setsid, timeout=300)"
      enforced_by: "reviewer"
```

### 5.5 `decisions.yaml`
Catalogs settled architectural decisions, non-functional requirements, and tradeoffs.

```yaml
schema_version: "1.0"
decisions:
  - id: "adr-014-status-semantics"
    title: "ADR-014: Machine Protocol Semantics, STATUS Definition, and Orchestration Invariants"
    status: HUMAN_LOCKED                 # Explicitly locked by engineering team
    confidence: 1.0
    verification_count: 20
    provenance: { ... }
    grounding:
      files:
        - path: "docs/designs/adr_014_protocol_status_invariants.md"
          sha256: "d412...01"
    payload:
      adr_number: 14
      date: "2026-09-23"
      status: "APPROVED"
      decision: "STATUS authoritatively answers 'Did this role successfully complete its own responsibility?'. STATUS=BLOCKED is reserved exclusively for external operational blockers and requires HANDOFF=NONE."
      immutable_constraints:
        - "STATUS == BLOCKED <=> HANDOFF == NONE"
        - "STATUS == REJECTED <=> HANDOFF == NONE"
        - "Internal code debt, broken tests, or lint errors are implementation work, never BLOCKED."
```

### 5.6 `unresolved.yaml`
Captures active technical debt, recurring failure modes, and knowledge disputes.

```yaml
schema_version: "1.0"
anomalies:
  - id: "debt-mock-pid-cleanup"
    title: "Process Group Cleanup Fails on Mock PIDs in Unit Tests"
    status: VERIFIED
    confidence: 0.85
    verification_count: 3
    provenance:
      originating_run: "run-018"
      originating_role: "tester"
      created_at: "2026-09-23T08:12:00Z"
      last_verified_run: "run-020"
      last_verified_role: "tester"
      last_verified_at: "2026-09-23T11:45:00Z"
      verification_source: "test_suite:tests/test_adapters_cli.py"
    grounding:
      files:
        - path: "tests/test_adapters_cli.py"
          sha256: "331a...9b"
    payload:
      category: "RECURRING_BUG"           # RECURRING_BUG | TECH_DEBT | KNOWLEDGE_DISPUTE | FLAKY_TEST | SECURITY_CONCERN
      severity: "MEDIUM"                  # CRITICAL | MAJOR | MEDIUM | LOW
      symptoms: "Mock adapters with synthetic PIDs (e.g. 99999) cause ProcessLookupError or PermissionError during atexit or timeout kill handlers."
      reproduction_hint: "Run pytest tests/test_adapters_cli.py with mocks active."
      remediation_strategy: "Check os.killpg inside try/except ProcessLookupError or verify PID existence before killing."
```

### 5.7 `glossary.yaml`
Canonical nomenclature and domain dictionary.

```yaml
schema_version: "1.0"
terms:
  - id: "term-auto-repair-loop"
    title: "Auto-Repair Loop"
    status: VERIFIED
    confidence: 0.95
    verification_count: 15
    provenance: { ... }
    grounding:
      files:
        - path: "src/forge/cli.py"
          sha256: "44ab...02"
    payload:
      term: "Auto-Repair Loop"
      definition: "The autonomous cyclic execution between Executor (repairing code) and Verifiers (Tester/Reviewer evaluating results) up to max_repairs iterations without human intervention."
      synonyms: ["self-healing loop", "retry loop"]
      anti_terms: ["infinite loop", "hallucination loop"]
      context: "Orchestration engine (auto_pipeline)"
```

---

## 6. Role Ownership & Mutation Rights Matrix

To eliminate role trespass and corruption of knowledge, mutation authority is partitioned by role specialization:

```
┌──────────────┬──────────────────┬─────────────────┬──────────────────┬──────────────────┐
│ Role         │ Primary Creation │ Verification    │ Dispute Rights   │ Direct Mutation? │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Architect    │ architecture     │ decisions       │ unresolved       │ NO (Proposal)    │
│              │ modules          │ glossary        │                  │                  │
│              │ features (New)   │                 │                  │                  │
│              │ decisions        │                 │                  │                  │
│              │ glossary         │                 │                  │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Planner      │ NONE             │ NONE            │ NONE             │ NO (Read-Only)   │
│              │ (Decomposition)  │                 │                  │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Executor     │ NONE             │ NONE            │ NONE             │ NO (Forbidden)   │
│              │ (Execution only) │                 │                  │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Tester       │ unresolved       │ features        │ architecture     │ NO (Proposal)    │
│              │ (Behavior bugs)  │ modules         │ modules          │                  │
│              │                  │                 │ conventions      │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Reviewer     │ conventions      │ modules         │ architecture     │ NO (Proposal)    │
│              │ glossary         │ decisions       │ features         │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Critic       │ features (Disc.) │ architecture    │ all files        │ NO (Proposal)    │
│              │ unresolved       │ conventions     │                  │                  │
│              │ (Debt/Anomalies) │ modules         │                  │                  │
├──────────────┼──────────────────┼─────────────────┼──────────────────┼──────────────────┤
│ Human Dev    │ ALL FILES        │ ALL FILES       │ ALL FILES        │ YES (Absolute)   │
└──────────────┴──────────────────┴─────────────────┴──────────────────┴──────────────────┘
```

### 6.1 The Executor Non-Mutation Invariant
> **Invariant PKB-01 (Executor Prohibition):**  
> **The Executor is strictly prohibited from proposing or modifying knowledge base entries.**

*Rationale:* The Executor is the code producer under test. Allowing the Executor to modify knowledge creates an irreconcilable conflict of interest: an Executor could bypass architectural constraints or test failures simply by redefining the module boundary or marking a failing requirement as deprecated in `features.yaml`. The Executor produces code diffs and execution logs; verifiers and architects evaluate and record knowledge.

### 6.2 The Planner Read-Only Invariant
> **Invariant PKB-02 (Planner Read-Only):**  
> **The Planner consumes knowledge to decompose tasks but is strictly prohibited from proposing or modifying knowledge base entries.**

*Rationale:* The Planner decomposes work into executable steps; it is not a discovery or verification engine. Conflating task planning with knowledge authorship creates ungrounded, speculative entries before code is written or tested. User-facing capabilities are proposed by the Architect during system design, discovered by the Critic during repository audits, and verified by the Tester through behavioral execution. Planner remains purely read-only with respect to the PKB.

---

## 7. Update Protocol & Transaction Lifecycle

No agent role writes directly to `.forge/knowledge/` during stage execution. Updates flow through an atomic, multi-stage transaction lifecycle:

```
┌────────────────────────────────────────────────────────────────────────┐
│ STAGE EXECUTION                                                        │
│ Agent Role executes task -> Emits Machine Report with:                 │
│ KNOWLEDGE_PROPOSALS: [ Proposal_1, Proposal_2, ... ]                   │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ STAGING IN RUN DIRECTORY                                               │
│ Orchestrator saves:                                                    │
│ .forge/runs/<run_id>/knowledge_proposals/{seq}_{role}_proposals.yaml   │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ RUN COMPLETION / CLOSING RECONCILIATION                                │
│ Stage 6 (Closing Critic) & Forge Reconciler Engine:                    │
│ 1. Schema Validation of all proposals                                  │
│ 2. Role Ownership & Invariant Verification                             │
│ 3. Grounding Verification (Git HEAD hash check)                        │
│ 4. Conflict & Dispute Resolution Algorithm                             │
│ 5. Confidence Score Recalibration                                      │
│ 6. Atomic Write to .forge/knowledge/ under Run Lock                    │
│ 7. Emit Run Artifact: .forge/runs/<run_id>/knowledge_diff.json         │
└────────────────────────────────────────────────────────────────────────┘
```

### 7.1 Proposal Action Primitives
Agents emit one of four explicit action primitives in their proposals:
1. `ASSERT`: Propose a brand-new knowledge entry (requires owning role authority).
2. `VERIFY`: Confirm that an existing entry accurately reflects code/runtime behavior (bumps confidence, updates last verified timestamp).
3. `DISPUTE`: Report that an existing entry is contradicted by observable reality (penalizes confidence, logs dispute, generates `unresolved.yaml` entry).
4. `DEPRECATE`: Propose that an existing entry has been superseded or removed from the codebase.

---

## 8. Conflict Resolution & Dispute Semantics

### 8.1 The Core Dilemma: Specification vs Reality
A classic failure mode in autonomous coding occurs when the Architect specifies:
> *"Payments are completely encapsulated by `PaymentService`."*

While during Stage 4, the Tester observes:
> *"Runtime test fails because `CheckoutService` still executes direct payment retries."*

### 8.2 The Forge Resolution Algorithm
Forge rejects the naive solutions:
- ❌ *Naive A:* Silently overwrite the Architect with the Tester's observation (erases architectural intent).
- ❌ *Naive B:* Discard the Tester's observation because Architect outranks Tester (ignores broken reality).

**The Reconciler executes the following deterministic reconciliation:**
1. **Preserve Specification with Disputed State:** The entry in `modules.yaml` is retained, but its status transitions from `VERIFIED` to `DISPUTED`.
2. **Apply Confidence Penalty:** The confidence score of the asserted entry is downgraded by $0.35$ (e.g., $0.85 \to 0.50$).
3. **Attach Dispute Record:** An audit entry is appended directly to the `disputes` list of the target entry:
   ```yaml
   disputes:
     - run_id: "run-021"
       role: "tester"
       timestamp: "2026-09-25T09:30:00Z"
       claim: "CheckoutService performs payment retries directly at checkout.py:142."
       evidence_file: "tests/test_checkout_retries.py"
   ```
4. **Synthesize Actionable Defect in `unresolved.yaml`:** The Reconciler automatically generates a new entry in `unresolved.yaml`:
   ```yaml
   id: "dispute-payment-checkout-retries"
   title: "Architectural Leak: CheckoutService Performs Direct Payment Retries"
   status: DISPUTED
   confidence: 0.90
   payload:
     category: "KNOWLEDGE_DISPUTE"
     severity: "MAJOR"
     conflicting_entries: ["module-payment-service", "module-checkout-service"]
     symptoms: "CheckoutService bypasses PaymentService boundary during gateway timeout."
     remediation: "Refactor payment retry logic from CheckoutService into PaymentService."
   ```
5. **Contextual Warning in Downstream Runs:** Future runs targeting either module automatically receive this dispute prominently in their compiled prompt.

---

## 9. Confidence Semantics & Recalibration Mathematics

Confidence is a continuous float $C \in [0.0, 1.0]$ governed by bounded asymptotic recalibration:

```
[0.00 ----------- 0.39]   Low / Provisional     (Single LLM assertion, unverified)
[0.40 ----------- 0.79]   Moderate / Grounded   (Multi-stage verified, grounded in code)
[0.80 ----------- 0.99]   Hardened / Proven     (Passes automated tests & static audits)
[        1.00         ]   Human-Locked          (Developer verified; inviolable by AI)
```

### 9.1 Bounded Update Formulae
Let $C_t$ be the current confidence at run $t$.

1. **Initial Assertion (`ASSERT`):**
   $$C_0 = \begin{cases} 0.50 & \text{if grounded in valid file paths} \\ 0.30 & \text{if conceptual / ungrounded} \end{cases}$$

2. **Empirical Verification (`VERIFY` by Tester via executable tests):**
   $$C_{t+1} = C_t + (0.95 - C_t) \times 0.30$$
   *(Asymptotically approaches 0.95 ceiling for autonomous verification)*

3. **Static Verification (`VERIFY` by Reviewer/Critic via AST / Git diff):**
   $$C_{t+1} = C_t + (0.95 - C_t) \times 0.15$$

4. **Contradiction / Dispute (`DISPUTE` by Tester/Critic):**
   $$C_{t+1} = \max(0.10, C_t - 0.35)$$

5. **Code Drift Penalty (Underlying code modified without reverification):**
   $$C_{t+1} = \max(0.20, C_t \times 0.75)$$

6. **Human Lock (`HUMAN_LOCKED`):**
   $$C = 1.00 \quad (\text{Strictly immutable by autonomous updates})$$

---

## 10. Knowledge Drift Detection & Lifecycle State Machine

A primary hazard in software intelligence is **knowledge decay**: code is refactored, functions are moved, or files are deleted, leaving knowledge as misleading baggage.

### 10.1 Grounding Hash Verification
Every entry records the SHA-256 hashes of the files it describes. At the start of every run (during pre-run Critic or prompt preparation), the orchestrator checks:

```
For each file in entry.grounding.files:
  current_hash = sha256(file)
  if current_hash != recorded_hash:
      flag_drift(entry, file)
  if not file.exists():
      flag_orphaned(entry, file)
```

### 10.2 Entry Lifecycle State Machine

```
               ┌───────────────┐
               │   PROPOSED    │ (Initial model assertion)
               └───────┬───────┘
                       │
                       │ Schema validation & ownership verified
                       ▼
               ┌───────────────┐
         ┌────►│   VERIFIED    │◄────────────────────────┐
         │     └───────┬───────┘                         │
         │             │                                 │
         │             ├──[ Grounded file modified ]────┐│
         │             │                                ││
         │             ├──[ Contradiction observed ]───┐││ Reverified
         │             │                               │││
         │             ▼                               ▼▼│
         │     ┌───────────────┐               ┌─────────┴─────┐
         │     │   DISPUTED    │               │ STALE / DRIFT │
         │     └───────┬───────┘               └───────┬───────┘
         │             │                               │
         │             └──[ Resolved by Fix ]──────────┘
         │
         │     ┌───────────────┐
         └─────┤ HUMAN_LOCKED  │ (Developer authored or locked; C=1.0)
               └───────────────┘
                       │
                       │ [ Grounded files deleted or feature removed ]
                       ▼
               ┌───────────────┐
               │  DEPRECATED   │ ──► [ Archived to .forge/knowledge/archive/ ]
               └───────────────┘
```

- **`STALE`:** The code changed. The entry remains available for context, but with an explicit warning: `"[STALE: Grounded code modified in commit ab12cd; verify before relying]"`.
- **`DEPRECATED`:** The target files no longer exist. The entry is moved out of active prompts and stored in `.forge/knowledge/archive/` for historical reference.

---

## 11. Consumption Model: Role-Aware Context Filtering & Token Budgeting

Injecting all 7 knowledge files into every prompt would quickly exhaust LLM context windows and trigger severe prompt truncation under Forge's `apply_transport_budget` algorithm.

Forge enforces **Role-Aware Context Projections**: each role receives a curated, targeted slice of the knowledge base.

```
┌──────────────┬───────────────────────────────┬───────────────────────────────────────────┐
│ Stage / Role │ Projected Knowledge Files     │ Filtering & Focus Scope                   │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Critic (Pre) │ architecture.yaml, modules,   │ Systemic audit, conventions compliance,   │
│              │ conventions, unresolved       │ drift detection across all modules        │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Architect    │ architecture.yaml, modules,   │ Structural topology, interface contracts, │
│              │ decisions.yaml, unresolved    │ open disputes, ADR constraints            │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Planner      │ features.yaml, modules.yaml,  │ Existing feature inventory, target module │
│              │ unresolved.yaml               │ interfaces, past bug reproduction hints   │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Executor     │ Target module only,           │ Scoped strictly to files/modules touched; │
│              │ relevant conventions,         │ target conventions (error handling, etc.) │
│              │ relevant glossary terms       │                                           │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Tester       │ features.yaml, target module, │ Acceptance criteria, expected journeys,   │
│              │ unresolved.yaml (known bugs)  │ known bug reproduction procedures         │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Reviewer     │ conventions.yaml,             │ Engineering standards, ADR compliance,    │
│              │ decisions.yaml, target module │ module boundary violations in diff        │
├──────────────┼───────────────────────────────┼───────────────────────────────────────────┤
│ Critic (Post)│ Full summary diff,            │ Verifies whether run introduced new debt  │
│              │ unresolved.yaml, manifest     │ or resolved existing disputes             │
└──────────────┴───────────────────────────────┴───────────────────────────────────────────┘
```

### 11.1 Dynamic Relevance Projection (Token Budgeting)
For large repositories with hundreds of module entries, `InstructionBuilder` applies deterministic relevance filtering:
1. **Target Module Extraction:** Inspect the user task, touched Git files, and prior stage outputs to determine active modules.
2. **First-Degree Dependency Ingestion:** Include full detail for active modules and their immediate dependencies.
3. **Compact Catalog Indexing:** Summarize distant modules as one-line index entries (`id`, `title`, `path`).
4. **Transport Budget Enforcement:** The total injected knowledge section is bounded by `MAX_KNOWLEDGE_CHARS = 30000`. If exceeded, low-confidence entries ($C < 0.60$) and resolved historical entries are omitted first, with explicit truncation notices.

---

## 12. Human Interaction & Developer Sovereignty

Forge treats the software developer as the supreme authority over project knowledge:

### 12.1 Manual Editing & Git Workflows
- Developers can edit any `.forge/knowledge/*.yaml` file directly in VSCode, Vim, or any editor.
- Since files are standard YAML, edits are diffed, staged, and committed via Git (`git add .forge/knowledge/`).
- When a human edits an entry, they can add `status: HUMAN_LOCKED`. The Reconciler treats `HUMAN_LOCKED` as an inviolable constraint.

### 12.2 Human Review of Run Knowledge Updates
In standard interactive mode (`forge run` with `auto_commit: false`):
1. At the end of the run, Forge displays a colorized summary of proposed knowledge changes:
   ```text
   ┌───────────────────────────────────────────────────────────────┐
   │ Project Knowledge Base Updates (run-021)                      │
   ├───────────────────────────────────────────────────────────────┤
   │ + [NEW]     module-auth-rate-limiter (confidence: 0.50)       │
   │ ✓ [VERIFIED] feature-exclusive-run-locking (confidence: 0.95) │
   │ ⚠️ [DISPUTED] module-payment-service (confidence: 0.50)        │
   │ ! [NEW DEBT] dispute-payment-checkout-retries (severity: MAJ) │
   └───────────────────────────────────────────────────────────────┘
   Apply knowledge updates to repository? [Y/n/inspect/edit]:
   ```
2. If the user selects `inspect`, Forge renders the exact YAML diff.
3. If the user selects `n` (reject), the run's proposals are discarded and `.forge/knowledge/` remains untouched.

### 12.3 CLI Management Suite
Forge exposes first-class CLI commands for manual knowledge curation:
- `forge knowledge list [--category <cat>] [--status <status>]`
- `forge knowledge show <id>`
- `forge knowledge lock <id>` *(sets `status: HUMAN_LOCKED`)*
- `forge knowledge unlock <id>`
- `forge knowledge verify <id>` *(records manual developer verification)*
- `forge knowledge audit` *(runs drift detection and reports stale entries)*
- `forge knowledge prune [--stale] [--dry-run]`

---

## 13. Invariants & Protocol Rules

| Invariant ID | Rule Statement | Violation Behavior |
|---|---|---|
| **INV-PKB-01** | `Executor` must never emit `KNOWLEDGE_PROPOSALS`. | Fatal Validation Error: Machine report marked `is_valid: False`. |
| **INV-PKB-02** | `Planner` must never emit `KNOWLEDGE_PROPOSALS` (Read-Only Consumer). | Fatal Validation Error: Machine report marked `is_valid: False`. |
| **INV-PKB-03** | Staged proposals must never mutate `.forge/knowledge/` in-place during run execution. | Build Invariant: All mutations deferred to atomic closing reconciliation. |
| **INV-PKB-04** | `status == HUMAN_LOCKED` entries cannot be modified, downgraded, or deleted by any autonomous agent. If contradicted by runtime tests, the locked entry remains unchanged, and an anomaly is logged in `unresolved.yaml` as `SPECIFICATION_VIOLATION`. | Reconciler rejects agent proposals targeting `HUMAN_LOCKED` entries with diagnostic notice in `knowledge_diff.json`. |
| **INV-PKB-05** | Every asserted entry must specify at least one existing repository path in `grounding.files` or be categorized as `CONCEPTUAL` ($C \le 0.40$). | Reconciler downgrades or rejects ungrounded assertions. |
| **INV-PKB-06** | A contradiction observed by `Tester` against `Architect` or `Reviewer` must yield `status: DISPUTED` and an active anomaly in `unresolved.yaml`. | Systemic Invariant: Silent overwrites are strictly disallowed. |
| **INV-PKB-07** | Confidence score $C$ must remain strictly bounded: $0.0 \le C \le 1.0$. Autonomous agents cannot bump confidence beyond $0.95$. | Reconciler clamps confidence values: $C = \min(0.95, \max(0.10, C))$. |
| **INV-PKB-08** | **Feature Boundary Invariant:** Entries in `features.yaml` must define observable user or consumer capabilities with external entry points (CLI, HTTP, UI, public API). Internal code components (classes, services, repositories) belong strictly in `architecture.yaml`. | Reconciler flags schema violation if architectural symbols are asserted as features. |
| **INV-PKB-09** | Deleting a grounded file in Git transitions dependent knowledge entries to `DEPRECATED` or `STALE`. | Audit engine flags or archives orphaned entries upon drift scan. |

---

## 14. Consequences & Benefits

### 14.1 Positive Consequences
1. **Dramatic Token & Time Savings:** Subsequent Forge runs no longer need to spend 15 minutes rediscoveries. Architect and Planner start with verified module boundaries and conventions.
2. **Deterministic Context Quality:** Prompts contain proven project facts, verified route definitions, and concrete pitfalls rather than speculative LLM assumptions.
3. **Transparent Architectural Evolution:** The entire cognitive history of the repository is auditable via standard `git log .forge/knowledge/`.
4. **Hermetic Portability:** A repository cloned on a new machine or running in CI retains all verified knowledge immediately without external database dependencies.
5. **Human Alignment:** Developers inspect, correct, and lock knowledge entries using standard code review pull requests.

### 14.2 Negative Consequences & Mitigations
1. **File Maintenance Overhead:** An additional directory `.forge/knowledge/` must be maintained in Git.
   - *Mitigation:* The system works completely out-of-the-box autonomously. Developers only interact when they wish to inspect or override.
2. **Risk of Stale Knowledge:** If developers refactor code outside Forge, knowledge can drift.
   - *Mitigation:* Built-in Git SHA-256 drift detection automatically flags stale entries and forces reverification before use.

---

## 15. Future Extension Points

1. **Incremental AST Graph Export:** Future iterations can compile `modules.yaml` directly from Tree-sitter or LSP symbol tables to establish automated structural baselines.
2. **Cross-Run Anomaly Regression Testing:** Tester can consume `unresolved.yaml` to automatically synthesize regression tests targeting recurring bug areas.
3. **CI Knowledge Verification Gate:** A GitHub Action (`forge knowledge audit --ci`) that blocks pull requests if architectural invariants or conventions defined in the PKB are violated in code.

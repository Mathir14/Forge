# PKB Implementation Roadmap: Pragmatic v1 to Long-Term Vision

**Document Version:** 1.0.0  
**Status:** Approved Architecture Plan  
**Author:** Forge Architecture & Engineering  
**Date:** 2026-09-25  
**Target Milestone:** Forge 1.1.0 (PKB v1) → Forge 1.2.0 (PKB v2) → Forge 2.0.0 (PKB v3)  
**Related Documents:** [ADR-015: Repository Project Knowledge Base](file:///home/mathir14/forge/docs/designs/adr_015_project_knowledge_base.md), [ADR-014: Machine Protocol Semantics](file:///home/mathir14/forge/docs/designs/adr_014_protocol_status_invariants.md), [ADR-002: Tester Role](file:///home/mathir14/forge/docs/designs/adr_tester_role.md)

---

## 1. Executive Summary & Guiding Philosophy

ADR-015 defines the authoritative, long-term architectural destination for Forge's Project Knowledge Base (PKB). It establishes that project knowledge belongs to the repository, not the model, and must remain deterministic, human-readable, schema-validated, and reviewable in Git.

However, in accordance with Forge’s evidence-driven engineering philosophy:
> **Forge advances by shipping one verified capability at a time, testing it against real-world runs, and expanding it only when empirical evidence demands it.**

Attempting to build the complete ADR-015 vision in a single release introduces unnecessary engineering overhead, premature optimization (such as continuous confidence floats and SHA-256 drift trees), and maintenance burden for capabilities whose value has not yet been demonstrated in production.

This document derives **PKB v1**: the smallest, highest-ROI implementation that immediately stops the rediscovery tax across Forge runs while establishing a clean migration path to PKB v2 and v3.

---

## 2. Core Architectural Pivot: The Fact-Centric Knowledge Model

A central insight of this roadmap is a fundamental simplification of the internal knowledge model:

> **The atomic unit of knowledge is not a YAML file. It is a FACT.**  
> Files (`architecture.yaml`, `features.yaml`, etc.) are merely physical projections that organize facts for human inspection and Git diffing.

### 2.1 Why Fact-Centricity Radically Simplifies v1

In a document-centric design, the engine requires separate parser classes, validation logic, reconciliation rules, and schema definitions for each individual file.

In a **Fact-Centric** model:
- Forge implements **exactly ONE data structure** (`KnowledgeFact`).
- Forge implements **exactly ONE reconciliation loop** and **ONE proposal validator**.
- Facts are categorized by `type`, stored in memory uniformly, and serialized into their respective domain files on disk.

```yaml
# The Uniform Fact Schema (v1)
id: "auth-rate-limiter"
type: "architecture"                     # architecture | feature | decision | unresolved
title: "Per-IP Sign-In Rate Limiting"
status: VERIFIED                         # PROVISIONAL | VERIFIED | DISPUTED | HUMAN_LOCKED
summary: "Enforces 5 sign-in attempts per minute per IP using Redis token bucket."
evidence:
  - "src/auth/limiter.py"
  - "tests/test_auth_limiter.py"
provenance:
  originating_run: "run-021"
  originating_role: "architect"
  source: "verified"                      # inferred | observed | verified | human
  last_verified_run: "run-023"
  last_verified_role: "tester"
payload:
  exports: ["check_rate_limit", "RateLimitExceeded"]
  dependencies: ["src/storage/redis.py"]
```

---

## 3. Scope Evaluation: What We Keep vs. What We Defer

Every capability in ADR-015 has been challenged against the standard:  
*"Does this solve a critical failure mode observed in Forge runs today?"*

```
┌──────────────────────────────────────┬──────────┬────────────────────────────────────────────────────────┐
│ Capability                           │ Status   │ Architectural Rationale & Target Phase                 │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ Staged Proposal System               │ KEEP v1  │ Critical: Prevents in-place race conditions & clobber. │
│ Closing Reconciler Engine            │ KEEP v1  │ Critical: Single point of atomic truth at run finish.  │
│ Strict Role Ownership Boundaries    │ KEEP v1  │ Critical: Enforces INV-PKB-01 (Executor non-mutation). │
│ Human Editing & HUMAN_LOCKED status  │ KEEP v1  │ Critical: Preserves developer sovereignty.             │
│ Dispute State Transition             │ KEEP v1  │ Critical: Resolves Architect vs. Tester contradictions.│
│ Role-Aware Prompt Filtering          │ KEEP v1  │ Critical: Protects context window & transport budget.  │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ Continuous Confidence Mathematics    │ DEFERRED │ Deferred to v2/v3: No orchestration decision uses      │
│ (Asymptotic float formulas)          │          │ float weights today. Discrete statuses suffice.        │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ SHA-256 Grounding Hashes             │ DEFERRED │ Deferred to v2: Git already provides content hashes.   │
│                                      │          │ Storing hashes in YAML causes severe diff churn.       │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ manifest.yaml & Integrity Checksums  │ DEFERRED │ Deferred to v3: Redundant on a single local Git repo.  │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ glossary.yaml                        │ DEFERRED │ Deferred to v2: Domain naming confusion is rare today; │
│                                      │          │ high authoring friction for marginal ROI.              │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ Separate modules.yaml / conventions  │ DEFERRED │ Deferred to v2: Folded into architecture.yaml and      │
│                                      │          │ decisions.yaml for v1 to keep file count to 4.         │
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ Archival Subsystem (archive/)        │ DEFERRED │ Deferred to v2: Early repositories do not generate     │
│                                      │          │ enough deprecated facts to warrant archival cold-store.│
├──────────────────────────────────────┼──────────┼────────────────────────────────────────────────────────┤
│ Automated AST Drift Engine           │ DEFERRED │ Deferred to v2: Pre-run Critic already observes git    │
│                                      │          │ diffs. Dedicated AST parser is unnecessary in v1.      │
└──────────────────────────────────────┴──────────┴────────────────────────────────────────────────────────┘
```

---

## 4. PKB v1 Specification (The Pragmatic Baseline)

### 4.1 Target Directory Layout
In v1, `.forge/knowledge/` contains exactly **four files**:

```
.forge/
└── knowledge/
    ├── architecture.yaml   # Subsystems, modules, component boundaries, and invariants
    ├── features.yaml       # User-facing capabilities, CLI flags, routes, acceptance criteria
    ├── decisions.yaml      # ADR index, settled engineering patterns, conventions
    └── unresolved.yaml     # Known technical debt, recurring bugs, active disputes
```

### 4.2 Status Model: 4 Discrete States
Continuous floats ($0.0 \le C \le 1.0$) are eliminated in v1. No orchestration rule today branches on numeric probabilities.

v1 establishes a clean, four-state discrete status machine:
- `PROVISIONAL`: Proposed by an agent role during execution, grounded in file paths, but unconfirmed by automated tests or peer verification.
- `VERIFIED`: Confirmed by passing runtime tests (Tester) or static verification (Reviewer/Critic).
- `DISPUTED`: Contradicted by empirical observation (e.g. Tester observes behavior contradicting Architect).
- `HUMAN_LOCKED`: Manually authored, verified, or locked by a developer. Immutable to all autonomous agents.

```
                    ┌───────────────┐
                    │  PROVISIONAL  │ (Initial proposal by Architect/Planner)
                    └───────┬───────┘
                            │
               Tester confirms via tests
                            ▼
                    ┌───────────────┐
        ┌──────────►│   VERIFIED    │◄─────────────────────────┐
        │           └───────┬───────┘                          │
        │                   │                                  │
        │      Tester observes contradiction                   │ Disputed issue
        │                   ▼                                  │ resolved in code
        │           ┌───────────────┐                          │
        │           │   DISPUTED    │──────────────────────────┘
        │           └───────────────┘
        │
        │           ┌───────────────┐
        └───────────┤ HUMAN_LOCKED  │ (Developer authored or locked; C=1.0)
                    └───────────────┘
```

### 4.3 Role Ownership Contract (v1)
- **`Architect`**: Owns `architecture.yaml`, `decisions.yaml`, and proposes new user capabilities in `features.yaml`.
- **`Critic`**: Discovers existing undocumented features in `features.yaml` and technical debt in `unresolved.yaml`.
- **`Planner`**: Pure read-only consumer for task decomposition; **STRICTLY PROHIBITED** from proposing or modifying knowledge (INV-PKB-02).
- **`Executor`**: Pure code producer; **STRICTLY PROHIBITED** from proposing or modifying knowledge (INV-PKB-01).
- **`Tester`**: Owns behavioral defects in `unresolved.yaml`. Verifies `features.yaml`. Disputes `architecture.yaml`.
- **`Reviewer`**: Owns conventions inside `decisions.yaml`. Verifies `architecture.yaml`.

### 4.4 Feature Boundary & HUMAN_LOCKED Invariants
1. **INV-PKB-08 (Feature Boundary):**  
   A "Feature" represents an observable user or consumer capability (e.g. `User Login`, `Checkout`, `Export CSV`). Internal architectural components, services, or models (`AuthService`, `PaymentRepository`) belong strictly in `architecture.yaml`.
2. **INV-PKB-04 (HUMAN_LOCKED Sovereignty):**  
   Autonomous agents cannot overwrite, modify, downgrade, or delete `HUMAN_LOCKED` facts. If runtime behavior contradicts a locked fact, the locked fact remains unchanged and an anomaly is synthesized in `unresolved.yaml` as `SPECIFICATION_VIOLATION`. Developers unlock or edit facts directly via Git or `forge knowledge unlock <id>`.

### 4.5 The Machine Protocol Block (v1)
Agents emit knowledge proposals in standard YAML blocks within their Machine Report:

```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
KNOWLEDGE_PROPOSALS:
  - action: ASSERT                         # ASSERT | VERIFY | DISPUTE | DEPRECATE
    id: "module-auth-guard"
    type: "architecture"                   # architecture | feature | decision | unresolved
    title: "Authentication Route Guard"
    summary: "Guards all /api/v1/* routes via Bearer token verification."
    evidence: ["src/auth/guard.py"]
    payload:
      exports: ["require_auth"]
```

---

## 5. Phased Roadmap: v1 → v2 → v3

### Milestone 1: PKB v1 — Minimal Viable Intelligence (Forge 1.1.0)
**Core Value:** Immediate reduction in rediscovery tax for architecture, features, and historical bugs. Zero external dependencies.

#### Features Included:
1. **Fact-Centric Engine:** Unified `KnowledgeFact` internal model.
2. **4 Canonical Projections:** `architecture.yaml`, `features.yaml`, `decisions.yaml`, `unresolved.yaml`.
3. **4-State Discrete Status Model:** `PROVISIONAL`, `VERIFIED`, `DISPUTED`, `HUMAN_LOCKED`.
4. **Staged Proposals & Reconciler:** Stages output proposals to `.forge/runs/<run_id>/knowledge_proposals/`; reconciled atomically at run finish.
5. **Conflict & Dispute Engine:** Automated transition to `DISPUTED` and automatic synthesis of anomaly in `unresolved.yaml`.
6. **Role-Aware Context Projections:** Curated facts injected into prompts via `MAX_KNOWLEDGE_CHARS = 20000` cap.
7. **Basic Developer CLI:** `forge knowledge list` and `forge knowledge show <id>`.

---

### Milestone 2: PKB v2 — Verification Hardening & Operational Scale (Forge 1.2.0)
**Core Value:** Prevents knowledge staleness as codebases evolve over months of production development.

#### Justification & Trigger Evidence Needed:
- **Trigger Evidence 1:** Repositories accumulate > 50 features and > 30 architectural components, making a combined `architecture.yaml` unwieldy.
- **Trigger Evidence 2:** Developers refactor code outside Forge, causing agents to rely on stale module paths.

#### Features Added:
1. **Automated Git Drift Detection:** Pre-run Critic cross-references fact `evidence` paths against `git diff` since `last_verified_run`. Facts with modified files transition to `STALE`.
2. **Dedicated `modules.yaml` & `conventions.yaml` Partitioning:** Extracted cleanly from `architecture.yaml` and `decisions.yaml` without breaking backward compatibility.
3. **Verification Counters:** Track `verification_count` across runs to measure knowledge stability.
4. **Archival Subsystem (`archive/`):** Cold-storage directory for facts marked `DEPRECATED`.
5. **Interactive Reconciler CLI (`forge knowledge audit`, `forge knowledge prune`).**

---

### Milestone 3: PKB v3 — Autonomous Optimization & Enterprise Governance (Forge 2.0.0)
**Core Value:** High-concurrency enterprise pipelines, CI governance gates, and autonomous test synthesis.

#### Justification & Trigger Evidence Needed:
- **Trigger Evidence 1:** Multi-agent concurrent branches merge knowledge, requiring automated 3-way semantic conflict resolution.
- **Trigger Evidence 2:** Teams require automated compliance checks in CI to block pull requests violating architectural invariants.

#### Features Added:
1. **Continuous Confidence Mathematics:** Introduce asymptotic float scoring ($C \in [0.0, 1.0]$) if automated agent decision-making requires probabilistic filtering.
2. **SHA-256 Grounding Trees & `manifest.yaml`:** Content-addressed integrity tracking for distributed multi-repo caching.
3. **Automated Tree-sitter AST Extraction:** Bootstrap `architecture.yaml` directly from source code AST without requiring model inference.
4. **Defect-Driven Regression Test Synthesis:** Tester consumes `unresolved.yaml` to automatically write executable test cases guarding against historical bugs.
5. **CI Architectural Gate (`forge knowledge audit --ci`):** GitHub Actions runner blocking pull requests that violate conventions or architectural contracts.

---

## 6. Migration Path & Backward Compatibility

Because the internal model is **Fact-Centric**, migration from v1 to v2 and v3 requires **zero data loss and zero breaking changes**:

1. **v1 to v2 Migration (File Splitting):**
   - In v1, conventions live in `decisions.yaml` with `type: decision, category: convention`.
   - In v2, the serializer simply filters facts where `category == convention` into `conventions.yaml`. Existing v1 files continue to parse seamlessly.
2. **v1 to v3 Migration (Confidence Upgrade):**
   - In v1, statuses are discrete (`PROVISIONAL`, `VERIFIED`, `HUMAN_LOCKED`).
   - In v3, when numeric confidence is introduced, existing statuses map deterministically to initial float baselines:
     $$\text{PROVISIONAL} \to 0.50, \quad \text{VERIFIED} \to 0.85, \quad \text{DISPUTED} \to 0.40, \quad \text{HUMAN_LOCKED} \to 1.00$$
3. **Git Cleanliness:**
   All versions remain 100% human-readable YAML committed directly to Git.

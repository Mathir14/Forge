# ADR-002: Architecture Decision Record — Tester Role & Behavioral Verification Pipeline

**Status:** Approved (Pending Implementation)  
**Author:** Forge Architecture & Engineering  
**Date:** 2026-09-23  
**Target Version:** Forge 1.1.0  
**Supersedes/Extends:** Extends Pipeline Verification Architecture  

---

## 1. Context & Problem Statement

Autonomous multi-agent pipelines commonly suffer from a deceptive failure mode: **"Works on Paper, Broken in Practice"**.

In real-world software engineering runs within Forge, the pipeline has consistently demonstrated that an implementation can pass through the current **Reviewer** stage with flying colors—satisfying architectural contracts, presenting immaculate Git diffs, adhering to modular patterns, and passing unit test suites—while delivering an entirely non-functional, visually deformed, or unusable product to human end users.

### 1.1 The Behavioral Quality Gap
Specific real-world failure patterns observed in autonomous execution include:
1. **Dead UI & Unlinked Handlers:** Buttons and navigation links that render cleanly in React/HTML/Vue components but are wired to `console.log("TODO")`, stub functions, or missing state reducers.
2. **Runtime Crashes on Interaction:** Form submissions that throw uncaught promise rejections or DOM access exceptions (`TypeError: Cannot read properties of undefined`) as soon as a user clicks "Submit".
3. **Broken Layouts & Visual Clipping:** Critical UI elements overlapping, text clipped behind sticky headers, buttons rendered off-screen on standard viewports, or unreadable contrast in dark mode.
4. **Missing Visual States:** Lack of loading spinners, disabled button states during async requests (allowing duplicate submissions), or blank screens when API queries return empty arrays.
5. **Incomplete Workflows:** A user can view a list of items, but clicking "Edit" or "Delete" renders a blank modal or redirects to a 404 route.

### 1.2 The Limits of the Reviewer Role
The current Forge **Reviewer** is fundamentally an **engineering and code-level auditor**:
- **Perspective:** White-box static analysis and diff verification.
- **Primary Source of Truth:** Git diffs, AST structures, code formatting, security patterns, and static unit test files.
- **Guiding Question:** *"Is this code architecturally sound, maintainable, secure, and properly tested according to engineering standards?"*

The Reviewer is neither equipped nor prompted to interact with the software as an end user. Reviewing code by reading diffs cannot substitute for launching the application, executing user journeys, and verifying that the rendered software actually behaves correctly.

### 1.3 Separation of Responsibilities

To achieve end-to-end quality assurance, Forge establishes a clear division across three distinct evaluative roles:

| Dimension | Tester (New) | Reviewer (Existing) | Critic (Existing) |
|---|---|---|---|
| **Role Classification** | `verifier` (Behavioral / UX) | `verifier` (Implementation / Code) | `audit` (Systemic Health) |
| **Perspective** | **Black-Box / User-Centric** | **White-Box / Engineer-Centric** | **Macro / Architect-Centric** |
| **Primary Artifact** | Executable app, DOM, UI, API behavior | Git diff, repository code, unit tests, **Tester report** | Whole codebase, dependencies, tech debt |
| **Primary Question** | *"Does this software work and feel right to a human user?"* | *"Is the code correctly, cleanly, and securely written?"* | *"What systemic risks, smells, or debt exist?"* |
| **Failure Triggers** | Broken UI, dead buttons, unhandled errors, bad UX | Bugs, logic flaws, architectural violations, bad tests | High complexity, security drift, poor conventions |
| **Pipeline Position** | Stage 4 (Immediate Post-Execution) | Stage 5 (Post-Behavioral Verification) | Stage 0 (Pre-Run) / Stage 6 (Post-Run) |

> **Core Invariant:**  
> **The Tester tests the software; the Reviewer tests the code; the Critic audits the project.**

---

## 2. Inputs to the Tester Role

The Tester role requires a rich execution context that bridges the original user intent with the runtime implementation. The Tester receives the following inputs:

```
┌──────────────────────────────────────────────────────────────────┐
│                          TESTER INPUTS                           │
├────────────────────────────────┬─────────────────────────────────┤
│ Specification & Requirements   │ Implementation & Runtime Context│
│ • User Task / Specification    │ • Live Repository Workspace     │
│ • Architect Spec (01_arch.md)  │ • Executor Report (03_exec.md)  │
│ • Planner Plan (02_plan.md)    │ • Git Status & Changed Files    │
│ • Project Docs (.ai/project/)  │ • Runtime Shell & Tool Outputs  │
│ • Prior Auto-Repair Feedback   │ • Baseline Git Reference        │
└────────────────────────────────┴─────────────────────────────────┘
```

### Detailed Input Specification:

1. **User Task & Original Requirements:**
   - The raw user goal or requirements document specifying the functional capabilities requested.
2. **Architect Specification (`01_architect.md`):**
   - System design, expected user workflows, route mappings, data models, and interface boundaries.
3. **Planner Task Plan (`02_planner.md`):**
   - The breakdown of user stories, acceptance criteria, UI flows, and checklist of expected deliverables.
4. **Executor Report (`03_executor.md` & `03_executor.json`):**
   - The claims made by the Executor regarding what was built, how to launch or run the system, what routes/commands exist, and what tests were run.
5. **Live Repository Workspace & Tooling:**
   - Full read and command execution access to the project directory to launch dev servers, run headless browsers/curl/CLI commands, and inspect runtime logs.
6. **Git Status & Changed Files List:**
   - The exact list of modified, added, and deleted files since the run's baseline, identifying where UI, routes, or CLI entry points were introduced.
7. **Project Documentation & Conventions (`.ai/project/*.md`):**
   - Project-specific instructions on build tools, start scripts, environment variables, test runners, port conventions, and design system guidelines.
8. **Previous Auto-Repair Feedback (Iterative Runs):**
   - In retry iterations ($Attempt > 1$), the feedback generated from prior testing passes, allowing Tester to verify regression fixes.

### Downstream Context: Reviewer Receives Complete Tester Report
A fundamental architectural requirement of this pipeline is that **the Reviewer must always receive the complete Tester report (`04_tester.md` and `04_tester.json`) as input**, regardless of whether the Tester returned `PASS`, `FAIL`, `BLOCKED`, or `NOT_TESTABLE`.

- The Reviewer treats the Tester report as **concrete behavioral evidence**.
- If Tester marked `PASS`, Reviewer verifies that the underlying implementation achieving that pass is not built on hacks, hardcoded constants, or security vulnerabilities.
- If Tester marked `FAIL` or reported minor behavioral flaws, Reviewer incorporates those observations when evaluating code quality.
- If Tester marked `NOT_TESTABLE`, Reviewer knows that behavioral verification was bypassed and increases scrutiny on static test suites and edge-case mocking.
- The Reviewer remains solely responsible for implementation correctness, code quality, and maintainability.

---

## 3. Outputs of the Tester Role

The Tester produces two coordinated artifacts adhering to Forge's Common Agent Protocol:

### 3.1 Human Markdown Report (`04_tester.md`)
The human report provides an exhaustive, readable assessment structured as follows:

1. **Executive Summary & Verdict:** Clear high-level pass/fail summary and release readiness assessment.
2. **Test Environment & Strategy:** How the application was executed (dev server, CLI harness, headless browser, curl, mock server) and tools used.
3. **User Journey & Flow Verification Matrix:** A structured table tracking each user scenario, expected outcome, actual outcome, and status:
   | Flow / Scenario | Steps Taken | Expected Behavior | Actual Behavior | Result |
   |---|---|---|---|---|
   | User Signup | Fill email & submit | Redirect to dashboard | Uncaught 500 error | FAIL |
4. **UI & UX Quality Assessment:** Evaluation of visual presentation, layout stability, spacing, responsiveness across viewports, dark/light theme integrity, and typography.
5. **Interactive States & Edge Cases:** Validation of loading indicators, empty states, boundary values, disabled buttons during async operations, and error banners.
6. **Deficiencies, Regressions & Reproduction Steps:** Detailed, reproducible bug reports categorized by severity (Critical, Major, Minor, UX Polish). Each defect includes explicit steps to reproduce, expected behavior, actual behavior, and concrete evidence.
7. **Remediation Guidance for Executor:** Precise, actionable instructions for the Executor to repair behavioral flaws.

### 3.2 Machine Report (`04_tester.json` & Embedded Protocol Block)
A strictly formatted YAML block conforming to Forge's Common Agent Protocol, saved alongside the Markdown report as `{seq:02d}_tester.json`:

- **Canonical Artifact Paths:**
  - Human Report: `.forge/runs/<run_id>/04_tester.md`
  - Machine Report: `.forge/runs/<run_id>/04_tester.json`
- **Attempt History Paths (Autonomous Retries):**
  - `.forge/runs/<run_id>/04_tester_attempt_{iteration}.md`
  - `.forge/runs/<run_id>/04_tester_attempt_{iteration}.json`

> **Score-Free Policy:**  
> The machine report strictly excludes numeric rating systems or subjective scores (e.g. `SCORES: { FUNCTIONALITY: 8, ... }`). Subjective scores introduce prompt noise and non-deterministic variability without actionable debugging value. The machine report relies exclusively on **empirical, forensic findings**: severity levels, concrete reproduction steps, expected behavior, actual behavior, and attached evidence.

---

## 4. Status Model

The Tester status model balances QA-specific behavioral semantics with Forge's pipeline protocol standards.

```
                  ┌────────────────────────┐
                  │   Tester Evaluation    │
                  └───────────┬────────────┘
                              │
         ┌────────────────────┼────────────────────┐
         │                    │                    │
         ▼                    ▼                    ▼
     [ PASS ]              [ FAIL ]           [ BLOCKED ]
 (All flows work;     (Behavioral defects; (Environment halts
  UI is functional)   triggers auto-repair)  test execution)
         │                    │                    │
         ▼                    ▼                    ▼
Pipeline to Reviewer    Executor Repairs      Pipeline Halts
                              │
                              ▼
                     [ NOT_TESTABLE ]
                  (Pure docs/static task;
                   bypasses to Reviewer)
```

### 4.1 Defined Statuses

| Status | Semantics | Pipeline Action | Exit Code |
|---|---|---|---|
| `PASS` | All critical and major user journeys succeed. UI renders correctly without fatal visual flaws. No unhandled runtime errors occur during interaction. | Advances pipeline to **Reviewer** stage (with complete Tester report attached). | 0 |
| `FAIL` | Observable behavioral defects detected (e.g. dead buttons, uncaught runtime exceptions, broken navigation, missing UI elements, clipped layout, unhandled error states). | Triggers **Auto-Repair Loop** back to **Executor** with structured feedback. (If forwarded to Reviewer, serves as evidence). | 1 |
| `BLOCKED` | The environment, infrastructure, or missing external prerequisites prevent the software from launching or being tested (e.g., missing database daemon, port bind collision, missing required system tool). | Halts the pipeline immediately to prevent futile token expenditure. | 1 |
| `NOT_TESTABLE` | The task or change is fundamentally untestable through behavioral interaction (e.g., pure documentation updates, CI config edits, prompt template tweaks, abstract library with no runnable entry point). | Gracefully bypasses behavioral verification and delegates verification directly to **Reviewer**. | 0 |

### 4.2 Protocol Status Mapping
In Forge's underlying `StageDefinition`:
- `success_statuses`: `frozenset({"PASS", "APPROVED", "SUCCESS", "COMPLETED", "NOT_TESTABLE"})`
- `allowed_statuses`: `frozenset({"PASS", "FAIL", "BLOCKED", "NOT_TESTABLE", "APPROVED", "CHANGES_REQUIRED", "REJECTED"})`
- If the agent emits `PASS`, Forge treats the stage as succeeded.
- If the agent emits `FAIL`, Forge treats it as `CHANGES_REQUIRED`, initiating auto-repair.
- If the agent emits `NOT_TESTABLE`, Forge records the audit log and proceeds to Reviewer.

---

## 5. Scope & Verification Boundaries

To prevent role creep between the Tester, Reviewer, and Critic, the Tester's evaluation scope is strictly bound to **observable software behavior and user experience**.

### 5.1 What the Tester MUST Evaluate (In Scope)

1. **Interactive Flows & Navigation:**
   - Can the user complete primary and secondary workflows from end to end?
   - Do links, buttons, modals, dropdowns, and form submits trigger their expected behavioral targets?
2. **Visual Hierarchy & Layout Hygiene:**
   - Is there visual clipping, overlapping text, missing padding, or awkward wrapping?
   - Do elements render responsively across mobile, tablet, and desktop viewport dimensions?
3. **Interactive & Asynchronous States:**
   - Does the UI display loading spinners or skeleton screens during asynchronous latency?
   - Are submit buttons disabled while requests are in flight to prevent duplicate submissions?
   - Do hover, active, focus, and disabled states visually convey interaction cues?
4. **Error Resilience & Boundary Handling:**
   - When given invalid, empty, or extreme input, does the UI present clear, human-readable error messages?
   - Does an API failure render a graceful fallback screen instead of a blank page or unhandled promise rejection?
5. **High-Confidence, User-Observable Accessibility:**
   - **Keyboard Usability:** Can the user tab through interactive controls (buttons, links, inputs) in a logical order without getting trapped? Can actions be activated via Enter or Space?
   - **Obvious Focus Indicators:** Does tabbing visibly highlight which element currently has focus, or does focus become completely invisible?
   - **Clearly Missing Form Labels:** Are inputs completely unlabelled or missing `<label>` associations that prevent basic screen reader or user comprehension?
   - **Severe Contrast Problems:** Are there glaring, obvious visual contrast failures that make text illegible to human vision (e.g. light gray text on a white background, dark blue text on a black background)?
6. **Incomplete Implementations & Placeholders:**
   - Are there "mock" buttons that do nothing, empty stub handlers, or placeholder `Lorem Ipsum` text that was supposed to be dynamically populated?
7. **User-Facing Regressions:**
   - Did the new feature break pre-existing visible functionality or navigation routes in the application?

### 5.2 What the Tester MUST NOT Evaluate (Out of Scope)

1. **Comprehensive WCAG / ARIA Auditing:**
   - The Tester explicitly does **NOT** perform formal WCAG 2.1 AA/AAA compliance audits, full accessibility tree traversals, complex ARIA role/state conformance matrices, or automated aXe/Lighthouse certification.
   - Deep structural ARIA markup verification and automated accessibility linting belong to automated Linters and the Reviewer.
2. **Internal Code Quality & Style:**
   - Formatting, variable naming conventions, lint rules, PEP 8/ESLint compliance (*Sole responsibility of Reviewer & Linters*).
3. **Architecture & Design Patterns:**
   - Clean architecture, dependency injection, class hierarchies, cyclomatic complexity (*Sole responsibility of Reviewer & Architect*).
4. **Internal Test Mock Fidelity:**
   - Whether unit test mocks accurately mirror backend database schemas (*Sole responsibility of Reviewer*).
5. **Technical Debt & Maintainability:**
   - Code duplication, deprecation warnings, long-term refactoring suggestions (*Sole responsibility of Critic*).
6. **Deep Security Static Analysis:**
   - AST security scanning, cryptographic implementation audits (*Sole responsibility of Reviewer & Critic*).

> **Core Invariant:**  
> **The Tester tests the software; the Reviewer tests the code; the Critic audits the project.**

---

## 6. Pipeline Position & Ordering

### 6.1 Proposed Stage Sequence

```
[00_critic] (Pre-Run Codebase Audit)
    │
[01_architect] (System Architecture)
    │
[02_planner] (Task Planning)
    │
┌───▼────────────────────────────────────────────────────────┐
│                   AUTONOMOUS REPAIR LOOP                   │
│                                                            │
│  [03_executor] (Change Producer)                           │
│         │                                                  │
│  [04_tester] (Behavioral Verification Gate) ◄──────────┐   │
│         │                                              │   │
│         ├── [FAIL] ────────► (Auto-Repair Feedback) ───┤   │
│         │                                              │   │
│         ▼ [PASS / NOT_TESTABLE]                        │   │
│  [05_reviewer] (Code Verifier — receives 04_tester)    │   │
│         │                                              │   │
│         └── [CHANGES_REQUIRED] ────────────────────────┘   │
└─────────┬──────────────────────────────────────────────────┘
          │ [APPROVED]
[06_critic] (Post-Execution Audit)
```

### 6.2 Architectural Justification for Placing Tester Before Reviewer

1. **Fail-Fast on Broken Software:**
   If an application cannot start, crashes on interaction, or has dead buttons, conducting a deep, costly code review of architecture and diff aesthetics is completely wasteful.
2. **Token & Latency Efficiency:**
   When Tester rejects a build on Attempt 1, the pipeline immediately triggers an auto-repair iteration without invoking the Reviewer. The Reviewer is only invoked once the software is verified to actually work.
3. **Behavior Precedes Craftsmanship:**
   Working software is the primary measure of progress. Reviewing the elegance of non-functioning code is an anti-pattern.
4. **Enriched Reviewer Context:**
   When Reviewer runs, it possesses empirical proof of what happens when the software runs. If Tester identified an edge case that barely passed, Reviewer can inspect that exact codepath in the diff to ensure it is robust.
5. **Regression Prevention:**
   If the Reviewer subsequently requests code-level changes (e.g. refactoring a database query), the next iteration triggers the Executor, which must then re-pass the Tester before returning to the Reviewer, guaranteeing that code refactoring does not introduce behavioral regressions.

---

## 7. Retry & Auto-Repair Semantics

### 7.1 Autonomous Repair Dynamics

When the Tester emits `FAIL`, the autonomous pipeline orchestrates an immediate targeted repair loop:

1. **Defect Extraction:**
   Forge parses the Tester's machine report (`04_tester.json`), extracting all `CRITICAL`, `MAJOR`, and `UX` issues along with reproduction steps, expected behavior, actual behavior, and evidence.
2. **Context Enrichment:**
   Forge injects a structured auto-repair prompt into `context.repair_feedback`:
   ```markdown
   ### Auto-Repair Feedback from Tester (Attempt 1):
   Status: FAIL
   Issues Identified:
   - [CRITICAL] Checkout button on /cart throws TypeError: cannot read properties of null (reading 'total')
     Reproduction:
       1. Launch app with 'npm run dev'.
       2. Navigate to '/cart' with 1 item.
       3. Click 'Proceed to Checkout'.
     Expected: Redirect to '/checkout' order summary.
     Actual: UI freezes, button remains disabled, console throws unhandled TypeError.
     Evidence: TypeError at Cart.tsx:42
   - [MAJOR] Navigation drawer fails to open on viewports under 768px width.
   - [UX] Missing loading spinner on form submission; button remains clickable causing duplicate requests.

   Fix all behavioral defects and verify before resubmitting.
   ```
3. **Executor Iteration:**
   Executor is launched for Attempt $N+1$, receiving the original task plus the targeted Tester repair feedback.
4. **Retry Budget:**
   The loop respects `--max-retries` (default: 3). If Attempt $N$ reaches the limit without a `PASS`, the pipeline terminates with exit code 1, saving all attempt artifacts (`04_tester_attempt_1.md`, `04_tester_attempt_2.md`, etc.) for developer inspection.

---

## 8. Prompt Philosophy & Persona

The Tester prompt template (`.ai/roles/tester.md`) establishes a dedicated persona: **The User Advocate / Principal QA Automation Engineer**.

### 8.1 Core Directives
1. **"Assume Broken Until Proven Working":**
   Do not trust Executor assertions or docstrings. Verify every claim by launching the system and executing user flows.
2. **"Act as a Real Human User":**
   Users do not read code comments; they click buttons, type unexpected input, resize browser windows, click buttons twice, and expect instant, intuitive feedback.
3. **"Document with Forensic Reproducibility":**
   Every reported defect must include:
   - Starting state / URL / command.
   - Exact sequence of inputs and interactions.
   - Expected behavior vs. actual behavior observed.
   - Supporting evidence (terminal output, console log, HTTP status, DOM dump).
4. **"Avoid Subjective Opinions":**
   Report facts and observable failures, never subjective scores or aesthetic musings.

---

## 9. Machine Report Schema

The machine report is embedded as a single YAML block parseable by `yaml.safe_load()`. **No numeric scores are included**:

```yaml
ROLE: TESTER
PROMPT_VERSION: 1.0
TASK_ID: run-012
START_TIME: "2026-09-23T10:00:00Z"
END_TIME: "2026-09-23T10:04:30Z"
DURATION: 270s
STATUS: FAIL
EXIT_CODE: 1
HANDOFF: EXECUTOR
VERDICT: REJECTED
REASON: "Checkout flow crashes on submission with unhandled TypeError; mobile layout broken."
TEST_ENVIRONMENT:
  TYPE: WEB
  RUNTIME: "Node.js v20.12.0 / Vite"
  COMMANDS_EXECUTED:
    - "npm run build"
    - "npm run test:e2e"
    - "curl -s http://localhost:5173/api/health"
TEST_SUITE:
  TOTAL: 8
  PASSED: 6
  FAILED: 2
  BLOCKED: 0
TEST_CASES:
  - ID: TC-01
    NAME: "User can add product to cart"
    STATUS: PASS
    EVIDENCE: "Cart counter incremented to 1; item displayed in drawer."
  - ID: TC-02
    NAME: "User can checkout"
    STATUS: FAIL
    EVIDENCE: "Clicking checkout button resulted in unhandled TypeError in console."
ISSUES:
  CRITICAL:
    - ID: C1
      DESCRIPTION: "Checkout button throws TypeError on click; user unable to complete purchase."
      STEPS_TO_REPRODUCE:
        - "Launch app with 'npm run dev'"
        - "Navigate to '/cart' with 1 item"
        - "Click 'Proceed to Checkout'"
      EXPECTED: "User redirected to '/checkout' order confirmation screen"
      ACTUAL: "UI freezes, button stays disabled, console throws unhandled TypeError"
      EVIDENCE: "TypeError: Cannot read properties of null (reading 'total') at Cart.tsx:42"
  MAJOR:
    - ID: M1
      DESCRIPTION: "Navigation drawer fails to open on viewports under 768px width."
      STEPS_TO_REPRODUCE:
        - "Resize viewport to 375x667 (mobile)"
        - "Click hamburger menu icon"
      EXPECTED: "Side drawer slides in with navigation links"
      ACTUAL: "Drawer does not trigger; aria-expanded remains false"
      EVIDENCE: "DOM element #nav-drawer has class 'hidden' after click"
  MINOR:
    - ID: m1
      DESCRIPTION: "Cart subtotal missing currency symbol prefix."
      EXPECTED: "$49.99"
      ACTUAL: "49.99"
  UX:
    - ID: U1
      DESCRIPTION: "No loading spinner on checkout button submission; button remains clickable."
      EXPECTED: "Button enters loading state and is disabled during API flight"
      ACTUAL: "Button appears active and allows duplicate clicks"
NEXT_ACTION: "Executor must fix checkout button event handler and responsive navigation drawer."
```

---

## 10. Integration Plan

When approved, implementation will proceed according to this concrete integration plan:

### 10.1 Stage Definitions & Order (`src/forge/stages/definition.py`)
- Insert `tester` at sequence number 4:
  ```python
  StageDefinition(
      name="tester",
      sequence_number=4,
      phase="pre_run",
      display_title="Tester",
      emoji="🧪",
      preposition="for task:",
      role_type="verifier",
      is_verifier=True,
      repair_target="executor",
      success_statuses=frozenset({"PASS", "APPROVED", "SUCCESS", "COMPLETED", "NOT_TESTABLE"}),
  )
  ```
- Shift `reviewer` to sequence number 5.
- Shift post-run `critic` to sequence number 6.
- Update `StageOrder.verification_stages()` to automatically return `[tester, reviewer]`.

### 10.2 Role Template (`.ai/roles/tester.md`)
- Create standard role prompt containing the Tester persona, testing methodology, high-confidence accessibility guidance, human report layout, and score-free machine report YAML specification.

### 10.3 Prompt Builder & Compiler (`src/forge/prompts/`)
- Update `InstructionBuilder.build()` for `role.name == "reviewer"`:
  - Load both `04_tester.md` and `04_tester.json` from the run directory.
  - Store in `instruction.tester_report` and `instruction.tester_machine_report`.
- Update `PromptCompiler._compile_reviewer()`:
  - Add explicit section: `## Tester Report & Behavioral Evidence` rendering the complete markdown report and parsed machine report for Reviewer's analysis.
- Add `_compile_tester` in `PromptCompiler` prioritizing user journeys, setup scripts, and acceptance criteria.

### 10.4 CLI Extensions (`src/forge/cli.py`)
- Add standalone command `forge test`:
  ```bash
  forge test [--run RUN_ID]
  ```
- Update pipeline banners and stage counts across `run_pipeline` and `auto_pipeline`.

### 10.5 Protocol Validator (`src/forge/protocol/validator.py`)
- Register `TESTER` role and valid statuses (`PASS`, `FAIL`, `BLOCKED`, `NOT_TESTABLE`).
- Ensure validator requires structured issues and strictly disallows `SCORES`.

---

## 11. Alternatives Considered & Trade-Offs

| Alternative | Evaluation & Why Rejected |
|---|---|
| **Merge Testing into Reviewer** | Rejected. Prompts with dual personas (code quality auditor + end-user behavioral tester) suffer from attention dilution. Reviewers invariably default to diff reading and ignore interactive verification. |
| **Run Tester in Parallel with Reviewer** | Rejected. Running both verifiers concurrently wastes tokens when the build is fundamentally broken. Sequential execution with fail-fast semantics is significantly more token-efficient. |
| **Place Reviewer Before Tester** | Rejected. Reviewing code aesthetics before verifying whether the application runs wastes engineer/token time on code that may need to be discarded. |
| **Include Numeric Scores (1-10)** | Rejected upon review. Numeric scores are arbitrary, non-deterministic, and subjective. Structured bug reports with reproduction steps and evidence provide actionable engineering value. |

---

## 12. Summary of Architectural Guarantees

1. **Complete Verification Coverage:** Code correctness (Reviewer) and runtime user experience (Tester) are treated as two independent, non-negotiable verification gates.
2. **Behavioral Evidence for Code Review:** Reviewer always receives the full Tester report (`04_tester.md` and `04_tester.json`) to cross-reference behavioral reality against code changes.
3. **Objective, Score-Free Reporting:** Replaces subjective ratings with forensic reproduction steps, expected vs actual behavior, and concrete error logs.
4. **Pragmatic Accessibility:** Focuses on high-confidence, user-observable accessibility flaws without getting bogged down in complex WCAG compliance paperwork.
5. **Fail-Fast Efficiency & Deterministic Repair:** Saves tokens by catching behavioral failures before code review and giving the Executor precise steps to reproduce.

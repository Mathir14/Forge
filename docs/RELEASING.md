# Forge Production Staged Release Guide

This document details the production-grade, staged release process for `forge-orchestrator`.

---

## 1. Core Principles

- **Zero Local Publishing**: No developer publishes from local machines.
- **Zero API Tokens**: No API tokens or passwords are stored in GitHub Secrets.
- **Trusted Publishing (OIDC) Only**: Cryptographic, short-lived OpenID Connect token minting.
- **Single Build Guarantee**: Wheel and sdist are built once, validated once, uploaded to TestPyPI, and subsequently deployed to production PyPI. **Artifacts are never rebuilt between staging and production.**
- **Automatic Smoke Testing**: The pipeline automatically installs the freshly staged package from TestPyPI into a clean container and executes import and CLI diagnostics before requesting production approval.
- **Environment Gatekeeper**: Production PyPI publication requires explicit manual approval in GitHub by designated maintainers.
- **Automated GitHub Release**: Once PyPI publication succeeds, a GitHub Release is automatically published with release notes and distribution assets attached.

---

## 2. Release Architecture & Flow

```
Developer
  │
  ├─ 1. Bump version in pyproject.toml & src/forge/__init__.py
  ├─ 2. Commit changes
  ├─ 3. Tag (e.g., git tag -a v0.1.0b11 -m "Release v0.1.0b11")
  └─ 4. Push tag to GitHub
        │
GitHub Actions (.github/workflows/release.yml)
  │
  ├─ Job 1: test-and-build
  │    ├─ Validate version tag format (PEP 440)
  │    ├─ Run full pytest suite (750+ tests)
  │    ├─ Single Build: build wheel and sdist (python -m build)
  │    ├─ Validate with twine check --strict dist/*
  │    └─ Upload dist/ artifacts to workflow run storage
  │
  ├─ Job 2: publish-testpypi (Environment: testpypi)
  │    ├─ Download dist/ artifacts
  │    └─ Publish to TestPyPI via pypa/gh-action-pypi-publish (OIDC)
  │
  ├─ Job 3: smoke-test-testpypi
  │    ├─ Poll and install forge-orchestrator FROM TestPyPI (20 attempts x 10s)
  │    ├─ Verify import: python -c "import forge; print(forge.__version__)"
  │    ├─ Verify: forge --version
  │    ├─ Verify: forge --help
  │    └─ Verify: forge doctor
  │
  ├─ Job 4: publish-pypi (Environment: pypi)
  │    ├─ 🛑 PAUSES: Waits for maintainer review & approval
  │    ├─ Download EXACT SAME dist/ artifacts (No Rebuild)
  │    └─ Publish to Production PyPI via pypa/gh-action-pypi-publish (OIDC)
  │
  └─ Job 5: create-github-release
       ├─ Download EXACT SAME dist/ artifacts
       └─ Auto-publish GitHub Release with assets via gh release create
```

---

## 3. One-Time Setup: PyPI & TestPyPI Trusted Publishing (OIDC)

### A. Production PyPI Setup

1. Log into your account at [pypi.org](https://pypi.org).
2. If `forge-orchestrator` is not yet published:
   - Go to [PyPI Publishing (Add Pending Publisher)](https://pypi.org/manage/account/publishing/).
   - Select **GitHub**.
   - **PyPI Project Name**: `forge-orchestrator`
   - **Owner**: `Mathir14`
   - **Repository name**: `Forge`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`
3. If already registered:
   - Go to **Project Settings** ➔ **Publishing** ➔ **Add a new publisher** with the above details.

### B. TestPyPI Setup

1. Log into your account at [test.pypi.org](https://test.pypi.org).
2. Go to [TestPyPI Publishing (Add Pending Publisher)](https://test.pypi.org/manage/account/publishing/).
   - Select **GitHub**.
   - **PyPI Project Name**: `forge-orchestrator`
   - **Owner**: `Mathir14`
   - **Repository name**: `Forge`
   - **Workflow name**: `release.yml`
   - **Environment name**: `testpypi`

### C. GitHub Repository Environments Setup

In the GitHub repository (`https://github.com/Mathir14/Forge`):

1. Go to **Settings** ➔ **Environments**.
2. Create environment `testpypi`:
   - No approval restrictions needed (automated staging).
3. Create environment `pypi`:
   - Enable **Required reviewers**.
   - Select authorized maintainers who must sign off before any production PyPI deployment.
   - *(Optional)* Set **Deployment branches/tags** to `v*`.

---

## 4. Maintainer Release Process

To cut a new release:

### Step 1: Bump Version & Validate Locally

Update the version in:
- `pyproject.toml` (e.g. `version = "0.1.0b16"`)
- `src/forge/__init__.py` (e.g. `__version__ = "0.1.0b16"`)

Run local tests to confirm:
```bash
pytest
```

### Step 2: Commit and Tag

```bash
git commit -am "chore: release v0.1.0b16"
git push origin master

git tag -a v0.1.0b16 -m "Release v0.1.0b16"
git push origin v0.1.0b16
```

### Step 3: Monitor Staged Release in GitHub Actions

1. The `Staged Release` workflow triggers on the tag push.
2. It validates the tag format, runs the full test suite, builds distributions, uploads to TestPyPI, and runs automated smoke tests.
3. Once smoke tests succeed, the workflow pauses at `publish-pypi` with status `Waiting for review`.

### Step 4: Approve Production Release

1. In the GitHub Actions run page, review the TestPyPI smoke test logs.
2. Click **Review deployments** ➔ Select `pypi` ➔ Click **Approve and deploy**.
3. The exact same wheel and sdist files are immediately deployed to [pypi.org/p/forge-orchestrator](https://pypi.org/p/forge-orchestrator).
4. GitHub Actions then automatically creates and publishes the GitHub Release with generated release notes and attached distribution assets.

---

## 5. PEP 440 Versioning Reference

| Release Phase | PEP 440 Version Format | Git Tag | Target |
| :--- | :--- | :--- | :--- |
| **Development** | `0.2.0.dev1` | `v0.2.0-dev1` | TestPyPI |
| **Alpha** | `0.2.0a1` | `v0.2.0a1` | TestPyPI / PyPI |
| **Beta** | `0.2.0b1` | `v0.2.0b1` | TestPyPI / PyPI |
| **Release Candidate** | `0.2.0rc1` | `v0.2.0rc1` | TestPyPI / PyPI |
| **Final / Stable** | `0.2.0` | `v0.2.0` | PyPI |

## 6. Release Notes: v0.1.0b18

`v0.1.0b18` resolves persistent Antigravity post-terminal CLI lifecycle issues and hardens cross-platform cleanup across POSIX and native Windows environments:

- **Verified Post-Terminal Forced Cleanup**: In Antigravity CLI, background Go routines (such as `printmode.waitForIdle`) can remain active for up to 30 minutes following terminal event emission and stream draining. Forge now enforces verified post-terminal escalation: when a valid successful terminal event is received, graceful shutdown is dispatched, and the process remains alive past the configured monotonic grace deadline (`post_terminal_grace_seconds`, default 15.0s), Forge escalates to process-tree termination.
- **Direct OS Process Liveness Confirmation**: Forge re-validates process identity and confirms process death directly against the operating system (`not is_pid_alive(pid)`) before accepting cleanup. Successful termination with a valid terminal result and verified report schema normalizes the stage exit code to 0 with full audit metadata (`graceful_shutdown=True`, `forced_cleanup=True`, `raw_exit_code=proc_code`).
- **OpenCode & Shared Cleanup Invariant Preservation**: Retained unescalated failure semantics for OpenCode adapter process-lingering, enforced process death confirmation in shared `_safe_cleanup_subprocess`, and preserved strict failure handling for PID mismatches, missing terminal events, and unverified cleanup.
- **Cross-Platform & Windows Lifecycle Hardening**: Hardened subprocess cleanup on Windows by avoiding OS process identity checks on mock processes, normalizing exit code checks, adding process exit polling latency guards, and scoping POSIX-only signal fixtures.

---

## 7. Release Notes: v0.1.0b17

`v0.1.0b17` delivers dashboard architecture consolidation and post-terminal process lifecycle hardening:

- **Unified Dashboard Architecture (4-Tab Model)**: Consolidated top-level tabs to 4 core operational views (`[1: Console]`, `[2: Artifact]`, `[3: PKB]`, `[4: Compare]`). Removed the redundant top-level `Tester` tab, unifying test execution matrices, journey summaries, and failure evidence into the `[2: Artifact]` view as a dedicated subtab `(H: Human  M: Machine  T: Tests)` when viewing test-bearing stages. Added global hotkey `t` to jump directly to the Tester stage and activate the Tests view.
- **Antigravity Post-Terminal Process Lifecycle Hardening**: Resolved run-003 failure where Antigravity emitted a terminal `COMPLETE` event while the CLI process was still actively tearing down. Replaced arbitrary short exit timeouts with configurable post-terminal grace periods (`post_terminal_grace_seconds`, default 2.0s) and bounded monotonic deadlines (`time.monotonic()`).
- **Bounded Stream Draining & Monotonic Deadlines**: Enforced that stream draining after terminal events respects overall stage deadlines without hanging indefinitely on descendant pipes, while preserving the distinction between terminal event receipt and verified OS-level process termination.
- **Strict Process Identity & Signal Safety**: Hardened `terminate_process_tree` and process cleanup routines to verify process identity (PID and creation/start time) immediately before signaling, preventing dangerous PID reuse or process-group misdirection.
- **Cleanup Failure Tracking & Nonzero Exit Preservation**: Nonzero exit codes, malformed terminal events, or forced terminations are strictly treated as failures and recorded with explicit failure reasons in `ProcessTracker`.

---

## 7. Release Notes: v0.1.0b16

`v0.1.0b16` delivers comprehensive process lifecycle tracking, orphan prevention, safe recovery, and native Windows Job Object containment:

- **Native Windows Process Containment (Job Objects)**: Replaced best-effort `taskkill` with native Win32 Job Objects configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Agent processes are launched suspended (`CREATE_SUSPENDED`), assigned to the Job Object, and verified via `IsProcessInJob` before the primary thread is resumed. Refuses uncontained launch if Job Object assignment fails.
- **Cross-Platform Descendant Cleanup & Orphan Prevention**: Kernel-enforced termination of all descendant processes when Forge terminates or when timeouts/cancellations occur. Prevents descendant processes from outliving Forge or holding inherited stdout/stderr pipes open.
- **Bounded Stream Reading & EOF Independence**: Eliminated stdout pipe EOF deadlocks in `BaseAdapter`, `AntigravityAdapter`, and `OpenCodeAdapter` via `BoundedLineReader` and post-terminal bounded draining (`POST_TERMINAL_DRAIN_TIMEOUT_SECONDS = 0.2`). Adapters no longer block on unclosed inherited descriptors from child processes.
- **Auditable Process Lifecycle State Tracking**: Introduced `ProcessTracker` and atomic persistence of `ProcessRecord` (`STARTING`, `RUNNING`, `EXIT_VERIFIED`, `TERMINATED`, `CLEANUP_FAILED`, `UNRESOLVED`) under `.forge/runs/<run_id>/processes/<seq>_<stage>.json`. Tracks start time ticks / creation times to protect against PID reuse.
- **Truth-Grounded Stage Completion & Reconciliation**: `Stage.run()` strictly verifies that zero exit codes originate from natural process completion (`EXIT_VERIFIED`) rather than forced termination. Stage recovery reconciles orphaned stages with mutual exclusion via `RunLock`.

---

## 7. Release Notes: v0.1.0b15

`v0.1.0b15` delivers cross-project Tester v2 archetype generalization, runtime supervisor isolation, adversarial verdict arbitration, complete cache fingerprinting, and deterministic browser fallback:

- **Cross-Project Tester v2 Generalization**: Dedicated interaction drivers and heuristics for conventional HTML/JS web applications, CLI tools with discoverable non-`main.py` entrypoints, HTTP REST APIs with schema and status validation, and baseline Streamlit applications.
- **Runtime Environment Isolation & Process Supervision**: `RuntimeSupervisor` enforces clean virtual environment execution (`PYTHONNOUSERSITE=1`), explicit dependency boundaries, dynamic port allocation without reservation races, and reliable process group termination preventing orphaned background listeners.
- **Adversarial Reliability & Intent-Aware Verdict Arbitration**: Arbitrates between intended authentication checks (`401`/`403`) and unexpected security failures, enforces fail-closed policies on unexpected `4xx`/`5xx` responses, and prevents unverified application code changes from masquerading as passes.
- **Cache Fingerprint Completeness & Tamper-Evident Invalidation**: Added `Role.template_hash` and `compute_stage_config_fingerprint` to ensure prompt template edits, protocol changes, and material execution parameters (`adapter`, `model`, `effort`, `extra_flags`) reliably invalidate stage caches across pipeline resumes.
- **Deterministic Browser Fallback & Thread Lifecycle Safety**: Wrapped `PlaywrightBrowserDriver.start()` in robust cleanup guards to prevent unclosed event loops from polluting threads on startup failure, and expanded `MockBrowserDriver` with quote-agnostic HTML attribute matching.

---

## 7. Release Notes: v0.1.0b14

`v0.1.0b14` delivers cross-adapter tool-event rendering reliability, stage-boundary tool-tracking isolation, adapter session recovery hardening, and platform-safe duration testing:

- **OpenCode Session Recovery Hardening & Artifact Extraction**: Hardened adapter session recovery transport handling and diagnostics in `OpenCodeAdapter` and `Stage.run()`, bounded session retry attempts, ensured stderr/stdout fallback streams are extracted properly into stage deliverables, and expanded regression test suites.
- **Cross-Platform Duration Assertion Resilience**: Made sub-second duration assertions Windows-safe in `tests/test_run016_regressions.py`, handling coarse OS clock granularity (`0.0s`) on fast mock executions.
- **Standalone Tool-Finish Argument Rendering**: Instrumented `RunModel.apply_event` to detect standalone `TOOL_FINISH` events from batch CLI adapters (e.g. OpenCode batch execution) and format invocation input arguments inline (`✔ TOOL_FINISH: tool ({args})`) rather than omitting them, while preserving clean names for paired start/finish streaming lifecycles (e.g. Antigravity streaming).
- **Tool Error Diagnostics & Failure Preservation**: Updated `OpenCodeAdapter` and `AntigravityAdapter` to extract provider execution error payloads, flagging `data["error"] = True` and normalized `data["status"] = "error"`. Preserved error diagnostics in `data["output"]` without clobbering existing stdout/stderr text and prevented error events from routing to `TOOL_START`.
- **Stage Boundary Tool-Tracking Isolation**: Added `RunModel._clear_stage_tool_tracking()` to purge dangling in-flight tool tracking keys from `_started_tool_calls` and `_open_unidentified_tools` at `stage_start`, `stage_finish`, `stage_skipped`, and `AgentEventType.ERROR` boundaries. Prevents in-flight tools from aborted stage attempts from contaminating retries, ensuring retried attempts display standalone tool arguments correctly while preserving tracking across concurrent/interleaved stages.
- **Targeted Console Error Styling**: Restricted bold red text styling in `render_console_view` strictly to genuine `TOOL_ERROR` lines and stage/run `ERROR` lines (`"TOOL_ERROR" in line or (stripped.startswith("✗ [") and "] ERROR" in stripped)`). Ordinary agent streaming narration beginning with `✗` (e.g. indented test assertions or bullet points) retains default unstyled font.

---

## 7. Release Notes: v0.1.0b13

`v0.1.0b13` addresses run-016 operational defects, Critic protocol validation hardening, Tester completion lifecycle propagation, and terminal dashboard scrolling responsiveness:

- **Critic Prompt Isolation & Contamination Remediation**: Delimited prior-stage outputs inside `<audit_evidence stage="...">` tags and sanitized nested YAML blocks (` ```yaml ` -> ` ```yaml-evidence `) in `PromptCompiler._compile_critic`. Prevents models from echoing prior Executor/Reviewer roles or handoffs. Injects authoritative schema requirements originating strictly from trusted Critic stage configuration.
- **Strict Role Validation & Bounded Protocol Retry**: Updated `MachineReportValidator` to strictly preserve emitted roles (e.g. `ROLE: EXECUTOR`) rather than silently normalizing to the expected role. Implemented a bounded single retry (`Attempt 2/2`) in `auto_pipeline` for protocol validation failures, preserving attempt artifacts (`*_attempt_1.md`, `*_attempt_1.json`, `*_attempt_1_raw.txt`), delivering targeted repair feedback, and failing closed if the retry does not succeed.
- **Tester Completion & Stage Synchronization**: Instrumented `TesterEngine.run()` to emit `AgentEventType.COMPLETE` with execution status (`PASS`), duration, and exit code. Handled unhandled exceptions with `AgentEventType.ERROR`. Updated `RunModel.apply_event` to mutate stage records and timeline duration rendering for sub-second executions (`<0.1s` and `<1.0s`), ensuring accurate live and post-mortem presentation.
- **Virtual Viewport Slicing & Motion Responsiveness**: Introduced `VirtualViewport` renderable which slices pre-rendered Rich terminal rows (`console.render_lines`) with exact 1-row precision. Eliminates blank-line freezes caused by Markdown AST parser whitespace stripping, ensures line counter synchronization, dynamically bounds scroll offsets, and maintains row caching.
- **ANSI/SGR Input Tokenizer & Mouse-Wheel Routing**: Implemented `parse_key_buffer` tokenizer handling SGR extended mouse events (`wheel_up:X:Y`, `wheel_down:X:Y`, `click:X:Y`), legacy mouse sequences, and multi-key escape bursts. Enabled SGR mouse tracking in terminal sessions. Added pane-aware routing (mouse wheel over timeline navigates stages; mouse wheel over content scrolls lines by 3 rows; click toggles focus). Unified scroll directions across Document views and Console history with Home/End jump support.

---

## 8. Release Notes: v0.1.0b12

`v0.1.0b12` hardens agent recovery, working-tree concurrency, and lease portability across environments:

- **Recovery Timeout & Lease Portability**: Resolved lease portability across multi-agent environments and hardened session recovery timeouts.
- **Working-Tree Concurrency**: Hardened concurrent access to working-tree state and metadata across background worker processes.

---

## 9. Release Notes: v0.1.0b11

`v0.1.0b11` addresses findings RC-01 through RC-04 from the adversarial release-candidate audit:

- **RC-01 Task/Feedback Disambiguation & Stale Artifact Archival**: Decoupled auto-repair feedback from `Run.task`. User task modifications are strictly opaque, preserving task fingerprint integrity and triggering automatic archival of stale generation artifacts. Auto-repair feedback is routed through explicit `Run.set_auto_repair_feedback()`.
- **RC-02 Dashboard Incomplete State Reconciliation**: Fixed dashboard terminal-state handling. Interrupted and incomplete runs truthfully display incomplete/interrupted states rather than reconciling to false failures. Completed stages reconcile to APPROVED.
- **RC-03 Stage Deliverable Completion Verification**: Removed metadata-summary fallback from `verify_pipeline_completion()`. Pipeline verification strictly audits physical stage deliverables on disk, enforcing task fingerprint, run ID, role identity, sequence ordering, successful stage status, and zero exit code.
- **RC-04 Mandatory Artifact Provenance Envelopes**: Enforced all four provenance fields (`parent_fingerprint`, `producer_agent`, `generated_at_utc`, `run_id`). Missing or malformed provenance is strictly rejected as invalid, and `save_stage_artifacts()` guarantees full provenance persistence across all stages.

---

## 10. Release Notes: v0.1.0b10

`v0.1.0b10` is a corrective release following the `v0.1.0b9` Native Windows release:

- **Windows RunLock mandatory-locking compatibility fix**: On Windows NT, byte-range locking via `msvcrt.locking` is kernel-enforced and mandatory.
- **Windows lock byte moved away from owner metadata**: Windows file lock offset moved to `WINDOWS_LOCK_OFFSET` (1 GiB) instead of byte 0.
- **Diagnostic metadata readable while locked**: `run.lock` owner diagnostic metadata at offset 0 remains readable while locked without triggering `PermissionError` (`ERROR_LOCK_VIOLATION`).
- **Regression coverage added**: Dedicated regression test `test_windows_run_lock_uses_windows_lock_offset` in `tests/test_windows_platform_support.py`.
- **Full Linux/Windows matrix validation**: Verified across full 8-job CI matrix (Ubuntu 3.10–3.13 and Windows 3.10–3.13).

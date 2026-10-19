# Forge

[![PyPI version](https://img.shields.io/pypi/v/forge-orchestrator.svg)](https://pypi.org/project/forge-orchestrator/)
[![Python Versions](https://img.shields.io/pypi/pyversions/forge-orchestrator.svg)](https://pypi.org/project/forge-orchestrator/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/Mathir14/Forge/blob/master/LICENSE)
[![Release: v0.1.0b16](https://img.shields.io/badge/Release-v0.1.0b16-green.svg)](https://github.com/Mathir14/Forge/releases/tag/v0.1.0b16)

Forge is an open-source, CLI-first multi-agent orchestration framework for autonomous, auditable software engineering. Designed for developers and engineering teams who want to incorporate AI coding agents into real production codebases safely, Forge eliminates the risks of unconstrained single-model generation—such as silent defects, architectural drift, hallucinated test passes, and unverified claims—by organizing specialized local CLI agents into a disciplined engineering pipeline with strict separation of concerns, empirical runtime testing, adversarial diff-first review, and complete human-in-the-loop oversight.

---

## Why Forge?

Standalone AI coding agents often operate as monolithic loops: the same model designs, writes, and tests its own code. This frequently results in:
- **Confirmation Bias**: The agent tests only what it implemented, masking edge cases and regressions.
- **Architectural Drift**: Agents make localized edits that violate existing system conventions and component boundaries.
- **Unverifiable Claims**: Output reports claim that tests passed or services started without generating verifiable proof.
- **Stale Cache & Artifact Reuse**: Resuming a pipeline can re-use outdated artifacts even after upstream inputs changed.

Forge replaces unmonitored agent loops with a structured multi-role pipeline where implementation is strictly separated from specification, empirical verification, and final review.

---

## Architecture & Multi-Role Pipeline

Forge coordinates specialized agents through an ordered sequence of stages defined in `StageOrder`. Each stage produces paired human-readable (`.md`) and machine-readable (`.json`) artifacts stored in `.forge/runs/<run_id>/`:

```text
00_critic.md / .json       -> Codebase Critic (Pre-run architecture and tech-debt audit)
01_architect.md / .json    -> Software Architect (Module boundaries and interface contracts)
02_planner.md / .json      -> Project Planner (Task breakdown and acceptance criteria)
03_executor.md / .json     -> Software Executor (Implementation and unit test execution)
04_tester.md / .json       -> Empirical Tester v2 (Observable runtime behavior verification)
05_reviewer.md / .json     -> Diff-First Reviewer (Adversarial diff and evidence audit)
06_critic.md / .json       -> Post-Execution Critic (Closing codebase sanity audit)
```

### How Stages Fit Together

1. **Planning & Design**: **Architect** (`01`) defines boundaries and interface contracts without modifying code. **Planner** (`02`) decomposes the architecture into dependency-ordered implementation tasks.
2. **Implementation**: **Executor** (`03`) implements the planned changes and runs local linters, builds, and unit tests.
3. **Empirical Verification**: **Tester v2** (`04`) independently boots the application under process supervision and drives user journeys across native interfaces (browser, API, CLI, or library), capturing forensic proof (screenshots, HTTP logs, console telemetry).
4. **Adversarial Review**: **Reviewer** (`05`) performs a change-centric audit. It checks the bounded Git diff against original requirements and empirical test evidence before granting approval.
5. **Self-Repair Loop**: In `forge auto`, if tests fail or review changes are required, structured defect reports route back to the Executor for up to `--max-retries` iterations.
6. **Closing Audit**: **Post-Execution Critic** (`06`) verifies uncommitted diffs for newly introduced tech debt or regressions before final completion.

---

## Core Capabilities

- **CLI-Native Subprocess Invocation**: Operates directly through installed local developer binaries (**OpenCode**, **Google Antigravity** / `agy`, and **OpenAI Codex**) as subprocesses without proprietary cloud API wrappers or token markups.
- **Tester v2 Multi-Archetype Support**:
  - **`WEB_SPA`**: Web applications and SPAs (React, Next.js, Vue, Vite, and Streamlit) verified via headless Chromium with Playwright, automatically falling back to `MockBrowserDriver` when browser binaries are absent.
  - **`API`**: HTTP/REST/GraphQL services (FastAPI, uvicorn, Express, Flask, Django) probed with intent-aware HTTP status assertions (distinguishing intentional negative tests from application crashes).
  - **`CLI`**: Command-line tools verified with discoverable entry point search (`cli.py`, `pyproject.toml` console scripts), arguments, stdout/stderr, and exit code assertions.
  - **`LIBRARY`**: Reusable packages and modules tested via direct consumer sandbox imports.
- **Runtime Supervision & Environment Isolation**: `RuntimeSupervisor` manages dev server lifecycles, port conflict detection, virtual environment isolation (`PYTHONNOUSERSITE=1`), and process tree teardown (`SIGKILL` on POSIX, `taskkill /F /T` on Windows).
- **Artifact Freshness & Downstream Invalidation**: Deterministic caching keyed by stage configuration fingerprints, prompt template hashes, upstream artifact SHA256 hashes, and git working tree state. Rerunning any stage automatically invalidates downstream artifacts.
- **Diff-First Adversarial Review**: Git diffs serve as the primary review artifact. The Reviewer validates claims against actual modifications and test evidence rather than trusting model assertions.
- **Git Safety & Change Attribution**: Diffs filter runtime logs and `.env*` secrets; Forge detects mixed-ownership files containing pre-existing user edits and refuses to commit them automatically.
- **Project Knowledge Base (PKB)**: Discovers and tracks persistent repository knowledge in `.forge/knowledge/` with role authority rules, context projection, and developer locking (`forge knowledge lock`).
- **Interactive Terminal Dashboard**: Full-terminal UI (`forge dashboard`) with timeline navigation, streaming console output, stage artifacts, test evidence, and knowledge base browser.
- **Kernel-Backed Run Locking**: Exclusive run locking (`flock` on POSIX, `msvcrt` on Windows) prevents concurrent process conflicts with automatic stale-lock recovery.

---

## Requirements & Platform Support

- **Python**: `>= 3.10` (tested on Python 3.10, 3.11, 3.12, 3.13, and 3.14)
- **Git**: `>= 2.25` in system `PATH`
- **Supported Agent CLIs** (at least one installed and authenticated):
  - **OpenCode** (`opencode`)
  - **Google Antigravity** (`agy` or `antigravity`)
  - **OpenAI Codex** (`codex`)
- **Optional for Web Testing**:
  - `playwright` (`pip install playwright && playwright install chromium`)

### Platform Matrix

| Platform | Status | Operating Characteristics |
| :--- | :--- | :--- |
| **Linux** | Supported | Reference development platform with POSIX process group isolation and `flock` run locking. |
| **macOS** | Supported | Full POSIX compatibility with process group isolation and `flock` run locking. |
| **WSL2** | Supported | Runs as native Linux inside WSL2. Agent CLIs must be installed natively inside the WSL Linux distribution (Windows `/mnt/c` wrappers are rejected). |
| **Native Windows** | Supported | First-class Windows NT support using Windows process trees (`CREATE_NEW_PROCESS_GROUP`, `taskkill`), `msvcrt` locking, and transparent `.cmd`/`.bat` CLI execution. Windows Terminal recommended. |

---

## Installation

Install the latest release of Forge from PyPI:

```bash
pip install forge-orchestrator
```

For pre-release versions:

```bash
pip install --pre forge-orchestrator
```

### Optional Browser Automation Setup

To enable real Chromium browser automation for Web applications in Tester v2:

```bash
pip install playwright
playwright install chromium
```

*(If Chromium is not installed, Forge automatically falls back to `MockBrowserDriver` for in-memory testing without crashing).*

### Development Installation from Source

```bash
git clone https://github.com/Mathir14/Forge.git
cd Forge
pip install -e ".[dev]"
```

Verify your installation:

```bash
forge --version
forge doctor
```

---

## Quick Start

Get up and running with Forge in under 5 minutes:

```bash
# 1. Initialize Forge prompt templates and configuration in your repository
forge init

# 2. Check installed CLI tools, git state, and environment diagnostics
forge doctor

# 3. Perform a standalone codebase audit for technical debt and vulnerabilities
forge critic "Audit authentication system and error handling"

# 4. Implement a task with step-by-step confirmation checkpoints
forge run "Add /healthz JSON endpoint with uptime and git commit SHA"

# 5. Inspect execution artifacts, timelines, and test evidence in the terminal dashboard
forge dashboard
```

To run the pipeline completely unattended with autonomous self-repair:

```bash
forge auto "Implement rate limiting middleware using token bucket algorithm" --auto-commit
```

---

## CLI Overview

Forge provides a focused set of CLI commands for pipeline execution, stage inspection, and configuration:

| Command | Purpose |
| :--- | :--- |
| `forge init` | Initialize `.ai/` prompt templates and `forge.yaml` in the current project. |
| `forge doctor` | Diagnose environment, installed CLI tools, Git repository state, and browser drivers. |
| `forge run [TASK]` | Run the standard pipeline with interactive confirmation checkpoints between stages. |
| `forge auto [TASK]` | Run the fully autonomous iterative loop with automatic self-repair (`--max-retries`). |
| `forge dashboard [RUN]` | Launch the interactive full-terminal UI dashboard to inspect live or past runs. |
| `forge runs` | List historical runs, statuses, and execution metadata. |
| `forge test` | Run the standalone Tester role to verify observable software behavior. |
| `forge review` | Run the standalone Reviewer role for adversarial diff inspection. |
| `forge execute` | Run the standalone Executor role to implement planned changes. |
| `forge planner` | Run the standalone Planner role to break architecture into tasks. |
| `forge architect` | Run the standalone Architect role to generate system designs. |
| `forge critic` | Run the standalone Critic audit (`--post-run` for post-execution checks). |
| `forge recover` | Recover and reconstruct a stage result from an external daemon session. |
| `forge adapters` | List registered adapters, prompt transports, default models, and capabilities. |
| `forge config` | Inspect and manage configuration (`show`, `get`, `set`, `edit`, `validate`, `reset`). |
| `forge knowledge` | Manage the Project Knowledge Base (`list`, `show`, `lock`, `unlock`). |

---

## Supported Adapters

Forge connects to installed developer CLI tools using native adapters. Inspect capabilities at any time with `forge adapters`:

| Adapter | Binary | Default Model | Prompt Transport | Core Capabilities |
| :--- | :--- | :--- | :--- | :--- |
| `opencode` | `opencode` | Provider default | `stdin` | `code_read`, `code_edit`, `shell`, `git`, `structured_output`, `custom_flags` |
| `antigravity` (`agy`) | `agy` or `antigravity` | `gemini-3.7-flash-high` | `-p` flag | `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `custom_flags` |
| `codex` | `codex` | `gpt-5.6-terra` | `stdin` (`-`) | `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `tool_calling`, `custom_flags` |

---

## Complete Documentation

For complete operational details, configuration schemas, CLI reference, testing engine guides, and troubleshooting workflows, consult the **[Complete User Guide](docs/USER_GUIDE.md)**.

- **[Installation & Platform Setup](docs/USER_GUIDE.md#2-installation)**
- **[Configuration Reference (`forge.yaml`)](docs/USER_GUIDE.md#4-configuration)**
- **[CLI Command Reference](docs/USER_GUIDE.md#6-cli-commands)**
- **[Roles & Machine Protocol](docs/USER_GUIDE.md#7-roles--pipeline)**
- **[Testing Engine (Tester v2)](docs/USER_GUIDE.md#11-testing-system-tester-v2)**
- **[Project Knowledge Base](docs/USER_GUIDE.md#10-knowledge-system)**
- **[Terminal Dashboard](docs/USER_GUIDE.md#8-dashboard)**
- **[Troubleshooting Guide](docs/USER_GUIDE.md#13-reliability-security--troubleshooting)**

---

## License

Forge is licensed under the [MIT License](LICENSE).

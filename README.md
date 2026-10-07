# Forge

Forge is an open-source, CLI-first multi-agent orchestration framework for autonomous, auditable software development. Designed for software engineers and development teams who want to incorporate AI coding agents into real codebases safely, Forge eliminates the risks of unconstrained single-model generation—such as silent bugs, architectural drift, and unverifiable claims—by organizing specialized local CLI agents into a disciplined engineering pipeline with strict separation of concerns, empirical runtime testing, adversarial diff-first review, and complete human-in-the-loop oversight.

## Features

- **Multi-Role Engineering Pipeline**: Coordinates specialized roles in an auditable sequence: **Critic** (`00`), **Architect** (`01`), **Planner** (`02`), **Executor** (`03`), **Tester** (`04`), **Reviewer** (`05`), and post-execution **Critic** (`06`).
- **CLI-Native Agent Invocation**: Operates directly through installed developer CLI binaries (**OpenCode**, **Google Antigravity** / `agy`, and **OpenAI Codex**) as subprocesses without proprietary cloud API wrappers or token markups.
- **Empirical Black-Box Testing**: Evaluates running applications through real interfaces (Headless Chromium via Playwright, REST/GraphQL APIs, CLI binaries, Library imports) under process group supervision (`RuntimeSupervisor`), collecting viewport screenshots, telemetry logs, and executable reproduction scripts.
- **Diff-First Adversarial Review**: Change-centric quality gate where the Git diff is the primary review artifact. The Reviewer validates Executor claims against actual codebase changes and empirical test evidence before granting approval.
- **Autonomous Self-Repair Loop**: In `forge auto`, automatically iterates between the Executor, Tester, and Reviewer. If tests fail or changes are requested, structured issue feedback is fed back into subsequent implementation attempts until approved or retry limits are reached.
- **Interactive Checkpoints & Resumption**: Run interactively with step-by-step confirmation checkpoints (`forge run`) or resume any previous run seamlessly (`--run run-XXX`), automatically skipping already completed stages.
- **Project Knowledge Base (PKB)**: Automatically discovers and tracks persistent repository knowledge across runs in `.forge/knowledge/` (`architecture.yaml`, `features.yaml`, `decisions.yaml`, `unresolved.yaml`) with role-based permissions and human locking.
- **Terminal User Interface Dashboard**: Interactive terminal dashboard (`forge dashboard`) to monitor live runs or inspect historical runs with timeline navigation, streaming console output, stage artifacts, test evidence, and knowledge base browser.
- **Git Safety & Change Attribution**: Captures unified diffs with automatic filtering of `.forge/` runtime data and `.env*` secrets, intelligently attributing changes to avoid committing mixed-ownership files containing pre-existing user edits.
- **Process-Level Run Locking**: Kernel-backed exclusive run locking (`flock` on POSIX, `msvcrt` on Windows) prevents concurrent process conflicts on the same run directory with automatic stale-lock recovery.

## Requirements

- **Python**: `>= 3.10`
- **Git**: `>= 2.25` installed and available in `PATH`
- **Supported Agent CLIs** (at least one installed and authenticated):
  - **OpenCode** (`opencode`)
  - **Google Antigravity** (`agy` or `antigravity`)
  - **OpenAI Codex** (`codex`)
- **Optional for Web Testing**:
  - `playwright` (`pip install playwright && playwright install chromium`)

## Platform Support

Forge officially supports Linux, macOS, WSL2, and Native Windows as first-class platforms:

| Platform | Status | Operating Characteristics |
| :--- | :--- | :--- |
| **Linux** | Supported | Reference development platform with POSIX process group isolation and `flock` run locking. |
| **WSL2** | Supported | Runs as native Linux inside WSL2. Agent CLIs must be installed natively inside the WSL2 Linux distribution (Windows-host binaries exposed via `/mnt/c` are not supported). |
| **macOS** | Supported | Full POSIX compatibility with process group isolation and `flock` run locking. |
| **Native Windows** | Supported | Full autonomous and interactive pipeline support. Uses Windows process trees (`CREATE_NEW_PROCESS_GROUP`, `taskkill`), kernel file locking (`msvcrt`), and transparent `.cmd`/`.bat` agent CLI execution. Windows Terminal is recommended for optimal interactive dashboard rendering. |

## Installation

Install Forge from PyPI:

```bash
pip install forge-orchestrator
```

### Development Installation

To contribute to Forge or install directly from the source repository:

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

## Quick Start

Get started with Forge in under 5 minutes:

```bash
# 1. Initialize Forge prompt templates and configuration in your project
forge init

# 2. Check installed CLI tools, git state, and stage configurations
forge doctor

# 3. Perform a standalone codebase audit for technical debt and vulnerabilities
forge critic "Audit authentication system and error handling"

# 4. Implement a task with step-by-step confirmation checkpoints
forge run "Add /healthz JSON endpoint with uptime and git commit SHA"

# 5. Inspect the execution artifacts and timeline in the interactive dashboard
forge dashboard
```

To run the pipeline completely unattended with autonomous self-repair:

```bash
forge auto "Implement rate limiting middleware using token bucket algorithm" --auto-commit
```

## Supported Adapters

Forge connects to installed CLI tools using native adapters. View all registered adapters and their capabilities at any time using `forge adapters`:

| Adapter | Binary | Default Model | Prompt Transport | Core Capabilities |
| :--- | :--- | :--- | :--- | :--- |
| `opencode` | `opencode` | Provider default | `stdin` | `code_read`, `code_edit`, `shell`, `git`, `structured_output`, `custom_flags` |
| `antigravity` (`agy`) | `agy` or `antigravity` | `gemini-3.7-flash-high` | `-p` flag | `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `custom_flags` |
| `codex` | `codex` | `gpt-5.6-terra` | `stdin` (`-`) | `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `tool_calling`, `custom_flags` |

## Documentation

For full operational details, configuration schemas, CLI command reference, testing engine guides, and troubleshooting workflows, consult the [Complete User Guide](https://github.com/Mathir14/Forge/blob/master/docs/USER_GUIDE.md).

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](https://github.com/Mathir14/Forge/blob/master/CONTRIBUTING.md) for guidelines on development environment setup, coding conventions, and submitting pull requests.

## License

Forge is licensed under the [MIT License](https://github.com/Mathir14/Forge/blob/master/LICENSE).


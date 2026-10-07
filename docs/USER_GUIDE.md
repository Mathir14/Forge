# Forge User Guide

Welcome to the complete user manual for **Forge**, an open-source, CLI-first multi-agent orchestration framework. This guide covers installation, configuration, command-line operations, pipeline stages, the testing engine, the Project Knowledge Base (PKB), the terminal dashboard, and troubleshooting procedures.

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Installation](#2-installation)
3. [Requirements](#3-requirements)
4. [Configuration](#4-configuration)
5. [Creating or Opening a Project](#5-creating-or-opening-a-project)
6. [CLI Commands](#6-cli-commands)
   - [Environment & Diagnostics](#environment--diagnostics)
   - [Run Inspection & Dashboard](#run-inspection--dashboard)
   - [Individual Stages](#individual-stages)
   - [Pipeline Execution](#pipeline-execution)
   - [Configuration Management](#configuration-management)
   - [Project Knowledge Base](#project-knowledge-base)
7. [Roles & Pipeline](#7-roles--pipeline)
8. [Dashboard](#8-dashboard)
9. [Adapters](#9-adapters)
10. [Knowledge System](#10-knowledge-system)
11. [Testing System](#11-testing-system)
12. [Common Workflows](#12-common-workflows)
13. [Troubleshooting](#13-troubleshooting)
14. [FAQ](#14-faq)

---

## 1. Introduction

Forge coordinates specialized artificial intelligence coding agents into a disciplined software engineering pipeline. Rather than relying on a single model to design, implement, and self-evaluate code in an unmonitored loop, Forge enforces separation of concerns across dedicated engineering roles:

- **Auditing**: Codebase Critic evaluates architecture and identifies technical debt.
- **System Design**: Software Architect defines module boundaries and interfaces without touching code.
- **Task Breakdown**: Project Planner organizes work into dependency-ordered implementation tasks.
- **Implementation**: Software Executor implements code changes and runs builds, linters, and unit tests.
- **Empirical Verification**: Empirical Tester boots running applications to exercise native public interfaces (Web browsers, APIs, CLIs, Libraries) and collects forensic evidence.
- **Adversarial Review**: Diff-First Reviewer evaluates Git diffs and concrete test reports before approving changes.

Forge connects directly to installed developer CLI tools—**OpenCode**, **Google Antigravity** (`agy`), and **OpenAI Codex** (`codex`)—as subprocesses. It requires no cloud API keys, vendor-specific SDK wrappers, or subscription proxy services.

---

## 2. Installation

Forge is published as a Python package (`forge-orchestrator`) on PyPI.

### Standard Installation

Install Forge into your active Python environment using `pip`:

```bash
pip install forge-orchestrator
```

### Development Installation

To contribute to Forge or install directly from the source repository along with development and testing dependencies:

```bash
git clone https://github.com/Mathir14/Forge.git
cd Forge
pip install -e ".[dev]"
```

### Windows Users

Forge officially supports both **Native Windows** and **WSL2** as first-class development environments:

#### Native Windows

Forge runs natively on Windows NT (Windows 10, Windows 11, and Windows Server 2019+):

- **Environment Prerequisites**:
  - Python (>= 3.10) and Git (>= 2.25) installed and available in your system `PATH`.
  - Windows Terminal (default in Windows 11) is recommended for truecolor ANSI rendering and box-drawing in the interactive dashboard.
- **Install Forge**:
  ```powershell
  pip install forge-orchestrator
  ```
  For development from source:
  ```powershell
  git clone https://github.com/Mathir14/Forge.git
  cd Forge
  pip install -e ".[dev]"
  ```
- **CLI Agent Tools on Windows**:
  - Install your desired AI coding tools (e.g. OpenCode via `npm install -g opencode`, Google Antigravity, or OpenAI Codex) directly in your Windows environment.
  - Tools installed via npm place `.cmd` and `.ps1` shims in `%APPDATA%\npm`. Ensure this directory is included in your user or system `PATH`.
- **Built-in Windows Abstractions**:
  - **Process Trees & Cleanup**: Forge launches child processes within isolated Windows process groups (`CREATE_NEW_PROCESS_GROUP`) and cleanly tears down entire subprocess trees using native `taskkill /F /T /PID`, with protective guards preventing Forge from terminating itself or system PIDs.
  - **Script Wrapping**: Executes `.cmd` and `.bat` wrappers automatically through `cmd.exe /c` without hanging or leaking file handles.
  - **Kernel Run Locking**: Enforces process mutual exclusion using native Windows `msvcrt.locking`, preserving full diagnostic readability of run owner metadata.
  - **Filesystem & Paths**: Seamlessly handles backslashes, drive letters (`C:\`), paths containing spaces, and semicolon (`;`) `PATH` delimiters.

#### WSL2 (Windows Subsystem for Linux)

If you prefer operating inside a Linux container or distribution on Windows:

- **Run as Linux**: Forge runs as a standard Linux process inside your WSL2 distribution (e.g., Ubuntu).
- **Install Forge inside WSL2**:
  ```bash
  pip install forge-orchestrator
  ```
  For development inside WSL2:
  ```bash
  git clone https://github.com/Mathir14/Forge.git
  cd Forge
  pip install -e ".[dev]"
  ```
- **Agent Tool Isolation**:
  > [!IMPORTANT]
  > **CLI Agent Tools in WSL2**: When running Forge inside WSL2, all CLI agent tools you use (**OpenCode**, **Antigravity**, or **Codex**) must be installed natively within the WSL2 Linux distribution (e.g. `npm install -g opencode` inside your WSL bash shell). Running Windows-installed agent binaries or npm wrappers exposed across the Windows mount (such as `/mnt/c/Users/.../AppData/Roaming/npm/opencode` or `.cmd` files) is not supported for the Linux Forge process and will be rejected with an informative diagnostic error. Ensure your Linux `$PATH` places native Linux binary directories (`/home/<user>/.opencode/bin`, `/usr/local/bin`) before any `/mnt/c/` PATH entries.

### Browser Automation Setup (Optional)

The Tester role can drive real headless browsers during web application verification. To enable web browser testing:

```bash
pip install playwright
playwright install chromium
```

### Verifying Installation

Verify the CLI entry point and run environment diagnostics:

```bash
forge --version
forge doctor
```

---

## 3. Requirements

| Component | Requirement | Details |
| :--- | :--- | :--- |
| **Operating System** | Linux, WSL2, macOS, Native Windows | Uses standard POSIX process groups and file locking (`flock`) on Linux/macOS, and Windows process trees (`CREATE_NEW_PROCESS_GROUP`, `taskkill`) and file locking (`msvcrt`) on Windows. |
| **Python** | `>= 3.10` | Standard CPython interpreter. |
| **Git** | `>= 2.25` | Must be available in `PATH` for baseline tracking, diff generation, and commits. |
| **CLI Agent Tools** | At least one installed | • **OpenCode** (`opencode`)<br>• **Google Antigravity** (`agy` or `antigravity`)<br>• **OpenAI Codex** (`codex`) |
| **Browser Testing** | Optional | `playwright` with Chromium browser binaries installed. |

---

## 4. Configuration

Forge uses a cascading configuration system. Configuration files are loaded and merged in ascending order of precedence, where later sources override earlier ones:

1. **Built-in Defaults**: Hardcoded defaults defined by Forge.
2. **Global User Configuration**: `~/.forge/config.yaml` (applies to all projects on your machine).
3. **Project Configuration**: `forge.yaml` in your project root.
4. **Local Workspace Configuration**: `.forge/config.yaml` in your project root (highest precedence).

### `forge.yaml` Schema Reference

Here is an annotated project configuration file:

```yaml
version: '2.0'

# Global defaults inherited by all stages unless explicitly overridden
defaults:
  adapter: opencode              # Default CLI adapter: opencode, antigravity, or codex
  model: null                    # Specific model alias, or null to use adapter default
  effort: medium                 # Reasoning effort: low, medium, high, max, or none
  timeout: 300                   # Execution timeout in seconds per stage
  auto_approve: false            # Automatically approve agent tool permissions
  extra_flags: {}                # Additional CLI flags passed to the agent binary

# Stage-specific overrides
stages:
  critic:
    effort: high
    timeout: 900
    post_run_override:           # Applied specifically to the closing post-execution audit (Stage 06)
      effort: high
      timeout: 600

  architect:
    effort: high

  planner:
    effort: high

  executor:
    adapter: antigravity         # Use Google Antigravity for implementation
    model: gemini-3.7-flash-high
    effort: high
    timeout: 1200
    auto_approve: false          # Set to true to permit unattended command execution

  tester:
    effort: high

  reviewer:
    effort: high

# Pipeline execution settings
execution:
  mode: interactive              # Default mode: interactive or autonomous
  auto_commit: false             # Automatically create git commits upon approved completion
  default_timeout: 300           # Global execution timeout fallback in seconds
```

### Configuration Keys & Types

| Section | Key | Type | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Top-Level** | `version` | `str` | `'2.0'` | Configuration schema version. |
| **`defaults`** | `adapter` | `str` | `'opencode'` | Default CLI tool adapter to invoke. |
| | `model` | `str?` | `null` | Model identifier passed to the CLI agent. |
| | `effort` | `str?` | `'medium'` | Reasoning effort tier (`low`, `medium`, `high`, `max`, `none`). |
| | `timeout` | `int` | `300` | Hard execution timeout in seconds. |
| | `auto_approve` | `bool` | `false` | Automatically approve agent tool execution permissions. |
| | `auth_method` | `str` | `'api_key'` | Authentication mechanism identifier. |
| | `auth_provider`| `str?` | `null` | Provider identifier. |
| | `auth_scopes`  | `str` | `""` | Granted authorization scopes. |
| | `auth_token_ttl`| `int` | `3600` | Authentication token validity duration in seconds. |
| | `extra_flags`  | `dict`| `{}` | Custom key-value flags passed as `--<key> <value>` to the CLI. |
| **`stages.<role>`** | `adapter` | `str?` | *inherited* | CLI adapter override for this specific role. |
| | `model` | `str?` | *inherited* | Model override for this specific role. |
| | `effort` | `str?` | *inherited* | Effort tier override for this specific role. |
| | `timeout` | `int?` | *inherited* | Timeout override for this specific role in seconds. |
| | `auto_approve` | `bool?`| *inherited* | Permission auto-approval override for this role. |
| | `extra_flags`  | `dict`| `{}` | Additional CLI flags merged with default flags. |
| | `post_run_override`| `dict?`| `null` | Dedicated override applied only to the post-run Critic (Stage 06). |
| **`execution`** | `mode` | `str` | `'interactive'` | Default execution mode (`interactive` or `autonomous`). |
| | `auto_commit` | `bool` | `false` | Automatically commit code changes upon approved completion. |
| | `default_timeout` | `int` | `300` | Global default stage execution timeout ceiling in seconds. |

### Boolean Coercion Rules

Forge parses configuration values strictly. Accepted truthy values are `true`, `yes`, `on`, `1`. Accepted falsy values are `false`, `no`, `off`, `0`, and empty strings. Ambiguous values cause a validation failure.

---

## 5. Creating or Opening a Project

### 1. Initialize Forge in a Repository

To prepare a project for Forge, navigate to your Git repository root and execute:

```bash
forge init
```

This creates the following directory structure:

```text
.
├── .ai/
│   ├── project/                  # High-level repository documentation loaded into prompts
│   │   ├── architecture.md       # Architectural boundaries, layering, and core components
│   │   ├── conventions.md        # Code formatting, style rules, and testing standards
│   │   ├── decisions.md          # Architectural decisions and rationale
│   │   └── roadmap.md            # Product milestones and known constraints
│   ├── roles/                    # Role system prompt templates
│   │   ├── critic.md
│   │   ├── architect.md
│   │   ├── planner.md
│   │   ├── executor.md
│   │   ├── tester.md
│   │   └── reviewer.md
│   └── templates/
│       └── protocol.md           # Machine protocol schema and instructions
├── .gitignore                    # Updated with entries for .forge/runs/ and .forge/cache/
└── forge.yaml                    # Generated project configuration file
```

### 2. Verify Environment & Setup

After initializing, run `forge doctor` to verify that your environment is properly configured:

```bash
forge doctor
```

`forge doctor` validates:
- Installed and missing CLI agent tools.
- Git repository initialization and active branch.
- `.ai/` directory presence and template file count.
- Resolved configuration defaults and assigned stage adapters.
- Browser automation status (Playwright and Chromium availability).

---

## 6. CLI Commands

Forge exposes 13 top-level commands and 2 command groups (`config` and `knowledge`).

```text
forge
├── adapters           # List registered adapters and capabilities
├── doctor             # Check CLI tools, git status, and environment
├── init               # Initialize .ai/ templates and forge.yaml
├── runs               # List historical execution runs
├── dashboard          # Interactive terminal UI dashboard
├── critic             # Run Codebase Critic audit (Stage 00 or 06)
├── architect          # Run Architect role (Stage 01)
├── planner            # Run Planner role (Stage 02)
├── execute            # Run Executor role (Stage 03)
├── test               # Run Tester role (Stage 04)
├── review             # Run Reviewer role (Stage 05)
├── run                # Run interactive pipeline with confirmation checkpoints
├── auto               # Run autonomous loop with self-repair
├── config             # Configuration management command group
│   ├── show           # Display current configuration
│   ├── get            # Query configuration value by key
│   ├── set            # Update configuration value by key
│   ├── edit           # Open configuration file in $EDITOR
│   ├── validate       # Validate configuration syntax and schema
│   ├── reset          # Reset configuration file to defaults
│   ├── auth-show      # Display authentication configuration
│   ├── auth-get       # Query authentication key
│   └── auth-set       # Update authentication key
└── knowledge          # Project Knowledge Base command group
    ├── list           # List repository knowledge facts
    ├── show           # Display details of a knowledge fact
    ├── lock           # Lock a fact against autonomous agent changes
    └── unlock         # Unlock a fact for agent updates
```

---

### Environment & Diagnostics

#### `forge doctor`

- **Purpose**: Performs comprehensive diagnostics on your local development environment, verifying CLI binaries, Git status, configuration validity, prompt templates, and browser automation drivers.
- **Syntax**: `forge doctor`
- **Arguments / Options**: None.
- **Example**:
  ```bash
  forge doctor
  ```
- **Expected Behaviour**: Outputs status lines indicating whether each component is present (`✓`) or missing (`✗`). If browser dependencies are missing, displays specific installation commands.

#### `forge init`

- **Purpose**: Initializes Forge in the current directory by installing default role templates, protocol definitions, starter project documentation, `.gitignore` exclusions, and a default `forge.yaml` file.
- **Syntax**: `forge init`
- **Arguments / Options**: None.
- **Example**:
  ```bash
  forge init
  ```
- **Expected Behaviour**: Creates `.ai/roles/`, `.ai/templates/`, and `.ai/project/`, writes `forge.yaml` if not already present, updates `.gitignore`, and reports the number of created templates.

#### `forge adapters`

- **Purpose**: Displays canonical CLI adapters, their default models, prompt transport mechanisms, and supported capabilities.
- **Syntax**: `forge adapters [OPTIONS]`
- **Arguments / Options**:
  - `--json`: Output adapter capabilities as a formatted JSON array.
- **Example**:
  ```bash
  forge adapters
  forge adapters --json
  ```
- **Expected Behaviour**: Displays a formatted list showing supported features (session resume, browser automation, streaming, structured output) and capability sets for each registered adapter.

---

### Run Inspection & Dashboard

#### `forge runs`

- **Purpose**: Lists historical runs stored in `.forge/runs/`, displaying run IDs, creation timestamps, final statuses, task summaries, and active adapters.
- **Syntax**: `forge runs [OPTIONS]`
- **Arguments / Options**:
  - `-n, --limit INTEGER`: Number of recent runs to display (default: `10`). Pass `0` to show all runs.
  - `-j, --json-output`: Output run history as formatted JSON.
- **Example**:
  ```bash
  forge runs
  forge runs -n 25
  forge runs --json-output
  ```
- **Expected Behaviour**: Displays a formatted table of historical runs ordered chronologically, showing total runs and displaying count.

#### `forge dashboard`

- **Purpose**: Launches an interactive full-terminal user interface (TUI) to inspect runs, browse timelines, read human markdown and machine JSON reports, review empirical test evidence, and query the Project Knowledge Base.
- **Syntax**: `forge dashboard [RUN_ID] [OPTIONS]`
- **Arguments / Options**:
  - `[RUN_ID]`: Optional run ID to inspect (e.g. `run-005` or `5`). Defaults to the most recent run.
  - `--render-once`: Render a single text snapshot of the dashboard and exit without entering interactive mode.
- **Example**:
  ```bash
  forge dashboard
  forge dashboard run-003
  forge dashboard --render-once
  ```
- **Expected Behaviour**: Enters interactive terminal mode with header, timeline sidebar, tabbed content view, and footer keybinding hints.

---

### Individual Stages

Each stage can be executed individually to inspect its prompt compilation, verify output, or debug specific roles.

#### `forge critic`

- **Purpose**: Executes the Codebase Critic role to analyze the codebase for architectural debt, security vulnerabilities, code smells, and performance bottlenecks.
- **Syntax**: `forge critic [TARGET] [OPTIONS]`
- **Arguments / Options**:
  - `[TARGET]`: Optional audit scope or focus area. Defaults to `"Audit and critique the codebase for architecture, security, code smells, and maintainability."`.
  - `--post-run`: Run the closing post-execution audit (Stage 06) on an existing run instead of the pre-run audit (Stage 00).
  - `--run TEXT`: Run ID to audit (required with `--post-run` unless targeting the latest run).
- **Example**:
  ```bash
  forge critic
  forge critic "Audit error handling and connection leaks in src/database/"
  forge critic --post-run --run run-004
  ```
- **Expected Behaviour**: Creates or updates a run directory, invokes the Critic adapter, parses the machine report, and writes `00_critic.md` / `00_critic.json` (or `06_critic.md` / `06_critic.json`).

#### `forge architect`

- **Purpose**: Executes the Architect role (Stage 01) to produce architectural designs, component boundaries, and interface contracts for a specified task. Creates a new run directory.
- **Syntax**: `forge architect TASK`
- **Arguments / Options**:
  - `TASK`: Required task description string.
- **Example**:
  ```bash
  forge architect "Design database connection pool with automatic retry and circuit breaking"
  ```
- **Expected Behaviour**: Allocates a new run directory (`run-XXX`), compiles instructions, invokes the configured Architect adapter, validates the emitted machine report, and writes `01_architect.md` and `01_architect.json`.

#### `forge planner`

- **Purpose**: Executes the Planner role (Stage 02) to decompose approved architectural designs into an ordered task breakdown with acceptance criteria and validation requirements.
- **Syntax**: `forge planner [OPTIONS]`
- **Arguments / Options**:
  - `--run TEXT`: Target Run ID containing approved Architect output (defaults to latest run).
- **Example**:
  ```bash
  forge planner
  forge planner --run run-008
  ```
- **Expected Behaviour**: Validates that `01_architect.json` exists with an approved status, compiles the task planning prompt, invokes the Planner adapter, and writes `02_planner.md` and `02_planner.json`.

#### `forge execute`

- **Purpose**: Executes the Executor role (Stage 03) to implement the approved plan directly within the repository codebase, running formatters, linters, builds, and test suites.
- **Syntax**: `forge execute [OPTIONS]`
- **Arguments / Options**:
  - `--run TEXT`: Target Run ID containing approved Planner output (defaults to latest run).
- **Example**:
  ```bash
  forge execute
  forge execute --run run-008
  ```
- **Expected Behaviour**: Verifies that `02_planner.json` is approved, compiles the execution prompt including project docs and plan, invokes the Executor adapter, captures command outputs and file modifications, and writes `03_executor.md` and `03_executor.json`.

#### `forge test`

- **Purpose**: Executes the Tester role (Stage 04) to empirically verify observable software behavior. Automatically detects project archetypes, supervises background dev servers, exercises user journeys via native drivers, captures forensic evidence, and issues a structured verdict (`PASS`, `FAIL`, `BLOCKED`, `NOT_TESTABLE`).
- **Syntax**: `forge test [OPTIONS]`
- **Arguments / Options**:
  - `--run TEXT`: Target Run ID containing Executor output (defaults to latest run).
- **Example**:
  ```bash
  forge test
  forge test --run run-008
  ```
- **Expected Behaviour**: Launches application runtime processes if required, executes planned test journeys within budget limits, writes screenshots and telemetry to `.forge/runs/<run_id>/evidence/`, and generates `04_tester.md` and `04_tester.json`.

#### `forge review`

- **Purpose**: Executes the Reviewer role (Stage 05) to perform an adversarial, diff-first quality audit. Verifies Executor claims against actual Git diffs and empirical Tester evidence.
- **Syntax**: `forge review [OPTIONS]`
- **Arguments / Options**:
  - `--run TEXT`: Target Run ID containing Executor and Tester output (defaults to latest run).
- **Example**:
  ```bash
  forge review
  forge review --run run-008
  ```
- **Expected Behaviour**: Compiles the review prompt containing the bounded Git diff, original requirements, Executor report, and Tester results, executes the Reviewer adapter, validates the emitted machine report, and writes `05_reviewer.md` and `05_reviewer.json`.

---

### Pipeline Execution

#### `forge run`

- **Purpose**: Executes the standard multi-agent pipeline with step-by-step confirmation checkpoints between stages (`Architect` → `Planner` → `Executor` → `Tester` → `Reviewer` → `Critic`).
- **Syntax**: `forge run [TASK] [OPTIONS]`
- **Arguments / Options**:
  - `[TASK]`: Task description (required unless `--from-critic` or `--run` is supplied).
  - `-c, --from-critic`: Resume from the latest Critic audit report, automatically setting the task to address identified findings.
  - `--run TEXT`: Resume an existing run by ID. Automatically skips already completed stages.
  - `--auto-commit`: Automatically commit changes to Git (`feat: <task>`) upon approved review.
  - `--no-critic`: Skip the closing post-execution codebase health audit.
- **Example**:
  ```bash
  forge run "Add token-bucket rate limiting middleware"
  forge run --from-critic
  forge run --run run-012
  forge run "Fix login bug" --no-critic
  ```
- **Expected Behaviour**: Executes each stage sequentially. After each stage completes with a success status, prompts: `Proceed to next stage (<STAGE>)? [Y/n]`. If paused by the user, saves run status as `PAUSED_AFTER_<STAGE>`.

#### `forge auto`

- **Purpose**: Executes the fully autonomous, unattended self-repair loop: `Architect` → `Planner` → `[Executor <-> (Tester -> Reviewer) Repair Loop]` → `Critic`.
- **Syntax**: `forge auto [TASK] [OPTIONS]`
- **Arguments / Options**:
  - `[TASK]`: Task description string (can also be provided via `-f` or `-c`).
  - `-f, --file FILE`: Path to a markdown requirements or product specification file.
  - `-c, --from-critic`: Resume from the latest Critic audit report.
  - `--run TEXT`: Resume an existing run by ID, skipping previously approved stages.
  - `-r, --max-retries INTEGER`: Maximum auto-repair iterations between Executor, Tester, and Reviewer (default: `3`, minimum: `1`).
  - `--auto-commit`: Automatically commit changes to Git (`feat: <task>`) upon approved verification and successful closing audit.
  - `--no-critic`: Skip the closing post-execution codebase health audit.
  - `-d, --dashboard`: Launch the live interactive terminal dashboard during execution.
- **Example**:
  ```bash
  forge auto "Add Redis caching for user sessions"
  forge auto -f specs/rate_limiting.md --max-retries 4 --auto-commit
  forge auto --from-critic --auto-commit
  forge auto --run run-014 -d
  ```
- **Expected Behaviour**: Executes Architect and Planner. Then runs Executor followed by Tester and Reviewer. If Tester fails or Reviewer returns `CHANGES_REQUIRED`, captures structured defects and feeds them back into the Executor prompt for up to `--max-retries` iterations. Upon approval, runs closing Critic, performs safe Git commits if requested, and reconciles knowledge base updates.

---

### Configuration Management

Manage configuration files safely with syntax validation and type checking.

#### `forge config show`
- **Purpose**: Displays the active merged configuration or raw project configuration.
- **Syntax**: `forge config show [OPTIONS]`
- **Options**:
  - `--raw`: Display only the raw project file without merged defaults.
  - `--json`: Output configuration in formatted JSON.
- **Example**: `forge config show`, `forge config show --raw`, `forge config show --json`

#### `forge config get`
- **Purpose**: Retrieves a specific configuration value using dot notation.
- **Syntax**: `forge config get KEY`
- **Example**: `forge config get defaults.adapter`, `forge config get stages.executor.timeout`

#### `forge config set`
- **Purpose**: Updates a configuration value in `forge.yaml` or global configuration with pre-save validation.
- **Syntax**: `forge config set KEY VALUE [OPTIONS]`
- **Options**:
  - `-g, --global`: Write to global `~/.forge/config.yaml` instead of project `forge.yaml`.
- **Example**: `forge config set stages.executor.timeout 1800`, `forge config set defaults.auto_approve true -g`

#### `forge config edit`
- **Purpose**: Opens the configuration file in your system `$EDITOR` with automatic pre-save validation to prevent syntax errors or invalid fields.
- **Syntax**: `forge config edit [OPTIONS]`
- **Options**:
  - `-g, --global`: Edit global `~/.forge/config.yaml`.
- **Example**: `forge config edit`

#### `forge config validate`
- **Purpose**: Validates YAML syntax, stage names, adapter compatibility, effort levels, and timeout values.
- **Syntax**: `forge config validate [OPTIONS]`
- **Options**:
  - `-p, --path PATH`: Validate a specific configuration file path.
- **Example**: `forge config validate`, `forge config validate -p custom-config.yaml`

#### `forge config reset`
- **Purpose**: Resets project or global configuration to Forge defaults.
- **Syntax**: `forge config reset [OPTIONS]`
- **Options**:
  - `-f, --force`: Skip interactive confirmation prompt.
  - `-g, --global`: Reset global `~/.forge/config.yaml`.
- **Example**: `forge config reset`, `forge config reset --force`

#### `forge config auth-show`, `auth-get`, `auth-set`
- **Purpose**: Inspect and configure authentication methods, providers, scopes, and token TTLs.
- **Syntax**:
  - `forge config auth-show`
  - `forge config auth-get KEY`
  - `forge config auth-set KEY VALUE [-g/--global]`
- **Example**:
  ```bash
  forge config auth-show
  forge config auth-set auth_method api_key
  forge config auth-set auth_token_ttl 7200
  ```

---

### Project Knowledge Base

Manage persistent repository facts in `.forge/knowledge/`.

#### `forge knowledge list`
- **Purpose**: Lists tracked knowledge facts in the repository with status indicators and titles.
- **Syntax**: `forge knowledge list [OPTIONS]`
- **Options**:
  - `-t, --type [architecture|feature|decision|unresolved]`: Filter facts by type.
  - `-s, --status [PROVISIONAL|VERIFIED|DISPUTED|HUMAN_LOCKED|DEPRECATED]`: Filter facts by status.
- **Example**:
  ```bash
  forge knowledge list
  forge knowledge list --type architecture
  forge knowledge list --status HUMAN_LOCKED
  ```

#### `forge knowledge show`
- **Purpose**: Displays full details of a knowledge fact, including title, summary, evidence paths, provenance, disputes, and payload.
- **Syntax**: `forge knowledge show FACT_ID`
- **Example**:
  ```bash
  forge knowledge show auth-rate-limiter
  ```

#### `forge knowledge lock`
- **Purpose**: Locks a knowledge fact as developer-verified truth (`HUMAN_LOCKED`). Prevents autonomous agent stages from modifying, disputing, or deprecating the fact.
- **Syntax**: `forge knowledge lock FACT_ID`
- **Example**:
  ```bash
  forge knowledge lock auth-jwt-rotation
  ```

#### `forge knowledge unlock`
- **Purpose**: Unlocks a previously locked fact, returning its status to `VERIFIED` and permitting agent updates during future runs.
- **Syntax**: `forge knowledge unlock FACT_ID`
- **Example**:
  ```bash
  forge knowledge unlock auth-jwt-rotation
  ```

---

## 7. Roles & Pipeline

Forge enforces a strict sequence of stages defined in `StageOrder`. Each stage produces paired human-readable (`.md`) and machine-readable (`.json`) artifacts saved under `.forge/runs/<run_id>/`:

```text
00_critic.md / .json       -> Pre-run audit
01_architect.md / .json    -> System architecture
02_planner.md / .json      -> Task breakdown & acceptance criteria
03_executor.md / .json     -> Code changes & test output
04_tester.md / .json       -> Empirical runtime test report
05_reviewer.md / .json     -> Adversarial review scorecard
06_critic.md / .json       -> Post-execution codebase health audit
```

### Stage Summary Table

| Seq | Role Name | Phase | Purpose | Success Statuses | Allowed Handoffs |
| :---: | :--- | :--- | :--- | :--- | :--- |
| **`00`** | **Critic** | `pre_run` | Audits codebase for architectural decay and vulnerabilities | `CRITIQUE_COMPLETE`, `APPROVED`, `SUCCESS`, `COMPLETED`, `PASSED` | `ARCHITECT`, `PLANNER`, `NONE` |
| **`01`** | **Architect** | `pre_run` | Defines system architecture, module boundaries, and interfaces | `APPROVED`, `READY`, `SUCCESS`, `COMPLETED` | `PLANNER`, `NONE` |
| **`02`** | **Planner** | `pre_run` | Breaks architecture into dependency-ordered tasks and criteria | `READY`, `APPROVED`, `SUCCESS`, `COMPLETED` | `EXECUTOR`, `ARCHITECT`, `NONE` |
| **`03`** | **Executor** | `pre_run` | Implements changes and executes build and automated test suites | `SUCCESS`, `APPROVED`, `COMPLETED` | `TESTER`, `REVIEWER`, `PLANNER`, `ARCHITECT`, `NONE` |
| **`04`** | **Tester** | `pre_run` | Evaluates running software through native interfaces | `PASS`, `APPROVED`, `SUCCESS`, `COMPLETED`, `NOT_TESTABLE` | `REVIEWER`, `EXECUTOR`, `NONE` |
| **`05`** | **Reviewer** | `pre_run` | Adversarially inspects Git diffs and empirical test evidence | `APPROVED` | `NONE`, `EXECUTOR`, `ARCHITECT` |
| **`06`** | **Critic** | `post_run` | Final sanity audit of uncommitted diffs before completion | `CRITIQUE_COMPLETE`, `APPROVED`, `SUCCESS`, `COMPLETED`, `PASSED` | `NONE` |

### Machine Protocol Schema

Every agent must format its verdict in a standardized YAML block:

```yaml
```yaml
ROLE: ARCHITECT
STATUS: APPROVED
HANDOFF: PLANNER
EXIT_CODE: 0
REASON: Architecture approved; modular boundaries and interfaces verified.
CONFIDENCE: HIGH
NEXT_ACTION: Planner decomposes architectural specifications into ordered tasks.
ISSUES:
  CRITICAL: []
  MAJOR: []
  MINOR: []
KNOWLEDGE_PROPOSALS: []
```
```

### Protocol Validation Rules

Forge validates every machine report before accepting stage completion:
1. `ROLE` must match the expected stage name.
2. `STATUS` must belong to the role's allowed status set.
3. `HANDOFF` must point to an authorized downstream role.
4. **Invariant 1**: A status of `BLOCKED` requires `HANDOFF: NONE`.
5. **Invariant 2**: A status of `REJECTED` requires `HANDOFF: NONE`.
6. **Invariant 3**: Intermediate stages (`Architect`, `Planner`) require active downstream handoffs upon success.
7. **Invariant 4**: `Executor` and `Planner` are strictly prohibited from emitting `KNOWLEDGE_PROPOSALS`.
8. Non-success statuses (`FAILED`, `BLOCKED`, `REJECTED`, `CHANGES_REQUIRED`, `UNKNOWN`) immediately halt standard pipeline execution.

---

## 8. Dashboard

The Forge terminal dashboard (`forge dashboard`) provides an interactive interface for monitoring active executions or inspecting historical runs.

```text
┌─ Forge Dashboard ────────────────────────────────────────────────────────────┐
│ Run: run-012 │ Status: APPROVED │ Task: Add rate limiting middleware         │
├──────────────┬───────────────────────────────────────────────────────────────┤
│ Stages       │ [1] Console   [2] Artifacts   [3] Tester   [4] PKB  [5] Comp  │
│              ├───────────────────────────────────────────────────────────────┤
│ ✓ 00_critic  │ # ROLE: REVIEWER                                              │
│ ✓ 01_arch    │                                                               │
│ ✓ 02_plan    │ ## Verification Summary                                       │
│ ✓ 03_exec    │ All acceptance criteria satisfied. Diff bounded to 142 lines.│
│ ✓ 04_test    │ Tester report confirmed 4/4 passing journeys.                 │
│ ▶ 05_review  │                                                               │
│ ✓ 06_critic  │                                                               │
├──────────────┴───────────────────────────────────────────────────────────────┤
│ [Tab] Focus  [1-5] View  [h/m] Markdown/JSON  [↑/↓] Navigate  [q] Quit       │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Views & Tabs

- **Header**: Displays Run ID, current status, task summary, active stage, and elapsed duration.
- **Timeline Sidebar**: Shows chronological stages with completion icons (`✓`, `▶`, `✗`), durations, and retry iteration labels.
- **Tab 1 — Console View**: Real-time streaming log of adapter stdout/stderr and tool execution events.
- **Tab 2 — Artifact View**: Stage outputs. Press `h` to view human-readable markdown (`.md`) or `m` to inspect structured machine reports (`.json`).
- **Tab 3 — Tester View**: Breakdown of planned test journeys, step results, detected defects, and evidence file locations.
- **Tab 4 — PKB View**: Project Knowledge Base facts filtered by type and status, with dispute logs.
- **Tab 5 — Compare View**: Side-by-side or unified diff comparison between attempts or across different runs.

### Keybinding Reference

| Key | Action |
| :--- | :--- |
| `Tab` | Switch focus between timeline sidebar and main content area |
| `1` – `5` | Switch active tab (Console, Artifacts, Tester, PKB, Compare) |
| `h` | In Artifacts tab, switch to Human Markdown view (`.md`) |
| `m` | In Artifacts tab, switch to Machine Protocol JSON view (`.json`) |
| `Up` / `k` | Move selection up in timeline or scroll up in content |
| `Down` / `j` | Move selection down in timeline or scroll down in content |
| `PageUp` / `PageDown` | Scroll content by full page |
| `q` | Quit dashboard |
| `Ctrl+C` | Abort running process (in live mode) or exit dashboard |

### Terminal Compatibility & Windows Environment

- **Recommended Terminal**: On Windows, modern **Windows Terminal** (or VS Code's integrated terminal) is strongly recommended. Windows Terminal natively supports truecolor 24-bit ANSI escape codes, full UTF-8 Unicode glyphs, and box-drawing characters used by Rich.
- **Legacy Console (`conhost.exe`)**: Traditional `cmd.exe` or PowerShell running in legacy Windows Console Host (`conhost.exe`) may experience degraded rendering, character clipping, or font fallback issues for box-drawing glyphs. If running inside a legacy console, ensure your console code page is UTF-8 (`chcp 65001`) and a TrueType font (such as Cascadia Code or Consolas) is selected.
- **Cancellation & Navigation**: Press `q` to cleanly exit inspection mode at any time without affecting run state. In live execution mode, pressing `Ctrl+C` sends a cancellation signal to the active run, cleanly terminating running agent subprocesses before exiting.

---

## 9. Adapters

Forge connects to installed developer CLI tools through native adapters.

### Supported Adapters

#### 1. OpenCode (`opencode`)
- **Binary**: `opencode`
- **Execution**: `opencode run [-m MODEL] [--auto] [--variant EFFORT] [EXTRA_FLAGS]`
- **Prompt Transport**: Standard input (`stdin`).
- **Capabilities**: `code_read`, `code_edit`, `shell`, `git`, `structured_output`, `custom_flags`.

#### 2. Google Antigravity (`antigravity` / `agy`)
- **Binary**: `agy` (or `antigravity`)
- **Execution**: `agy -p <PROMPT> --output-format text [--model MODEL] [--effort EFFORT] [--dangerously-skip-permissions] [--print-timeout <TIMEOUT>s] [EXTRA_FLAGS]`
- **Default Model**: `gemini-3.7-flash-high`
- **Default Effort**: `high`
- **Prompt Transport**: Command-line argument (`-p`). Bounded by OS argument limits (`MAX_PROMPT_BYTES = 130000`).
- **Capabilities**: `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `custom_flags`.

#### 3. OpenAI Codex (`codex`)
- **Binary**: `codex`
- **Execution**: `codex exec --color never [-C WORKDIR] [-m MODEL] [-c model_reasoning_effort="<EFFORT>"] [--dangerously-bypass-approvals-and-sandbox] [EXTRA_FLAGS] -`
- **Default Model**: `gpt-5.6-terra`
- **Default Effort**: `medium`
- **Prompt Transport**: Standard input (`stdin` via `-`).
- **Capabilities**: `code_read`, `code_edit`, `shell`, `git`, `long_running`, `structured_output`, `tool_calling`, `custom_flags`.

### Capability Pre-Flight Checks

Before invoking any stage, Forge compares the stage's required capabilities against the adapter's declared capabilities. If an adapter lacks a required capability (for example, attempting to execute the Executor stage with an adapter lacking `code_edit` or `shell`), Forge halts immediately with a `CapabilityValidationError` before any execution occurs.

### Security & Auto-Approval

By default, `auto_approve` is set to `false`. When running with `auto_approve: true`:
- OpenCode receives `--auto`.
- Antigravity receives `--dangerously-skip-permissions`.
- Codex receives `--dangerously-bypass-approvals-and-sandbox`.

> [!WARNING]
> Enabling `auto_approve: true` permits CLI agents to execute shell commands, install packages, and write files without human confirmation. Forge displays a prominent terminal warning whenever `auto_approve` is active.

---

## 10. Knowledge System

The **Project Knowledge Base (PKB)** maintains persistent repository facts across runs, eliminating the need to re-explain architectural decisions, conventions, or discovered tech debt to agents.

### Knowledge Store Layout

Knowledge facts are stored in YAML projections under `.forge/knowledge/`:

- `.forge/knowledge/architecture.yaml`: Core system boundaries, module contracts, and invariants.
- `.forge/knowledge/features.yaml`: User-facing capabilities and observable behaviors.
- `.forge/knowledge/decisions.yaml`: Approved architectural decisions, library selections, and conventions.
- `.forge/knowledge/unresolved.yaml`: Identified tech debt, known bugs, and security findings.

### Fact Schema

```yaml
id: auth-jwt-rotation
type: decision
title: JWT Authentication with Refresh Token Rotation
status: VERIFIED                  # PROVISIONAL, VERIFIED, DISPUTED, HUMAN_LOCKED, DEPRECATED
summary: Short-lived access tokens (15m) paired with rotating refresh tokens stored in Redis.
evidence:
  - src/auth/tokens.py
  - tests/test_tokens.py
provenance:
  source: human                   # inferred, observed, verified, human
  run_id: run-005
  role: architect
disputes: []
payload: {}
```

### Knowledge Update Workflow

1. **Staged Proposals**: When Critic, Architect, Tester, or Reviewer emits `KNOWLEDGE_PROPOSALS` in its machine report, proposals are validated against role authority rules:
   - `Architect` can assert or verify `architecture`, `feature`, and `decision`.
   - `Critic` can assert discovered `feature` or `unresolved` tech debt.
   - `Tester` can assert `unresolved` defects and verify observable `feature` behaviors.
   - `Reviewer` can assert conventions (`decision`) and verify contracts.
   - `Executor` and `Planner` are strictly prohibited from modifying knowledge.
2. **Reconciliation**: When a run completes successfully under `RunLock`, `KnowledgeReconciler` deterministically merges proposals into `.forge/knowledge/`.
3. **Human Sovereignty**: Facts locked via `forge knowledge lock <id>` receive status `HUMAN_LOCKED`. No agent proposal is permitted to modify or dispute a locked fact.
4. **Context Projection**: During prompt compilation, `KnowledgeSelector` scores repository facts against touched files and task keywords, injecting high-priority facts into the prompt within character budgets.

---

## 11. Testing System

The **Tester** stage (`04_tester`) is powered by `TesterEngine`, an empirical black-box testing engine that validates observable runtime behavior.

### Testing Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                        TesterEngine                         │
├──────────────────────────────┬──────────────────────────────┤
│ 1. Archetype Auto-Detection  │ Web SPA, API, CLI, Library   │
│ 2. Runtime Supervision       │ Background server lifecycle  │
│ 3. Journey Planning          │ 4-tier prioritized journeys  │
│ 4. Interaction Drivers       │ Browser, API, CLI, Library   │
│ 5. Resource Budgets          │ Timeout, journey & step caps │
│ 6. Forensic Evidence Bundle  │ Screenshots, logs, scripts   │
│ 7. Verdict & Coverage        │ PASS, FAIL, BLOCKED metrics  │
└──────────────────────────────┴──────────────────────────────┘
```

### 1. Archetype Auto-Detection

`ArchetypeDetector` inspects package manifests, configuration files, and directory layouts to determine how the project should be exercised:
- **`WEB_SPA`**: Detected via `package.json`, `vite.config.*`, `next.config.*`, etc.
- **`API`**: Detected via FastAPI, Flask, Django, Express, or route decorators.
- **`CLI`**: Detected via `click`, `argparse`, `pyproject.toml` console scripts, or CLI bin files.
- **`LIBRARY`**: Pure Python, TypeScript, or Go libraries tested via direct imports and export assertions.

### 2. Runtime Supervision

For Web and API applications, `RuntimeSupervisor` manages the local server lifecycle:
- Launches the application in an isolated POSIX process group (`os.setsid`).
- Polls ports and HTTP endpoints with exponential backoff until ready (up to 25s timeout).
- Captures standard output and error telemetry.
- Guarantees clean process tree teardown (`SIGTERM` followed by `SIGKILL` if needed).

### 3. Interaction Drivers

- **Web Driver** (`WebInteractionDriver`): Drives Chromium via Playwright (or in-memory mock fallback). Navigates URLs, clicks buttons, enters text, asserts DOM mutations, detects dead clicks, captures unhandled browser console exceptions, and records full-page screenshots.
- **API Driver** (`ApiInteractionDriver`): Probes REST/GraphQL endpoints, manages authorization headers and session cookies, and asserts response schemas and status codes.
- **CLI Driver** (`CliInteractionDriver`): Invokes commands with arguments and stdin payloads, asserts exit codes, and checks error streams.
- **Library Driver** (`LibraryInteractionDriver`): Imports modules and verifies public functions and type contracts.

### 4. Forensic Evidence Bundle

All test artifacts are organized in `.forge/runs/<run_id>/evidence/`:

```text
.forge/runs/run-012/evidence/
├── screenshots/
│   ├── journey_01_step_02.png
│   └── defect_nav_dead_click.png
├── telemetry/
│   ├── console.log               # Browser console messages
│   ├── failed_requests.json      # HTTP 4xx/5xx network failures
│   ├── page_errors.json          # Uncaught JavaScript exceptions
│   ├── process_stdout.log        # Dev server standard output
│   └── process_stderr.log        # Dev server standard error
└── reproduction/
    ├── repro_journey_01.sh       # Executable reproduction script
    └── repro_defect_01.py        # Standalone Python reproduction script
```

### 5. Verdict & Coverage

`04_tester.json` records:
- **Verdict**: `PASS`, `FAIL`, `BLOCKED` (dev server failed to start), or `NOT_TESTABLE`.
- **Coverage**: Planned journeys, executed journeys, passed journeys, failed journeys, and confidence rating (`HIGH`, `MEDIUM`, `LOW`).

---

## 12. Common Workflows

### Workflow 1: Auditing an Existing Codebase

Perform an architectural and security audit before planning new features:

```bash
# Audit entire codebase
forge critic

# Audit a specific subsystem
forge critic "Audit authentication, session handling, and token storage"

# Inspect the audit findings
forge dashboard
```

### Workflow 2: Interactive Feature Development

Implement a feature with confirmation checkpoints between every stage:

```bash
# 1. Start the interactive pipeline
forge run "Add /metrics Prometheus endpoint"

# 2. Forge executes Architect and displays 01_architect summary
#    Prompt: Proceed to next stage (PLANNER)? [Y/n]

# 3. Forge executes Planner and displays 02_planner summary
#    Prompt: Proceed to next stage (EXECUTOR)? [Y/n]

# 4. Forge executes Executor, Tester, and Reviewer
#    Reviewer verifies git diff and tester evidence

# 5. Pipeline completes and saves all artifacts
```

### Workflow 3: Autonomous Implementation with Self-Repair

Let Forge design, implement, test, and self-repair code unattended:

```bash
forge auto "Implement Redis cache for database queries" --max-retries 3 --auto-commit
```

If the Tester discovers broken journeys or the Reviewer finds unfulfilled criteria, Forge feeds structured issue reports back to the Executor for up to 3 repair iterations.

### Workflow 4: Resuming Interrupted Work

If execution is paused or interrupted, resume from the exact point of interruption:

```bash
# Check the ID of the run
forge runs -n 5

# Resume standard pipeline (skips completed stages)
forge run --run run-015

# Resume autonomous loop
forge auto --run run-015 --max-retries 3
```

### Workflow 5: Resuming from an Audit Report

Turn Critic audit findings directly into an implementation run:

```bash
# Step 1: Run the audit
forge critic "Audit SQL query performance and missing indexes"

# Step 2: Implement fixes directly from the audit report
forge auto --from-critic --auto-commit
```

### Workflow 6: Specification-Driven Implementation

Feed a comprehensive product requirements document (PRD) to Forge:

```bash
forge auto --file specs/webhook_system.md --max-retries 4 --auto-commit
```

---

## 13. Troubleshooting

### 1. Run Ownership Conflict (`RunOwnershipError`)

- **Symptom**: `RunOwnershipError: Run 'run-XXX' is currently locked by an active Forge process.`
- **Cause**: Another running process is executing on that run directory, or a previous run crashed without releasing its kernel lock.
- **Resolution**:
  - Check the owner details printed in the terminal error (PID, Hostname, User, Started timestamp).
  - If the process is still running, wait for it to finish or terminate it cleanly:
    - **Linux/macOS/WSL2**: `kill <PID>`
    - **Native Windows**: `taskkill /F /PID <PID>` (or inspect active processes via `tasklist | findstr forge`)
  - If the process terminated abnormally or the recorded PID no longer exists, simply re-running your command will automatically detect the dead PID and safely recover the stale lock without manual file deletion.

### 2. Non-Interactive Execution Deadlock (`StageValidationError`)

- **Symptom**: `StageValidationError: Stage 'executor' cannot run non-interactively with auto_approve=false.`
- **Cause**: Forge was run in a non-interactive shell (such as a CI/CD pipeline or redirected stdin) with `auto_approve: false`. The CLI agent would hang waiting for user keyboard input that cannot arrive.
- **Resolution**:
  - Run the command in an interactive terminal (TTY).
  - Or enable auto-approval: `forge config set stages.executor.auto_approve true`.

### 3. Missing CLI Adapter Binary

- **Symptom**: `Adapter tool 'opencode' is not installed or not in PATH.`
- **Cause**: The configured CLI binary cannot be located by the operating system.
- **Resolution**:
  - Run `forge doctor` to check tool availability.
  - Install the tool or add its directory to your system `PATH`.
  - Alternatively, configure an available tool in `forge.yaml` (e.g. `adapter: antigravity` or `adapter: codex`).

### 4. Missing Adapter Capabilities (`CapabilityValidationError`)

- **Symptom**: `Capability validation error for stage 'executor': Configured adapter is missing: code_edit, shell.`
- **Cause**: The adapter assigned to a stage does not support the capabilities required by that stage.
- **Resolution**:
  - Run `forge adapters` to inspect which adapters provide which capabilities.
  - Assign an adapter that supports code editing and shell execution (`antigravity`, `opencode`, or `codex`) to the Executor stage in `forge.yaml`.

### 5. Antigravity Prompt Transport Size Exceeded

- **Symptom**: `Prompt size (135000 bytes) exceeds Antigravity CLI argv transport limit (130000 bytes).`
- **Cause**: Google Antigravity receives prompts via the `-p` command-line argument, which is bounded by the operating system argument string limit (`MAX_ARG_STRLEN`).
- **Resolution**:
  - Break tasks into smaller units of work.
  - Use `opencode` or `codex` (which accept prompts via `stdin` without argument limits) for very large context prompts.

### 6. Application Server Startup Failure in Tester (`BLOCKED`)

- **Symptom**: Tester stage finishes with status `BLOCKED` and message `"Application runtime failed to become ready."`
- **Cause**: The application dev server crashed on startup, encountered a missing dependency, or did not respond on the expected port within 25 seconds.
- **Resolution**:
  - Inspect server logs in `.forge/runs/<run_id>/evidence/telemetry/process_stdout.log` and `process_stderr.log`.
  - Fix missing environment variables or dependencies in your project.

### 7. Auto-Commit Skipped for Mixed-Ownership Files

- **Symptom**: `⚠️ Skipped auto-commit for mixed-ownership file(s)`
- **Cause**: A file modified by Forge already had uncommitted user changes before the Forge run started. Forge refuses to automatically commit mixed files to prevent accidentally committing unrelated user work.
- **Resolution**:
  - Review the changed file using `git diff <file>`.
  - Stage and commit your changes manually using `git commit`.

### 8. Browser Automation Falling Back to Mock Driver

- **Symptom**: `forge doctor` displays `⚠️ Browser automation is unavailable. Web/UI interactions will fall back to MockBrowserDriver.`
- **Cause**: `playwright` or Chromium browser binaries are not installed.
- **Resolution**:
  - Install Playwright: `pip install playwright`.
  - Install Chromium: `playwright install chromium`.

---

## 14. FAQ

### Can I use Forge with a single CLI tool?
Yes. If you only have OpenCode installed, configure `opencode` for all stages in `forge.yaml`. If you only have Google Antigravity installed, configure `antigravity` across stages.

### Where does Forge store execution history?
All runs, reports, and evidence are stored in the `.forge/` directory in your project root:
- Runs and stage reports: `.forge/runs/run-XXX/`
- Test screenshots and telemetry: `.forge/runs/run-XXX/evidence/`
- Project Knowledge Base: `.forge/knowledge/`

### Does Forge commit secrets or `.env` files?
No. Forge's Git service enforces exclusions that ignore `.env`, `.env*`, and `.forge/` from diffs, changed file lists, staging, and automated commits.

### How does Forge handle concurrent execution?
Forge enforces kernel-backed file locking (`run.lock`) using `flock` on Linux/macOS and `msvcrt` on Windows. Only one process can execute against a specific run directory at a time. If an active process holds the lock, subsequent commands abort immediately with owner diagnostic information.

### What happens if a run is interrupted?
Run state and stage artifacts are written atomically to disk using temporary files and filesystem renames. If a run is interrupted, all completed stages are safely preserved. You can resume at any time using `forge run --run <run_id>` or `forge auto --run <run_id>`.

### Does Forge support Windows?
Yes. Forge supports both **Native Windows** and **WSL2**.
- On **Native Windows**, Forge manages processes using Windows process groups and `taskkill` process-tree termination, locks runs using `msvcrt.locking`, and runs agent CLI wrappers (`.cmd`/`.bat`) seamlessly via `cmd.exe /c`. For the best terminal dashboard experience, Windows Terminal is recommended.
- On **WSL2**, Forge runs as a Linux process. Note that when running inside WSL2, AI agent CLI tools (such as OpenCode) must be installed natively within your WSL Linux distribution; Windows host binaries exposed via `/mnt/c` are not supported.

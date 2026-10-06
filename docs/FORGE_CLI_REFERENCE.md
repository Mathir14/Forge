# Forge CLI Complete Reference Manual

**Forge Version Inspected:** `0.1.0b5`  
**Inspection Date:** 2026-10-04  
**Target Repository:** `/home/mathir/Stress-Testing-Framework`  
**Verified Source Files:**
- `src/forge/cli.py` (Command routing, argument parser, CLI error handling)
- `src/forge/core/config.py` (Configuration cascade, schema, validation, inheritance)
- `src/forge/stages/stage.py` (Stage execution engine, streaming worker, timeouts, process tracking)
- `src/forge/stages/definition.py` (Authoritative stage definitions, ordering, success statuses)
- `src/forge/adapters/base.py` (Adapter base abstraction, process lifecycle, signal handlers)
- `src/forge/adapters/registry.py` (Canonical adapter registry, capabilities, tool probing)
- `src/forge/adapters/opencode.py` (OpenCode adapter, dynamic variant discovery via `model.list`)
- `src/forge/adapters/antigravity.py` (Antigravity CLI adapter, argv transport, `--effort` handling)
- `src/forge/adapters/codex.py` (OpenAI Codex CLI adapter, command construction)
- `src/forge/dashboard/app.py` (Terminal UI layout, keyboard bindings, live event bus)
- `src/forge/storage/run_manager.py` (Run directory structure, atomic artifact persistence)
- `src/forge/storage/run_lock.py` (Exclusive run locking, ownership conflict diagnostics)
- `src/forge/storage/knowledge.py` (Project Knowledge Base YAML projections, fact locking)
- `src/forge/storage/git.py` (Git baseline capture, change attribution, auto-commit guards)

---

## Table of Contents

1. [Architectural Overview & Global CLI Semantics](#1-architectural-overview--global-cli-semantics)
2. [Command Matrix](#2-command-matrix)
3. [Safety & Destructiveness Classification](#3-safety--destructiveness-classification)
4. [The Multi-Agent Orchestration Pipeline](#4-the-multi-agent-orchestration-pipeline)
5. [Top-Level Commands Reference](#5-top-level-commands-reference)
   - [`forge doctor`](#forge-doctor)
   - [`forge adapters`](#forge-adapters)
   - [`forge init`](#forge-init)
   - [`forge runs`](#forge-runs)
   - [`forge dashboard`](#forge-dashboard)
   - [`forge critic`](#forge-critic)
   - [`forge architect`](#forge-architect)
   - [`forge planner`](#forge-planner)
   - [`forge execute`](#forge-execute)
   - [`forge test`](#forge-test)
   - [`forge review`](#forge-review)
   - [`forge run`](#forge-run)
   - [`forge auto`](#forge-auto)
6. [Subcommand Groups](#6-subcommand-groups)
   - [`forge config`](#forge-config-group)
     - `auth-show`, `auth-get`, `auth-set`, `show`, `get`, `set`, `edit`, `validate`, `reset`
   - [`forge knowledge`](#forge-knowledge-group)
     - `list`, `show`, `lock`, `unlock`
7. [Adapter Architectures & Model/Effort Translation](#7-adapter-architectures--modeleffort-translation)
8. [Configuration Hierarchy & Precedence Rules](#8-configuration-hierarchy--precedence-rules)
9. [Run Lifecycle, Lock Ownership, & Artifact Layout](#9-run-lifecycle-lock-ownership--artifact-layout)
10. [Diagnostic & Troubleshooting Runbook](#10-diagnostic--troubleshooting-runbook)
11. [Cheat Sheet](#11-cheat-sheet)

---

## 1. Architectural Overview & Global CLI Semantics

Forge is a CLI-first multi-agent orchestration framework designed for structured, verifiable software engineering workflows. It coordinates specialized AI roles (Critic, Architect, Planner, Executor, Tester, Reviewer) across isolated execution stages.

### Global Syntax

```bash
forge [OPTIONS] COMMAND [ARGS]...
```

### Global Options

| Option | Type | Description |
|---|---|---|
| `--version` | Flag | Displays the installed Forge version (`forge, version 0.1.0b5`) and exits immediately. |
| `--help` | Flag | Displays top-level command list and usage summary. |

### Fundamental Design Principle: CLI Flags vs. Configuration

> [!IMPORTANT]
> In Forge v0.1.0b5, individual stage commands (`architect`, `planner`, `execute`, `test`, `review`, `critic`) **do not accept inline `--adapter`, `--model`, `--effort`, or `--timeout` flags**.
> 
> Stage runtime settings (adapter selection, model IDs, reasoning effort, timeout limits, and approval policies) are configured declaratively in `forge.yaml` (or via `forge config set stages.<stage>.<property> <value>`).
> 
> CLI stage commands only accept positional task targets and run resumption switches (`--run <ID>`, `--post-run`). Pipeline commands (`forge run`, `forge auto`) accept pipeline execution flags (`--from-critic`, `--run`, `--auto-commit`, `--no-critic`, `--max-retries`, `--file`, `--dashboard`).

---

## 2. Command Matrix

Forge exposes **15 top-level commands** and **13 subcommands** (total **28 distinct command entry points**):

| Command | Arguments | Options | Interactive | Executes Agent | Modifies Workspace | Writes `.forge/` | Typical Purpose |
|---|---|---|:---:|:---:|:---:|:---:|---|
| `forge doctor` | None | `--help` | No | No | No | No | Check tools, git, python env, browser drivers |
| `forge adapters` | None | `--json`, `--help` | No | No | No | No | Probes and lists registered adapter capabilities |
| `forge init` | None | `--help` | No | No | Yes (`.ai/`, `forge.yaml`, `.gitignore`) | Yes | Scaffold initial project configuration and role templates |
| `forge runs` | None | `-n/--limit`, `-j/--json-output`, `--help` | No | No | No | No | List historical execution runs and statuses |
| `forge dashboard` | `[RUN_ID]` | `--render-once`, `--help` | Yes (Terminal TUI) | No | No | No | Inspect live or historical run timeline and artifacts |
| `forge critic` | `[TARGET]` | `--post-run`, `--run TEXT`, `--help` | No | Yes | Potential (via tools) | Yes (`00_critic.*` or `06_critic.*`) | Pre-run codebase audit or post-run closing audit |
| `forge architect` | `TASK` (req) | `--help` | No | Yes | No | Yes (`01_architect.*`) | Synthesize architectural specification from task |
| `forge planner` | None | `--run TEXT`, `--help` | No | Yes | No | Yes (`02_planner.*`) | Break architecture into actionable work packages |
| `forge execute` | None | `--run TEXT`, `--help` | No | Yes | Yes (Writes code) | Yes (`03_executor.*`) | Implement approved plan into codebase |
| `forge test` | None | `--run TEXT`, `--help` | No | Yes | Yes (Test harnesses, artifacts) | Yes (`04_tester.*`) | Black-box behavioral evaluation & browser testing |
| `forge review` | None | `--run TEXT`, `--help` | No | Yes | No | Yes (`05_reviewer.*`) | Adversarial security and correctness code review |
| `forge run` | `[TASK]` | `-c/--from-critic`, `--run TEXT`, `--auto-commit`, `--no-critic`, `--help` | Yes (Prompt gates) | Yes | Yes | Yes | Standard step-by-step pipeline with manual confirmation |
| `forge auto` | `[TASK]` | `-f/--file`, `-c`, `--run`, `-r/--max-retries`, `--auto-commit`, `--no-critic`, `-d/--dashboard`, `--help` | No (or TUI if `-d`) | Yes | Yes | Yes | Fully autonomous self-repair loop |
| `forge config show` | None | `--raw`, `--json`, `--help` | No | No | No | No | Display effective configuration |
| `forge config get` | `KEY` (req) | `--help` | No | No | No | No | Query single config key using dot notation |
| `forge config set` | `KEY` `VALUE` | `-g/--global`, `--help` | No | No | No | Yes (`forge.yaml`) | Mutate configuration with schema validation |
| `forge config edit` | None | `-g/--global`, `--help` | Yes (`$EDITOR`) | No | No | Yes (`forge.yaml`) | Interactive editor with pre-save syntax validation |
| `forge config validate` | None | `-p/--path`, `--help` | No | No | No | No | Check configuration syntax, adapters, and stages |
| `forge config reset` | None | `-f/--force`, `-g/--global`, `--help` | Yes (Prompt) | No | No | Yes (`forge.yaml`) | Reset configuration to factory defaults |
| `forge config auth-show` | None | `--help` | No | No | No | No | Display authentication settings |
| `forge config auth-get` | `KEY` (req) | `--help` | No | No | No | No | Query auth config property |
| `forge config auth-set` | `KEY` `VALUE` | `-g/--global`, `--help` | No | No | No | Yes (`forge.yaml`) | Mutate auth config property |
| `forge knowledge list` | None | `-t/--type`, `-s/--status`, `--help` | No | No | No | No | Filter and list repository knowledge facts |
| `forge knowledge show` | `FACT_ID` (req) | `--help` | No | No | No | No | Inspect detailed provenance and disputes for a fact |
| `forge knowledge lock` | `FACT_ID` (req) | `--help` | No | No | No | Yes (`.forge/knowledge/`) | Lock fact to `HUMAN_LOCKED` sovereign truth |
| `forge knowledge unlock` | `FACT_ID` (req) | `--help` | No | No | No | Yes (`.forge/knowledge/`) | Return fact to `VERIFIED` status for agent updates |

---

## 3. Safety & Destructiveness Classification

| Command | Read-Only | Executes Agent | Modifies Workspace Files | Git Effects | Potentially Destructive / Mutation Risk |
|---|:---:|:---:|:---:|:---:|---|
| `forge doctor` | **Yes** | No | No | Reads status | **None** |
| `forge adapters` | **Yes** | No | No | None | **None** |
| `forge runs` | **Yes** | No | No | None | **None** |
| `forge dashboard` | **Yes** | No | No | None | **None** |
| `forge config show/get/validate/auth-show/auth-get` | **Yes** | No | No | None | **None** |
| `forge knowledge list/show` | **Yes** | No | No | None | **None** |
| `forge init` | No | No | Creates `.ai/`, `forge.yaml`, `.gitignore` | Modifies `.gitignore` | **Low**: Will not overwrite existing role files or forge.yaml. |
| `forge config set/edit/reset/auth-set` | No | No | Modifies `forge.yaml` or `~/.forge/config.yaml` | None | **Medium**: Can misconfigure timeouts/adapters; guarded by schema validation. |
| `forge knowledge lock/unlock` | No | No | Modifies `.forge/knowledge/*.yaml` | None | **Low**: Changes fact lifecycle status metadata. |
| `forge critic` | No | **Yes** | Low (Inspects files, may run background shell tools) | Baseline capture | **Medium**: Invoked tools could alter files if auto-approved. |
| `forge architect` | No | **Yes** | No (Generates specification artifact in `.forge/runs/`) | Baseline capture | **Low**: Read-only against repository code. |
| `forge planner` | No | **Yes** | No (Generates plan artifact in `.forge/runs/`) | Baseline capture | **Low**: Read-only against repository code. |
| `forge test` | No | **Yes** | Yes (May execute scripts, create test logs/artifacts) | Baseline capture | **Medium**: Executes code under test. |
| `forge review` | No | **Yes** | No (Generates audit review artifact in `.forge/runs/`) | Baseline capture | **Low**: Read-only against repository code. |
| `forge execute` | No | **Yes** | **Yes (Directly edits/creates project source code)** | Baseline capture | **High**: Overwrites and refactors codebase files. |
| `forge run` | No | **Yes** | **Yes (Executes full pipeline: Architect -> Planner -> Executor -> Tester -> Reviewer -> Critic)** | Optional auto-commit | **High**: Edits files; prompts before every stage transition. |
| `forge auto` | No | **Yes** | **Yes (Full pipeline with autonomous self-repair iteration loop)** | Optional auto-commit | **High**: Edits files autonomously up to `max_retries` attempts. |

---

## 4. The Multi-Agent Orchestration Pipeline

Forge stages are ordered sequentially with strict prerequisite gates and status protocols:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        PRE-PIPELINE (Optional)                         │
│  forge critic [TARGET] ────────► .forge/runs/run-XXX/00_critic.md      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ forge run -c / forge auto -c
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        STANDARD / AUTO PIPELINE                        │
│                                                                        │
│  [Stage 01]  forge architect "TASK"                                    │
│                     │  (Artifact: 01_architect.md & .json)             │
│                     ▼                                                  │
│  [Stage 02]  forge planner --run <ID>                                  │
│                     │  (Artifact: 02_planner.md & .json)               │
│                     ▼                                                  │
│  [Stage 03]  forge execute --run <ID>  ◄─────────────────────────┐     │
│                     │  (Artifact: 03_executor.md & .json)        │     │
│                     ▼                                            │     │
│  [Stage 04]  forge test --run <ID>                               │     │
│                     │  (Artifact: 04_tester.md & .json)          │     │
│                     ▼                                            │     │
│  [Stage 05]  forge review --run <ID>                             │     │
│                     │  (Artifact: 05_reviewer.md & .json)        │     │
│                     │                                            │     │
│                     ├─── [CHANGES_REQUIRED in forge auto] ───────┘     │
│                     ▼    (Auto-repair feedback passed back to 03)      │
│                                                                        │
│  [Stage 06]  forge critic --post-run --run <ID>                        │
│                     │  (Artifact: 06_critic.md & .json)                │
│                     ▼                                                  │
│  [Post-Run]  Reconcile PKB (.forge/knowledge/) & Optional Auto-Commit  │
└────────────────────────────────────────────────────────────────────────┘
```

### Stage Prerequisites & Allowed Success Protocols

| Sequence | Stage Name | Phase | Default Adapter | Default Timeout | Prerequisite Gate | Success Statuses | Non-Success / Halt Statuses |
|:---:|---|:---:|---|:---:|---|---|---|
| `00` | `critic` | `pre_run` | `opencode` | 900s / 1200s | None | `CRITIQUE_COMPLETE`, `APPROVED`, `SUCCESS`, `COMPLETED`, `PASSED` | `FAILED`, `BLOCKED`, `REJECTED` |
| `01` | `architect` | `pre_run` | `opencode` | 900s | Valid `TASK` string or `--file` | `APPROVED`, `READY`, `SUCCESS`, `COMPLETED` | `FAILED`, `BLOCKED`, `REJECTED` |
| `02` | `planner` | `pre_run` | `opencode` | 900s | Succeeded Stage 01 (`architect`) | `READY`, `APPROVED`, `SUCCESS`, `COMPLETED` | `FAILED`, `BLOCKED`, `REJECTED` |
| `03` | `executor` | `pre_run` | `antigravity` | 1200s | Succeeded Stage 02 (`planner`) | `SUCCESS`, `APPROVED`, `COMPLETED` | `FAILED`, `BLOCKED`, `REJECTED` |
| `04` | `tester` | `pre_run` | `opencode` | 900s | Succeeded Stage 03 (`executor`) | `PASS`, `APPROVED`, `SUCCESS`, `COMPLETED`, `NOT_TESTABLE` | `FAIL`, `FAILED`, `BLOCKED`, `CHANGES_REQUIRED` |
| `05` | `reviewer` | `pre_run` | `opencode` | 900s | Succeeded Stage 03 (`executor`) | `APPROVED` | `CHANGES_REQUIRED`, `REJECTED`, `FAILED`, `BLOCKED` |
| `06` | `critic` | `post_run` | `opencode` | 1200s | Succeeded Stage 05 (`reviewer`) | `CRITIQUE_COMPLETE`, `APPROVED`, `SUCCESS`, `COMPLETED`, `PASSED` | `FAILED`, `BLOCKED`, `REJECTED` |

---

## 5. Top-Level Commands Reference

### `forge doctor`

#### Purpose
Comprehensive diagnostic health check. Verifies external CLI tools, WSL2 binary compatibility, Git repository status, `.ai/` prompt templates, configuration cascade defaults, configured stage mapping, and Playwright/Chromium browser testing drivers.

#### Syntax
```bash
forge doctor [OPTIONS]
```

#### Options
- `--help`: Show usage message.

#### Behavior
- Probes PATH for `opencode`, `agy`/`antigravity`, `codex`, `claude`, `aider`, and `gemini`.
- Under WSL2, checks if resolved binaries are Windows executables and warns if native Linux binaries are missing.
- Inspects Git repository state and reports the active branch.
- Inspects `.ai/roles/*.md` and reports count of configured roles.
- Validates Playwright and Chromium availability for the Tester engine.

#### Output Example
```text
🔨 Forge Doctor — Environment & Tool Diagnostics
==================================================

[CLI Tools]
  ✓ OpenCode       found (/home/mathir/.opencode/bin/opencode)
  ✓ Antigravity    found (/home/mathir/.local/bin/agy)
  ✗ Codex          missing (OpenAI Codex CLI)
  ✗ Claude Code    missing (Anthropic Claude Code CLI)
  ✗ Aider          missing (Aider AI Pair Programmer)
  ✗ Gemini CLI     missing (Gemini CLI tool)

[Git Repository]
  ✓ Git initialized (branch: main)

[Project Setup]
  ✓ .ai/ directory found (7 roles configured)

[Configuration Defaults]
  • Adapter: opencode
  • Model:   opencode/big-pickle
  • Effort:  medium
  • Timeout: 300s

[Configured Stages]
  • Critic     -> opencode [model: opencode/big-pickle] [effort: high] [timeout: 1200s]
  • Architect  -> opencode [model: opencode/big-pickle] [effort: high] [timeout: 900s]
  • Planner    -> opencode [model: opencode/big-pickle] [effort: high] [timeout: 900s]
  • Executor   -> antigravity [model: gemini-3.8-flash] [effort: high] [timeout: 1200s]
  • Tester     -> opencode [model: opencode/big-pickle] [effort: high] [timeout: 900s]
  • Reviewer   -> opencode [model: opencode/big-pickle] [effort: high] [timeout: 900s]

[Tester]
  Browser Driver
    ✓ Playwright
    ✓ Chromium

  Evidence Collection
    ✓ Enabled

  Browser Backend
    PlaywrightDriver
```

---

### `forge adapters`

#### Purpose
Inspects all registered model/agent adapters, their canonical names, aliases, default model bindings, and supported functional capabilities.

#### Syntax
```bash
forge adapters [OPTIONS]
```

#### Options
| Flag | Type | Default | Description |
|---|---|---|---|
| `--json` | Flag | `False` | Output adapters list and capabilities as structured JSON. |
| `--help` | Flag | — | Show usage message. |

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge adapters` | **Valid / Read-only** | Human-readable terminal list with checkmarks. |
| `forge adapters --json` | **Valid / Read-only** | Machine-readable JSON array of adapter specifications. |
| `forge adapters --json --raw` | **Invalid** | Rejected: `--raw` is not an option of `adapters`. |

---

### `forge init`

#### Purpose
Initializes Forge in the current working directory. Scaffolds `.ai/` prompt directories (`roles/`, `templates/`, `project/`), writes default role prompts, installs `.ai/templates/protocol.md`, appends `.forge/runs/` and `.forge/cache/` to `.gitignore`, and generates a baseline `forge.yaml` if not already present.

#### Syntax
```bash
forge init [OPTIONS]
```

#### Behavior & Safety
- **Safe / Non-destructive**: If `forge.yaml` or a role template already exists, `forge init` preserves the existing file and skips overwriting.
- If `.gitignore` exists, it appends `.forge/` protection rules rather than clobbering existing ignore rules.

---

### `forge runs`

#### Purpose
Queries and lists historical execution runs stored in `.forge/runs/`. Displays Run ID, creation timestamp, terminal status, and task description.

#### Syntax
```bash
forge runs [OPTIONS]
```

#### Options
| Flag | Shorthand | Type | Default | Description |
|---|---|---|---|---|
| `--limit` | `-n` | `INTEGER` | `10` | Number of recent runs to display. Passing `0` or negative shows all runs. |
| `--json-output` | `-j` | Flag | `False` | Output runs list as formatted JSON array. |
| `--help` | — | Flag | — | Show usage message. |

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge runs` | **Valid / Read-only** | Lists up to 10 most recent runs in tabular format. |
| `forge runs -n 25` | **Valid / Read-only** | Shows up to 25 recent runs. |
| `forge runs -n 0` | **Valid / Read-only** | Shows all historical runs without limit. |
| `forge runs -j` | **Valid / Read-only** | Returns formatted JSON output for scripts. |
| `forge runs -n 5 -j` | **Valid / Read-only** | Returns JSON array of the last 5 runs. |

---

### `forge dashboard`

#### Purpose
Opens a Rich-based terminal dashboard to inspect stage timelines, streamed execution outputs, human Markdown reports, machine JSON reports, Tester evidence, and Project Knowledge Base (PKB) state.

#### Syntax
```bash
forge dashboard [OPTIONS] [RUN_ID]
```

#### Arguments & Options
| Parameter | Type | Default | Description |
|---|---|---|---|
| `[RUN_ID]` | Positional (Optional) | Latest run (`run_mgr.latest()`) | Specific run identifier to inspect (e.g. `run-001` or `001`). |
| `--render-once` | Flag | `False` | Render a single static snapshot to stdout and exit immediately without entering interactive raw TUI mode. |
| `--help` | Flag | — | Show usage message. |

#### TUI Views & Keyboard Controls
- **Layout**: 3-tier view (Header, Main with Sidebar Timeline & Content Viewport, Footer).
- **Navigation**:
  - `Tab`: Toggle keyboard focus between the left sidebar (stage timeline) and the main content viewport.
  - `Up` / `k`: Scroll content up, or select previous stage if focused on sidebar.
  - `Down` / `j`: Scroll content down, or select next stage if focused on sidebar.
  - `PageUp` / `PageDown`: Scroll content by 15 lines.
  - `1`: View Tab 1: Live Console Stream / Human tab.
  - `2`: View Tab 2: Artifacts View / Machine tab.
  - `3`: View Tab 3: Tester View (test results, assertions, browser evidence).
  - `4`: View Tab 4: Project Knowledge Base (PKB) facts.
  - `5`: View Tab 5: Compare View.
  - `h`: Switch to Human (`.md`) view inside Artifacts tab.
  - `m`: Switch to Machine (`.json`) view inside Artifacts tab.
  - `[` and `]`: Cycle fact type filter in PKB view (`None` -> `architecture` -> `feature` -> `decision` -> `unresolved`).
  - `q` or `Ctrl+C`: Exit dashboard.

#### Headless / Non-TTY Behavior
If `sys.stdin.isatty()` is `False` (e.g. running in CI/CD, redirected stdin, or subshell), `forge dashboard` automatically avoids entering raw mode and prints a static layout snapshot to stdout.

---

### `forge critic`

#### Purpose
Executes the Critic role to audit the codebase for architectural debt, security vulnerabilities, code smells, test gaps, and maintainability issues.

#### Syntax
```bash
forge critic [OPTIONS] [TARGET]
```

#### Arguments & Options
| Parameter | Type | Default | Description |
|---|---|---|---|
| `[TARGET]` | Positional (Optional) | Default audit prompt | Description of target scope or audit objective. |
| `--post-run` | Flag | `False` | Run closing Critic audit (Stage 06, sequence 06) on an existing run instead of pre-run Critic (Stage 00). |
| `--run` | Option (`TEXT`) | Latest run (if `--post-run`) | Existing Run ID to attach the post-run audit to. |
| `--help` | Flag | — | Show usage message. |

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge critic` | **Valid** | Creates a new run and executes Stage 00 pre-run Critic on default repository scope. |
| `forge critic "Audit data preprocessing and label encoders in app.py"` | **Valid** | Executes pre-run Critic focused on the specified target. |
| `forge critic --post-run` | **Conditionally Valid** | Executes closing Critic (Stage 06) on the latest run. Requires the run to have completed Reviewer (`05_reviewer`). |
| `forge critic --post-run --run run-002` | **Conditionally Valid** | Executes closing Critic on run `run-002`. Requires run `run-002` to exist and have passed Reviewer. |
| `forge critic --post-run "Focus audit on memory leaks"` | **Valid** | Executes closing Critic on the latest run with custom audit instructions. |
| `forge critic --adapter opencode` | **Invalid** | Rejected: `--adapter` is not a CLI flag. Set in `forge.yaml` under `stages.critic.adapter`. |

---

### `forge architect`

#### Purpose
Executes the Architect role (Stage 01, sequence 01) to produce a formal architecture specification document from a task prompt.

#### Syntax
```bash
forge architect [OPTIONS] TASK
```

#### Arguments
| Parameter | Type | Required | Description |
|---|---|:---:|---|
| `TASK` | Positional | **Yes** | Detailed natural-language requirements or task prompt. |

#### Behavior
- Always initializes a new sequentially numbered run directory (e.g. `.forge/runs/run-003/`).
- Captures initial `git_baseline.json`.
- Compiles the Architect prompt using `.ai/roles/architect.md` and `.ai/templates/protocol.md`.
- Saves `.forge/runs/run-XXX/01_architect.md` and `01_architect.json`.

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge architect "Add PostgreSQL database adapter with connection pooling"` | **Valid** | Initializes new run and produces architecture specification. |
| `forge architect` | **Invalid** | Error: Missing argument 'TASK'. |
| `forge architect --run run-001 "New Task"` | **Invalid** | Rejected: `architect` does not accept `--run`. It always creates a new run. |

---

### `forge planner`

#### Purpose
Executes the Planner role (Stage 02, sequence 02) to break down an approved architecture specification into actionable implementation tasks.

#### Syntax
```bash
forge planner [OPTIONS]
```

#### Options
| Option | Type | Default | Description |
|---|---|---|---|
| `--run` | Option (`TEXT`) | Latest run (`run_mgr.latest()`) | Run ID containing the approved Architect output to plan against. |
| `--help` | Flag | — | Show usage message. |

#### Prerequisites
- Requires an existing run where Stage 01 (`architect`) succeeded with one of: `READY`, `APPROVED`, `SUCCESS`, `COMPLETED`.
- If no run exists or Stage 01 failed, halts with: `Planner requires prior Architect output. Run 'forge architect' first.`

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge planner` | **Conditionally Valid** | Plans against the most recent run if its Architect stage succeeded. |
| `forge planner --run run-002` | **Conditionally Valid** | Plans against run `run-002`. |
| `forge planner --run run-999` | **Invalid** | Error: Run `run-999` not found. |

---

### `forge execute`

#### Purpose
Executes the Executor role (Stage 03, sequence 03, default adapter: `antigravity`) to implement the planned code modifications in the repository.

#### Syntax
```bash
forge execute [OPTIONS]
```

#### Options
| Option | Type | Default | Description |
|---|---|---|---|
| `--run` | Option (`TEXT`) | Latest run (`run_mgr.latest()`) | Run ID containing the approved Planner output. |
| `--help` | Flag | — | Show usage message. |

#### Prerequisites
- Requires an existing run where Stage 02 (`planner`) succeeded with one of: `READY`, `APPROVED`, `SUCCESS`, `COMPLETED`.

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge execute` | **Conditionally Valid / Destructive** | Implements code changes for the latest planned run. |
| `forge execute --run run-002` | **Conditionally Valid / Destructive** | Implements code changes for run `run-002`. |

---

### `forge test`

#### Purpose
Executes the Tester role (Stage 04, sequence 04) to empirically verify observable software behavior, run automated test suites, execute Playwright/Chromium UI workflows, and produce evidence artifacts.

#### Syntax
```bash
forge test [OPTIONS]
```

#### Options
| Option | Type | Default | Description |
|---|---|---|---|
| `--run` | Option (`TEXT`) | Latest run (`run_mgr.latest()`) | Run ID containing completed Executor output. |
| `--help` | Flag | — | Show usage message. |

#### Prerequisites
- Requires an existing run where Stage 03 (`executor`) succeeded with one of: `SUCCESS`, `APPROVED`, `COMPLETED`.

---

### `forge review`

#### Purpose
Executes the Reviewer role (Stage 05, sequence 05) to conduct an adversarial code and security audit on modified files and git diffs.

#### Syntax
```bash
forge review [OPTIONS]
```

#### Options
| Option | Type | Default | Description |
|---|---|---|---|
| `--run` | Option (`TEXT`) | Latest run (`run_mgr.latest()`) | Run ID containing completed Executor and Tester outputs. |
| `--help` | Flag | — | Show usage message. |

#### Prerequisites
- Requires an existing run where Stage 03 (`executor`) succeeded.
- Only status `APPROVED` satisfies the gate. If issues are found, the reviewer emits `CHANGES_REQUIRED` or `REJECTED`.

---

### `forge run`

#### Purpose
Executes the standard multi-agent pipeline sequentially with interactive manual confirmation checkpoints between every stage.

#### Syntax
```bash
forge run [OPTIONS] [TASK]
```

#### Arguments & Options
| Parameter | Shorthand | Type | Default | Description |
|---|---|---|---|---|
| `[TASK]` | Positional | `TEXT` | `None` | Task description. Required unless `--from-critic` or `--run` is specified. |
| `--from-critic` | `-c` | Flag | `False` | Automatically resume from the latest Critic audit report, creating a new run linked to that audit. |
| `--run` | — | Option (`TEXT`) | `None` | Existing Run ID to resume from. |
| `--auto-commit` | — | Flag | `False` | Automatically git commit pure Forge-owned changes upon approved pipeline completion. |
| `--no-critic` | — | Flag | `False` | Skip the final post-execution codebase health audit (Stage 06). |
| `--help` | — | Flag | — | Show usage message. |

#### Stage Progression & Pause Semantics
`forge run` executes:
1. `01_architect`
2. `02_planner`
3. `03_executor`
4. `04_tester`
5. `05_reviewer`
6. `06_critic` (unless `--no-critic` is supplied)

After each stage succeeds, the CLI displays:
```text
Proceed to next stage (PLANNER)? [Y/n]:
```
- If confirmed (`Y`), proceeds to the next stage.
- If declined (`n`), pauses the pipeline, saves status `PAUSED_AFTER_<STAGE>` in `metadata.json`, and exits cleanly with exit code 0.
- The paused run can be resumed at any time using: `forge run --run <RUN_ID>`. Stages already completed in that run will be skipped automatically (`⏭ Skipping Stage: Architect (already completed with status 'APPROVED')`).

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge run "Refactor database models"` | **Valid** | Starts new interactive pipeline for the specified task. |
| `forge run -c` | **Conditionally Valid** | Starts new pipeline addressing issues identified in the latest Critic report. |
| `forge run -c "Fix only security vulnerabilities"` | **Valid** | Links to latest Critic report but overrides the task summary. |
| `forge run --run run-002` | **Conditionally Valid** | Resumes paused or incomplete run `run-002`, skipping already passed stages. |
| `forge run --auto-commit "Fix bug"` | **Valid / Mutating** | Executes pipeline and commits pure Forge-owned changes if approved. |
| `forge run --no-critic "Quick fix"` | **Valid** | Runs stages 01 through 05, skipping post-execution Critic (Stage 06). |
| `forge run` | **Invalid** | Error: Missing TASK. Please provide a task or use --from-critic (-c). |

---

### `forge auto`

#### Purpose
Executes the fully autonomous iterative loop:
`Architect -> Planner -> [Executor <-> (Tester -> Reviewer) Self-Repair Loop] -> Critic`.
If the Tester fails or the Reviewer requests changes (`CHANGES_REQUIRED`), structured feedback is automatically passed back to the Executor for an auto-repair attempt (up to `--max-retries` times).

#### Syntax
```bash
forge auto [OPTIONS] [TASK]
```

#### Arguments & Options
| Parameter | Shorthand | Type | Default | Description |
|---|---|---|---|---|
| `[TASK]` | Positional | `TEXT` | `None` | Task description. Required unless `--file`, `--from-critic`, or `--run` is specified. |
| `--file` | `-f` | Option (`FILE`) | `None` | Path to Markdown specification or requirements file to load task from. |
| `--from-critic` | `-c` | Flag | `False` | Automatically resume from the latest Critic audit report. |
| `--run` | — | Option (`TEXT`) | `None` | Existing Run ID to resume from. |
| `--max-retries` | `-r` | `INTEGER >= 1` | `3` | Maximum auto-repair retry iterations between Executor, Tester, and Reviewer. |
| `--auto-commit` | — | Flag | `False` | Automatically git commit upon approved verification. |
| `--no-critic` | — | Flag | `False` | Skip the final post-execution codebase health audit. |
| `--dashboard` | `-d` | Flag | `False` | Launch interactive live terminal dashboard in the foreground while the autonomous pipeline runs in the background. |
| `--help` | — | Flag | — | Show usage message. |

#### Historical Attempt Archiving
During self-repair iterations, intermediate outputs are never overwritten. For attempt $N$:
- `.forge/runs/run-XXX/03_executor_attempt_N.md` & `.json`
- `.forge/runs/run-XXX/04_tester_attempt_N.md` & `.json`
- `.forge/runs/run-XXX/05_reviewer_attempt_N.md` & `.json`

#### Permutations

| Invocation | Classification | Description |
|---|---|---|
| `forge auto "Implement rate limiting middleware"` | **Valid** | Fully autonomous loop with default 3 repair retries. |
| `forge auto -f specs/auth.md` | **Valid** | Loads task prompt directly from Markdown file. |
| `forge auto -c` | **Conditionally Valid** | Autonomous repair loop targeting issues from latest Critic audit. |
| `forge auto -c -r 5 --auto-commit` | **Valid** | Autonomous Critic repair with up to 5 attempts and automatic Git commit upon success. |
| `forge auto --run run-002` | **Conditionally Valid** | Resumes run `run-002` in autonomous mode. |
| `forge auto -d "Optimize SQL queries"` | **Valid / Interactive** | Launches live terminal dashboard while autonomous pipeline executes. |
| `forge auto -r 0 "Task"` | **Invalid** | Rejected by CLI: `max-retries` must satisfy `x >= 1`. |
| `forge auto` | **Invalid** | Error: Missing TASK. Provide a task string, --file spec.md, or --from-critic (-c). |

---

## 6. Subcommand Groups

### `forge config` Group

Inspects, validates, mutates, and manages Forge hierarchical configuration.

#### Syntax
```bash
forge config COMMAND [ARGS]...
```

#### Subcommands

#### 1. `forge config show`
Displays effective configuration.
- Syntax: `forge config show [OPTIONS]`
- Options:
  - `--raw`: Displays raw project configuration from `forge.yaml` without cascading built-in defaults.
  - `--json`: Formats output as JSON instead of YAML.
- Permutations:
  - `forge config show`: Effective merged configuration (YAML).
  - `forge config show --json`: Effective configuration as JSON.
  - `forge config show --raw`: Unmerged project file content.
  - `forge config show --raw --json`: Raw project configuration formatted as JSON.

#### 2. `forge config get`
Queries a configuration value by dot-notation key.
- Syntax: `forge config get KEY`
- Examples:
  - `forge config get defaults.model` -> `opencode/big-pickle`
  - `forge config get defaults.timeout` -> `300`
  - `forge config get stages.executor.adapter` -> `antigravity`
  - `forge config get stages.critic.timeout` -> `1200`

#### 3. `forge config set`
Sets a configuration key to a value. Automatically parses integers, floats, booleans, nulls, JSON mappings, and YAML syntax, and validates the file against the Forge schema before writing.
- Syntax: `forge config set [OPTIONS] KEY VALUE`
- Options:
  - `-g`, `--global`: Write to global configuration (`~/.forge/config.yaml`) instead of repository project configuration (`forge.yaml`).
- Examples:
  - `forge config set defaults.timeout 600`
  - `forge config set stages.critic.timeout 1500`
  - `forge config set stages.executor.model gemini-3.7-flash-high`
  - `forge config set -g defaults.effort high`
  - `forge config set execution.auto_commit true`

#### 4. `forge config edit`
Opens the active configuration file in the user's `$EDITOR` (or system default editor). Validates YAML syntax and schema before saving; if invalid, changes are aborted without corrupting the file.
- Syntax: `forge config edit [OPTIONS]`
- Options:
  - `-g`, `--global`: Edit `~/.forge/config.yaml` instead of `./forge.yaml`.

#### 5. `forge config validate`
Validates configuration syntax, stage names, adapter availability, effort values, and timeout parameters.
- Syntax: `forge config validate [OPTIONS]`
- Options:
  - `-p`, `--path FILE`: Path to a specific YAML configuration file to validate.
- Examples:
  - `forge config validate` (Validates global, project, and resolved configuration).
  - `forge config validate -p /tmp/custom_forge.yaml`

#### 6. `forge config reset`
Resets the configuration file to factory default templates.
- Syntax: `forge config reset [OPTIONS]`
- Options:
  - `-f`, `--force`: Skip confirmation prompt.
  - `-g`, `--global`: Reset `~/.forge/config.yaml` instead of `./forge.yaml`.
- Examples:
  - `forge config reset` (Prompts: `Are you sure you want to reset forge.yaml...?`)
  - `forge config reset -f`

#### 7. `forge config auth-show`
Displays authentication configuration parameters (`auth_method`, `auth_provider`, `auth_scopes`, `auth_token_ttl`).
- Syntax: `forge config auth-show`

#### 8. `forge config auth-get`
Queries an authentication property by key.
- Syntax: `forge config auth-get KEY`
- Examples:
  - `forge config auth-get auth_method`

#### 9. `forge config auth-set`
Sets an authentication property.
- Syntax: `forge config auth-set [OPTIONS] KEY VALUE`
- Options:
  - `-g`, `--global`: Write to `~/.forge/config.yaml`.
- Examples:
  - `forge config auth-set auth_method api_key`
  - `forge config auth-set auth_token_ttl 7200`

---

### `forge knowledge` Group

Inspects and manages the repository Project Knowledge Base (PKB) stored in `.forge/knowledge/`.

#### Syntax
```bash
forge knowledge COMMAND [ARGS]...
```

#### Subcommands

#### 1. `forge knowledge list`
Lists stored knowledge facts with status coloring and category tags.
- Syntax: `forge knowledge list [OPTIONS]`
- Options:
  - `-t`, `--type [architecture|feature|decision|unresolved]`: Filter by fact category.
  - `-s`, `--status [PROVISIONAL|VERIFIED|DISPUTED|HUMAN_LOCKED|DEPRECATED]`: Filter by status.
- Examples:
  - `forge knowledge list`
  - `forge knowledge list -t architecture`
  - `forge knowledge list -s HUMAN_LOCKED`
  - `forge knowledge list -t decision -s VERIFIED`

#### 2. `forge knowledge show`
Displays complete details of a single knowledge fact including title, type, status, provenance, summary, evidence citations, and active dispute records.
- Syntax: `forge knowledge show FACT_ID`
- Examples:
  - `forge knowledge show ARCH-001`
  - `forge knowledge show FEAT-LOGIN`

#### 3. `forge knowledge lock`
Locks a fact to `HUMAN_LOCKED`. In Forge, `HUMAN_LOCKED` represents sovereign developer truth. Autonomous agents (such as Architect or Reviewer) are strictly forbidden from modifying or disputing locked facts during knowledge reconciliation.
- Syntax: `forge knowledge lock FACT_ID`
- Examples:
  - `forge knowledge lock ARCH-001`

#### 4. `forge knowledge unlock`
Unlocks a previously locked fact, returning its status to `VERIFIED` and permitting autonomous agents to update it during subsequent runs.
- Syntax: `forge knowledge unlock FACT_ID`
- Examples:
  - `forge knowledge unlock ARCH-001`

---

## 7. Adapter Architectures & Model/Effort Translation

Forge provides a provider-agnostic semantic abstraction for reasoning effort: `low`, `medium`, `high`, `max`. Each adapter translates this abstraction into provider-native mechanisms:

```text
┌──────────────────────────────────────────────────────────────┐
│ Forge Semantic Abstraction: effort = "low"|"medium"|"high"   │
└──────────────────────────────┬───────────────────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
 ┌───────────────┐     ┌───────────────┐     ┌───────────────┐
 │   OpenCode    │     │  Antigravity  │     │  OpenAI Codex │
 │    Adapter    │     │    Adapter    │     │    Adapter    │
 └───────┬───────┘     └───────┬───────┘     └───────┬───────┘
         │                     │                     │
         ▼                     ▼                     ▼
 Query metadata:         CLI Flag:             CLI Config:
 'opencode api           --effort <level>      -c model_reasoning_
 model.list'                                   effort="<level>"
 Append variant:
 provider/model#<level>
 (No CLI flag emitted)
```

### Detailed Adapter Comparison

| Feature / Property | OpenCode Adapter (`opencode`) | Antigravity Adapter (`antigravity` / `agy`) | Codex Adapter (`codex`) |
|---|---|---|---|
| **Underlying CLI Binary** | `opencode` | `agy` or `antigravity` | `codex` |
| **Default Model** | Provider default (`None`) | `gemini-3.7-flash-high` | `gpt-5.6-terra` |
| **Execution Subcommand** | `opencode run` | `agy -p <prompt> --output-format text` | `codex exec --color never -` |
| **Streaming Subcommand** | `opencode run --format json` | `agy -p <prompt> --output-format stream-json` | Wrapped `execute()` event generator |
| **Prompt Transport** | Piped via **stdin** (unbounded) | Passed via **argv** `-p <prompt>` | Piped via **stdin** `-` |
| **Transport Size Limit** | Unbounded | **128 KB** (`MAX_PROMPT_BYTES = 131072`) | Unbounded |
| **Model Selection Flag** | `-m <provider/model>` | `--model <model>` | `-m <model>` |
| **Effort Handling** | **Dynamic Variant Synthesis**: Queries `opencode api model.list` via subprocess. If variant exists, translates to `model#variant`. If unsupported, warns and uses base model. **Never emits `--variant` or `--effort` flags.** | Native CLI flag: `--effort <effort>` | Config flag: `-c model_reasoning_effort="<effort>"` |
| **Explicit Variant Precedence** | Suffix `#variant` in model string (e.g. `model#custom`) takes precedence over semantic effort. | N/A | N/A |
| **Auto-Approval Flag** | `--auto` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` |
| **Timeout Flag** | Subprocess timeout enforcement | Subprocess timeout + `--print-timeout <N>s` | Subprocess timeout enforcement |
| **Process Group Isolation** | POSIX `start_new_session=True` with tracked PID/PGID cancellation | POSIX `start_new_session=True` with tracked PID/PGID cancellation | Standard subprocess handling |

---

## 8. Configuration Hierarchy & Precedence Rules

Forge resolves configuration settings using a 4-level ascending cascade:

```text
Level 1: Built-in Defaults (Config.default())
   ▲
   │ Overridden by
Level 2: Global Configuration (~/.forge/config.yaml)
   ▲
   │ Overridden by
Level 3: Repository Project Configuration (./forge.yaml)
   ▲
   │ Overridden by
Level 4: Repository Private Configuration (./.forge/config.yaml)
```

### Stage Inheritance Rules

Within the resolved configuration:
1. `defaults`: Establishes repository-wide fallback values (`adapter`, `model`, `effort`, `timeout`, `auto_approve`, `extra_flags`).
2. `stages.<stage_name>`: Explicit properties override the defaults for that specific stage.
3. If a stage does not explicitly define a property, it inherits from `defaults`.
4. `extra_flags` are deep-merged: default flags are combined with stage-specific extra flags.
5. `stages.<stage_name>.post_run_override`: Allows Stage 06 (closing Critic) to have a different model, effort, timeout, or adapter than Stage 00 (pre-run Critic).

### Example `forge.yaml`

```yaml
version: '2.0'

defaults:
  adapter: opencode
  model: opencode/big-pickle
  effort: medium
  timeout: 300
  auto_approve: false

stages:
  critic:
    effort: high
    timeout: 1200
  architect:
    effort: high
    timeout: 900
  planner:
    effort: high
    timeout: 900
  executor:
    adapter: antigravity
    model: gemini-3.8-flash
    effort: high
    timeout: 1200
    auto_approve: true
  tester:
    effort: high
    timeout: 900
  reviewer:
    effort: high
    timeout: 900

execution:
  mode: interactive
  auto_commit: false
  default_timeout: 300
```

---

## 9. Run Lifecycle, Lock Ownership, & Artifact Layout

### Directory Layout

Every pipeline run creates an isolated directory in `.forge/runs/run-XXX/`:

```text
.forge/runs/run-002/
├── metadata.json                 # Run status, task, timestamps, prompt hashes
├── git_baseline.json             # Git SHA, branch, unstaged/staged diff baseline
├── run.lock                      # Exclusive file lock (deleted upon clean exit)
├── 00_critic.md                  # Human report for Stage 00
├── 00_critic.json                # Machine report protocol for Stage 00
├── 01_architect.md               # Human report for Stage 01
├── 01_architect.json             # Machine report protocol for Stage 01
├── 02_planner.md                 # Human report for Stage 02
├── 02_planner.json               # Machine report protocol for Stage 02
├── 03_executor.md                # Human report for Stage 03
├── 03_executor.json              # Machine report protocol for Stage 03
├── 03_executor_attempt_1.md      # Archived attempt 1 (if retried)
├── 04_tester.md                  # Human report for Stage 04
├── 04_tester.json                # Machine report protocol for Stage 04
├── 05_reviewer.md                # Human report for Stage 05
├── 05_reviewer.json              # Machine report protocol for Stage 05
├── 06_critic.md                  # Closing audit report
├── 06_critic.json                # Closing audit machine report
└── debug/                        # Raw adapter diagnostics
    ├── critic_prompt.md          # Exact rendered prompt sent to adapter
    ├── critic_stdout.txt         # Text chunks extracted from stream
    ├── critic_stderr.txt         # Stderr captured from adapter CLI
    └── critic_raw.txt            # Raw unparsed NDJSON event stream
```

### Exclusive Run Locking (`run.lock`)

To prevent concurrent processes from clobbering run artifacts:
- When a run is executed or resumed, Forge acquires an exclusive non-blocking file lock on `run.lock`.
- `run.lock` stores owner metadata: PID, process group ID (PGID), hostname, username, command line, start timestamp, and Forge version.
- If another process attempts to access the same run, Forge raises `RunOwnershipError` and displays owner details.
- Stale locks from crashed processes are automatically detected and recovered if the owning PID is no longer alive.

### Safe Git Auto-Commit Behavior

When `--auto-commit` is enabled:
1. Forge compares working directory changes against `git_baseline.json`.
2. It classifies modified files as:
   - **Pure Forge Changes**: Files created or modified solely by Forge during the run.
   - **Mixed Ownership Changes**: Files that already had uncommitted user changes *before* Forge began and were further modified by Forge.
3. Forge auto-commits **only Pure Forge Changes** with message `feat: <task>`.
4. If Mixed Ownership files are detected, Forge **skips committing them**, outputs a warning, and leaves them unstaged for manual developer review.

---

## 10. Diagnostic & Troubleshooting Runbook

### Diagnostic Flowchart

```text
[Failure Observed]
       │
       ▼
1. Check Environment & Tooling
   $ forge doctor
       │
       ▼
2. Check Configuration Validity
   $ forge config validate
       │
       ▼
3. Inspect Run Artifacts & Reason
   $ forge runs -n 5
   $ cat .forge/runs/run-XXX/0X_stage.json | grep -A 5 "machine_report"
       │
       ▼
4. Inspect Low-Level Raw Output
   $ cat .forge/runs/run-XXX/debug/<stage>_stderr.txt
   $ tail -n 30 .forge/runs/run-XXX/debug/<stage>_raw.txt
```

### Common Failure Modes & Solutions

#### 1. Provider Transport Failure (`ECONNRESET` / Socket Closed)
- **Symptom**: `00_critic.json` reports:
  `Connection lost while reading the response: ECONNRESET: The socket connection was closed unexpectedly.`
- **Cause**: The remote AI model provider server abruptly closed the HTTP streaming socket during generation.
- **Resolution**: This is a transient network/upstream provider issue. Verify network connectivity and rerun:
  ```bash
  forge critic
  ```

#### 2. Absolute Timeout Exceeded (Exit Code 124)
- **Symptom**: Stage terminates with `exit_code: 124` after exactly $N$ seconds.
- **Cause**: Workload exceeded the configured stage timeout.
- **Resolution**: Increase the stage timeout in `forge.yaml` or via CLI:
  ```bash
  forge config set stages.critic.timeout 1800
  ```

#### 3. Run Ownership Conflict (`RunOwnershipError`)
- **Symptom**: `❌ Run Ownership Conflict: Run 'run-XXX' is currently locked by an active Forge process.`
- **Cause**: Another terminal or background task is operating on `run-XXX`, or a previous process was forcefully killed (`SIGKILL`) leaving an unexpired lock.
- **Resolution**: Verify if a Forge process is running:
  ```bash
  ps aux | grep forge
  ```
  If no process is running, verify the owning PID shown in the diagnostic message and rerun; Forge will automatically recover the stale lock.

#### 4. WSL2 Windows Binary Incompatibility
- **Symptom**: `OpenCode resolved to a Windows installation while Forge is running inside WSL2`.
- **Cause**: Forge inside WSL2 found `opencode.cmd` or `opencode.exe` on Windows PATH instead of a native Linux binary.
- **Resolution**: Install OpenCode natively inside WSL Linux (`npm install -g opencode` or curl installer) and ensure `/home/<user>/.opencode/bin` precedes Windows PATH in `~/.bashrc`.

#### 5. Reviewer Requests Changes (`CHANGES_REQUIRED`)
- **Symptom**: Stage 05 (`reviewer`) halts with non-success status `CHANGES_REQUIRED`.
- **Resolution**:
  - In `forge run`: Review issues in `.forge/runs/run-XXX/05_reviewer.md`, fix them manually, or re-run `forge execute --run <ID>`.
  - In `forge auto`: Forge automatically triggers an auto-repair iteration loop, feeding the reviewer issues back to Executor.

---

## 11. Cheat Sheet

### Inspection & Health

```bash
# Verify system tools, git repository, and browser drivers
forge doctor

# List registered adapters and capabilities
forge adapters

# List adapters in JSON format
forge adapters --json

# List recent runs
forge runs

# List last 20 runs
forge runs -n 20

# Output runs list as JSON
forge runs -j

# Open interactive dashboard on latest run
forge dashboard

# Open interactive dashboard on specific run
forge dashboard run-002

# Non-interactive snapshot of dashboard to stdout
forge dashboard --render-once
```

### Pre-Run & Post-Run Codebase Auditing

```bash
# Run codebase Critic on default repository scope
forge critic

# Run codebase Critic on specific subsystem
forge critic "Audit authentication and JWT session handling"

# Run post-execution closing Critic on latest run
forge critic --post-run

# Run post-execution closing Critic on specific run
forge critic --post-run --run run-002
```

### Running Individual Stages Manually

```bash
# 1. Synthesize architecture for a new task (creates new run)
forge architect "Add Stripe webhook listener with idempotency keys"

# 2. Plan work packages (attaches to latest run)
forge planner

# 3. Implement code changes (attaches to latest run)
forge execute

# 4. Run automated behavioral and UI verification
forge test

# 5. Conduct adversarial code review
forge review

# Run specific stage on specific run ID
forge planner --run run-003
forge execute --run run-003
forge test --run run-003
forge review --run run-003
```

### Orchestrated Pipelines

```bash
# Standard pipeline with step-by-step confirmation prompts
forge run "Refactor notification service to use async queues"

# Resume paused pipeline on existing run
forge run --run run-003

# Standard pipeline resuming from latest Critic audit findings
forge run -c

# Standard pipeline with automatic git commit upon approval
forge run --auto-commit "Fix SQL injection in search endpoint"

# Standard pipeline skipping post-execution Critic
forge run --no-critic "Update dependencies"
```

### Autonomous Self-Repair Execution

```bash
# Fully autonomous loop with auto-repair (default 3 retries)
forge auto "Build REST API endpoint for user profile export"

# Autonomous loop reading task from Markdown specification
forge auto -f specs/export_api.md

# Autonomous repair loop addressing latest Critic audit
forge auto -c

# Autonomous loop with up to 5 repair attempts and auto-commit
forge auto -c -r 5 --auto-commit

# Autonomous loop with live terminal dashboard attached
forge auto -d "Implement Redis cache layer"
```

### Configuration Management

```bash
# Show effective resolved configuration
forge config show

# Show raw project forge.yaml without defaults
forge config show --raw

# Validate configuration files
forge config validate

# Query specific settings
forge config get defaults.model
forge config get stages.executor.adapter
forge config get stages.critic.timeout

# Mutate project configuration
forge config set stages.critic.timeout 1500
forge config set defaults.effort high
forge config set execution.auto_commit true

# Mutate global configuration (~/.forge/config.yaml)
forge config set -g defaults.model opencode/big-pickle

# Edit configuration interactively with pre-save validation
forge config edit
forge config edit -g

# Reset configuration to default template
forge config reset
```

### Project Knowledge Base (PKB)

```bash
# List all knowledge facts
forge knowledge list

# Filter facts by category or status
forge knowledge list -t architecture
forge knowledge list -s HUMAN_LOCKED

# Inspect full details and provenance of a fact
forge knowledge show ARCH-001

# Lock a fact to prevent autonomous agent modifications
forge knowledge lock ARCH-001

# Unlock a fact to permit agent updates
forge knowledge unlock ARCH-001
```

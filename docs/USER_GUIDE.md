# 🔨 Forge: Complete CLI User Guide & Command Reference

Forge is a **CLI-first multi-agent orchestration framework** designed to coordinate local AI agents (`opencode` and `antigravity`/`agy`) through structured development workflows with zero cloud API token costs.

---

## 📋 Table of Contents
1. [Installation & Setup](#1-installation--setup)
2. [Complete Command Reference](#2-complete-command-reference)
3. [Core Workflow Patterns](#3-core-workflow-patterns)
4. [Configuration (`forge.yaml`)](#4-configuration-forgeyaml)
5. [Storage & Artifacts (`.forge/runs/`)](#5-storage--artifacts-forgeruns)
6. [Safe Self-Improvement (Dogfooding)](#6-safe-self-improvement-dogfooding)

---

## 1. Installation & Setup

### Install Forge in Editable Mode
From the Forge repository root:
```bash
pip install -e .
```

### Verify Environment & Tools
```bash
forge doctor
```
Checks for:
- Installed AI CLI agents (`opencode`, `agy`/`antigravity`, `claude`, `aider`, `gemini`)
- Git repository state & active branch
- `.ai/` prompt pack health (roles & project templates)
- Configured stage-to-adapter mappings

---

## 2. Complete Command Reference

| Command | Category | Description | Modifies Code? |
| :--- | :--- | :--- | :---: |
| **`forge doctor`** | Diagnostics | Check CLI tools, git status, and environment | ❌ No |
| **`forge init`** | Setup | Initialize `.ai/` prompt templates and `forge.yaml` in any project | ❌ No |
| **`forge critic [TARGET]`** | Analysis | Relentlessly audit codebase/module for flaws, smells & tech debt | ❌ No |
| **`forge architect "<TASK>"`** | Design | Generate architecture decisions, module boundaries & interfaces | ❌ No |
| **`forge planner [--run ID]`** | Planning | Break approved architecture into tasks & acceptance criteria | ❌ No |
| **`forge execute [--run ID]`** | Execution | Implement plan with Antigravity (`agy`) and run validation tests | ✅ Yes |
| **`forge review [--run ID]`** | Quality Gate | Perform adversarial audit on implementation and `git diff` | ❌ No |
| **`forge run "<TASK>"`** | Orchestration | Execute full 5-stage pipeline with interactive confirmation | ✅ Yes |
| **`forge run "<TASK>" -a`** | Orchestration | Execute full 5-stage pipeline autonomously without prompts | ✅ Yes |
| **`forge run -c`** | Loop | Resume from latest Critic report and fix discovered issues | ✅ Yes |
| **`forge runs`** | Telemetry | List all historical runs, timestamps, and status | ❌ No |

---

## 3. Core Workflow Patterns

### Pattern A: Step-by-Step Stage Execution (Full Human Control)
Run each agent independently when you want to review and verify every decision:

```bash
# 1. Standalone Codebase Health Check
forge critic "Audit error handling in src/forge/adapters/"

# 2. Design the Architecture
forge architect "Add timeout handling to CLI adapters"

# 3. Plan the Tasks
forge planner

# 4. Implement and Run Unit Tests (Antigravity)
forge execute

# 5. Review Diff and Code Quality
forge review
```

---

### Pattern B: Full Autonomous Pipeline
Run the complete 5-stage chain in one command:

```bash
# Interactive mode (prompts for confirmation between stages):
forge run "Add JSON export to forge runs command"

# Fully autonomous mode (runs all 5 stages continuously):
forge run "Add JSON export to forge runs command" --autonomous

# Skip the post-run Critic audit:
forge run "Quick fix" --no-critic
```

---

### Pattern C: Continuous Self-Improvement Loop
Forge can continuously audit and refine code in an iterative feedback loop:

```text
1. forge run "Add Feature X"
   └─► Stages 1-4 execute -> Feature implemented
   └─► Stage 5: Critic audits fresh codebase -> saves 05_critic.md

2. forge run --from-critic
   └─► Automatically fixes the issues discovered in the previous run!
```

---

## 4. Configuration (`forge.yaml`)

Forge looks for configuration in priority order:
1. `~/.forge/config.yaml` (Global)
2. `./forge.yaml` (Project)
3. `./.forge/config.yaml` (Workspace override)

### Example `forge.yaml`
```yaml
version: "1.0"

stages:
  critic:
    adapter: opencode
    model: null             # Uses default opencode model or specify alias

  architect:
    adapter: opencode
    model: big-pickle

  planner:
    adapter: opencode
    model: big-pickle

  executor:
    adapter: antigravity
    model: gemini-3.7-flash-high
    effort: high
    auto_approve: true      # Automatically grants tool permissions to execute code

  reviewer:
    adapter: opencode
    model: big-pickle

execution:
  mode: interactive         # "interactive" or "autonomous"
  auto_commit: false        # Set true to auto-commit on approved review
```

---

## 5. Storage & Artifacts (`.forge/runs/`)

Every run creates an isolated directory in `.forge/runs/run-XXX/` storing paired Human (`.md`) and Machine (`.json`) artifacts:

```text
.forge/
└── runs/
    ├── run-001/
    │   ├── 00_critic.md       # Pre-run or standalone audit
    │   ├── 00_critic.json     # Typed machine protocol
    │   ├── 01_architect.md    # Architecture specification
    │   ├── 01_architect.json  # Approved modules & boundaries
    │   ├── 02_planner.md      # Actionable task list
    │   ├── 02_planner.json    # Acceptance criteria & validation steps
    │   ├── 03_executor.md     # Code changes & test execution evidence
    │   ├── 03_executor.json   # Modified artifacts & exit code
    │   ├── 04_reviewer.md     # Adversarial review & score card
    │   ├── 04_reviewer.json   # APPROVAL: YES | NO
    │   ├── 05_critic.md       # Post-execution fresh codebase audit
    │   ├── 05_critic.json     # Next improvement targets
    │   └── metadata.json      # Timestamps, SHA-256 prompt hashes, adapters
    └── run-002/
```

---

## 6. Safe Self-Improvement (Dogfooding)

When using Forge to improve Forge itself:

```bash
# 1. Create a safety branch
git checkout -b self-improvement

# 2. Run the loop
forge run "Improve error reporting on missing adapters"

# 3. Verify tests
pytest -v

# 4. If satisfied, commit or merge; if not, instantly rollback:
git restore .
```

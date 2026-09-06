# Forge 🔨

**CLI-first multi-agent orchestration framework for autonomous development workflows.**

Forge connects local AI CLI tools (`opencode`, `antigravity`, `claude`, `aider`) into a structured pipeline:
```
User Request → Architect → Planner → Executor → Reviewer → Verified Output
```

## Features
- **Zero API Lock-in / Free Tier CLI First**: Operates directly through installed CLI binaries. No paid cloud tokens required.
- **Auditable Artifact Logs**: Every stage execution is persisted as paired `.md` (human) and `.json` (machine) files under `.forge/runs/run-XXX/`.
- **Adversarial Multi-Agent Roles**: Architect designs, Planner organizes, Executor implements, Reviewer stress-tests.
- **Git Native**: Automatic git tracking, diff captures, and safety restore checkpoints.

## Installation
```bash
pip install -e .
```

## Quickstart
Check installed CLI agents:
```bash
forge doctor
```

Run Architect on a feature:
```bash
forge architect "Add JWT authentication"
```

Run full multi-agent pipeline:
```bash
forge run "Add JWT authentication"
```

## Configuration
Forge looks for `forge.yaml` in your project or `.forge/config.yaml`:
```yaml
version: "1.0"
stages:
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
    auto_approve: true
  reviewer:
    adapter: opencode
    model: big-pickle
```

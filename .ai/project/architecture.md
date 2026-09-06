# Forge Architecture

Forge is a CLI-first multi-agent orchestration framework for software development workflows.

## Core Components

### Adapters (`src/forge/adapters/`)
- `base.py` - Abstract `BaseAdapter` interface with `execute()` method
- `opencode.py` - OpenCode CLI adapter (uses stdin for prompts)
- `antigravity.py` - Antigravity (agy) CLI adapter (uses stdin for prompts)
- `registry.py` - Adapter registration and lookup

### Core (`src/forge/core/`)
- `config.py` - Configuration loading from `forge.yaml`
- `context.py` - Runtime context (run, project root, config, git)
- `git.py` - Git operations (diff, commit, changed files)
- `role.py` - Role definitions with sequence numbers
- `run.py` - Run state management

### Stages (`src/forge/stages/`)
- `stage.py` - Stage execution engine
- `result.py` - StageResult with machine report parsing

### Prompts (`src/forge/prompts/`)
- `instruction.py` - Instruction dataclass (task, docs, git state)
- `builder.py` - Constructs Instruction from Context and Role
- `compiler.py` - Compiles prompts from templates
- `rendered_prompt.py` - Rendered prompt with hash

### Protocol (`src/forge/protocol/`)
- `parser.py` - Parses YAML machine reports from responses
- `validator.py` - Validates machine report fields
- `report.py` - MachineReport dataclass

### Storage (`src/forge/storage/`)
- `run_manager.py` - Run directory management, artifact I/O

## Pipeline Flow

```
forge run "task" -> Architect -> Planner -> Executor -> Reviewer -> Critic
forge auto "task" -> Architect -> Planner -> [Executor <-> Reviewer loop] -> Critic
```

Each stage:
1. Builds instruction from context + role template
2. Compiles prompt with previous outputs
3. Executes via adapter (subprocess)
4. Parses machine report from response
5. Saves artifacts (.md + .json)

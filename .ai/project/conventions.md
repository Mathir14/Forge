# Forge Conventions

## Code Style
- Python 3.10+ required
- Type hints on all public methods
- Click for CLI commands
- Dataclasses for data structures
- Logging via `logging` module (not print)

## File Organization
- `src/forge/` - Main package
- `.ai/roles/` - Role prompt templates (.md files)
- `.ai/project/` - Project context docs
- `.forge/runs/` - Run artifacts

## Naming
- snake_case for functions/variables
- PascalCase for classes
- Role names: critic, architect, planner, executor, reviewer
- Sequence numbers: 1=architect, 2=planner, 3=executor, 4=reviewer, 5=critic

## Machine Report Protocol
Every agent response MUST include a YAML block:
```yaml
ROLE:
STATUS: READY | BLOCKED | APPROVED | REJECTED | CHANGES_REQUIRED
HANDOFF: ARCHITECT | PLANNER | EXECUTOR | REVIEWER | CRITIC
```

## Error Handling
- Use `click.secho()` for colored output
- Use `sys.exit(1)` for fatal errors
- Adapter errors return `AdapterResponse` with exit_code != 0
- Subprocess timeout: 300s default for adapters, 60s for git

## Git Operations
- Always check `is_git_repo()` before git commands
- Use `changed_files()` not raw `git status`
- Commit messages: `feat:`, `fix:`, `refactor:` prefixes

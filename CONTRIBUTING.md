# Contributing to Forge

Thank you for your interest in contributing to Forge!

## Principles
1. **CLI-First**: Never couple Forge directly to remote vendor APIs. Wrap CLI tools as adapters.
2. **Downward Dependencies Only**: `CLI → Orchestrator → Run → Stage → Instruction → Prompt → Adapter → Storage`.
3. **Reproducibility**: All prompts are hashed and outputs stored as paired `.md` and `.json`.
4. **No Premature Abstraction**: Only introduce abstractions when their absence is painful in real usage.

## Development Setup
```bash
git clone https://github.com/your-repo/forge.git
cd forge
pip install -e ".[dev]"
pytest
```

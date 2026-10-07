# Contributing to Forge

Thank you for contributing to Forge! Forge is an open-source, CLI-first multi-agent orchestration framework for autonomous software development. We welcome contributions from developers helping to fix bugs, improve documentation, and enhance functionality.

## Development Setup

Forge requires Python 3.10 or higher and Git 2.25 or higher. Forge officially supports Linux, WSL2, macOS, and Native Windows. Windows contributors can develop and test natively (using PowerShell or Command Prompt) or within WSL2. All pull requests are automatically validated across both Ubuntu and Windows CI runners.

1. Clone the repository:
   ```bash
   git clone https://github.com/Mathir14/Forge.git
   cd Forge
   ```

2. Install Forge in editable mode with development dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

3. Verify your installation:
   ```bash
   forge --version
   forge doctor
   ```

## Repository Structure

- `src/`: Core source code for Forge, including the CLI, agent adapters, stage orchestration, protocol validation, storage, testing engine, and dashboard.
- `tests/`: Automated test suite containing unit, integration, and regression tests.
- `docs/`: Project documentation and user guides.
- `.github/`: GitHub Actions CI workflows, issue templates, and pull request configuration.

## Development Workflow

1. Create a topic branch:
   ```bash
   git checkout -b your-branch-name
   ```
2. Implement your changes.
3. Run the test suite to verify changes and prevent regressions.
4. Update documentation when behavior, commands, or configuration change.
5. Open a pull request against the main branch with a clear description of your changes.

## Testing

Forge uses `pytest` for automated testing. Run the test suite before submitting changes:

```bash
# Run the complete test suite
pytest

# Run tests with verbose output
pytest -v

# Run a specific test module
pytest tests/test_config.py
```

Ensure all tests pass before submitting a pull request. The CI matrix validates all PRs across both Ubuntu and Windows runners on Python 3.10, 3.11, 3.12, and 3.13.

## Coding Guidelines

- **Small focused commits**: Keep commits atomic and focused on a single change with descriptive messages.
- **Readable code**: Write clear, readable Python code consistent with surrounding style and conventions.
- **Preserve backwards compatibility**: Maintain existing CLI behavior, configuration options, and storage formats when practical.
- **Add tests for new behaviour**: Accompany new features, bug fixes, and adapter updates with appropriate tests.
- **Keep documentation accurate**: Keep README, user guides, and docstrings aligned with any behavior or interface changes.

## Pull Requests

When submitting a pull request:

- Describe the change and the rationale behind it.
- Reference any related issues (for example, `Fixes #123`).
- Keep pull requests focused on a single issue or feature.
- Ensure the test suite passes locally and CI checks pass.

## Reporting Issues

If you encounter a bug, have a feature suggestion, or want to provide feedback, please open an issue on GitHub using our templates:

- **Bug Report**: For reproducible errors, broken commands, or unexpected behavior.
- **Feature Request**: For proposing additions or improvements.
- **Usability Feedback**: For sharing usability experiences and general workflow feedback.

## Releases & Packaging

For maintainers managing package versions, PyPI publication, and Trusted Publishing workflows, refer to the [Release Guide](https://github.com/Mathir14/Forge/blob/master/docs/RELEASING.md).


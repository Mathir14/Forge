"""Forge CLI Interface."""

import sys
from pathlib import Path
import click

from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.context import Context
from forge.adapters.registry import AdapterRegistry
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


@click.group()
@click.version_option(version="0.1.0", prog_name="forge")
def main():
    """Forge: CLI-First Multi-Agent Orchestration Framework."""
    pass


@main.command(name="doctor")
def doctor():
    """Check installed CLI tools, git status, and environment."""
    click.echo("\n🔨 Forge Doctor — Environment & Tool Diagnostics\n" + "=" * 50)

    # 1. Check CLI tools
    tools = AdapterRegistry.check_all_tools()
    click.echo("\n[CLI Tools]")
    for t in tools:
        if t["found"]:
            click.secho(f"  ✓ {t['name']:<14} found ({t['path']})", fg="green")
        else:
            click.secho(f"  ✗ {t['name']:<14} missing ({t['desc']})", fg="yellow")

    # 2. Check Git
    git = GitService()
    click.echo("\n[Git Repository]")
    if git.is_git_repo():
        branch = git.current_branch()
        click.secho(f"  ✓ Git initialized (branch: {branch})", fg="green")
    else:
        click.secho("  ✗ Not a git repository (run 'git init')", fg="yellow")

    # 3. Check Prompt Templates & Config
    click.echo("\n[Project Setup]")
    root = Path.cwd()
    ai_dir = root / ".ai"
    if ai_dir.exists():
        roles_count = len(list((ai_dir / "roles").glob("*.md"))) if (ai_dir / "roles").exists() else 0
        click.secho(f"  ✓ .ai/ directory found ({roles_count} roles configured)", fg="green")
    else:
        click.secho("  ✗ .ai/ directory missing (run 'forge init')", fg="yellow")

    config = Config.load(root)
    click.echo("\n[Configured Stages]")
    for stage_name, stage_cfg in config.stages.items():
        model_str = f" [model: {stage_cfg.model}]" if stage_cfg.model else ""
        click.echo(f"  • {stage_name.capitalize():<10} -> {stage_cfg.adapter}{model_str}")

    click.echo("\n" + "=" * 50)


@main.command(name="init")
def init():
    """Initialize .ai/ prompt templates and forge.yaml in current repository."""
    root = Path.cwd()
    click.echo(f"Initializing Forge in {root}...")

    # Create directories
    ai_dir = root / ".ai"
    (ai_dir / "project").mkdir(parents=True, exist_ok=True)
    (ai_dir / "roles").mkdir(parents=True, exist_ok=True)
    (ai_dir / "templates").mkdir(parents=True, exist_ok=True)

    # Create forge.yaml if not present
    cfg_file = root / "forge.yaml"
    if not cfg_file.exists():
        cfg_content = """version: "1.0"
stages:
  architect:
    adapter: opencode
    model: null
  planner:
    adapter: opencode
    model: null
  executor:
    adapter: antigravity
    model: gemini-3.7-flash-high
    effort: high
    auto_approve: true
  reviewer:
    adapter: opencode
    model: null

execution:
  mode: interactive
  auto_commit: false
"""
        with open(cfg_file, "w", encoding="utf-8") as f:
            f.write(cfg_content)
        click.secho("  ✓ Created forge.yaml", fg="green")
    else:
        click.echo("  • forge.yaml already exists")

    click.secho("\nInitialization complete! Run 'forge doctor' to verify.", fg="green")


@main.command(name="architect")
@click.argument("task", type=str)
def architect_cmd(task: str):
    """Run Architect role to generate system architecture for TASK."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    # Create a new run
    run = run_mgr.create_run(task=task)
    context = Context(run=run, project_root=root, config=config, git=git)

    # Load Architect role and configured adapter
    arch_cfg = config.stages.get("architect")
    adapter_name = arch_cfg.adapter if arch_cfg else "opencode"
    model = arch_cfg.model if arch_cfg else None

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=arch_cfg.effort if arch_cfg else None,
            auto_approve=arch_cfg.auto_approve if arch_cfg else False,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        sys.exit(1)

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        sys.exit(1)

    role = Role.load("architect", project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    click.echo(f"\n🔨 [Run: {run.run_id}] Invoking Architect ({adapter.name}) for task:")
    click.secho(f"   \"{task}\"\n", bold=True)

    result = stage.run(context)

    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 Architect Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run.run_id}/01_architect.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


@main.command(name="runs")
def list_runs():
    """List all previous Forge runs."""
    run_mgr = RunManager()
    runs = run_mgr.list_runs()
    if not runs:
        click.echo("No runs found in .forge/runs/")
        return

    click.echo(f"\nFound {len(runs)} Forge runs:\n")
    for r in runs:
        click.echo(f"  • {r.run_id} | {r.created_at} | Status: {r.status}")
        click.echo(f"    Task: {r.task[:70]}")


if __name__ == "__main__":
    main()

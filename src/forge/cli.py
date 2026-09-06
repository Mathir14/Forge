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


@main.command(name="critic")
@click.argument("target", type=str, required=False, default="Audit and critique the codebase for architecture, security, code smells, and maintainability.")
def critic_cmd(target: str):
    """Run Critic role to audit and expose flaws, code smells, and tech debt in TARGET."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    # Create a new run
    run = run_mgr.create_run(task=f"Critique: {target}")
    context = Context(run=run, project_root=root, config=config, git=git)

    # Load Critic role and configured adapter
    critic_cfg = config.stages.get("critic")
    adapter_name = critic_cfg.adapter if critic_cfg else "opencode"
    model = critic_cfg.model if critic_cfg else None

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=critic_cfg.effort if critic_cfg else None,
            auto_approve=critic_cfg.auto_approve if critic_cfg else False,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        sys.exit(1)

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        sys.exit(1)

    role = Role.load("critic", project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    click.echo(f"\n🧐 [Run: {run.run_id}] Invoking Codebase Critic ({adapter.name}) on:")
    click.secho(f"   \"{target}\"\n", bold=True)

    result = stage.run(context)

    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 Critic Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run.run_id}/00_critic.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


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


@main.command(name="planner")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Planner on (defaults to latest).")
def planner_cmd(run_id: str):
    """Run Planner role to break approved architecture into actionable tasks."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    try:
        run = run_mgr.resume(run_id)
    except Exception as e:
        click.secho(f"Error loading run: {e}", fg="red")
        click.echo("Run 'forge architect \"<task>\"' first to create an architecture specification.")
        sys.exit(1)

    # Check that architect output exists for this run
    arch_json = run_mgr.load_stage_json(run, "architect")
    arch_md = run_mgr.load_stage_markdown(run, "architect")
    if not arch_json and not arch_md:
        click.secho(f"No Architect artifacts found in {run.run_id}.", fg="red")
        click.echo("Planner requires prior Architect output. Run 'forge architect' first.")
        sys.exit(1)

    context = Context(run=run, project_root=root, config=config, git=git)

    # Load Planner role and configured adapter
    plan_cfg = config.stages.get("planner")
    adapter_name = plan_cfg.adapter if plan_cfg else "opencode"
    model = plan_cfg.model if plan_cfg else None

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=plan_cfg.effort if plan_cfg else None,
            auto_approve=plan_cfg.auto_approve if plan_cfg else False,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        sys.exit(1)

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        sys.exit(1)

    role = Role.load("planner", project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    click.echo(f"\n📋 [Run: {run.run_id}] Invoking Planner ({adapter.name}) for task:")
    click.secho(f"   \"{run.task}\"\n", bold=True)

    result = stage.run(context)

    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 Planner Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run.run_id}/02_planner.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


@main.command(name="execute")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Executor on (defaults to latest).")
def execute_cmd(run_id: str):
    """Run Executor role (Antigravity) to implement the approved plan."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    try:
        run = run_mgr.resume(run_id)
    except Exception as e:
        click.secho(f"Error loading run: {e}", fg="red")
        click.echo("Run 'forge architect' and 'forge planner' first.")
        sys.exit(1)

    # Check that planner output exists
    plan_json = run_mgr.load_stage_json(run, "planner")
    plan_md = run_mgr.load_stage_markdown(run, "planner")
    if not plan_json and not plan_md:
        click.secho(f"No Planner artifacts found in {run.run_id}.", fg="red")
        click.echo("Executor requires prior Planner output. Run 'forge planner' first.")
        sys.exit(1)

    context = Context(run=run, project_root=root, config=config, git=git)

    # Load Executor role and configured adapter
    exec_cfg = config.stages.get("executor")
    adapter_name = exec_cfg.adapter if exec_cfg else "antigravity"
    model = exec_cfg.model if exec_cfg else "gemini-3.7-flash-high"
    effort = exec_cfg.effort if exec_cfg else "high"
    auto_approve = exec_cfg.auto_approve if exec_cfg else True

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=effort,
            auto_approve=auto_approve,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        sys.exit(1)

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        sys.exit(1)

    role = Role.load("executor", project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    click.echo(f"\n⚡ [Run: {run.run_id}] Invoking Executor ({adapter.name}) for task:")
    click.secho(f"   \"{run.task}\"\n", bold=True)

    result = stage.run(context)

    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 Executor Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run.run_id}/03_executor.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


@main.command(name="review")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Reviewer on (defaults to latest).")
def review_cmd(run_id: str):
    """Run Reviewer role to perform adversarial audit on implementation and diffs."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    try:
        run = run_mgr.resume(run_id)
    except Exception as e:
        click.secho(f"Error loading run: {e}", fg="red")
        click.echo("Run 'forge architect', 'forge planner', and 'forge execute' first.")
        sys.exit(1)

    # Check that executor output exists
    exec_json = run_mgr.load_stage_json(run, "executor")
    exec_md = run_mgr.load_stage_markdown(run, "executor")
    if not exec_json and not exec_md:
        click.secho(f"No Executor artifacts found in {run.run_id}.", fg="red")
        click.echo("Reviewer requires prior Executor output. Run 'forge execute' first.")
        sys.exit(1)

    context = Context(run=run, project_root=root, config=config, git=git)

    # Load Reviewer role and configured adapter
    rev_cfg = config.stages.get("reviewer")
    adapter_name = rev_cfg.adapter if rev_cfg else "opencode"
    model = rev_cfg.model if rev_cfg else None

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=rev_cfg.effort if rev_cfg else None,
            auto_approve=rev_cfg.auto_approve if rev_cfg else False,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        sys.exit(1)

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        sys.exit(1)

    role = Role.load("reviewer", project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    click.echo(f"\n🔍 [Run: {run.run_id}] Invoking Reviewer ({adapter.name}) for task:")
    click.secho(f"   \"{run.task}\"\n", bold=True)

    result = stage.run(context)

    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 Reviewer Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run.run_id}/04_reviewer.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


@main.command(name="run")
@click.argument("task", type=str, required=False, default=None)
@click.option("--from-critic", "-c", is_flag=True, default=False, help="Automatically resume from the latest Critic audit report.")
@click.option("--run", "run_id", type=str, default=None, help="Existing Run ID to resume from.")
@click.option("--autonomous", "-a", is_flag=True, default=False, help="Run all stages autonomously without confirmation prompts.")
def run_pipeline(task: Optional[str], from_critic: bool, run_id: Optional[str], autonomous: bool):
    """Run full multi-agent pipeline: (Critic ->) Architect -> Planner -> Executor -> Reviewer."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    # 1. Determine Run & Task
    if from_critic or run_id:
        try:
            run = run_mgr.resume(run_id)
        except Exception as e:
            click.secho(f"Error loading run: {e}", fg="red")
            sys.exit(1)

        critic_md = run_mgr.load_stage_markdown(run, "critic")
        if not critic_md:
            click.secho(f"No Critic report found in {run.run_id}.", fg="red")
            sys.exit(1)

        task = task or f"Implement recommendations and fix issues identified in Critic report ({run.run_id})."
        click.echo(f"\n🔗 Linking pipeline to Critic report in {run.run_id}!")
    else:
        if not task:
            click.secho("Error: Missing TASK. Please provide a task or use --from-critic.", fg="red")
            sys.exit(1)
        run = run_mgr.create_run(task=task)

    context = Context(run=run, project_root=root, config=config, git=git)

    stages_to_run = ["architect", "planner", "executor", "reviewer"]
    click.echo(f"\n🚀 [Run: {run.run_id}] Starting Forge Pipeline for task:")
    click.secho(f"   \"{run.task}\"\n", bold=True)

    for stage_name in stages_to_run:
        stage_cfg = config.stages.get(stage_name)
        adapter_name = stage_cfg.adapter if stage_cfg else "opencode"
        model = stage_cfg.model if stage_cfg else None
        effort = stage_cfg.effort if stage_cfg else None
        auto_approve = stage_cfg.auto_approve if stage_cfg else (stage_name == "executor")

        try:
            adapter = AdapterRegistry.get(
                name=adapter_name,
                model=model,
                effort=effort,
                auto_approve=auto_approve,
            )
        except Exception as e:
            click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
            sys.exit(1)

        if not adapter.is_available():
            click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
            sys.exit(1)

        role = Role.load(stage_name, project_root=root)
        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

        click.echo(f"\n▶ Executing Stage: {role.sequence_number:02d}_{role.name.upper()} ({adapter.name})...")
        result = stage.run(context)

        click.echo(f"  ✓ {role.name.capitalize()} completed | Status: {result.status} ({result.duration_seconds:.1f}s)")

        if result.status in ("REJECTED", "BLOCKED", "FAILED"):
            click.secho(f"\n⚠️ Pipeline halted at stage '{role.name}' due to status '{result.status}'.", fg="yellow")
            run.status = result.status
            run.save_metadata()
            return

        if not autonomous and stage_name != stages_to_run[-1]:
            if not click.confirm(f"\nProceed to next stage ({stages_to_run[stages_to_run.index(stage_name) + 1].upper()})?", default=True):
                click.echo("Pipeline paused by user.")
                run.status = f"PAUSED_AFTER_{role.name.upper()}"
                run.save_metadata()
                return

    run.status = "COMPLETED"
    run.save_metadata()
    click.secho(f"\n✨ Forge Pipeline completed successfully for {run.run_id}!", fg="green", bold=True)
    click.echo(f"   Artifacts saved in .forge/runs/{run.run_id}/\n")
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

"""Forge CLI Interface."""

import sys
from pathlib import Path
from typing import Optional, Dict, Any, List
import click

from forge.core.config import Config
from forge.core.git import GitService
from forge.core.role import Role
from forge.core.context import Context
from forge.stages.result import StageResult
from forge.adapters.base import BaseAdapter
from forge.adapters.registry import AdapterRegistry
from forge.storage.run_manager import RunManager
from forge.stages.stage import Stage


def _get_adapter(config: Config, stage_name: str) -> BaseAdapter:
    """Resolve and validate adapter for a given stage."""
    stage_cfg = config.stages.get(stage_name)
    if stage_name == "executor":
        adapter_name = stage_cfg.adapter if (stage_cfg and stage_cfg.adapter) else "antigravity"
        model = stage_cfg.model if (stage_cfg and stage_cfg.model) else "gemini-3.7-flash-high"
        effort = stage_cfg.effort if (stage_cfg and stage_cfg.effort) else "high"
        auto_approve = stage_cfg.auto_approve if stage_cfg else False
    else:
        adapter_name = stage_cfg.adapter if (stage_cfg and stage_cfg.adapter) else "opencode"
        model = stage_cfg.model if stage_cfg else None
        effort = stage_cfg.effort if stage_cfg else None
        auto_approve = stage_cfg.auto_approve if stage_cfg else False

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

    return adapter


def _print_stage_summary(result: StageResult, run_id: str, role_name: str, seq: int) -> None:
    """Print standard stage completion summary and issues."""
    click.echo("=" * 60)
    click.echo(result.raw_markdown)
    click.echo("=" * 60)

    click.echo(f"\n📊 {role_name.capitalize()} Summary:")
    click.echo(f"  • Status:       {result.status}")
    click.echo(f"  • Handoff:      {result.handoff}")
    click.echo(f"  • Prompt Hash:  {result.prompt.prompt_hash}")
    click.echo(f"  • Duration:     {result.duration_seconds:.2f}s")
    click.echo(f"  • Exit Code:    {result.response.exit_code}")
    click.echo(f"  • Artifacts:    .forge/runs/{run_id}/{seq:02d}_{role_name}.md & .json\n")

    if result.machine_report.issues:
        click.echo("⚠️ Reported Issues:")
        for severity, issues in result.machine_report.issues.items():
            for issue in issues:
                click.echo(f"  [{severity}] {issue}")


@click.group()
@click.version_option(version="0.1.0", prog_name="forge")
def main():
    """Forge: CLI-First Multi-Agent Orchestration Framework."""
    pass


STAGE_FORMATS = {
    "critic": ("🧐", "Codebase Critic", "on:"),
    "architect": ("🔨", "Architect", "for task:"),
    "planner": ("📋", "Planner", "for task:"),
    "executor": ("⚡", "Executor", "for task:"),
    "reviewer": ("🔍", "Reviewer", "for task:"),
}


def _run_stage(
    stage_name: str,
    task: Optional[str] = None,
    display_task: Optional[str] = None,
    run_id: Optional[str] = None,
    resume: bool = False,
    resume_error: Optional[str] = None,
    prerequisite_stage: Optional[str] = None,
    prerequisite_error: Optional[str] = None,
) -> None:
    """Execute a single stage with common setup and teardown."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    # Get or create run
    if resume or run_id:
        try:
            run = run_mgr.resume(run_id)
        except Exception as e:
            click.secho(f"Error loading run: {e}", fg="red")
            if resume_error:
                click.echo(resume_error)
            sys.exit(1)
    else:
        if not task:
            click.secho(f"Error: Missing task for {stage_name}.", fg="red")
            sys.exit(1)
        run = run_mgr.create_run(task=task)

    # Check prerequisite stage exists
    if prerequisite_stage:
        pre_json = run_mgr.load_stage_json(run, prerequisite_stage)
        pre_md = run_mgr.load_stage_markdown(run, prerequisite_stage)
        if not pre_json and not pre_md:
            click.secho(f"No {prerequisite_stage.capitalize()} artifacts found in {run.run_id}.", fg="red")
            click.echo(prerequisite_error or f"Run 'forge {prerequisite_stage}' first.")
            sys.exit(1)

    context = Context(run=run, project_root=root, config=config, git=git)
    adapter = _get_adapter(config, stage_name)
    role = Role.load(stage_name, project_root=root)
    stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

    emoji, title, preposition = STAGE_FORMATS.get(stage_name, ("🔄", stage_name.capitalize(), "for task:"))
    shown_text = display_task if display_task is not None else run.task
    click.echo(f"\n{emoji} [Run: {run.run_id}] Invoking {title} ({adapter.name}) {preposition}")
    click.secho(f"   \"{shown_text}\"\n", bold=True)

    result = stage.run(context)
    _print_stage_summary(result, run.run_id, stage_name, role.sequence_number)


def _resolve_pipeline_run(
    run_mgr: RunManager,
    task: Optional[str],
    from_critic: bool,
    run_id: Optional[str],
    pipeline_type: str = "pipeline",
):
    """Resolve or resume run for pipeline/auto execution."""
    if from_critic or (run_id and not task):
        try:
            prev_run = run_mgr.resume(run_id)
        except Exception as e:
            click.secho(f"Error loading run: {e}", fg="red")
            sys.exit(1)

        critic_md = run_mgr.load_stage_markdown(prev_run, "critic")
        if not critic_md:
            click.secho(f"No Critic report found in {prev_run.run_id}.", fg="red")
            sys.exit(1)

        task = task or f"Fix issues and tech debt identified in Critic audit from {prev_run.run_id}"
        run = run_mgr.create_run(task=task)
        critic_json = run_mgr.load_stage_json(prev_run, "critic") or {}
        run_mgr.save_stage_artifacts(
            run=run,
            sequence_number=0,
            role_name="critic",
            markdown_content=critic_md,
            json_data=critic_json,
            adapter_name="critic_handoff",
        )
        verb = "autonomous loop" if pipeline_type == "auto" else "pipeline"
        click.echo(f"\n🔗 Linked {verb} to previous Critic report from {prev_run.run_id}!")
    else:
        if not task:
            if pipeline_type == "auto":
                click.secho("Error: Missing TASK. Provide a task string, --file spec.md, or --from-critic (-c).", fg="red")
            else:
                click.secho("Error: Missing TASK. Please provide a task or use --from-critic (-c).", fg="red")
            sys.exit(1)
        run = run_mgr.create_run(task=task)
    return run


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
    auto_approve: false
  reviewer:
    adapter: opencode
    model: null

execution:
  mode: interactive
  auto_commit: false
  timeout: 300
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
    _run_stage(
        stage_name="critic",
        task=f"Critique: {target}",
        display_task=target,
    )


@main.command(name="architect")
@click.argument("task", type=str)
def architect_cmd(task: str):
    """Run Architect role to generate system architecture for TASK."""
    _run_stage(
        stage_name="architect",
        task=task,
    )


@main.command(name="planner")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Planner on (defaults to latest).")
def planner_cmd(run_id: Optional[str]):
    """Run Planner role to break approved architecture into actionable tasks."""
    _run_stage(
        stage_name="planner",
        run_id=run_id,
        resume=True,
        resume_error='Run \'forge architect "<task>"\' first to create an architecture specification.',
        prerequisite_stage="architect",
        prerequisite_error="Planner requires prior Architect output. Run 'forge architect' first.",
    )


@main.command(name="execute")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Executor on (defaults to latest).")
def execute_cmd(run_id: Optional[str]):
    """Run Executor role (Antigravity) to implement the approved plan."""
    _run_stage(
        stage_name="executor",
        run_id=run_id,
        resume=True,
        resume_error="Run 'forge architect' and 'forge planner' first.",
        prerequisite_stage="planner",
        prerequisite_error="Executor requires prior Planner output. Run 'forge planner' first.",
    )


@main.command(name="review")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Reviewer on (defaults to latest).")
def review_cmd(run_id: Optional[str]):
    """Run Reviewer role to perform adversarial audit on implementation and diffs."""
    _run_stage(
        stage_name="reviewer",
        run_id=run_id,
        resume=True,
        resume_error="Run 'forge architect', 'forge planner', and 'forge execute' first.",
        prerequisite_stage="executor",
        prerequisite_error="Reviewer requires prior Executor output. Run 'forge execute' first.",
    )


@main.command(name="run")
@click.argument("task", type=str, required=False, default=None)
@click.option("--from-critic", "-c", is_flag=True, default=False, help="Automatically resume from the latest Critic audit report.")
@click.option("--run", "run_id", type=str, default=None, help="Existing Run ID to resume from.")
@click.option("--no-critic", is_flag=True, default=False, help="Skip the final post-execution codebase health audit.")
def run_pipeline(task: Optional[str], from_critic: bool, run_id: Optional[str], no_critic: bool):
    """Run standard multi-agent pipeline with step-by-step confirmation checkpoints."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    run = _resolve_pipeline_run(run_mgr, task, from_critic, run_id, pipeline_type="pipeline")
    context = Context(run=run, project_root=root, config=config, git=git)

    stages_to_run = ["architect", "planner", "executor", "reviewer"]
    if not no_critic:
        stages_to_run.append("critic")

    click.echo(f"\n🚀 [Run: {run.run_id}] Starting Standard Forge Pipeline:")
    click.secho(f"   \"{run.task}\"\n", bold=True)

    for stage_name in stages_to_run:
        adapter = _get_adapter(config, stage_name)
        seq = 5 if (stage_name == "critic" and len(stages_to_run) == 5) else None
        role = Role.load(stage_name, project_root=root)
        if seq is not None:
            role = Role(
                name=role.name,
                sequence_number=seq,
                template_content=role.template_content,
                protocol_content=role.protocol_content,
            )

        stage = Stage(role=role, adapter=adapter, run_manager=run_mgr)

        click.echo(f"\n▶ Executing Stage: {role.sequence_number:02d}_{role.name.upper()} ({adapter.name})...")
        result = stage.run(context)

        click.echo(f"  ✓ {role.name.capitalize()} completed | Status: {result.status} ({result.duration_seconds:.1f}s)")

        if result.status in ("REJECTED", "BLOCKED", "FAILED"):
            click.secho(f"\n⚠️ Pipeline halted at stage '{role.name}' due to status '{result.status}'.", fg="yellow")
            run.status = result.status
            run.save_metadata()
            return

        if stage_name != stages_to_run[-1]:
            next_stage_name = stages_to_run[stages_to_run.index(stage_name) + 1]
            if not click.confirm(f"\nProceed to next stage ({next_stage_name.upper()})?", default=True):
                click.echo("Pipeline paused by user.")
                run.status = f"PAUSED_AFTER_{role.name.upper()}"
                run.save_metadata()
                return

    run.status = "COMPLETED"
    run.save_metadata()
    click.secho(f"\n✨ Forge Pipeline completed successfully for {run.run_id}!", fg="green", bold=True)
    click.echo(f"   Artifacts saved in .forge/runs/{run.run_id}/\n")


@main.command(name="auto")
@click.argument("task", type=str, required=False, default=None)
@click.option("--file", "-f", "spec_file", type=click.Path(exists=True, dir_okay=False), help="Path to markdown spec/requirements file.")
@click.option("--from-critic", "-c", is_flag=True, default=False, help="Automatically resume from the latest Critic audit report.")
@click.option("--run", "run_id", type=str, default=None, help="Existing Run ID to resume from.")
@click.option("--max-retries", "-r", type=click.IntRange(min=1), default=3, help="Max auto-repair retry iterations between Executor and Reviewer.")
@click.option("--auto-commit", is_flag=True, default=False, help="Automatically git commit upon approved review.")
@click.option("--no-critic", is_flag=True, default=False, help="Skip the final post-execution codebase health audit.")
def auto_pipeline(
    task: Optional[str],
    spec_file: Optional[str],
    from_critic: bool,
    run_id: Optional[str],
    max_retries: int,
    auto_commit: bool,
    no_critic: bool,
):
    """Run fully autonomous iterative loop: Architect -> Planner -> [Executor <-> Reviewer Self-Repair Loop] -> Critic."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    # 1. Determine task
    if spec_file:
        try:
            with open(spec_file, "r", encoding="utf-8") as f:
                task = f.read()
            click.echo(f"📄 Loaded requirements spec from {spec_file} ({len(task)} chars)")
        except Exception as e:
            click.secho(f"Error reading spec file '{spec_file}': {e}", fg="red")
            sys.exit(1)

    run = _resolve_pipeline_run(run_mgr, task, from_critic, run_id, pipeline_type="auto")
    context = Context(run=run, project_root=root, config=config, git=git)
    task_summary = task.strip().splitlines()[0][:70] if task else ""

    click.echo(f"\n⚡ [Run: {run.run_id}] Starting Fully Autonomous Forge Loop:")
    click.secho(f"   \"{task_summary}...\"\n", bold=True)

    # Stage 1: Architect
    arch_role = Role.load("architect", project_root=root)
    arch_adapter = _get_adapter(config, "architect")
    click.echo(f"▶ [1/5] Executing Architect ({arch_adapter.name})...")
    arch_stage = Stage(role=arch_role, adapter=arch_adapter, run_manager=run_mgr)
    arch_res = arch_stage.run(context)
    click.echo(f"  ✓ Architect completed | Status: {arch_res.status} ({arch_res.duration_seconds:.1f}s)")

    if arch_res.status in ("REJECTED", "BLOCKED"):
        click.secho(f"\n⚠️ Autonomous loop halted: Architect rejected design with status '{arch_res.status}'.", fg="yellow")
        run.status = arch_res.status
        run.save_metadata()
        return

    # Stage 2: Planner
    plan_role = Role.load("planner", project_root=root)
    plan_adapter = _get_adapter(config, "planner")
    click.echo(f"\n▶ [2/5] Executing Planner ({plan_adapter.name})...")
    plan_stage = Stage(role=plan_role, adapter=plan_adapter, run_manager=run_mgr)
    plan_res = plan_stage.run(context)
    click.echo(f"  ✓ Planner completed | Status: {plan_res.status} ({plan_res.duration_seconds:.1f}s)")

    if plan_res.status in ("BLOCKED", "REJECTED"):
        click.secho(f"\n⚠️ Autonomous loop halted: Planner blocked with status '{plan_res.status}'.", fg="yellow")
        run.status = plan_res.status
        run.save_metadata()
        return

    # Stage 3 & 4: Self-Healing Executor <-> Reviewer Loop
    exec_adapter = _get_adapter(config, "executor")
    exec_role = Role.load("executor", project_root=root)
    exec_stage = Stage(role=exec_role, adapter=exec_adapter, run_manager=run_mgr)

    rev_adapter = _get_adapter(config, "reviewer")
    rev_role = Role.load("reviewer", project_root=root)
    rev_stage = Stage(role=rev_role, adapter=rev_adapter, run_manager=run_mgr)

    approved = False
    max_retries = max(1, max_retries)
    for iteration in range(1, max_retries + 1):
        iter_label = f" (Attempt {iteration}/{max_retries})" if max_retries > 1 else ""
        click.echo(f"\n▶ [3/5] Executing Executor ({exec_adapter.name}){iter_label}...")
        exec_res = exec_stage.run(context)
        click.echo(f"  ✓ Executor finished | Status: {exec_res.status} ({exec_res.duration_seconds:.1f}s)")

        click.echo(f"\n▶ [4/5] Executing Reviewer ({rev_adapter.name}){iter_label}...")
        rev_res = rev_stage.run(context)
        click.echo(f"  ✓ Reviewer finished | Status: {rev_res.status} ({rev_res.duration_seconds:.1f}s)")

        if rev_res.status == "APPROVED":
            approved = True
            click.secho(f"\n✅ Implementation APPROVED by Reviewer on attempt {iteration}!", fg="green", bold=True)
            break
        elif rev_res.status == "CHANGES_REQUIRED" and iteration < max_retries:
            click.secho(f"\n🔄 Reviewer requested changes. Launching auto-repair iteration {iteration + 1}...", fg="yellow")
        else:
            click.secho(f"\n⚠️ Reviewer verdict: {rev_res.status}.", fg="yellow")
            break

    # Auto-commit if approved and requested
    if approved and (auto_commit or config.execution.auto_commit):
        commit_msg = f"feat: {task_summary}"
        if git.is_git_repo() and git.commit(commit_msg):
            click.secho(f"  ✓ Auto-committed changes: '{commit_msg}'", fg="green")

    # Stage 5: Closing Critic Audit
    if not no_critic:
        critic_adapter = _get_adapter(config, "critic")
        base_critic = Role.load("critic", project_root=root)
        critic_role = Role(
            name=base_critic.name,
            sequence_number=5,
            template_content=base_critic.template_content,
            protocol_content=base_critic.protocol_content,
        )
        click.echo(f"\n▶ [5/5] Executing Post-Execution Critic ({critic_adapter.name})...")
        critic_stage = Stage(role=critic_role, adapter=critic_adapter, run_manager=run_mgr)
        critic_res = critic_stage.run(context)
        click.echo(f"  ✓ Post-Execution Critic audit completed | Status: {critic_res.status} ({critic_res.duration_seconds:.1f}s)")

    run.status = "APPROVED" if approved else rev_res.status
    run.save_metadata()
    click.secho(f"\n✨ Autonomous Loop finished for {run.run_id} (Status: {run.status})!", fg="green", bold=True)
    click.echo(f"   Artifacts saved in .forge/runs/{run.run_id}/\n")


if __name__ == "__main__":
    main()

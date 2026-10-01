import json
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List
import click
import yaml

from forge import __version__
from forge.core.config import Config, StageConfig, ConfigValidationError
from forge.core.git import GitService, GitBaseline
from forge.core.role import Role
from forge.core.context import Context
from forge.stages.result import StageResult
from forge.adapters.base import BaseAdapter
from forge.adapters.antigravity import AntigravityAdapter
from forge.adapters.registry import AdapterRegistry
from forge.core.capabilities import Capability, CapabilityValidationError
from forge.storage.run_manager import RunManager
from forge.storage.run_lock import RunLock, RunOwnershipError
from forge.stages.stage import Stage
from forge.stages.definition import StageDefinition, StageOrder


def _get_adapter(
    config: Config,
    stage_name: str,
    phase: str = "pre_run",
    validate_capabilities: bool = True,
    exit_on_error: bool = True,
) -> BaseAdapter:
    """Resolve and validate adapter for a given stage using resolved StageConfig."""
    stage_cfg = config.get_stage_config(stage_name, phase=phase)

    adapter_name = stage_cfg.adapter or "opencode"
    model = stage_cfg.model
    effort = stage_cfg.effort
    auto_approve = stage_cfg.auto_approve if stage_cfg.auto_approve is not None else False
    extra_flags = stage_cfg.extra_flags

    # Provider default fallback from adapter class when model/effort is None
    try:
        adapter_cls = AdapterRegistry.get_class(adapter_name)
        if not model and getattr(adapter_cls, "DEFAULT_MODEL", None):
            model = adapter_cls.DEFAULT_MODEL
        if not effort and getattr(adapter_cls, "DEFAULT_EFFORT", None):
            effort = adapter_cls.DEFAULT_EFFORT
    except Exception:
        pass

    if auto_approve:
        click.secho("⚠️  SECURITY WARNING: auto_approve is ENABLED for this stage. CLI agent has permission to execute commands without confirmation.", fg="yellow")

    try:
        adapter = AdapterRegistry.get(
            name=adapter_name,
            model=model,
            effort=effort,
            auto_approve=auto_approve,
            extra_flags=extra_flags,
        )
    except Exception as e:
        click.secho(f"Error initializing adapter '{adapter_name}': {e}", fg="red")
        if exit_on_error:
            sys.exit(1)
        raise

    if not adapter.is_available():
        click.secho(f"Adapter tool '{adapter.name}' is not installed or not in PATH.", fg="red")
        click.echo("Run 'forge doctor' to inspect available tools.")
        if exit_on_error:
            sys.exit(1)
        raise RuntimeError(f"Adapter tool '{adapter.name}' is not installed or not in PATH.")

    # Validate compatibility before execution
    if validate_capabilities:
        required_caps = Stage.get_required_capabilities(stage_name)
        provided_caps = adapter.capabilities()
        missing_caps = required_caps - provided_caps
        if missing_caps:
            click.secho(f"\n❌ Capability validation error for stage '{stage_name}':", fg="red", bold=True)
            click.echo(f"\n{stage_name.capitalize()} requires:")
            for cap in sorted(required_caps):
                click.echo(f"  • {cap}")
            click.echo(f"\nConfigured adapter:\n  {adapter.name}")
            click.echo(f"\nMissing:")
            for cap in sorted(missing_caps):
                click.secho(f"  • {cap}", fg="red")
            click.echo("\nAbort before execution. Never fail halfway through execution because of unsupported functionality.\n")
            if exit_on_error:
                sys.exit(1)
            raise CapabilityValidationError(
                stage_name=stage_name,
                adapter_name=adapter.name,
                required_capabilities=required_caps,
                provided_capabilities=provided_caps,
                missing_capabilities=missing_caps,
            )

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
                if isinstance(issue, dict):
                    desc = issue.get("DESCRIPTION") or issue.get("description") or str(issue)
                    click.echo(f"  [{severity}] {desc}")
                else:
                    click.echo(f"  [{severity}] {issue}")


def _setup_cli_sigterm_handler() -> None:
    """Install graceful SIGTERM handler exclusively at the CLI boundary, preserving/chaining existing handlers."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        prev_handler = signal.getsignal(signal.SIGTERM)
        if prev_handler == signal.SIG_IGN:
            return

        def _handle_sigterm(signum, frame):
            if callable(prev_handler) and prev_handler not in (signal.SIG_DFL, signal.SIG_IGN):
                try:
                    prev_handler(signum, frame)
                except Exception:
                    pass
            sys.exit(128 + signum)

        signal.signal(signal.SIGTERM, _handle_sigterm)
    except (ValueError, OSError):
        pass


@click.group()
@click.version_option(version=__version__, prog_name="forge")
def main():
    """Forge: CLI-First Multi-Agent Orchestration Framework."""
    _setup_cli_sigterm_handler()


STAGE_SUCCESS_STATUSES: Dict[str, Set[str]] = {
    s.name: set(s.success_statuses) for s in StageOrder.all_stages()
}


def is_stage_completed(run: Any, stage_def: StageDefinition, run_mgr: RunManager) -> tuple[bool, Optional[str]]:
    """Check if a stage in a resumed run has already completed successfully."""
    prior_json = run_mgr.load_stage_json(run, stage_def)
    if not prior_json:
        return False, None
    prior_status = prior_json.get("status") or prior_json.get("STATUS")
    if not prior_status:
        return False, None
    valid_statuses = stage_def.success_statuses or StageOrder.get_success_statuses(stage_def.name, phase=stage_def.phase)
    if prior_status in valid_statuses:
        return True, prior_status
    return False, prior_status


def execute_stage(
    stage_def: StageDefinition,
    context: Context,
    run_mgr: RunManager,
    display_task: Optional[str] = None,
    banner_prefix: str = "",
) -> StageResult:
    """Execute a single stage defined by StageDefinition using the shared execution lifecycle."""
    config = context.config
    stage_cfg = config.get_stage_config(stage_def.name, phase=stage_def.phase)
    adapter = _get_adapter(config, stage_def.name, phase=stage_def.phase)
    role = Role.load(
        stage_def.name,
        project_root=context.project_root,
        sequence_number=stage_def.sequence_number,
        phase=stage_def.phase,
    )
    stage = Stage(
        role=role,
        adapter=adapter,
        run_manager=run_mgr,
        timeout=stage_cfg.timeout,
        event_listener=getattr(context, "event_listener", None),
    )

    if getattr(context, "event_listener", None) is not None:
        try:
            from forge.core.events import AgentEvent, AgentEventType
            context.event_listener(AgentEvent(
                event_type=AgentEventType.HEARTBEAT,
                timestamp=time.time(),
                data={
                    "lifecycle": "stage_start",
                    "stage_name": stage_def.artifact_prefix,
                    "role_name": stage_def.name,
                    "sequence_number": stage_def.sequence_number,
                    "display_name": stage_def.display_name,
                    "banner_prefix": banner_prefix,
                },
            ))
        except Exception:
            pass

    prefix_str = f"{banner_prefix} " if banner_prefix else ""
    click.echo(f"\n{prefix_str}▶ Executing Stage: {stage_def.artifact_prefix.upper()} ({adapter.name})...")
    result = stage.run(context)

    if getattr(context, "event_listener", None) is not None:
        try:
            from forge.core.events import AgentEvent, AgentEventType
            context.event_listener(AgentEvent(
                event_type=AgentEventType.HEARTBEAT,
                timestamp=time.time(),
                data={
                    "lifecycle": "stage_finish",
                    "stage_name": stage_def.artifact_prefix,
                    "role_name": stage_def.name,
                    "sequence_number": stage_def.sequence_number,
                    "status": result.status,
                    "duration_seconds": result.duration_seconds,
                },
            ))
        except Exception:
            pass

    return result


def _run_stage(
    stage_name: str,
    task: Optional[str] = None,
    display_task: Optional[str] = None,
    run_id: Optional[str] = None,
    resume: bool = False,
    resume_error: Optional[str] = None,
    prerequisite_stage: Optional[str] = None,
    prerequisite_error: Optional[str] = None,
    phase: str = "pre_run",
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

    try:
        with RunLock(run.run_dir, run_id=run.run_id) as run_lock_inst:
            if run_lock_inst.is_stale_recovered:
                click.secho(
                    f"⚠️ Recovered stale lock for run '{run.run_id}' (previous process terminated abnormally).",
                    fg="yellow",
                )

            # Check prerequisite stage exists and succeeded
            if prerequisite_stage:
                pre_json = run_mgr.load_stage_json(run, prerequisite_stage)
                pre_md = run_mgr.load_stage_markdown(run, prerequisite_stage)
                if not pre_json and not pre_md:
                    click.secho(f"No {prerequisite_stage.capitalize()} artifacts found in {run.run_id}.", fg="red")
                    click.echo(prerequisite_error or f"Run 'forge {prerequisite_stage}' first.")
                    sys.exit(1)
                pre_status = str((pre_json or {}).get("status") or (pre_json or {}).get("STATUS") or "").upper()
                valid_statuses = StageOrder.get_success_statuses(prerequisite_stage.lower())
                if pre_status not in valid_statuses:
                    click.secho(f"Prerequisite stage '{prerequisite_stage}' did not succeed in {run.run_id} (Status: '{pre_status or 'UNKNOWN'}').", fg="red")
                    click.echo(f"Cannot run '{stage_name}' on an unapproved or failed {prerequisite_stage.capitalize()}.")
                    sys.exit(1)

            stage_baseline: Optional[GitBaseline] = None
            if git.is_git_repo():
                baseline_file = run.run_dir / "git_baseline.json"
                if baseline_file.exists():
                    try:
                        stage_baseline = GitBaseline.load(baseline_file)
                    except Exception:
                        stage_baseline = git.capture_baseline()
                        stage_baseline.save(baseline_file)
                else:
                    stage_baseline = git.capture_baseline()
                    stage_baseline.save(baseline_file)
            context = Context(run=run, project_root=root, config=config, git=git, baseline=stage_baseline)

            stage_def = StageOrder.resolve_definition(stage_name, phase=phase)
            stage_cfg = config.get_stage_config(stage_name, phase=stage_def.phase)
            adapter = _get_adapter(config, stage_name, phase=stage_def.phase)
            role = Role.load(stage_name, project_root=root, sequence_number=stage_def.sequence_number, phase=stage_def.phase)
            stage = Stage(role=role, adapter=adapter, run_manager=run_mgr, timeout=stage_cfg.timeout)

            shown_text = display_task if display_task is not None else run.task
            click.echo(f"\n{stage_def.emoji} [Run: {run.run_id}] Invoking {stage_def.display_name} ({adapter.name}) {stage_def.preposition}")
            click.secho(f"   \"{shown_text}\"\n", bold=True)

            result = stage.run(context)
            _print_stage_summary(result, run.run_id, stage_name, role.sequence_number)

            if not result.success or not result.machine_report.is_valid or result.status in ("REJECTED", "BLOCKED", "FAILED", "UNKNOWN", "CHANGES_REQUIRED"):
                click.secho(f"\n⚠️ Stage '{stage_name}' finished with non-success status '{result.status}'.", fg="red")
                run.status = result.status if result.status in ("REJECTED", "BLOCKED", "CHANGES_REQUIRED") else "FAILED"
                run.save_metadata()
                sys.exit(1)

            run.status = result.status
            run.save_metadata()
    except RunOwnershipError as e:
        click.echo(e.format_diagnostic(), err=True)
        sys.exit(1)


def _resolve_pipeline_run(
    run_mgr: RunManager,
    task: Optional[str],
    from_critic: bool,
    run_id: Optional[str],
    pipeline_type: str = "pipeline",
) -> tuple["Run", bool]:
    """Resolve or resume run for pipeline/auto execution, returning (run, is_resumed_same_task)."""
    if from_critic:
        try:
            prev_run = run_mgr.resume(run_id)
        except Exception as e:
            click.secho(f"Error loading run: {e}", fg="red")
            sys.exit(1)

        pre_critic = StageOrder.get_pre_run_critic()
        closing_critic = StageOrder.get_closing_critic()

        critic_stage_def = closing_critic
        critic_md = run_mgr.load_stage_markdown(prev_run, closing_critic)
        if not critic_md:
            critic_md = run_mgr.load_stage_markdown(prev_run, pre_critic)
            if critic_md:
                critic_stage_def = pre_critic
        if not critic_md:
            critic_md = run_mgr.load_stage_markdown(prev_run, "critic")
            critic_stage_def = closing_critic
        if not critic_md:
            click.secho(f"No Critic report found in {prev_run.run_id}.", fg="red")
            click.echo("Run 'forge critic' first, or provide a task directly.")
            sys.exit(1)

        task = task or f"Fix issues and tech debt identified in Critic audit from {prev_run.run_id}"
        run = run_mgr.create_run(task=task)
        critic_json = run_mgr.load_stage_json(prev_run, critic_stage_def) or run_mgr.load_stage_json(prev_run, "critic") or {}
        run_mgr.save_stage_artifacts(
            run=run,
            sequence_number=pre_critic.sequence_number,
            role_name=pre_critic.name,
            markdown_content=critic_md,
            json_data=critic_json,
            adapter_name=f"critic_handoff_from_{critic_stage_def.artifact_prefix}",
        )
        verb = "autonomous loop" if pipeline_type == "auto" else "pipeline"
        click.echo(f"\n🔗 Linked {verb} to previous Critic report (seq {critic_stage_def.sequence_number:02d}) from {prev_run.run_id}!")
        return run, False
    elif run_id:
        try:
            run = run_mgr.resume(run_id)
        except Exception as e:
            click.secho(f"Error loading run: {e}", fg="red")
            sys.exit(1)
        if task and task.strip() != run.task.strip():
            run.task = task
            run.save_metadata()
            return run, False
        return run, True
    else:
        if not task:
            if pipeline_type == "auto":
                click.secho("Error: Missing TASK. Provide a task string, --file spec.md, or --from-critic (-c).", fg="red")
            else:
                click.secho("Error: Missing TASK. Please provide a task or use --from-critic (-c).", fg="red")
            sys.exit(1)
        run = run_mgr.create_run(task=task)
        return run, False


@main.command(name="adapters")
@click.option("--json", "json_output", is_flag=True, default=False, help="Output adapters list and capabilities as JSON.")
def adapters_cmd(json_output: bool):
    """List registered adapters, supported capabilities, and defaults."""
    canonical = AdapterRegistry.list_canonical_adapters()

    if json_output:
        data = []
        for name, adapter_cls in canonical.items():
            caps = sorted(adapter_cls.get_capabilities())
            data.append({
                "adapter": name,
                "default_model": getattr(adapter_cls, "DEFAULT_MODEL", None),
                "capabilities": caps,
                "supports_session_resume": "session_resume" in caps,
                "supports_browser": "browser" in caps,
                "supports_playwright": "playwright" in caps,
                "supports_streaming": "streaming" in caps,
                "supports_structured_output": "structured_output" in caps,
            })
        click.echo(json.dumps(data, indent=2))
        return

    click.echo("\n🔌 Forge Adapters & Capabilities\n" + "=" * 50)
    for name, adapter_cls in canonical.items():
        caps = adapter_cls.get_capabilities()
        default_model = getattr(adapter_cls, "DEFAULT_MODEL", None) or "(provider default)"

        display_name = name.capitalize() if name != "agy" else "Antigravity (agy)"
        click.secho(f"\n{display_name}", bold=True)
        click.echo(f"  • Default model: {default_model}")

        click.echo("  • Feature Support:")
        features = [
            ("Supports session resume?", "session_resume" in caps),
            ("Supports browser?", "browser" in caps),
            ("Supports Playwright?", "playwright" in caps),
            ("Supports streaming?", "streaming" in caps),
            ("Supports structured output?", "structured_output" in caps),
        ]
        for query, supported in features:
            if supported:
                click.secho(f"    ✓ {query}", fg="green")
            else:
                click.secho(f"    ✗ {query}", fg="yellow")

        click.echo("  • Capabilities:")
        for cap in sorted(caps):
            click.secho(f"    ✓ {cap}", fg="green")

    click.echo("\n" + "=" * 50 + "\n")


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
    click.echo("\n[Configuration Defaults]")
    click.echo(f"  • Adapter: {config.defaults.adapter}")
    click.echo(f"  • Model:   {config.defaults.model or '(provider default)'}")
    click.echo(f"  • Effort:  {config.defaults.effort or '(provider default)'}")
    click.echo(f"  • Timeout: {config.defaults.timeout}s")

    click.echo("\n[Configured Stages]")
    seen_stages = set()
    for stage_def in StageOrder.all_stages():
        if stage_def.name in seen_stages:
            continue
        seen_stages.add(stage_def.name)
        stage_name = stage_def.name
        resolved_cfg = config.get_stage_config(stage_name, phase=stage_def.phase)
        model_str = f" [model: {resolved_cfg.model}]" if resolved_cfg.model else ""
        effort_str = f" [effort: {resolved_cfg.effort}]" if resolved_cfg.effort else ""
        timeout_str = f" [timeout: {resolved_cfg.timeout}s]" if resolved_cfg.timeout else ""
        click.echo(f"  • {stage_name.capitalize():<10} -> {resolved_cfg.adapter}{model_str}{effort_str}{timeout_str}")

    # 4. Check Tester Diagnostics
    click.echo("\n[Tester]")
    try:
        from forge.testing.browser import BrowserDriverFactory
        browser_diag = BrowserDriverFactory.check_environment()
    except Exception as e:
        browser_diag = {
            "playwright_installed": False,
            "chromium_installed": False,
            "evidence_enabled": True,
            "backend_name": f"Error: {e}",
            "error_message": str(e),
        }

    click.echo("  Browser Driver")
    if browser_diag.get("playwright_installed"):
        click.secho("    ✓ Playwright", fg="green")
    else:
        click.secho("    ✗ Playwright missing", fg="yellow")

    if browser_diag.get("chromium_installed"):
        click.secho("    ✓ Chromium", fg="green")
    else:
        click.secho("    ✗ Chromium missing", fg="yellow")

    click.echo("\n  Evidence Collection")
    click.secho("    ✓ Enabled", fg="green")

    click.echo("\n  Browser Backend")
    click.echo(f"    {browser_diag.get('backend_name')}")

    if not (browser_diag.get("playwright_installed") and browser_diag.get("chromium_installed")):
        click.secho("\n  ⚠️ Browser automation is unavailable. Web/UI interactions will fall back to MockBrowserDriver.", fg="yellow")
        click.echo("  To enable browser automation:")
        if not browser_diag.get("playwright_installed"):
            click.echo("    1. pip install playwright")
            click.echo("    2. playwright install chromium")
        else:
            click.echo("    playwright install chromium")

    click.echo("\n" + "=" * 50)


@main.command(name="init")
def init():
    """Initialize .ai/ prompt templates and forge.yaml in current repository."""
    from forge.core.templates import (
        DEFAULT_ROLES,
        DEFAULT_PROTOCOL,
        DEFAULT_PROJECT_DOCS,
        DEFAULT_FORGE_YAML,
    )
    root = Path.cwd()
    click.echo(f"Initializing Forge in {root}...")

    # Create directories
    ai_dir = root / ".ai"
    roles_dir = ai_dir / "roles"
    templates_dir = ai_dir / "templates"
    project_dir = ai_dir / "project"

    project_dir.mkdir(parents=True, exist_ok=True)
    roles_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)

    # Write default roles
    created_roles = 0
    for role_name, content in DEFAULT_ROLES.items():
        role_path = roles_dir / f"{role_name}.md"
        if not role_path.exists():
            role_path.write_text(content, encoding="utf-8")
            created_roles += 1

    # Write protocol
    proto_path = templates_dir / "protocol.md"
    if not proto_path.exists():
        proto_path.write_text(DEFAULT_PROTOCOL, encoding="utf-8")
        click.secho("  ✓ Created .ai/templates/protocol.md", fg="green")

    # Write project docs
    for doc_name, content in DEFAULT_PROJECT_DOCS.items():
        doc_path = project_dir / doc_name
        if not doc_path.exists():
            doc_path.write_text(content, encoding="utf-8")

    if created_roles > 0:
        click.secho(f"  ✓ Installed {created_roles} default role prompt templates in .ai/roles/", fg="green")

    # Create or update .gitignore to prevent committing .forge runs and cache
    gitignore_file = root / ".gitignore"
    needed_ignores = [".forge/runs/", ".forge/cache/"]
    if not gitignore_file.exists():
        gitignore_file.write_text("\n".join(needed_ignores) + "\n", encoding="utf-8")
        click.secho("  ✓ Created .gitignore with .forge/ protection", fg="green")
    else:
        existing_ignore = gitignore_file.read_text(encoding="utf-8")
        to_add = [entry for entry in needed_ignores if entry not in existing_ignore]
        if to_add:
            with open(gitignore_file, "a", encoding="utf-8") as f:
                f.write("\n# Forge runtime data\n" + "\n".join(to_add) + "\n")
            click.secho("  ✓ Appended .forge/ protection to .gitignore", fg="green")

    # Create forge.yaml if not present
    cfg_file = root / "forge.yaml"
    if not cfg_file.exists():
        with open(cfg_file, "w", encoding="utf-8") as f:
            f.write(DEFAULT_FORGE_YAML)
        click.secho("  ✓ Created forge.yaml", fg="green")
    else:
        click.echo("  • forge.yaml already exists")

    click.secho("\nInitialization complete! Run 'forge doctor' to verify.", fg="green")


@main.command(name="runs")
@click.option("--limit", "-n", type=int, default=10, help="Number of recent runs to display.")
@click.option("--json-output", "-j", is_flag=True, default=False, help="Output runs list as formatted JSON.")
def runs_cmd(limit: int, json_output: bool):
    """List historical runs, timestamps, and status."""
    root = Path.cwd()
    run_mgr = RunManager(root)
    total_count = run_mgr.count_runs()
    runs = run_mgr.list_runs(limit=limit if limit > 0 else None)
    if not runs:
        if json_output:
            click.echo("[]")
        else:
            click.echo("No runs found in .forge/runs/.")
        return

    selected_runs = runs
    if json_output:
        run_data = [
            {
                "run_id": r.run_id,
                "created_at": r.created_at,
                "status": r.status,
                "task": r.task,
                "adapters": r.adapters_used,
            }
            for r in selected_runs
        ]
        click.echo(json.dumps(run_data, indent=2))
        return

    click.echo(f"\n{'RUN ID':<12} {'CREATED AT':<24} {'STATUS':<16} {'TASK'}")
    click.echo("─" * 78)
    for r in selected_runs:
        clean_task = r.task.strip().splitlines()[0][:35] if (r.task and r.task.strip()) else ""
        click.echo(f"{r.run_id:<12} {r.created_at[:19]:<24} {r.status:<16} {clean_task}")
    click.echo(f"\nTotal runs: {total_count} (showing {len(selected_runs)})\n")


@main.command(name="dashboard")
@click.argument("run_id", type=str, required=False, default=None)
@click.option("--render-once", is_flag=True, default=False, help="Render a single snapshot and exit (non-interactive mode).")
def dashboard_cmd(run_id: Optional[str], render_once: bool):
    """Inspect Forge runs in an interactive terminal dashboard."""
    root = Path.cwd()
    run_mgr = RunManager(root)

    # Resolve run directory
    target_dir: Optional[Path] = None
    if run_id:
        clean_id = run_id if run_id.startswith("run-") else f"run-{run_id}"
        candidate = run_mgr.runs_dir / clean_id
        if not candidate.exists():
            candidate = run_mgr.runs_dir / run_id
        if not candidate.exists() or not candidate.is_dir():
            click.secho(f"Error: Run directory for '{run_id}' not found in {run_mgr.runs_dir}.", fg="red")
            latest_run = run_mgr.latest()
            if latest_run:
                click.echo(f"Most recent run is: {latest_run.run_id}")
            sys.exit(1)
        target_dir = candidate
    else:
        latest_run = run_mgr.latest()
        if not latest_run:
            click.secho("No runs found in .forge/runs/.", fg="yellow")
            click.echo("Execute a stage or pipeline first (e.g. 'forge architect' or 'forge run').")
            return
        target_dir = latest_run.run_dir

    from forge.dashboard.app import DashboardApp
    app = DashboardApp(target_dir)

    if render_once:
        click.echo(app.render_once(), nl=False)
        return

    sys.exit(app.run())


@main.command(name="critic")
@click.argument("target", type=str, required=False, default=None)
@click.option("--post-run", is_flag=True, default=False, help="Run closing Critic audit on an existing run.")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute post-run Critic on (defaults to latest if --post-run).")
def critic_cmd(target: Optional[str], post_run: bool, run_id: Optional[str]):
    """Run Critic role to audit and expose flaws, code smells, and tech debt in TARGET."""
    if post_run:
        default_target = "Post-execution codebase health and security audit."
        shown_target = target or default_target
        _run_stage(
            stage_name="critic",
            task=f"Critique: {shown_target}",
            display_task=shown_target,
            run_id=run_id,
            resume=True,
            resume_error="Run 'forge run' or 'forge auto' first to produce a run to audit.",
            prerequisite_stage="reviewer",
            prerequisite_error="Closing Critic requires prior Reviewer output. Run 'forge review' or pipeline first.",
            phase="post_run",
        )
    else:
        default_target = "Audit and critique the codebase for architecture, security, code smells, and maintainability."
        shown_target = target or default_target
        _run_stage(
            stage_name="critic",
            task=f"Critique: {shown_target}",
            display_task=shown_target,
            run_id=run_id,
            resume=bool(run_id),
            phase="pre_run",
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


@main.command(name="test")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Tester on (defaults to latest).")
def test_cmd(run_id: Optional[str]):
    """Run Tester role to evaluate observable software behavior and user experience."""
    _run_stage(
        stage_name="tester",
        run_id=run_id,
        resume=True,
        resume_error="Run 'forge architect', 'forge planner', and 'forge execute' first.",
        prerequisite_stage="executor",
        prerequisite_error="Tester requires prior Executor output. Run 'forge execute' first.",
    )


@main.command(name="review")
@click.option("--run", "run_id", type=str, default=None, help="Run ID to execute Reviewer on (defaults to latest).")
def review_cmd(run_id: Optional[str]):
    """Run Reviewer role to perform adversarial audit on implementation and diffs."""
    _run_stage(
        stage_name="reviewer",
        run_id=run_id,
        resume=True,
        resume_error="Run 'forge architect', 'forge planner', 'forge execute', and 'forge test' first.",
        prerequisite_stage="executor",
        prerequisite_error="Reviewer requires prior Executor output. Run 'forge execute' first.",
    )


@main.command(name="run")
@click.argument("task", type=str, required=False, default=None)
@click.option("--from-critic", "-c", is_flag=True, default=False, help="Automatically resume from the latest Critic audit report.")
@click.option("--run", "run_id", type=str, default=None, help="Existing Run ID to resume from.")
@click.option("--auto-commit", is_flag=True, default=False, help="Automatically git commit upon approved pipeline completion.")
@click.option("--no-critic", is_flag=True, default=False, help="Skip the final post-execution codebase health audit.")
def run_pipeline(task: Optional[str], from_critic: bool, run_id: Optional[str], auto_commit: bool, no_critic: bool):
    """Run standard multi-agent pipeline with step-by-step confirmation checkpoints."""
    root = Path.cwd()
    config = Config.load(root)
    git = GitService(root)
    run_mgr = RunManager(root)

    run, is_resumed_same_task = _resolve_pipeline_run(run_mgr, task, from_critic, run_id, pipeline_type="pipeline")
    try:
        with RunLock(run.run_dir, run_id=run.run_id) as run_lock_inst:
            if run_lock_inst.is_stale_recovered:
                click.secho(
                    f"⚠️ Recovered stale lock for run '{run.run_id}' (previous process terminated abnormally).",
                    fg="yellow",
                )

            git_baseline: Optional[GitBaseline] = None
            if git.is_git_repo():
                baseline_file = run.run_dir / "git_baseline.json"
                if baseline_file.exists():
                    try:
                        git_baseline = GitBaseline.load(baseline_file)
                    except Exception:
                        git_baseline = git.capture_baseline()
                        git_baseline.save(baseline_file)
                else:
                    git_baseline = git.capture_baseline()
                    git_baseline.save(baseline_file)

            context = Context(run=run, project_root=root, config=config, git=git, baseline=git_baseline)


            stages_to_run = StageOrder.standard_pipeline_stages(no_critic=no_critic)

            click.echo(f"\n🚀 [Run: {run.run_id}] Starting Standard Forge Pipeline:")
            click.secho(f"   \"{run.task}\"\n", bold=True)

            for stage_def in stages_to_run:
                # If resuming an existing run for the same task, skip stages that already completed successfully
                if is_resumed_same_task:
                    completed, prior_status = is_stage_completed(run, stage_def, run_mgr)
                    if completed:
                        click.echo(f"  ⏭ Skipping Stage: {stage_def.name.capitalize()} (already completed with status '{prior_status}')")
                        continue

                result = execute_stage(stage_def, context, run_mgr)

                click.echo(f"  ✓ {stage_def.name.capitalize()} completed | Status: {result.status} ({result.duration_seconds:.1f}s)")

                if not result.success or not result.machine_report.is_valid or result.status in ("REJECTED", "BLOCKED", "FAILED", "UNKNOWN", "CHANGES_REQUIRED"):
                    click.secho(f"\n⚠️ Pipeline halted at stage '{stage_def.name}' due to status '{result.status}' (Valid: {result.machine_report.is_valid}).", fg="red")
                    run.status = result.status if result.status in ("REJECTED", "BLOCKED", "CHANGES_REQUIRED") else "FAILED"
                    run.save_metadata()
                    sys.exit(1)

                if stage_def != stages_to_run[-1]:
                    next_stage_def = stages_to_run[stages_to_run.index(stage_def) + 1]
                    if not click.confirm(f"\nProceed to next stage ({next_stage_def.name.upper()})?", default=True):
                        click.echo("Pipeline paused by user.")
                        run.status = f"PAUSED_AFTER_{stage_def.name.upper()}"
                        run.save_metadata()
                        return

            run.status = "APPROVED"
            run.save_metadata()

            # Auto-commit if approved and requested
            if auto_commit or config.execution.auto_commit:
                task_summary = run.task.strip().splitlines()[0][:70] if (run.task and run.task.strip()) else ""
                commit_msg = f"feat: {task_summary}"
                if git.is_git_repo():
                    if auto_commit_run(git=git, task_summary=task_summary, baseline=git_baseline, run=run):
                        click.secho(f"  ✓ Auto-committed changes: '{commit_msg}'", fg="green")
                    else:
                        click.secho("  ⚠️ Auto-commit skipped: no changes or git commit error.", fg="yellow")

            # Reconcile PKB knowledge proposals
            reconcile_run_knowledge(root, run.run_id, run.run_dir)

            click.secho(f"\n✨ Forge Pipeline completed successfully for {run.run_id}!", fg="green", bold=True)
            click.echo(f"   Artifacts saved in .forge/runs/{run.run_id}/\n")
    except RunOwnershipError as e:
        click.echo(e.format_diagnostic(), err=True)
        sys.exit(1)


def reconcile_run_knowledge(project_root: Path, run_id: str, run_dir: Path) -> Optional[Dict[str, Any]]:
    """Reconcile staged knowledge proposals into the repository Project Knowledge Base."""
    try:
        from forge.core.reconciler import KnowledgeReconciler
        reconciler = KnowledgeReconciler(project_root)
        diff = reconciler.reconcile_run(run_id, run_dir)
        total_changes = len(diff.get("added", [])) + len(diff.get("modified", [])) + len(diff.get("disputed", [])) + len(diff.get("deprecated", []))
        if total_changes > 0:
            click.secho(
                f"  🧠 Project Knowledge Base updated: +{len(diff['added'])} ~{len(diff['modified'])} !{len(diff['disputed'])} -{len(diff['deprecated'])}",
                fg="cyan",
            )
        return diff
    except Exception as e:
        logging.warning("Failed to reconcile run knowledge for %s: %s", run_id, e)
        return None


def auto_commit_run(
    git: GitService,
    task_summary: str,
    baseline: Optional[GitBaseline] = None,
    run: Optional[Run] = None,
) -> bool:
    """Production commit orchestration helper for Forge runs.

    Computes run-scoped changes attributable specifically to the current Forge run
    relative to baseline, and commits ONLY pure Forge-owned files to preserve unrelated
    user work and fail-safe on mixed ownership.
    """
    if not git.is_git_repo():
        return False

    resolved_baseline = baseline
    if resolved_baseline is None and run is not None and hasattr(run, "run_dir"):
        baseline_file = run.run_dir / "git_baseline.json"
        if baseline_file.exists():
            try:
                resolved_baseline = GitBaseline.load(baseline_file)
            except Exception as e:
                import logging
                logging.warning("Failed to load baseline from %s: %s", baseline_file, e)

    attribution = git.attribute_changes(resolved_baseline)
    pure_forge_paths = attribution.pure_forge_changes
    mixed_paths = attribution.mixed_ownership_changes

    if mixed_paths:
        click.secho("\n⚠️ Skipped auto-commit for mixed-ownership file(s):", fg="yellow", bold=True)
        for p in mixed_paths:
            click.secho(f"  - {p}", fg="yellow")
        click.echo(
            "\nThese files had pre-existing user modifications before the Forge run\n"
            "and were modified again by Forge. Forge cannot safely separate the\n"
            "changes automatically. Review and commit them manually.\n"
        )

    if not pure_forge_paths:
        return False

    commit_msg = f"feat: {task_summary}"
    return git.commit(commit_msg, paths=pure_forge_paths)



@main.command(name="auto")
@click.argument("task", type=str, required=False, default=None)
@click.option("--file", "-f", "spec_file", type=click.Path(exists=True, dir_okay=False), help="Path to markdown spec/requirements file.")
@click.option("--from-critic", "-c", is_flag=True, default=False, help="Automatically resume from the latest Critic audit report.")
@click.option("--run", "run_id", type=str, default=None, help="Existing Run ID to resume from.")
@click.option("--max-retries", "-r", type=click.IntRange(min=1), default=3, help="Max auto-repair retry iterations between Executor, Tester, and Reviewer.")
@click.option("--auto-commit", is_flag=True, default=False, help="Automatically git commit upon approved verification.")
@click.option("--no-critic", is_flag=True, default=False, help="Skip the final post-execution codebase health audit.")
@click.option("--dashboard", "-d", "use_dashboard", is_flag=True, default=False, help="Launch interactive live terminal dashboard during execution.")
def auto_pipeline(
    task: Optional[str],
    spec_file: Optional[str],
    from_critic: bool,
    run_id: Optional[str],
    max_retries: int,
    auto_commit: bool,
    no_critic: bool,
    use_dashboard: bool = False,
):
    """Run fully autonomous iterative loop: Architect -> Planner -> [Executor <-> (Tester -> Reviewer) Self-Repair Loop] -> Critic."""
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

    run, is_resumed_same_task = _resolve_pipeline_run(run_mgr, task, from_critic, run_id, pipeline_type="auto")
    task = task or run.task

    try:
        with RunLock(run.run_dir, run_id=run.run_id) as run_lock_inst:
            if run_lock_inst.is_stale_recovered:
                click.secho(
                    f"⚠️ Recovered stale lock for run '{run.run_id}' (previous process terminated abnormally).",
                    fg="yellow",
                )

            # Initialize run-scoped git baseline
            git_baseline: Optional[GitBaseline] = None
            if git.is_git_repo():
                baseline_file = run.run_dir / "git_baseline.json"
                if baseline_file.exists():
                    try:
                        git_baseline = GitBaseline.load(baseline_file)
                    except Exception:
                        git_baseline = git.capture_baseline()
                        git_baseline.save(baseline_file)
                else:
                    git_baseline = git.capture_baseline()
                    git_baseline.save(baseline_file)

            context = Context(run=run, project_root=root, config=config, git=git, baseline=git_baseline)
            task_summary = task.strip().splitlines()[0][:70] if (task and task.strip()) else ""

            def _run_autonomous_loop() -> None:
                nonlocal task
                total_stages = StageOrder.total_stages_count(no_critic=no_critic)

                click.echo(f"\n⚡ [Run: {run.run_id}] Starting Fully Autonomous Forge Loop:")
                click.secho(f"   \"{task_summary}...\"\n", bold=True)

                # Pre-loop stages: Architect, Planner
                for stage_def in StageOrder.pre_loop_stages():
                    stage_done = False
                    if is_resumed_same_task:
                        stage_done, stage_status = is_stage_completed(run, stage_def, run_mgr)
                        if stage_done:
                            click.echo(f"  ⏭ Skipping {stage_def.display_name} (already completed with status '{stage_status}')")
                            continue

                    stage_res = execute_stage(stage_def, context, run_mgr, banner_prefix=f"[{stage_def.sequence_number}/{total_stages}]")
                    click.echo(f"  ✓ {stage_def.display_name} completed | Status: {stage_res.status} ({stage_res.duration_seconds:.1f}s)")

                    if not stage_res.success or not stage_res.machine_report.is_valid or stage_res.status in ("REJECTED", "BLOCKED", "FAILED", "UNKNOWN", "CHANGES_REQUIRED"):
                        click.secho(f"\n⚠️ Autonomous loop halted: {stage_def.display_name} finished with status '{stage_res.status}'.", fg="red")
                        run.status = stage_res.status if (stage_res.status in ("REJECTED", "BLOCKED", "CHANGES_REQUIRED")) else "FAILED"
                        run.save_metadata()
                        sys.exit(1)

                # Implementation / Verification loop: Change Producer -> Verification Gate(s)
                producer_def = StageOrder.change_producer()
                verifier_defs = StageOrder.verification_stages()

                approved = False
                latest_verifier_res: Optional[StageResult] = None

                if is_resumed_same_task:
                    all_verifiers_approved = True
                    for v_def in verifier_defs:
                        v_done, v_status = is_stage_completed(run, v_def, run_mgr)
                        if not (v_done and v_status in v_def.success_statuses):
                            all_verifiers_approved = False
                            break
                    if all_verifiers_approved:
                        verifiers_label = " & ".join(v.display_name for v in verifier_defs)
                        click.echo(f"  ⏭ Skipping {producer_def.display_name} & {verifiers_label} (already approved)")
                        approved = True

                if not approved:
                    max_retries_val = max(1, max_retries)
                    for iteration in range(1, max_retries_val + 1):
                        iter_label = f" (Attempt {iteration}/{max_retries_val})" if max_retries_val > 1 else ""

                        # 1. Execute Change Producer (e.g. Executor)
                        producer_res = execute_stage(producer_def, context, run_mgr, banner_prefix=f"[{producer_def.sequence_number}/{total_stages}]{iter_label}")
                        click.echo(f"  ✓ {producer_def.display_name} finished | Status: {producer_res.status} ({producer_res.duration_seconds:.1f}s)")

                        # Preserve historical attempt artifact
                        attempt_prod_md = run.run_dir / f"{producer_def.artifact_prefix}_attempt_{iteration}.md"
                        attempt_prod_json = run.run_dir / f"{producer_def.artifact_prefix}_attempt_{iteration}.json"
                        attempt_prod_md.write_text(producer_res.raw_markdown, encoding="utf-8")
                        with open(attempt_prod_json, "w", encoding="utf-8") as f:
                            json.dump(producer_res.to_dict(), f, indent=2)

                        if not producer_res.success or producer_res.status in ("FAILED", "BLOCKED", "UNKNOWN", "REJECTED"):
                            if producer_res.status == "BLOCKED":
                                click.secho(f"\n⚠️ {producer_def.display_name} blocked: {producer_res.machine_report.reason or 'Requirements blocked'}.", fg="red")
                                run.status = "BLOCKED"
                                run.save_metadata()
                                sys.exit(1)
                            if iteration < max_retries_val:
                                click.secho(f"\n🔄 {producer_def.display_name} failed with status '{producer_res.status}'. Retrying execution (iteration {iteration + 1})...", fg="yellow")
                                context.repair_feedback = (
                                    f"### Auto-Repair Feedback from Failed Execution (Attempt {iteration}):\n"
                                    f"{producer_def.display_name} exited with status '{producer_res.status}'. Output:\n{producer_res.raw_markdown[:2000]}\n"
                                    f"Fix all failures and complete implementation."
                                )
                                context.run.task = f"{task}\n\n{context.repair_feedback}"
                                continue
                            else:
                                click.secho(f"\n⚠️ {producer_def.display_name} failed on final attempt with status '{producer_res.status}'.", fg="red")
                                run.status = producer_res.status if producer_res.status != "UNKNOWN" else "FAILED"
                                run.save_metadata()
                                sys.exit(1)

                        # 2. Execute Verification Gates sequentially (e.g. Tester, Reviewer)
                        all_verifiers_passed = True

                        for verifier_def in verifier_defs:
                            verifier_res = execute_stage(verifier_def, context, run_mgr, banner_prefix=f"[{verifier_def.sequence_number}/{total_stages}]{iter_label}")
                            latest_verifier_res = verifier_res
                            click.echo(f"  ✓ {verifier_def.display_name} finished | Status: {verifier_res.status} ({verifier_res.duration_seconds:.1f}s)")

                            # Preserve historical verifier artifact
                            attempt_v_md = run.run_dir / f"{verifier_def.artifact_prefix}_attempt_{iteration}.md"
                            attempt_v_json = run.run_dir / f"{verifier_def.artifact_prefix}_attempt_{iteration}.json"
                            attempt_v_md.write_text(verifier_res.raw_markdown, encoding="utf-8")
                            with open(attempt_v_json, "w", encoding="utf-8") as f:
                                json.dump(verifier_res.to_dict(), f, indent=2)

                            if verifier_res.status in verifier_def.success_statuses:
                                continue
                            else:
                                all_verifiers_passed = False
                                if verifier_res.status == "BLOCKED":
                                    click.secho(f"\n⚠️ {verifier_def.display_name} blocked: {verifier_res.machine_report.reason or 'Requirements blocked'}.", fg="red")
                                    run.status = "BLOCKED"
                                    run.save_metadata()
                                    sys.exit(1)

                                if iteration < max_retries_val:
                                    if verifier_res.status == "CHANGES_REQUIRED":
                                        click.secho(f"\n🔄 {verifier_def.display_name} requested changes. Launching auto-repair iteration {iteration + 1}...", fg="yellow")
                                    else:
                                        click.secho(f"\n🔄 {verifier_def.display_name} resulted in '{verifier_res.status}'. Launching auto-repair iteration {iteration + 1}...", fg="yellow")
                                    issues_lines = []
                                    if verifier_res.machine_report.issues:
                                        for sev, iss_list in verifier_res.machine_report.issues.items():
                                            for iss in iss_list:
                                                if isinstance(iss, dict):
                                                    desc = iss.get("DESCRIPTION") or iss.get("description") or str(iss)
                                                    repro = iss.get("STEPS_TO_REPRODUCE") or iss.get("steps")
                                                    exp = iss.get("EXPECTED") or iss.get("expected")
                                                    act = iss.get("ACTUAL") or iss.get("actual")
                                                    evid = iss.get("EVIDENCE") or iss.get("evidence")
                                                    entry = f"- [{sev}] {desc}"
                                                    if repro:
                                                        entry += f"\n    Reproduction: {repro}"
                                                    if exp:
                                                        entry += f"\n    Expected: {exp}"
                                                    if act:
                                                        entry += f"\n    Actual: {act}"
                                                    if evid:
                                                        entry += f"\n    Evidence: {evid}"
                                                    issues_lines.append(entry)
                                                else:
                                                    issues_lines.append(f"- [{sev}] {iss}")
                                    issues_summary = "\n".join(issues_lines) if issues_lines else verifier_res.machine_report.reason or f"{verifier_def.display_name} resulted in status '{verifier_res.status}'."
                                    context.repair_feedback = (
                                        f"### Auto-Repair Feedback from {verifier_def.display_name} (Attempt {iteration}):\n"
                                        f"Status: {verifier_res.status}\n"
                                        f"{issues_summary}\n"
                                        f"Fix all issues and satisfy all requirements."
                                    )
                                    context.run.task = f"{task}\n\n{context.repair_feedback}"
                                else:
                                    click.secho(f"\n⚠️ {verifier_def.display_name} verdict: {verifier_res.status}.", fg="yellow")
                                break

                        if all_verifiers_passed:
                            approved = True
                            context.repair_feedback = None
                            context.run.task = task
                            verifiers_label = " & ".join(v.display_name for v in verifier_defs)
                            click.secho(f"\n✅ Implementation APPROVED by {verifiers_label} on attempt {iteration}!", fg="green", bold=True)
                            break

                # Exit with code 1 if not approved
                if not approved:
                    final_status = latest_verifier_res.status if (latest_verifier_res and latest_verifier_res.status != "UNKNOWN") else "FAILED"
                    run.task = task
                    run.status = final_status
                    run.save_metadata()
                    click.secho(f"\n⚠️ Autonomous Loop finished without approval for {run.run_id} (Status: {run.status}).", fg="red")
                    sys.exit(1)

                # Post-loop stages (e.g. Stage 6: Closing Critic Audit)
                for stage_def in StageOrder.post_loop_stages(no_critic=no_critic):
                    stage_done = False
                    if is_resumed_same_task:
                        stage_done, stage_status = is_stage_completed(run, stage_def, run_mgr)
                        if stage_done:
                            click.echo(f"  ⏭ Skipping {stage_def.display_name} (already completed with status '{stage_status}')")
                            continue

                    critic_res = execute_stage(stage_def, context, run_mgr, banner_prefix=f"[{stage_def.sequence_number}/{total_stages}]")
                    click.echo(f"  ✓ {stage_def.display_name} audit completed | Status: {critic_res.status} ({critic_res.duration_seconds:.1f}s)")

                    if not critic_res.success or not critic_res.machine_report.is_valid or critic_res.status in ("BLOCKED", "FAILED", "REJECTED", "UNKNOWN"):
                        audit_label = "Closing Critic" if stage_def.is_closing_critic else stage_def.display_name
                        click.secho(f"\n⚠️ {audit_label} audit reported non-success status '{critic_res.status}'.", fg="red")
                        run.task = task
                        run.status = critic_res.status if (critic_res.status in ("BLOCKED", "REJECTED", "CHANGES_REQUIRED")) else "FAILED"
                        run.save_metadata()
                        sys.exit(1)

                # Auto-commit if approved and requested (only AFTER Critic audit)
                if approved and (auto_commit or config.execution.auto_commit):
                    commit_msg = f"feat: {task_summary}"
                    if git.is_git_repo():
                        if auto_commit_run(git=git, task_summary=task_summary, baseline=git_baseline, run=run):
                            click.secho(f"  ✓ Auto-committed changes: '{commit_msg}'", fg="green")
                        else:
                            click.secho("  ⚠️ Auto-commit skipped: no changes or git commit error.", fg="yellow")

                run.task = task
                run.status = "APPROVED"
                run.save_metadata()

                # Reconcile PKB knowledge proposals
                reconcile_run_knowledge(root, run.run_id, run.run_dir)

                click.secho(f"\n✨ Autonomous Loop finished for {run.run_id} (Status: {run.status})!", fg="green", bold=True)
                click.echo(f"   Artifacts saved in .forge/runs/{run.run_id}/\n")

            if not use_dashboard:
                _run_autonomous_loop()
            else:
                import queue
                import threading

                live_queue: queue.Queue = queue.Queue(maxsize=10000)
                stop_event = threading.Event()
                abort_event = threading.Event()
                pipeline_exit_code = [0]
                pipeline_exception = []

                def event_listener(event):
                    try:
                        live_queue.put_nowait(event)
                    except Exception:
                        pass

                context.event_listener = event_listener

                def _worker() -> None:
                    try:
                        _run_autonomous_loop()
                    except SystemExit as se:
                        code = se.code if isinstance(se.code, int) else (1 if se.code else 0)
                        pipeline_exit_code[0] = code
                    except Exception as exc:
                        pipeline_exception.append(exc)
                        pipeline_exit_code[0] = 1
                    finally:
                        stop_event.set()

                worker_thread = threading.Thread(
                    target=_worker,
                    name=f"ForgeAutoWorker-{run.run_id}",
                    daemon=True,
                )
                worker_thread.start()

                from forge.dashboard.app import DashboardApp
                app = DashboardApp(
                    run.run_dir,
                    live_queue=live_queue,
                    stop_event=stop_event,
                    abort_event=abort_event,
                    project_root=root,
                )
                try:
                    app.run()
                except Exception as ui_exc:
                    # Failure isolation: dashboard errors never disrupt running execution
                    pass

                if worker_thread.is_alive():
                    click.secho("\nDashboard detached. Execution continuing in background...", fg="cyan")
                    worker_thread.join()

                if pipeline_exception:
                    click.secho(f"\nExecution error: {pipeline_exception[0]}", fg="red")
                if pipeline_exit_code[0] != 0:
                    sys.exit(pipeline_exit_code[0])
    except RunOwnershipError as e:
        click.echo(e.format_diagnostic(), err=True)
        sys.exit(1)



def _parse_cli_value(raw: str) -> Any:
    cleaned = raw.strip()
    if cleaned.lower() in ("null", "none"):
        return None
    if cleaned.lower() in ("true", "yes", "on"):
        return True
    if cleaned.lower() in ("false", "no", "off"):
        return False
    try:
        return int(cleaned)
    except ValueError:
        pass
    try:
        return float(cleaned)
    except ValueError:
        pass
    if (cleaned.startswith("{") and cleaned.endswith("}")) or (cleaned.startswith("[") and cleaned.endswith("]")):
        try:
            return json.loads(cleaned)
        except Exception:
            try:
                return yaml.safe_load(cleaned)
            except Exception:
                pass
    return raw


@main.group(name="config")
def config_cmd():
    """Inspect and manage Forge configuration."""
    pass


@config_cmd.command(name="auth-show")
def config_auth_show_cmd():
    """Display current Forge authentication configuration."""
    root = Path.cwd()
    cfg = Config.load(root)
    click.echo(f"Auth Method: {cfg.defaults.auth_method}")
    click.echo(f"Auth Provider: {cfg.defaults.auth_provider or '(default)'}")
    click.echo(f"Auth Scopes: {cfg.defaults.auth_scopes or '(default)'}")
    click.echo(f"Auth Token TTL: {cfg.defaults.auth_token_ttl}s")


@config_cmd.command(name="auth-get")
@click.argument("key", type=str)
def config_auth_get_cmd(key: str):
    """Get an authentication configuration value by key."""
    root = Path.cwd()
    cfg = Config.load(root)
    cfg_dict = cfg.to_dict()
    try:
        val = Config.get_key_from_dict(cfg_dict, f"defaults.{key}")
    except KeyError as e:
        click.secho(f"Error: {e.args[0]}", fg="red")
        sys.exit(1)

    if isinstance(val, (dict, list)):
        click.echo(yaml.safe_dump(val, sort_keys=False, default_flow_style=False, indent=2).strip())
    elif val is None:
        click.echo("null")
    elif isinstance(val, bool):
        click.echo("true" if val else "false")
    else:
        click.echo(str(val))


@config_cmd.command(name="auth-set")
@click.argument("key", type=str)
@click.argument("value", type=str)
@click.option("--global", "-g", "is_global", is_flag=True, default=False, help="Set in global ~/.forge/config.yaml")
def config_auth_set_cmd(key: str, value: str, is_global: bool):
    """Set an authentication configuration value (e.g. forge config auth-set auth_method api_key)."""
    root = Path.cwd()
    target = Config.resolve_write_target(root, is_global=is_global)

    data = {}
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            click.secho(f"Error reading existing config file {target}: {e}", fg="red")
            sys.exit(1)

    parsed_value = _parse_cli_value(value)
    Config.set_key_in_dict(data, f"defaults.{key}", parsed_value)

    # Validate before saving
    errors = Config.validate_dict(data)
    if errors:
        click.secho(f"Validation failed for update '{key} = {value}':", fg="red")
        for err in errors:
            click.echo(f"  • {err}")
        click.echo("Config file was NOT modified.")
        sys.exit(1)

    try:
        Config.save_file_safely(target, data)
        click.secho(f"✓ Updated defaults.{key} = {parsed_value!r} in {target}", fg="green")
    except Exception as e:
        click.secho(f"Error saving configuration to {target}: {e}", fg="red")
        sys.exit(1)


@config_cmd.command(name="show")
@click.option("--raw", is_flag=True, default=False, help="Show raw project configuration without built-in defaults.")
@click.option("--json", "json_output", is_flag=True, default=False, help="Output configuration as JSON.")
def config_show_cmd(raw: bool, json_output: bool):
    """Display current Forge configuration."""
    root = Path.cwd()
    if raw:
        found = Config.resolve_active_project_config_file(root)
        if not found:
            click.secho("No project configuration file found (forge.yaml or .forge/config.yaml).", fg="yellow")
            return
        with open(found, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f) or {}
        if json_output:
            click.echo(json.dumps(raw_data, indent=2))
        else:
            click.echo(yaml.safe_dump(raw_data, sort_keys=False, default_flow_style=False, indent=2).strip())
    else:
        cfg = Config.load(root)
        if json_output:
            click.echo(json.dumps(cfg.to_dict(), indent=2))
        else:
            click.echo(cfg.to_yaml().strip())


@config_cmd.command(name="get")
@click.argument("key", type=str)
def config_get_cmd(key: str):
    """Get a configuration value by key (supports dot notation, e.g. defaults.model, stages.executor.timeout)."""
    root = Path.cwd()
    cfg = Config.load(root)
    cfg_dict = cfg.to_dict()
    try:
        val = Config.get_key_from_dict(cfg_dict, key)
    except KeyError as e:
        click.secho(f"Error: {e.args[0]}", fg="red")
        sys.exit(1)

    if isinstance(val, (dict, list)):
        click.echo(yaml.safe_dump(val, sort_keys=False, default_flow_style=False, indent=2).strip())
    elif val is None:
        click.echo("null")
    elif isinstance(val, bool):
        click.echo("true" if val else "false")
    else:
        click.echo(str(val))


@config_cmd.command(name="set", context_settings=dict(ignore_unknown_options=True))
@click.argument("key", type=str)
@click.argument("value", type=str)
@click.option("--global", "-g", "is_global", is_flag=True, default=False, help="Set in global ~/.forge/config.yaml")
def config_set_cmd(key: str, value: str, is_global: bool):
    """Set a configuration value by key (e.g. forge config set stages.executor.timeout 1200)."""
    root = Path.cwd()
    target = Config.resolve_write_target(root, is_global=is_global)

    data = {}
    if target.exists():
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            click.secho(f"Error reading existing config file {target}: {e}", fg="red")
            sys.exit(1)

    parsed_value = _parse_cli_value(value)
    Config.set_key_in_dict(data, key, parsed_value)

    # Validate before saving
    errors = Config.validate_dict(data)
    if errors:
        click.secho(f"Validation failed for update '{key} = {value}':", fg="red")
        for err in errors:
            click.echo(f"  • {err}")
        click.echo("Config file was NOT modified.")
        sys.exit(1)

    try:
        Config.save_file_safely(target, data)
        click.secho(f"✓ Updated {key} = {parsed_value!r} in {target}", fg="green")
    except Exception as e:
        click.secho(f"Error saving configuration to {target}: {e}", fg="red")
        sys.exit(1)


@config_cmd.command(name="edit")
@click.option("--global", "-g", "is_global", is_flag=True, default=False, help="Edit global ~/.forge/config.yaml")
def config_edit_cmd(is_global: bool):
    """Open configuration file in $EDITOR with pre-save validation."""
    from forge.core.templates import DEFAULT_FORGE_YAML
    root = Path.cwd()
    target = Config.resolve_write_target(root, is_global=is_global)

    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with open(target, "w", encoding="utf-8") as f:
            f.write(DEFAULT_FORGE_YAML)

    with open(target, "r", encoding="utf-8") as f:
        original_content = f.read()

    edited_content = click.edit(original_content, extension=".yaml")
    if edited_content is None or edited_content == original_content:
        click.echo("No changes made.")
        return

    # Validate edited content
    try:
        parsed_data = yaml.safe_load(edited_content)
    except yaml.YAMLError as e:
        click.secho(f"YAML Syntax Error:\n{e}", fg="red")
        click.echo("Changes were NOT saved to prevent configuration corruption.")
        sys.exit(1)

    if not isinstance(parsed_data, dict):
        click.secho("Error: Configuration must be a YAML mapping at the root level.", fg="red")
        click.echo("Changes were NOT saved.")
        sys.exit(1)

    errors = Config.validate_dict(parsed_data)
    if errors:
        click.secho("Validation failed for edited configuration:", fg="red")
        for err in errors:
            click.echo(f"  • {err}")
        click.echo("Changes were NOT saved to prevent configuration corruption.")
        sys.exit(1)

    try:
        Config.save_file_safely(target, parsed_data)
        click.secho(f"✓ Configuration in {target} updated and validated successfully.", fg="green")
    except Exception as e:
        click.secho(f"Error saving edited configuration: {e}", fg="red")
        sys.exit(1)


@config_cmd.command(name="validate")
@click.option("--path", "-p", type=click.Path(exists=True, dir_okay=False), help="Path to config file to validate.")
def config_validate_cmd(path: Optional[str]):
    """Validate configuration syntax, stage names, adapters, efforts, and timeouts."""
    if path:
        target = Path(path)
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            click.secho(f"✗ YAML syntax error in {target}:\n{e}", fg="red")
            sys.exit(1)
        except Exception as e:
            click.secho(f"✗ Error reading {target}: {e}", fg="red")
            sys.exit(1)

        errors = Config.validate_dict(data)
        if errors:
            click.secho(f"✗ Configuration in {target} is INVALID:", fg="red")
            for err in errors:
                click.echo(f"  • {err}")
            sys.exit(1)
        else:
            click.secho(f"✓ Configuration in {target} is valid.", fg="green")
            return

    # Validate resolved config and all existing config files
    root = Path.cwd()
    all_valid = True
    for p in Config.get_all_config_paths(root):
        if p.exists() and p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    file_data = yaml.safe_load(f)
                file_errors = Config.validate_dict(file_data)
                if file_errors:
                    all_valid = False
                    click.secho(f"✗ File {p} has errors:", fg="red")
                    for err in file_errors:
                        click.echo(f"  • {err}")
                else:
                    click.secho(f"✓ {p} is valid.", fg="green")
            except Exception as e:
                all_valid = False
                click.secho(f"✗ Failed to parse {p}: {e}", fg="red")

    cfg = Config.load(root)
    resolved_errors = cfg.validate()
    if resolved_errors:
        all_valid = False
        click.secho("✗ Resolved configuration has errors:", fg="red")
        for err in resolved_errors:
            click.echo(f"  • {err}")

    if not all_valid:
        sys.exit(1)
    else:
        click.secho("✓ All configurations and resolved pipeline settings are valid.", fg="green")


@config_cmd.command(name="reset")
@click.option("--force", "-f", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.option("--global", "-g", "is_global", is_flag=True, default=False, help="Reset global ~/.forge/config.yaml")
def config_reset_cmd(force: bool, is_global: bool):
    """Reset configuration to Forge default configuration."""
    from forge.core.templates import DEFAULT_FORGE_YAML
    root = Path.cwd()
    target = Config.resolve_write_target(root, is_global=is_global)

    if not force:
        if not click.confirm(f"Are you sure you want to reset {target} to default configuration?", default=False):
            click.echo("Reset cancelled.")
            return

    default_data = yaml.safe_load(DEFAULT_FORGE_YAML)
    try:
        Config.save_file_safely(target, default_data)
        click.secho(f"✓ Reset configuration to defaults in {target}", fg="green")
    except Exception as e:
        click.secho(f"Error resetting configuration: {e}", fg="red")
        sys.exit(1)


@main.group(name="knowledge")
def knowledge_cmd():
    """Inspect and manage the repository Project Knowledge Base (PKB)."""
    pass


@knowledge_cmd.command(name="list")
@click.option("--type", "-t", "fact_type", type=click.Choice(["architecture", "feature", "decision", "unresolved"], case_sensitive=False), default=None, help="Filter by fact type.")
@click.option("--status", "-s", "status", type=click.Choice(["PROVISIONAL", "VERIFIED", "DISPUTED", "HUMAN_LOCKED", "DEPRECATED"], case_sensitive=False), default=None, help="Filter by status.")
def knowledge_list_cmd(fact_type: Optional[str], status: Optional[str]):
    """List knowledge facts in the repository."""
    from forge.storage.knowledge import KnowledgeStore
    root = Path.cwd()
    store = KnowledgeStore(root)
    facts = store.load_all()

    if fact_type:
        clean_t = fact_type.lower().strip()
        facts = {k: v for k, v in facts.items() if v.type == clean_t}
    if status:
        clean_s = status.upper().strip()
        facts = {k: v for k, v in facts.items() if v.status == clean_s}

    if not facts:
        click.echo("No knowledge facts found.")
        return

    click.secho(f"\n🧠 Project Knowledge Base ({len(facts)} facts):\n", bold=True)
    for fact in sorted(facts.values(), key=lambda f: (f.type, f.id)):
        status_color = "green" if fact.is_verified else ("blue" if fact.is_locked else ("yellow" if fact.is_disputed else "white"))
        status_tag = f"[{fact.status}]"
        click.echo(f"  • {fact.id:<32} {click.style(status_tag, fg=status_color):<18} ({fact.type}) {fact.title}")
    click.echo("")


@knowledge_cmd.command(name="show")
@click.argument("fact_id")
def knowledge_show_cmd(fact_id: str):
    """Show details of a single knowledge fact."""
    from forge.storage.knowledge import KnowledgeStore
    root = Path.cwd()
    store = KnowledgeStore(root)
    fact = store.get_fact(fact_id)

    if not fact:
        click.secho(f"Error: Fact '{fact_id}' not found.", fg="red")
        sys.exit(1)

    status_color = "green" if fact.is_verified else ("blue" if fact.is_locked else ("yellow" if fact.is_disputed else "white"))
    click.secho(f"\n🧠 Knowledge Fact: {fact.id}", bold=True)
    click.echo(f"  Title:       {fact.title}")
    click.echo(f"  Type:        {fact.type}")
    click.echo(f"  Status:      {click.style(fact.status, fg=status_color)}")
    click.echo(f"  Source:      {fact.source}")
    if fact.summary:
        click.echo(f"  Summary:     {fact.summary}")
    if fact.evidence:
        click.echo(f"  Evidence:    {', '.join(fact.evidence)}")
    if fact.provenance:
        click.echo(f"  Provenance:  {fact.provenance}")
    if fact.disputes:
        click.echo(f"  Disputes ({len(fact.disputes)}):")
        for d in fact.disputes:
            click.echo(f"    - [{d.get('run', '?')} by {d.get('role', '?')}]: {d.get('claim', '')}")
    if fact.payload:
        click.echo(f"  Payload:     {fact.payload}")
    click.echo("")


@knowledge_cmd.command(name="lock")
@click.argument("fact_id")
def knowledge_lock_cmd(fact_id: str):
    """Lock a fact to prevent autonomous agent modifications (sets HUMAN_LOCKED)."""
    from datetime import datetime, timezone
    from forge.storage.knowledge import KnowledgeStore
    from forge.core.knowledge import FactStatus, FactSource
    root = Path.cwd()
    store = KnowledgeStore(root)
    fact = store.get_fact(fact_id)

    if not fact:
        click.secho(f"Error: Fact '{fact_id}' not found.", fg="red")
        sys.exit(1)

    fact.status = FactStatus.HUMAN_LOCKED.value
    fact.provenance["source"] = FactSource.HUMAN.value
    fact.provenance["last_verified_at"] = datetime.now(timezone.utc).isoformat()
    store.save_fact(fact)
    click.secho(f"✓ Fact '{fact_id}' is now HUMAN_LOCKED (sovereign developer truth).", fg="green")


@knowledge_cmd.command(name="unlock")
@click.argument("fact_id")
def knowledge_unlock_cmd(fact_id: str):
    """Unlock a fact, returning it to VERIFIED and allowing agent updates."""
    from datetime import datetime, timezone
    from forge.storage.knowledge import KnowledgeStore
    from forge.core.knowledge import FactStatus
    root = Path.cwd()
    store = KnowledgeStore(root)
    fact = store.get_fact(fact_id)

    if not fact:
        click.secho(f"Error: Fact '{fact_id}' not found.", fg="red")
        sys.exit(1)

    fact.status = FactStatus.VERIFIED.value
    fact.provenance["unlocked_at"] = datetime.now(timezone.utc).isoformat()
    store.save_fact(fact)
    click.secho(f"✓ Fact '{fact_id}' is now unlocked (VERIFIED).", fg="green")


if __name__ == "__main__":
    main()

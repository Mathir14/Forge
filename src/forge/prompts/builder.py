"""Builder that constructs an Instruction object from Context and Role."""

import logging
from pathlib import Path
from typing import Dict, List, Optional
from forge.core.context import Context
from forge.core.role import Role
from forge.prompts.instruction import Instruction


class InstructionBuilder:
    @staticmethod
    def build(context: Context, role: Role) -> Instruction:
        """Construct Instruction from Context and Role."""
        MAX_DOC_CHARS = 40000
        MAX_STAGE_OUTPUT_CHARS = 40000
        MAX_DIFF_CHARS = 60000
        MAX_STATUS_CHARS = 10000

        project_docs: Dict[str, str] = {}
        project_dir = context.project_root / ".ai" / "project"
        if project_dir.exists() and project_dir.is_dir():
            for doc_file in sorted(project_dir.glob("*.md")):
                try:
                    with open(doc_file, "r", encoding="utf-8") as f:
                        doc_content = f.read()
                    if len(doc_content) > MAX_DOC_CHARS:
                        excess = len(doc_content) - MAX_DOC_CHARS
                        doc_content = doc_content[:MAX_DOC_CHARS] + f"\n\n[... Truncated remaining {excess} chars ...]"
                    project_docs[doc_file.stem] = doc_content
                except Exception as e:
                    logging.warning("Failed to read project doc %s: %s", doc_file, e)

        # Load previous stage outputs from run
        previous_stage_outputs: Dict[str, str] = {}
        current_artifact = (
            f"{role.sequence_number:02d}_{role.name}.md"
            if role.sequence_number is not None
            else f"_{role.name}.md"
        )
        for p in sorted(context.run.run_dir.glob("*.md")):
            # Don't include the current role's own output or historical attempt artifacts
            if "_attempt_" in p.name:
                continue
            if p.name == current_artifact or (role.sequence_number is None and p.name.endswith(f"_{role.name}.md")):
                continue
            # If current role has a sequence number, enforce sequence ordering:
            # only artifacts from earlier stages (artifact_seq < role.sequence_number) are included.
            parts = p.stem.split("_", 1)
            if parts[0].isdigit():
                artifact_seq = int(parts[0])
                if role.sequence_number is not None and artifact_seq >= role.sequence_number:
                    continue
            elif role.sequence_number is not None:
                # Exclude unnumbered artifacts if role has a sequence number
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) > MAX_STAGE_OUTPUT_CHARS:
                    excess = len(content) - MAX_STAGE_OUTPUT_CHARS
                    content = content[:MAX_STAGE_OUTPUT_CHARS] + f"\n\n[... Truncated remaining {excess} chars to prevent context overflow ...]"
                stage_name = p.stem.split("_", 1)[-1] if "_" in p.stem else p.stem
                if stage_name in previous_stage_outputs:
                    stage_name = p.stem
                previous_stage_outputs[stage_name] = content
            except Exception as e:
                logging.warning("Failed to read previous stage output %s: %s", p, e)

        # Git diff, status, and changed files
        git_diff: Optional[str] = None
        changed_files: List[str] = []
        mixed_files: List[str] = []
        git_status: Optional[str] = None
        changed_file_summary: Optional[str] = None

        if context.git and context.git.is_git_repo():
            # Resolve baseline if available (from context or run_dir)
            baseline = getattr(context, "baseline", None)
            if baseline is None and context.run and hasattr(context.run, "run_dir"):
                baseline_file = context.run.run_dir / "git_baseline.json"
                if baseline_file.exists():
                    try:
                        from forge.core.git import GitBaseline
                        baseline = GitBaseline.load(baseline_file)
                    except Exception as e:
                        logging.warning("Failed to load baseline in InstructionBuilder: %s", e)

            # Pre-run Critic check (Part 10):
            # If pre-run Critic is executing and no baseline exists yet, it observes repository-wide state.
            # When baseline exists (Executor, Reviewer, closing Critic, etc.), use run-scoped Git views.
            if baseline is not None:
                attr = context.git.attribute_changes(baseline)
                changed_files = attr.pure_forge_changes
                mixed_files = attr.mixed_ownership_changes

                raw_diff = context.git.diff_since(baseline)
                raw_status = context.git.status_since(baseline)
                summary_lines = context.git.changed_files_summary_since(baseline)
            else:
                raw_diff = context.git.diff()
                changed_files = context.git.changed_files()
                raw_status = context.git.status()
                summary_lines = context.git.changed_files_summary()

            if raw_diff:
                if len(raw_diff) > MAX_DIFF_CHARS:
                    excess = len(raw_diff) - MAX_DIFF_CHARS
                    git_diff = raw_diff[:MAX_DIFF_CHARS] + f"\n\n[... Diff truncated ({excess} chars omitted to fit context window) ...]"
                else:
                    git_diff = raw_diff

            if raw_status:
                if len(raw_status) > MAX_STATUS_CHARS:
                    excess = len(raw_status) - MAX_STATUS_CHARS
                    git_status = raw_status[:MAX_STATUS_CHARS] + f"\n\n[... Status truncated ({excess} chars omitted) ...]"
                else:
                    git_status = raw_status

            if summary_lines:
                changed_file_summary = "\n".join(summary_lines)
            elif changed_files:
                changed_file_summary = "\n".join(f"- {f}" for f in changed_files)

        # Resolve executor / change-producer report for Reviewer and Tester
        executor_report: Optional[str] = None
        if role.name in ("reviewer", "tester"):
            executor_report = previous_stage_outputs.get("executor")
            if not executor_report and context.run and hasattr(context.run, "run_dir"):
                from forge.stages.definition import StageOrder
                producer_def = StageOrder.change_producer()
                exec_file = context.run.run_dir / f"{producer_def.artifact_prefix}.md"
                if not exec_file.exists():
                    candidates = list(context.run.run_dir.glob("*_executor.md"))
                    if candidates:
                        exec_file = candidates[0]
                if exec_file.exists():
                    try:
                        content = exec_file.read_text(encoding="utf-8")
                        if len(content) > MAX_STAGE_OUTPUT_CHARS:
                            excess = len(content) - MAX_STAGE_OUTPUT_CHARS
                            content = content[:MAX_STAGE_OUTPUT_CHARS] + f"\n\n[... Truncated remaining {excess} chars ...]"
                        executor_report = content
                    except Exception as e:
                        logging.warning("Failed to read change producer artifact %s: %s", exec_file, e)

        # Resolve complete Tester report (both markdown and machine report JSON) for Reviewer
        tester_report: Optional[str] = None
        tester_machine_report: Optional[str] = None
        if role.name == "reviewer":
            tester_report = previous_stage_outputs.get("tester")
            if not tester_report and context.run and hasattr(context.run, "run_dir"):
                from forge.stages.definition import StageOrder
                tester_def = StageOrder.get_tester()
                t_file = context.run.run_dir / f"{tester_def.artifact_prefix}.md"
                if not t_file.exists():
                    candidates_t = list(context.run.run_dir.glob("*_tester.md"))
                    if candidates_t:
                        t_file = candidates_t[0]
                if t_file.exists():
                    try:
                        content = t_file.read_text(encoding="utf-8")
                        if len(content) > MAX_STAGE_OUTPUT_CHARS:
                            excess = len(content) - MAX_STAGE_OUTPUT_CHARS
                            content = content[:MAX_STAGE_OUTPUT_CHARS] + f"\n\n[... Truncated remaining {excess} chars ...]"
                        tester_report = content
                    except Exception as e:
                        logging.warning("Failed to read tester markdown artifact %s: %s", t_file, e)

            # Also load tester machine JSON report
            if context.run and hasattr(context.run, "run_dir"):
                from forge.stages.definition import StageOrder
                tester_def = StageOrder.get_tester()
                t_json_file = context.run.run_dir / f"{tester_def.artifact_prefix}.json"
                if not t_json_file.exists():
                    candidates_tj = list(context.run.run_dir.glob("*_tester.json"))
                    if candidates_tj:
                        t_json_file = candidates_tj[0]
                if t_json_file.exists():
                    try:
                        t_json_content = t_json_file.read_text(encoding="utf-8")
                        if len(t_json_content) > MAX_STAGE_OUTPUT_CHARS:
                            excess = len(t_json_content) - MAX_STAGE_OUTPUT_CHARS
                            t_json_content = t_json_content[:MAX_STAGE_OUTPUT_CHARS] + f"\n\n[... Truncated remaining {excess} chars ...]"
                        tester_machine_report = t_json_content
                    except Exception as e:
                        logging.warning("Failed to read tester machine json artifact %s: %s", t_json_file, e)

        # Resolve clean user task for instruction so prompt compiler renders repair_feedback independently
        base_task = getattr(context.run, "_initial_task", None)
        if not base_task:
            base_task = context.run.task
            if context.repair_feedback and context.repair_feedback.strip() in base_task:
                base_task = base_task.replace(context.repair_feedback.strip(), "").strip()

        # Project PKB knowledge context
        knowledge_context = ""
        try:
            from forge.storage.knowledge import KnowledgeStore
            from forge.prompts.knowledge_projector import KnowledgeProjector
            k_store = KnowledgeStore(context.project_root)
            knowledge_context = KnowledgeProjector.project_context(
                store=k_store,
                role_name=role.name,
                task=base_task,
                changed_files=changed_files,
                max_chars=20000,
            )
        except Exception as e:
            logging.warning("Failed to project knowledge context in InstructionBuilder: %s", e)

        return Instruction(
            role_name=role.name,
            task=base_task,
            project_docs=project_docs,
            previous_stage_outputs=previous_stage_outputs,
            git_diff=git_diff if git_diff else None,
            changed_files=changed_files,
            mixed_files=mixed_files,
            git_status=git_status if git_status else None,
            changed_file_summary=changed_file_summary if changed_file_summary else None,
            executor_report=executor_report if executor_report else None,
            tester_report=tester_report if tester_report else None,
            tester_machine_report=tester_machine_report if tester_machine_report else None,
            protocol_schema=role.protocol_content,
            repair_feedback=context.repair_feedback,
            knowledge_context=knowledge_context,
        )


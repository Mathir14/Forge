"""Builder that constructs an Instruction object from Context and Role."""

from pathlib import Path
from typing import Dict, List, Optional
from forge.core.context import Context
from forge.core.role import Role
from forge.prompts.instruction import Instruction


class InstructionBuilder:
    @staticmethod
    def build(context: Context, role: Role) -> Instruction:
        """Construct Instruction from Context and Role."""
        project_docs: Dict[str, str] = {}
        project_dir = context.project_root / ".ai" / "project"
        if project_dir.exists() and project_dir.is_dir():
            for doc_file in sorted(project_dir.glob("*.md")):
                try:
                    with open(doc_file, "r", encoding="utf-8") as f:
                        project_docs[doc_file.stem] = f.read()
                except Exception:
                    pass

        # Load previous stage outputs from run
        previous_stage_outputs: Dict[str, str] = {}
        for p in sorted(context.run.run_dir.glob("*.md")):
            # Don't include the current role's own output if re-running
            if not p.name.endswith(f"_{role.name}.md"):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        stage_name = p.stem.split("_", 1)[-1] if "_" in p.stem else p.stem
                        previous_stage_outputs[stage_name] = f.read()
                except Exception:
                    pass

        # Git diff and changed files
        git_diff: Optional[str] = None
        changed_files: List[str] = []
        if context.git and context.git.is_git_repo():
            git_diff = context.git.diff()
            changed_files = context.git.changed_files()

        return Instruction(
            role_name=role.name,
            task=context.run.task,
            project_docs=project_docs,
            previous_stage_outputs=previous_stage_outputs,
            git_diff=git_diff if git_diff else None,
            changed_files=changed_files,
            protocol_schema=role.protocol_content,
        )

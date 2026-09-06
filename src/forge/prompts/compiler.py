"""Pure stateless prompt compiler."""

from typing import Optional
from forge.prompts.instruction import Instruction
from forge.prompts.rendered_prompt import RenderedPrompt


class PromptCompiler:
    @staticmethod
    def compile(instruction: Instruction, role_template: str) -> RenderedPrompt:
        """Compile an Instruction and role template into a RenderedPrompt."""
        parts = [
            f"# ROLE: {instruction.role_name.upper()}",
            role_template.strip(),
        ]

        if instruction.project_docs:
            parts.append("\n## PROJECT DOCUMENTATION & CONVENTIONS")
            for doc_name, content in instruction.project_docs.items():
                if content.strip():
                    parts.append(f"### {doc_name}\n{content.strip()}")

        if instruction.previous_stage_outputs:
            parts.append("\n## PREVIOUS STAGE ARTIFACTS")
            for stage_name, output in instruction.previous_stage_outputs.items():
                if output.strip():
                    parts.append(f"### Output from {stage_name.capitalize()}\n{output.strip()}")

        if instruction.git_diff:
            parts.append(f"\n## GIT DIFF\n```diff\n{instruction.git_diff.strip()}\n```")

        if instruction.changed_files:
            parts.append(
                f"\n## CHANGED FILES\n" + "\n".join(f"- {f}" for f in instruction.changed_files)
            )

        if instruction.task:
            parts.append(f"\n## USER TASK REQUEST\n{instruction.task.strip()}")

        if instruction.protocol_schema:
            parts.append(f"\n## PROTOCOL REQUIREMENT\n{instruction.protocol_schema.strip()}")

        rendered_text = "\n\n".join(parts)
        return RenderedPrompt.from_text(rendered_text)

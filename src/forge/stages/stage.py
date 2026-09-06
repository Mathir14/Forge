"""Generic Stage execution engine driven by Role and Adapter."""

from pathlib import Path
from typing import Optional
from forge.core.context import Context
from forge.core.role import Role
from forge.adapters.base import BaseAdapter
from forge.storage.run_manager import RunManager
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.protocol.parser import MachineReportParser
from forge.protocol.validator import MachineReportValidator
from forge.stages.result import StageResult


class Stage:
    def __init__(
        self,
        role: Role,
        adapter: BaseAdapter,
        run_manager: Optional[RunManager] = None,
    ):
        self.role = role
        self.adapter = adapter
        self.run_manager = run_manager or RunManager()

    def run(self, context: Context) -> StageResult:
        """Execute full stage lifecycle: prepare -> execute -> validate -> save."""
        context.current_role = self.role

        # 1. Prepare
        instruction = InstructionBuilder.build(context, self.role)
        rendered_prompt = PromptCompiler.compile(instruction, self.role.template_content)

        # 2. Execute
        response = self.adapter.execute(
            prompt=rendered_prompt.text,
            cwd=context.project_root,
        )

        # 3. Validate / Parse protocol
        raw_dict, raw_yaml = MachineReportParser.extract_yaml(response.raw_output)
        report = MachineReportValidator.validate(
            data=raw_dict,
            expected_role=self.role.name,
            raw_yaml=raw_yaml,
        )

        success = response.exit_code == 0 and report.status not in ("REJECTED", "FAILED", "BLOCKED")

        result = StageResult(
            role=self.role,
            prompt=rendered_prompt,
            response=response,
            machine_report=report,
            raw_markdown=response.raw_output,
            duration_seconds=response.duration_seconds,
            success=success,
        )

        # 4. Save artifacts (.md + .json)
        self.run_manager.save_stage_artifacts(
            run=context.run,
            sequence_number=self.role.sequence_number,
            role_name=self.role.name,
            markdown_content=response.raw_output,
            json_data=result.to_dict(),
            prompt_hash=rendered_prompt.prompt_hash,
            adapter_name=self.adapter.name,
        )

        return result

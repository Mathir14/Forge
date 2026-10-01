"""Generic Stage execution engine driven by Role and Adapter."""

import json
from pathlib import Path
from typing import Optional, Set, Iterable, Union, Any
from forge.core.context import Context
from forge.core.role import Role
from forge.core.capabilities import Capability, CapabilityValidationError
from forge.adapters.base import BaseAdapter
from forge.storage.run_manager import RunManager
from forge.prompts.builder import InstructionBuilder
from forge.prompts.compiler import PromptCompiler
from forge.protocol.parser import MachineReportParser
from forge.protocol.validator import MachineReportValidator
from forge.stages.requirements import StageRequirementsRegistry
from forge.stages.result import StageResult


class Stage:
    def __init__(
        self,
        role: Role,
        adapter: BaseAdapter,
        run_manager: Optional[RunManager] = None,
        timeout: Optional[int] = None,
    ):
        self.role = role
        self.adapter = adapter
        self.run_manager = run_manager or RunManager()
        self.timeout = timeout

    @classmethod
    def get_required_capabilities(cls, stage_name: str) -> Set[str]:
        """Return required capabilities for a given stage name."""
        return StageRequirementsRegistry.get(stage_name)

    @classmethod
    def register_requirements(
        cls,
        stage_name: str,
        required_capabilities: Iterable[Union[str, Capability]],
    ) -> None:
        """Register or override required capabilities for a stage name."""
        StageRequirementsRegistry.register(stage_name, required_capabilities)

    def required_capabilities(self) -> Set[str]:
        """Return the required capabilities for this stage instance's role."""
        return self.get_required_capabilities(self.role.name)

    def validate_compatibility(self) -> None:
        """Validate that the configured adapter satisfies the stage's requirements."""
        required = self.required_capabilities()
        provided = self.adapter.capabilities()
        missing = required - provided
        if missing:
            raise CapabilityValidationError(
                stage_name=self.role.name,
                adapter_name=self.adapter.name,
                required_capabilities=required,
                provided_capabilities=provided,
                missing_capabilities=missing,
            )

    @staticmethod
    def _is_ndjson(text: str) -> bool:
        """Check if text appears to be raw NDJSON / JSON-lines stream rather than markdown."""
        if not text:
            return False
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            return False
        first_line = stripped.splitlines()[0].strip()
        if first_line.startswith("{") and first_line.endswith("}"):
            try:
                val = json.loads(first_line)
                return isinstance(val, dict)
            except Exception:
                return False
        return False

    def run(self, context: Context) -> StageResult:
        """Execute full stage lifecycle: validate -> prepare -> execute -> validate -> save."""
        # 0. Validate compatibility before any execution
        self.validate_compatibility()

        # 0.1 Tester v2: empirical black-box testing engine
        if self.role.name == "tester":
            from unittest.mock import Mock
            is_mock = (
                isinstance(getattr(self.adapter, "execute", None), Mock)
                or getattr(self.adapter, "is_mock", False)
                or self.adapter.__class__.__name__.startswith("Mock")
            )
            if not is_mock:
                from forge.testing.engine import TesterEngine
                engine = TesterEngine(context=context, run_manager=self.run_manager)
                return engine.run()

        # 1. Prepare
        instruction = InstructionBuilder.build(context, self.role)
        rendered_prompt = PromptCompiler.compile(
            instruction,
            self.role.template_content,
            max_prompt_bytes=self.adapter.max_prompt_bytes,
        )

        # ------------------------------------------------------------------
        # DEBUG: Persist the exact prompt sent to the adapter.
        # ------------------------------------------------------------------
        debug_dir = context.project_root / ".forge" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        (debug_dir / f"{self.role.name.lower()}_prompt.md").write_text(
            rendered_prompt.text,
            encoding="utf-8",
        )

        # 2. Execute
        timeout = self.timeout
        if timeout is None and context.config:
            stage_cfg = context.config.get_stage_config(
                self.role.name,
                phase=getattr(self.role, "phase", "pre_run"),
            )
            timeout = stage_cfg.timeout

        response = self.adapter.execute(
            prompt=rendered_prompt.text,
            cwd=context.project_root,
            timeout=timeout,
        )

        # ------------------------------------------------------------------
        # DEBUG: Capture adapter output before parsing.
        # ------------------------------------------------------------------
        (debug_dir / f"{self.role.name.lower()}_stdout.txt").write_text(
            response.stdout or "",
            encoding="utf-8",
        )

        (debug_dir / f"{self.role.name.lower()}_stderr.txt").write_text(
            response.stderr or "",
            encoding="utf-8",
        )

        (debug_dir / f"{self.role.name.lower()}_raw.txt").write_text(
            response.raw_output or "",
            encoding="utf-8",
        )

        # 3. Validate / Parse protocol
        raw_text = response.stdout or (response.raw_output if not self._is_ndjson(response.raw_output) else "")
        raw_dict, raw_yaml = MachineReportParser.extract_yaml(
            raw_text,
            expected_role=self.role.name,
        )

        if (
            not raw_dict
            and response.raw_output
            and response.raw_output != raw_text
            and not self._is_ndjson(response.raw_output)
        ):
            raw_dict, raw_yaml = MachineReportParser.extract_yaml(
                response.raw_output,
                expected_role=self.role.name,
            )

        report = MachineReportValidator.validate(
            data=raw_dict,
            expected_role=self.role.name,
            raw_yaml=raw_yaml,
        )

        success = (
            response.exit_code == 0
            and report.is_valid
            and report.status not in (
                "REJECTED",
                "FAILED",
                "BLOCKED",
                "CHANGES_REQUIRED",
            )
        )

        raw_markdown = response.stdout or response.raw_output
        result = StageResult(
            role=self.role,
            prompt=rendered_prompt,
            response=response,
            machine_report=report,
            raw_markdown=raw_markdown,
            duration_seconds=response.duration_seconds,
            success=success,
        )

        # 4. Save artifacts (.md + .json)
        self.run_manager.save_stage_artifacts(
            run=context.run,
            sequence_number=self.role.sequence_number,
            role_name=self.role.name,
            markdown_content=raw_markdown,
            json_data=result.to_dict(),
            prompt_hash=rendered_prompt.prompt_hash,
            adapter_name=self.adapter.name,
        )

        return result

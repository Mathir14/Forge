"""Pure stateless prompt compiler."""

from typing import Optional
from forge.prompts.instruction import Instruction
from forge.prompts.rendered_prompt import RenderedPrompt


class PromptCompiler:
    @staticmethod
    def _truncate_bytes(text: str, max_bytes: int, notice: str = "") -> str:
        """Truncate text so text.encode('utf-8') + notice fits in max_bytes without splitting code points."""
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        notice_bytes = len(notice.encode("utf-8"))
        allowed = max(0, max_bytes - notice_bytes)
        sliced = encoded[:allowed].decode("utf-8", errors="ignore")
        return sliced + notice

    @staticmethod
    def apply_transport_budget(
        instruction: Instruction,
        role_template: str,
        max_prompt_bytes: int,
    ) -> Instruction:
        """Apply deterministic priority truncation to fit prompt within max_prompt_bytes.

        Priority order for truncation:
        1. project_docs truncated first with explicit notices.
        2. git_diff truncated second with explicit byte omission notices.
        3. git_status truncated third.
        4. older previous_stage_outputs truncated fourth (preserving most recent prior stage output).
        5. executor_report (for Reviewer & Tester), tester_report, and tester_machine_report (for Reviewer) truncated fifth.
        6. task truncated sixth if still exceeding transport budget.

        Inviolable sections:
        Common Agent Protocol schema and role_template are preserved as highest priority.
        """
        target_bytes = max(1000, max_prompt_bytes - 1500)

        budgeted = Instruction(
            role_name=instruction.role_name,
            task=instruction.task,
            project_docs=dict(instruction.project_docs),
            previous_stage_outputs=dict(instruction.previous_stage_outputs),
            git_diff=instruction.git_diff,
            changed_files=list(instruction.changed_files),
            mixed_files=list(instruction.mixed_files),
            git_status=instruction.git_status,
            changed_file_summary=instruction.changed_file_summary,
            executor_report=instruction.executor_report,
            tester_report=instruction.tester_report,
            tester_machine_report=instruction.tester_machine_report,
            protocol_schema=instruction.protocol_schema,
            repair_feedback=instruction.repair_feedback,
            knowledge_context=instruction.knowledge_context,
        )


        def current_byte_size() -> int:
            role_clean = budgeted.role_name.lower().strip()
            if role_clean == "reviewer":
                text = PromptCompiler._compile_reviewer(budgeted, role_template).text
            elif role_clean == "tester":
                text = PromptCompiler._compile_tester(budgeted, role_template).text
            else:
                text = PromptCompiler._compile_standard(budgeted, role_template).text
            return len(text.encode("utf-8"))

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 1: Truncate project_docs
        if budgeted.project_docs and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            total_doc_bytes = sum(len(c.encode("utf-8")) for c in budgeted.project_docs.values())
            if total_doc_bytes <= excess:
                for k in budgeted.project_docs:
                    budgeted.project_docs[k] = "[... Project documentation omitted to fit adapter transport budget ...]"
            else:
                allowed = max(0, total_doc_bytes - excess)
                ratio = allowed / total_doc_bytes if total_doc_bytes > 0 else 0
                for k, v in list(budgeted.project_docs.items()):
                    v_bytes = len(v.encode("utf-8"))
                    doc_allow = int(v_bytes * ratio)
                    if doc_allow < 200:
                        budgeted.project_docs[k] = "[... Project documentation omitted to fit adapter transport budget ...]"
                    else:
                        budgeted.project_docs[k] = PromptCompiler._truncate_bytes(
                            v,
                            doc_allow,
                            notice="\n\n[... Project documentation truncated to fit adapter transport budget ...]",
                        )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 1b: Truncate knowledge_context if still exceeding target_bytes
        if budgeted.knowledge_context and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            kc_bytes = len(budgeted.knowledge_context.encode("utf-8"))
            allowed = max(0, kc_bytes - excess)
            if allowed < 200:
                budgeted.knowledge_context = "[... Project knowledge base omitted to fit adapter transport budget ...]"
            else:
                budgeted.knowledge_context = PromptCompiler._truncate_bytes(
                    budgeted.knowledge_context,
                    allowed,
                    notice="\n\n[... Project knowledge base truncated to fit adapter transport budget ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 2: Truncate git_diff
        if budgeted.git_diff and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            diff_bytes = len(budgeted.git_diff.encode("utf-8"))
            allowed = max(0, diff_bytes - excess)
            if allowed < 200:
                budgeted.git_diff = "[... Git diff omitted to fit adapter transport budget ...]"
            else:
                budgeted.git_diff = PromptCompiler._truncate_bytes(
                    budgeted.git_diff,
                    allowed,
                    notice=f"\n\n[... Git diff truncated ({diff_bytes - allowed} bytes omitted to fit adapter transport budget) ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 3: Truncate git_status
        if budgeted.git_status and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            status_bytes = len(budgeted.git_status.encode("utf-8"))
            allowed = max(0, status_bytes - excess)
            if allowed < 100:
                budgeted.git_status = "[... Git status omitted to fit adapter transport budget ...]"
            else:
                budgeted.git_status = PromptCompiler._truncate_bytes(
                    budgeted.git_status,
                    allowed,
                    notice="\n\n[... Git status truncated to fit adapter transport budget ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 4: Truncate older previous_stage_outputs (preserving most recent prior stage output)
        if budgeted.previous_stage_outputs:
            stage_keys = list(budgeted.previous_stage_outputs.keys())
            if len(stage_keys) > 1:
                older_keys = stage_keys[:-1]
                for k in older_keys:
                    if current_byte_size() <= target_bytes:
                        break
                    budgeted.previous_stage_outputs[k] = f"[... Output from {k} omitted to fit adapter transport budget ...]"

            if current_byte_size() > target_bytes:
                latest_key = stage_keys[-1]
                current_sz = current_byte_size()
                excess = current_sz - target_bytes
                out_bytes = len(budgeted.previous_stage_outputs[latest_key].encode("utf-8"))
                allowed = max(0, out_bytes - excess)
                if allowed < 200:
                    budgeted.previous_stage_outputs[latest_key] = f"[... Output from {latest_key} omitted to fit adapter transport budget ...]"
                else:
                    budgeted.previous_stage_outputs[latest_key] = PromptCompiler._truncate_bytes(
                        budgeted.previous_stage_outputs[latest_key],
                        allowed,
                        notice=f"\n\n[... Output from {latest_key} truncated to fit adapter transport budget ...]",
                    )

        if budgeted.role_name.lower().strip() in ("reviewer", "tester") and budgeted.executor_report and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            rep_bytes = len(budgeted.executor_report.encode("utf-8"))
            allowed = max(0, rep_bytes - excess)
            if allowed < 200:
                budgeted.executor_report = "[... Executor report omitted to fit adapter transport budget ...]"
            else:
                budgeted.executor_report = PromptCompiler._truncate_bytes(
                    budgeted.executor_report,
                    allowed,
                    notice="\n\n[... Executor report truncated to fit adapter transport budget ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        if budgeted.role_name.lower().strip() == "reviewer" and budgeted.tester_report and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            t_bytes = len(budgeted.tester_report.encode("utf-8"))
            allowed = max(0, t_bytes - excess)
            if allowed < 200:
                budgeted.tester_report = "[... Tester markdown report omitted to fit adapter transport budget ...]"
            else:
                budgeted.tester_report = PromptCompiler._truncate_bytes(
                    budgeted.tester_report,
                    allowed,
                    notice="\n\n[... Tester markdown report truncated to fit adapter transport budget ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        if budgeted.role_name.lower().strip() == "reviewer" and budgeted.tester_machine_report and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            tm_bytes = len(budgeted.tester_machine_report.encode("utf-8"))
            allowed = max(0, tm_bytes - excess)
            if allowed < 200:
                budgeted.tester_machine_report = "[... Tester machine protocol omitted to fit adapter transport budget ...]"
            else:
                budgeted.tester_machine_report = PromptCompiler._truncate_bytes(
                    budgeted.tester_machine_report,
                    allowed,
                    notice="\n\n[... Tester machine protocol truncated to fit adapter transport budget ...]",
                )

        if current_byte_size() <= max_prompt_bytes:
            return budgeted

        # Priority 5: Truncate task if still exceeding budget
        if budgeted.task and current_byte_size() > target_bytes:
            current_sz = current_byte_size()
            excess = current_sz - target_bytes
            task_bytes = len(budgeted.task.encode("utf-8"))
            allowed = max(0, task_bytes - excess)
            if allowed < 200:
                budgeted.task = PromptCompiler._truncate_bytes(
                    budgeted.task,
                    200,
                    notice="\n\n[... Task truncated to fit adapter transport budget ...]",
                )
            else:
                budgeted.task = PromptCompiler._truncate_bytes(
                    budgeted.task,
                    allowed,
                    notice="\n\n[... Task truncated to fit adapter transport budget ...]",
                )

        return budgeted

    @staticmethod
    def compile(
        instruction: Instruction,
        role_template: str,
        max_prompt_bytes: Optional[int] = None,
    ) -> RenderedPrompt:
        """Compile an Instruction and role template into a RenderedPrompt, applying transport budgeting if needed."""
        template_str = role_template.template_content if hasattr(role_template, "template_content") else str(role_template)
        role_name_clean = instruction.role_name.lower().strip()
        if role_name_clean == "reviewer":
            compile_fn = PromptCompiler._compile_reviewer
        elif role_name_clean == "tester":
            compile_fn = PromptCompiler._compile_tester
        else:
            compile_fn = PromptCompiler._compile_standard

        rendered = compile_fn(instruction, template_str)
        if max_prompt_bytes is None or len(rendered.text.encode("utf-8")) <= max_prompt_bytes:
            return rendered

        budgeted_instruction = PromptCompiler.apply_transport_budget(
            instruction=instruction,
            role_template=template_str,
            max_prompt_bytes=max_prompt_bytes,
        )
        result = compile_fn(budgeted_instruction, template_str)
        # Enforce hard byte ceiling so prompt never exceeds max_prompt_bytes (e.g. Antigravity argv limit)
        if max_prompt_bytes is not None and len(result.text.encode("utf-8")) > max_prompt_bytes:
            truncated_text = PromptCompiler._truncate_bytes(
                result.text,
                max_prompt_bytes,
                notice="\n\n[... Prompt truncated to fit adapter transport budget ...]",
            )
            return RenderedPrompt.from_text(truncated_text)
        return result

    @staticmethod
    def _compile_standard(instruction: Instruction, role_template: str) -> RenderedPrompt:
        parts = [
            f"# ROLE: {instruction.role_name.upper()}",
            role_template.strip(),
        ]

        if instruction.project_docs:
            parts.append("\n## PROJECT DOCUMENTATION & CONVENTIONS")
            for doc_name, content in instruction.project_docs.items():
                if content.strip():
                    parts.append(f"### {doc_name}\n{content.strip()}")

        if instruction.knowledge_context:
            parts.append(instruction.knowledge_context.strip())

        if instruction.previous_stage_outputs:
            parts.append("\n## PREVIOUS STAGE ARTIFACTS")
            for stage_name, output in instruction.previous_stage_outputs.items():
                if output.strip():
                    parts.append(f"### Output from {stage_name.capitalize()}\n{output.strip()}")

        if instruction.git_diff:
            parts.append(f"\n## GIT DIFF\n```diff\n{instruction.git_diff.strip()}\n```")

        if instruction.changed_files or instruction.mixed_files:
            cf_lines = []
            if instruction.changed_files:
                cf_lines.extend(f"- {f}" for f in instruction.changed_files)
            if instruction.mixed_files:
                cf_lines.append("\n### Mixed-Ownership Files (Ambiguous Attribution):")
                cf_lines.extend(
                    f"- {f} (pre-existing user modifications detected; excluded from auto-commit)"
                    for f in instruction.mixed_files
                )
            parts.append(
                f"\n## CHANGED FILES\n" + "\n".join(cf_lines)
            )

        if instruction.task:
            task_str = instruction.task.strip()
            if instruction.repair_feedback and instruction.repair_feedback.strip() not in task_str:
                task_str += f"\n\n{instruction.repair_feedback.strip()}"
            parts.append(f"\n## USER TASK REQUEST\n{task_str}")

        if instruction.protocol_schema:
            parts.append(f"\n## PROTOCOL REQUIREMENT\n{instruction.protocol_schema.strip()}")

        rendered_text = "\n\n".join(parts)
        return RenderedPrompt.from_text(rendered_text)

    @staticmethod
    def _compile_reviewer(instruction: Instruction, role_template: str) -> RenderedPrompt:
        """Compile change-centric, diff-first prompt context for Reviewer."""
        parts = [
            f"# ROLE: REVIEWER",
            role_template.strip(),
        ]

        if instruction.project_docs:
            parts.append("\n## PROJECT DOCUMENTATION & CONVENTIONS")
            for doc_name, content in instruction.project_docs.items():
                if content.strip():
                    parts.append(f"### {doc_name}\n{content.strip()}")

        if instruction.knowledge_context:
            parts.append(instruction.knowledge_context.strip())

        # 1. Original requirements
        task_text = instruction.task.strip() if instruction.task else "(No requirements specified.)"
        if instruction.repair_feedback and instruction.repair_feedback.strip() not in task_text:
            task_text += f"\n\n{instruction.repair_feedback.strip()}"
        parts.append(f"\n## Original Requirements\n{task_text}")

        # 1a. Architect specification (if available from previous stages)
        arch_output = instruction.previous_stage_outputs.get("architect")
        if arch_output and arch_output.strip():
            parts.append(f"\n## Architect Specification\n{arch_output.strip()}")

        # 1b. Planner plan (if available from previous stages)
        planner_output = instruction.previous_stage_outputs.get("planner")
        if planner_output and planner_output.strip():
            parts.append(f"\n## Planner Plan\n{planner_output.strip()}")

        # 2. Executor report
        exec_report = instruction.executor_report
        if not exec_report and instruction.previous_stage_outputs:
            exec_report = instruction.previous_stage_outputs.get("executor")
        exec_report_text = exec_report.strip() if exec_report else "(No Executor report found.)"
        parts.append(f"\n## Executor Report\n{exec_report_text}")

        # 2a. Tester Report & Behavioral Evidence (always provided to Reviewer)
        tester_parts = []
        if instruction.tester_report and instruction.tester_report.strip():
            tester_parts.append(f"### Tester Markdown Report\n{instruction.tester_report.strip()}")
        if instruction.tester_machine_report and instruction.tester_machine_report.strip():
            tester_parts.append(f"### Tester Machine Protocol (JSON)\n```json\n{instruction.tester_machine_report.strip()}\n```")

        if tester_parts:
            behavioral_guidance = (
                "Treat Tester findings as objective behavioral evidence.\n"
                "Reviewer remains solely responsible for implementation quality, architecture, security, and maintainability.\n"
                "Cross-examine code changes and tests against observed runtime behaviors:\n"
                "- If Tester reported PASS: verify that code achieving the pass does not rely on hacks, mock bypasses, or security flaws.\n"
                "- If Tester reported FAIL: verify root causes in the diff and evaluate code-level defects.\n"
                "- If Tester reported NOT_TESTABLE: verify that static tests and mocks adequately cover changes.\n"
                "- If Tester reported BLOCKED: inspect environmental or dependency issues in the code."
            )
            parts.append(f"\n## Tester Report & Behavioral Evidence\n{behavioral_guidance}\n\n" + "\n\n".join(tester_parts))
        else:
            parts.append("\n## Tester Report & Behavioral Evidence\n(No prior Tester report found for this run.)")

        # 3. Git status
        status_text = instruction.git_status.strip() if instruction.git_status else "(Clean working tree / no uncommitted status changes detected.)"
        parts.append(f"\n## Git Status\n{status_text}")

        # 4. Changed-file summary
        changed_parts = []
        if instruction.changed_file_summary:
            changed_parts.append(instruction.changed_file_summary.strip())
        elif instruction.changed_files:
            changed_parts.append("\n".join(f"- {f}" for f in instruction.changed_files))
        else:
            changed_parts.append("(No changed files detected.)")

        if instruction.mixed_files:
            mixed_lines = "\n".join(f"- {f}" for f in instruction.mixed_files)
            changed_parts.append(
                f"\n### Mixed-Ownership Files (Ambiguous Attribution):\n"
                f"{mixed_lines}\n\n"
                f"These files had pre-existing user modifications before the Forge run\n"
                f"and were modified again by Forge. Forge cannot safely separate the\n"
                f"changes automatically. They are excluded from automatic commit and\n"
                f"require manual review."
            )
        parts.append(f"\n## Changed Files\n" + "\n".join(changed_parts))


        # 5. Git diff
        diff_content = instruction.git_diff.strip() if instruction.git_diff else "(No git diff detected.)"
        parts.append(f"\n## Git Diff\n```diff\n{diff_content}\n```")

        # 6. Review Instructions
        review_instructions = (
            "Treat the Executor as a change producer and verify all claims against reality.\n"
            "Do not trust the Executor report blindly — substantiate all claims:\n"
            "1. Executor Claims vs. Diff:\n"
            "   - Compare each claim in the Executor Report directly against the Git diff and repository state.\n"
            "   - Flag claims that are unsubstantiated, partially implemented, or contradicted by the diff (e.g. parameter ignored, test checks only a dummy constant, logic omitted).\n"
            "2. Requirement Coverage:\n"
            "   - Verify that all original requirements and edge cases are satisfied by the actual code changes.\n"
            "3. Code Quality & Regressions:\n"
            "   - Check for introduced bugs, security vulnerabilities, performance regressions, or anti-patterns.\n"
            "4. Untracked and New Files:\n"
            "   - Verify that all newly created and untracked files are complete, correct, and tested."
        )
        parts.append(f"\n## Review Instructions\n{review_instructions}")

        # 7. Repository Access
        repo_access = (
            "The repository is available for inspection.\n"
            "Use it when the diff alone is insufficient."
        )
        parts.append(f"\n## Repository Access\n{repo_access}")

        # 8. Protocol Requirement
        if instruction.protocol_schema:
            parts.append(f"\n## PROTOCOL REQUIREMENT\n{instruction.protocol_schema.strip()}")

        rendered_text = "\n\n".join(parts)
        return RenderedPrompt.from_text(rendered_text)

    @staticmethod
    def _compile_tester(instruction: Instruction, role_template: str) -> RenderedPrompt:
        """Compile user-centric, behavioral testing prompt context for Tester."""
        parts = [
            f"# ROLE: TESTER",
            role_template.strip(),
        ]

        if instruction.project_docs:
            parts.append("\n## PROJECT DOCUMENTATION & CONVENTIONS")
            for doc_name, content in instruction.project_docs.items():
                if content.strip():
                    parts.append(f"### {doc_name}\n{content.strip()}")

        if instruction.knowledge_context:
            parts.append(instruction.knowledge_context.strip())

        # 1. User requirements & acceptance criteria
        task_text = instruction.task.strip() if instruction.task else "(No requirements specified.)"
        if instruction.repair_feedback and instruction.repair_feedback.strip() not in task_text:
            task_text += f"\n\n{instruction.repair_feedback.strip()}"
        parts.append(f"\n## User Requirements & Acceptance Criteria\n{task_text}")

        # 2. Architect Specification (if available)
        arch_output = instruction.previous_stage_outputs.get("architect")
        if arch_output and arch_output.strip():
            parts.append(f"\n## Architect Specification\n{arch_output.strip()}")

        # 3. Planner Plan (if available)
        planner_output = instruction.previous_stage_outputs.get("planner")
        if planner_output and planner_output.strip():
            parts.append(f"\n## Planner Plan\n{planner_output.strip()}")

        # 4. Executor Implementation Claims
        exec_report = instruction.executor_report
        if not exec_report and instruction.previous_stage_outputs:
            exec_report = instruction.previous_stage_outputs.get("executor")
        exec_report_text = exec_report.strip() if exec_report else "(No Executor report found.)"
        parts.append(f"\n## Executor Implementation Claims\n{exec_report_text}")

        # 5. Changed Files Summary
        changed_parts = []
        if instruction.changed_file_summary:
            changed_parts.append(instruction.changed_file_summary.strip())
        elif instruction.changed_files:
            changed_parts.append("\n".join(f"- {f}" for f in instruction.changed_files))
        else:
            changed_parts.append("(No changed files detected.)")
        parts.append(f"\n## Changed Files\n" + "\n".join(changed_parts))

        # 6. Testing Instructions
        test_instructions = (
            "Evaluate observable runtime behavior and user experience:\n"
            "1. User Journey Verification:\n"
            "   - Launch the application, dev server, CLI, or test harness.\n"
            "   - Exercise primary and secondary user flows end-to-end.\n"
            "2. Interactive UI & Error Resilience:\n"
            "   - Check for dead buttons, unlinked click handlers, or missing states.\n"
            "   - Test invalid inputs and edge cases for clear, human-readable error messages.\n"
            "   - Check responsive layout stability and loading indicators.\n"
            "3. High-Confidence Accessibility:\n"
            "   - Check keyboard navigation (Tab order, Enter/Space activation).\n"
            "   - Check obvious focus indicators and clearly missing input labels.\n"
            "   - Check severe visual contrast problems.\n"
            "4. Defect Reporting:\n"
            "   - For every failure, provide explicit reproduction steps, expected vs actual behavior, and evidence.\n"
            "   - Do NOT emit numeric scores or critique code style/architecture."
        )
        parts.append(f"\n## Testing Instructions\n{test_instructions}")

        # 7. Protocol Requirement
        if instruction.protocol_schema:
            parts.append(f"\n## PROTOCOL REQUIREMENT\n{instruction.protocol_schema.strip()}")

        rendered_text = "\n\n".join(parts)
        return RenderedPrompt.from_text(rendered_text)

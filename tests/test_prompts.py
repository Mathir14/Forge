from forge.prompts.instruction import Instruction
from forge.prompts.compiler import PromptCompiler


def test_prompt_compiler_hashing():
    inst = Instruction(
        role_name="architect",
        task="Create login service",
        project_docs={"conventions": "Follow PEP8."},
        previous_stage_outputs={},
    )
    role_template = "You are the software architect."

    prompt1 = PromptCompiler.compile(inst, role_template)
    prompt2 = PromptCompiler.compile(inst, role_template)

    assert prompt1.prompt_hash == prompt2.prompt_hash
    assert len(prompt1.prompt_hash) == 16
    assert "Create login service" in prompt1.text
    assert "Follow PEP8." in prompt1.text
    assert prompt1.estimated_tokens > 0

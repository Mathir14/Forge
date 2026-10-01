"""Unit tests for Phase 5: KnowledgeProjector and Prompt Injection."""

from pathlib import Path
import pytest

from forge.core.knowledge import KnowledgeFact
from forge.storage.knowledge import KnowledgeStore
from forge.prompts.knowledge_projector import (
    KnowledgeProjection,
    KnowledgeSelector,
    KnowledgeBudgeter,
    KnowledgeProjector,
)
from forge.prompts.instruction import Instruction
from forge.prompts.compiler import PromptCompiler


def test_knowledge_projection_visibility_and_deprecated_filtering():
    facts = [
        KnowledgeFact(id="arch-1", type="architecture", title="Arch 1", status="VERIFIED"),
        KnowledgeFact(id="feat-1", type="feature", title="Feat 1", status="VERIFIED"),
        KnowledgeFact(id="dec-1", type="decision", title="Dec 1", status="HUMAN_LOCKED"),
        KnowledgeFact(id="debt-1", type="unresolved", title="Debt 1", status="DISPUTED"),
        KnowledgeFact(id="dead-1", type="architecture", title="Dead 1", status="DEPRECATED"),
    ]

    # Tester projection
    tester_projected = KnowledgeProjection.project(facts, role_name="tester")
    ids = [f.id for f in tester_projected]
    assert "dead-1" not in ids  # Deprecated must be excluded
    assert "feat-1" in ids
    assert "arch-1" in ids
    assert "debt-1" in ids


def test_knowledge_selector_scoring_and_priorities():
    disputed_fact = KnowledgeFact(
        id="payment-service",
        type="architecture",
        title="Payment Service",
        status="DISPUTED",
        summary="Payment logic.",
    )
    locked_fact = KnowledgeFact(
        id="adr-014-status",
        type="decision",
        title="ADR-014 Status Rules",
        status="HUMAN_LOCKED",
    )
    file_matched_fact = KnowledgeFact(
        id="auth-guard",
        type="architecture",
        title="Auth Guard",
        status="VERIFIED",
        evidence=["src/auth/guard.py"],
    )
    ordinary_fact = KnowledgeFact(
        id="misc-fact",
        type="architecture",
        title="Misc",
        status="PROVISIONAL",
    )

    projected = [ordinary_fact, file_matched_fact, locked_fact, disputed_fact]
    selected = KnowledgeSelector.select(
        projected,
        role_name="architect",
        task="refactor authentication routes",
        changed_files=["src/auth/guard.py"],
    )

    # Disputed fact has highest score (+1000), then locked (+800), then file matched (+500 + task match)
    assert selected[0].id == "payment-service"
    assert selected[1].id in ("adr-014-status", "auth-guard")
    assert selected[-1].id == "misc-fact"


def test_knowledge_budgeter_respects_max_chars():
    facts = [
        KnowledgeFact(id=f"fact-{i}", type="architecture", title=f"Architecture Fact {i}", summary="A" * 50)
        for i in range(20)
    ]

    # Render with tight budget of 500 chars
    rendered = KnowledgeBudgeter.apply_budget(facts, max_chars=500)
    assert len(rendered) <= 650  # Allows small notice overflow
    assert "## PROJECT KNOWLEDGE BASE" in rendered
    assert "[... Additional knowledge facts omitted to respect transport budget ...]" in rendered


def test_knowledge_projector_end_to_end(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    assert KnowledgeProjector.project_context(store, role_name="architect") == ""

    # Seed facts
    store.save_all([
        KnowledgeFact(id="module-core", type="architecture", title="Core Module", status="VERIFIED"),
        KnowledgeFact(id="feat-checkout", type="feature", title="Stripe Checkout", status="VERIFIED"),
        KnowledgeFact(id="adr-002", type="decision", title="ADR-002 Tester Role", status="HUMAN_LOCKED"),
    ])

    context_md = KnowledgeProjector.project_context(
        store=store,
        role_name="tester",
        task="Test Stripe Checkout journey",
    )
    assert "## PROJECT KNOWLEDGE BASE" in context_md
    assert "Stripe Checkout" in context_md
    assert "ADR-002 Tester Role" in context_md


def test_prompt_compiler_renders_knowledge_context():
    kc = "## PROJECT KNOWLEDGE BASE\n\n### Verified Features & Capabilities\n- **[VERIFIED] User Login** (`user-login`)"
    instruction = Instruction(
        role_name="architect",
        task="Design user profile",
        knowledge_context=kc,
    )
    rendered = PromptCompiler.compile(instruction, role_template="# Arch instructions")
    assert "## PROJECT KNOWLEDGE BASE" in rendered.text
    assert "User Login" in rendered.text


def test_prompt_compiler_applies_transport_budget_to_knowledge():
    huge_kc = "## PROJECT KNOWLEDGE BASE\n\n" + ("- **Fact**: details\n" * 500)
    instruction = Instruction(
        role_name="architect",
        task="Quick task",
        knowledge_context=huge_kc,
    )
    # Budget max 3000 bytes
    budgeted = PromptCompiler.apply_transport_budget(
        instruction,
        role_template="# Arch template",
        max_prompt_bytes=3000,
    )
    assert len(budgeted.knowledge_context.encode("utf-8")) < len(huge_kc.encode("utf-8"))
    assert "[... Project knowledge base" in budgeted.knowledge_context

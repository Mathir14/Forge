"""End-to-end integration tests for PKB v1 lifecycle."""

import json
from pathlib import Path
import pytest

from forge.core.context import Context
from forge.core.knowledge import (
    FactType,
    FactStatus,
    FactSource,
    KnowledgeFact,
    KnowledgeProposal,
)
from forge.core.reconciler import KnowledgeReconciler
from forge.core.role import Role
from forge.core.run import Run
from forge.prompts.builder import InstructionBuilder
from forge.protocol.report import MachineReport
from forge.protocol.validator import MachineReportValidator
from forge.storage.knowledge import KnowledgeStore
from forge.storage.run_manager import RunManager


def test_pkb_e2e_multi_run_lifecycle(tmp_path: Path):
    """Verify full end-to-end lifecycle:

    Run 1: Architect proposes architecture & feature facts.
           Tester verifies feature and disputes an architectural component.
           Reconciler persists updates to .forge/knowledge/ and synthesizes dispute anomaly.
    Run 2: Subsequent run projects updated knowledge (including dispute warning) into context prompt.
    """
    store = KnowledgeStore(tmp_path)
    run_mgr = RunManager(tmp_path)

    # 1. Seed initial settled decision (ADR-014) as HUMAN_LOCKED
    locked_adr = KnowledgeFact(
        id="adr-014-status-invariants",
        type="decision",
        title="ADR-014 Protocol Invariants",
        status="HUMAN_LOCKED",
        summary="BLOCKED requires HANDOFF: NONE.",
        provenance={"source": "human"},
    )
    store.save_fact(locked_adr)

    # 2. Simulate Run 1
    run1 = run_mgr.create_run(task="Implement payments subsystem")
    run1_dir = run1.run_dir
    proposals_dir = run1_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    # Architect stage emits proposals:
    # - New architecture fact: payment-service
    # - New feature fact: stripe-checkout
    arch_proposals = [
        KnowledgeProposal(
            action="ASSERT",
            id="payment-service",
            type="architecture",
            title="Payment Service",
            summary="Encapsulates all payment operations.",
            evidence=["src/payment.py"],
            role="architect",
        ),
        KnowledgeProposal(
            action="ASSERT",
            id="stripe-checkout",
            type="feature",
            title="Stripe Checkout Flow",
            summary="Allows user checkout with Stripe credit card processing.",
            evidence=["src/checkout.py"],
            role="architect",
        ),
    ]

    # Tester stage emits proposals:
    # - VERIFY stripe-checkout
    # - DISPUTE payment-service (checkout service still executes retries directly)
    tester_proposals = [
        KnowledgeProposal(
            action="VERIFY",
            id="stripe-checkout",
            type="feature",
            evidence=["tests/test_checkout.py"],
            role="tester",
        ),
        KnowledgeProposal(
            action="DISPUTE",
            id="payment-service",
            type="architecture",
            note="CheckoutService still executes direct payment retries in checkout.py:140",
            evidence=["src/checkout.py"],
            role="tester",
        ),
    ]

    # Reviewer stage emits proposal:
    # - ASSERT convention in decisions
    reviewer_proposals = [
        KnowledgeProposal(
            action="ASSERT",
            id="conv-payment-idempotency",
            type="decision",
            title="Payment Idempotency Keys",
            summary="All payment requests must carry an Idempotency-Key HTTP header.",
            role="reviewer",
        )
    ]

    all_run1_proposals = arch_proposals + tester_proposals + reviewer_proposals

    # Reconcile Run 1
    reconciler = KnowledgeReconciler(tmp_path, store=store)
    diff = reconciler.reconcile_run(run1.run_id, run1_dir, proposals=all_run1_proposals)

    # Verify reconciliation diff
    assert "stripe-checkout" in diff["added"]
    assert "payment-service" in diff["added"]
    assert "payment-service" in diff["disputed"]
    assert "dispute-payment-service" in diff["added"]
    assert "conv-payment-idempotency" in diff["added"]

    # Verify facts on disk
    k_dir = tmp_path / ".forge" / "knowledge"
    assert (k_dir / "architecture.yaml").exists()
    assert (k_dir / "features.yaml").exists()
    assert (k_dir / "decisions.yaml").exists()
    assert (k_dir / "unresolved.yaml").exists()

    checkout_fact = store.get_fact("stripe-checkout")
    assert checkout_fact.status == "VERIFIED"
    assert checkout_fact.source == "verified"
    assert "tests/test_checkout.py" in checkout_fact.evidence

    payment_fact = store.get_fact("payment-service")
    assert payment_fact.status == "DISPUTED"
    assert len(payment_fact.disputes) == 1
    assert payment_fact.disputes[0]["role"] == "tester"

    dispute_anomaly = store.get_fact("dispute-payment-service")
    assert dispute_anomaly is not None
    assert dispute_anomaly.type == "unresolved"
    assert "checkout.py:140" in dispute_anomaly.summary

    # 3. Simulate Run 2 starting for another task touching checkout
    from forge.core.config import Config
    from forge.core.git import GitService
    config = Config.load(tmp_path)
    git = GitService(tmp_path)

    run2 = run_mgr.create_run(task="Refactor checkout retry loop")
    context2 = Context(run=run2, project_root=tmp_path, config=config, git=git)
    architect_role = Role(
        name="architect",
        sequence_number=1,
        template_content="# Architect instructions",
    )

    instruction2 = InstructionBuilder.build(context2, architect_role)

    # Assert that Run 2's compiled knowledge context includes all verified and disputed facts
    assert instruction2.knowledge_context != ""
    assert "## PROJECT KNOWLEDGE BASE" in instruction2.knowledge_context
    assert "Stripe Checkout Flow" in instruction2.knowledge_context
    assert "⚠️ [DISPUTED] Payment Service" in instruction2.knowledge_context
    assert "Dispute (tester): CheckoutService still executes direct payment retries" in instruction2.knowledge_context
    assert "ADR-014 Protocol Invariants" in instruction2.knowledge_context
    assert "Payment Idempotency Keys" in instruction2.knowledge_context

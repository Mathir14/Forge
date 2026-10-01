"""Unit tests for KnowledgeReconciler."""

import json
from pathlib import Path
import pytest
import yaml

from forge.core.knowledge import (
    FactType,
    FactStatus,
    FactSource,
    ProposalAction,
    KnowledgeFact,
    KnowledgeProposal,
)
from forge.core.reconciler import KnowledgeReconciler
from forge.storage.knowledge import KnowledgeStore


def test_reconciler_assert_new_fact(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)

    proposal = KnowledgeProposal(
        action="ASSERT",
        id="auth-rate-limiter",
        type="architecture",
        title="Auth Rate Limiter",
        summary="5 attempts per minute.",
        evidence=["src/auth/limiter.py"],
        role="architect",
    )

    diff = reconciler.reconcile_run("run-001", run_dir, proposals=[proposal])
    assert "auth-rate-limiter" in diff["added"]
    assert diff["run_id"] == "run-001"

    fact = store.get_fact("auth-rate-limiter")
    assert fact is not None
    assert fact.title == "Auth Rate Limiter"
    assert fact.status == "PROVISIONAL"
    assert fact.source == "inferred"

    # Verify knowledge_diff.json was saved
    diff_file = run_dir / "knowledge_diff.json"
    assert diff_file.exists()
    with open(diff_file, "r", encoding="utf-8") as f:
        saved_diff = json.load(f)
    assert saved_diff["added"] == ["auth-rate-limiter"]


def test_reconciler_verify_fact(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    # Seed an existing provisional feature fact
    fact = KnowledgeFact(
        id="user-login",
        type="feature",
        title="User Login",
        status="PROVISIONAL",
        summary="Login journey",
    )
    store.save_fact(fact)

    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-002"
    run_dir.mkdir(parents=True, exist_ok=True)

    verify_prop = KnowledgeProposal(
        action="VERIFY",
        id="user-login",
        type="feature",
        evidence=["tests/test_login.py"],
        role="tester",
    )

    diff = reconciler.reconcile_run("run-002", run_dir, proposals=[verify_prop])
    assert "user-login" in diff["modified"]

    updated_fact = store.get_fact("user-login")
    assert updated_fact.status == "VERIFIED"
    assert updated_fact.source == "verified"
    assert updated_fact.provenance["last_verified_run"] == "run-002"
    assert updated_fact.provenance["last_verified_role"] == "tester"
    assert "tests/test_login.py" in updated_fact.evidence


def test_reconciler_dispute_synthesizes_anomaly(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    # Seed an existing architecture fact
    fact = KnowledgeFact(
        id="payment-service",
        type="architecture",
        title="Payment Service",
        status="VERIFIED",
        summary="Encapsulates all payments.",
    )
    store.save_fact(fact)

    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-003"
    run_dir.mkdir(parents=True, exist_ok=True)

    dispute_prop = KnowledgeProposal(
        action="DISPUTE",
        id="payment-service",
        type="architecture",
        note="CheckoutService still executes payment retries directly",
        evidence=["src/checkout.py"],
        role="tester",
    )

    diff = reconciler.reconcile_run("run-003", run_dir, proposals=[dispute_prop])
    assert "payment-service" in diff["disputed"]
    assert "dispute-payment-service" in diff["added"]

    updated_fact = store.get_fact("payment-service")
    assert updated_fact.status == "DISPUTED"
    assert len(updated_fact.disputes) == 1
    assert updated_fact.disputes[0]["role"] == "tester"

    anomaly = store.get_fact("dispute-payment-service")
    assert anomaly is not None
    assert anomaly.type == "unresolved"
    assert anomaly.payload["category"] == "KNOWLEDGE_DISPUTE"
    assert anomaly.payload["disputed_fact_id"] == "payment-service"


def test_reconciler_enforces_human_locked_invariant(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    locked_fact = KnowledgeFact(
        id="adr-014-status-invariants",
        type="decision",
        title="ADR-014 Status Rules",
        status="HUMAN_LOCKED",
        summary="BLOCKED requires HANDOFF: NONE.",
        provenance={"source": "human"},
    )
    store.save_fact(locked_fact)

    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-004"
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Attempting to ASSERT/modify locked fact should be rejected
    p_assert = KnowledgeProposal(
        action="ASSERT",
        id="adr-014-status-invariants",
        type="decision",
        title="Overwritten by agent",
        role="architect",
    )
    diff = reconciler.reconcile_run("run-004", run_dir, proposals=[p_assert])
    assert len(diff["rejected"]) == 1
    assert "HUMAN_LOCKED" in diff["rejected"][0]["reason"]
    assert store.get_fact("adr-014-status-invariants").title == "ADR-014 Status Rules"

    # 2. Attempting to DISPUTE locked fact should synthesize violation anomaly without mutating locked fact
    p_dispute = KnowledgeProposal(
        action="DISPUTE",
        id="adr-014-status-invariants",
        type="decision",
        note="Code failed to follow invariant",
        role="tester",
    )
    diff2 = reconciler.reconcile_run("run-004", run_dir, proposals=[p_dispute])
    assert store.get_fact("adr-014-status-invariants").is_locked
    assert "violation-adr-014-status-invariants" in diff2["added"]
    violation = store.get_fact("violation-adr-014-status-invariants")
    assert violation is not None
    assert violation.type == "unresolved"
    assert violation.payload["category"] == "SPECIFICATION_VIOLATION"


def test_reconciler_rejects_unauthorized_roles(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-005"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Executor rejected (INV-PKB-01)
    p_exec = KnowledgeProposal(
        action="ASSERT",
        id="feat-exec",
        type="feature",
        title="Exec Feature",
        role="executor",
    )
    diff1 = reconciler.reconcile_run("run-005", run_dir, proposals=[p_exec])
    assert any("INV-PKB-01" in r["reason"] for r in diff1["rejected"])

    # Planner rejected (INV-PKB-02)
    p_plan = KnowledgeProposal(
        action="ASSERT",
        id="feat-plan",
        type="feature",
        title="Plan Feature",
        role="planner",
    )
    diff2 = reconciler.reconcile_run("run-005", run_dir, proposals=[p_plan])
    assert any("INV-PKB-02" in r["reason"] for r in diff2["rejected"])


def test_reconciler_loads_staged_proposals_from_disk(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    reconciler = KnowledgeReconciler(tmp_path, store=store)
    run_dir = tmp_path / ".forge" / "runs" / "run-006"
    proposals_dir = run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    p_data = {
        "role": "architect",
        "sequence_number": 1,
        "proposals": [
            {
                "action": "ASSERT",
                "id": "arch-staged",
                "type": "architecture",
                "title": "Staged Architecture Fact",
                "summary": "Loaded from yaml file.",
            }
        ],
    }
    with open(proposals_dir / "01_architect_proposals.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(p_data, f)

    diff = reconciler.reconcile_run("run-006", run_dir)
    assert "arch-staged" in diff["added"]
    assert store.get_fact("arch-staged") is not None

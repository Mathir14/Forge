"""Tests for PKB v1 domain models and data structures."""

import pytest
from forge.core.knowledge import (
    FactType,
    FactStatus,
    FactSource,
    ProposalAction,
    KnowledgeFact,
    KnowledgeProposal,
)


def test_fact_enums():
    assert "architecture" in FactType.values()
    assert "feature" in FactType.values()
    assert "decision" in FactType.values()
    assert "unresolved" in FactType.values()

    assert "PROVISIONAL" in FactStatus.values()
    assert "VERIFIED" in FactStatus.values()
    assert "DISPUTED" in FactStatus.values()
    assert "HUMAN_LOCKED" in FactStatus.values()

    assert "inferred" in FactSource.values()
    assert "observed" in FactSource.values()
    assert "verified" in FactSource.values()
    assert "human" in FactSource.values()


def test_knowledge_fact_creation_and_defaults():
    fact = KnowledgeFact(
        id="auth-rate-limiter",
        type="architecture",
        title="Per-IP Sign-In Rate Limiting",
        summary="Rate limits auth endpoints to 5 attempts per minute.",
        evidence=["src/auth/limiter.py"],
    )
    assert fact.id == "auth-rate-limiter"
    assert fact.type == "architecture"
    assert fact.status == "PROVISIONAL"
    assert fact.source == "inferred"
    assert not fact.is_locked
    assert not fact.is_disputed
    assert not fact.is_verified
    assert fact.validate() == []


def test_knowledge_fact_status_properties():
    fact_locked = KnowledgeFact(
        id="core-invariant",
        type="decision",
        title="Invariant rule",
        status="HUMAN_LOCKED",
        provenance={"source": "human"},
    )
    assert fact_locked.is_locked
    assert fact_locked.source == "human"

    fact_disputed = KnowledgeFact(
        id="disputed-fact",
        type="architecture",
        title="Disputed module",
        status="DISPUTED",
    )
    assert fact_disputed.is_disputed

    fact_verified = KnowledgeFact(
        id="verified-feature",
        type="feature",
        title="User Login",
        status="VERIFIED",
        provenance={"source": "verified"},
    )
    assert fact_verified.is_verified
    assert fact_verified.source == "verified"


def test_knowledge_fact_validation_errors():
    # Invalid slug
    f_slug = KnowledgeFact(id="Invalid_Slug!", type="architecture", title="Title")
    errors = f_slug.validate()
    assert any("kebab-case" in e for e in errors)

    # Empty title
    f_title = KnowledgeFact(id="valid-slug", type="architecture", title="   ")
    errors = f_title.validate()
    assert any("Fact title must be a non-empty string" in e for e in errors)

    # Invalid type
    f_type = KnowledgeFact(id="valid-slug", type="unknown_type", title="Title")
    errors = f_type.validate()
    assert any("Fact type" in e for e in errors)

    # Invalid status
    f_status = KnowledgeFact(id="valid-slug", type="architecture", title="Title", status="BOGUS")
    errors = f_status.validate()
    assert any("Fact status" in e for e in errors)

    # Invalid source
    f_src = KnowledgeFact(
        id="valid-slug",
        type="architecture",
        title="Title",
        provenance={"source": "telepathy"},
    )
    errors = f_src.validate()
    assert any("Provenance source" in e for e in errors)


def test_knowledge_fact_serialization():
    fact = KnowledgeFact(
        id="payment-service",
        type="architecture",
        title="Payment Service",
        status="VERIFIED",
        summary="Handles Stripe charges.",
        evidence=["src/payment.py", "tests/test_payment.py"],
        provenance={
            "originating_run": "run-001",
            "originating_role": "architect",
            "source": "verified",
        },
        disputes=[{"run": "run-002", "role": "tester", "claim": "leaking retry"}],
        payload={"exports": ["charge"]},
    )
    d = fact.to_dict()
    assert d["id"] == "payment-service"
    assert d["status"] == "VERIFIED"
    assert d["disputes"][0]["run"] == "run-002"
    assert d["payload"]["exports"] == ["charge"]

    fact_restored = KnowledgeFact.from_dict(d)
    assert fact_restored.id == fact.id
    assert fact_restored.status == fact.status
    assert fact_restored.evidence == fact.evidence
    assert fact_restored.disputes == fact.disputes
    assert fact_restored.payload == fact.payload
    assert fact_restored.source == "verified"


def test_knowledge_proposal_validation():
    # Valid ASSERT
    p_assert = KnowledgeProposal(
        action="ASSERT",
        id="feature-checkout",
        type="feature",
        title="Cart Checkout Journey",
        evidence=["src/checkout.py"],
        role="architect",
    )
    assert p_assert.validate() == []
    assert p_assert.role == "architect"

    # ASSERT without title
    p_bad = KnowledgeProposal(action="ASSERT", id="feature-checkout", title="")
    assert any("ASSERT proposal requires a non-empty title" in e for e in p_bad.validate())

    # Invalid action
    p_act = KnowledgeProposal(action="DESTROY", id="feature-checkout", title="Test")
    assert any("Proposal action 'DESTROY' invalid" in e for e in p_act.validate())

    # Non-ASSERT actions don't strictly require title
    p_verify = KnowledgeProposal(action="VERIFY", id="feature-checkout", evidence=["tests/test_checkout.py"])
    assert p_verify.validate() == []


def test_knowledge_proposal_serialization():
    p = KnowledgeProposal(
        action="DISPUTE",
        id="payment-service",
        note="Retry loop observed in checkout service",
        evidence=["tests/test_checkout.py"],
        role="tester",
    )
    d = p.to_dict()
    assert d["action"] == "DISPUTE"
    assert d["id"] == "payment-service"
    assert d["note"] == "Retry loop observed in checkout service"

    p_restored = KnowledgeProposal.from_dict(d)
    assert p_restored.action == "DISPUTE"
    assert p_restored.id == "payment-service"
    assert p_restored.role == "tester"

"""Unit tests for KnowledgeStore."""

from pathlib import Path
import pytest
import yaml

from forge.core.knowledge import KnowledgeFact
from forge.storage.knowledge import KnowledgeStore


def test_knowledge_store_empty_directory(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    facts = store.load_all()
    assert facts == {}
    assert store.get_fact("nonexistent") is None


def test_knowledge_store_save_and_load_roundtrip(tmp_path: Path):
    store = KnowledgeStore(tmp_path)

    f_arch = KnowledgeFact(
        id="module-auth",
        type="architecture",
        title="Auth Subsystem",
        summary="JWT auth module.",
        evidence=["src/auth/jwt.py"],
    )
    f_feat = KnowledgeFact(
        id="feature-login",
        type="feature",
        title="User Login",
        summary="User login journey.",
    )
    f_dec = KnowledgeFact(
        id="adr-001-immutable-runs",
        type="decision",
        title="ADR-001",
        status="HUMAN_LOCKED",
    )
    f_debt = KnowledgeFact(
        id="bug-mock-pid",
        type="unresolved",
        title="Mock PID bug",
        status="DISPUTED",
    )

    store.save_all([f_arch, f_feat, f_dec, f_debt])

    # Check files exist on disk
    k_dir = tmp_path / ".forge" / "knowledge"
    assert (k_dir / "architecture.yaml").exists()
    assert (k_dir / "features.yaml").exists()
    assert (k_dir / "decisions.yaml").exists()
    assert (k_dir / "unresolved.yaml").exists()

    # Load back
    loaded = store.load_all()
    assert len(loaded) == 4
    assert loaded["module-auth"].title == "Auth Subsystem"
    assert loaded["feature-login"].type == "feature"
    assert loaded["adr-001-immutable-runs"].is_locked
    assert loaded["bug-mock-pid"].is_disputed

    # Load by type
    arch_facts = store.load_by_type("architecture")
    assert len(arch_facts) == 1
    assert arch_facts[0].id == "module-auth"

    feat_facts = store.load_by_type("feature")
    assert len(feat_facts) == 1
    assert feat_facts[0].id == "feature-login"


def test_knowledge_store_deterministic_sorting(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    f1 = KnowledgeFact(id="z-module", type="architecture", title="Z")
    f2 = KnowledgeFact(id="a-module", type="architecture", title="A")
    f3 = KnowledgeFact(id="m-module", type="architecture", title="M")

    store.save_all([f1, f2, f3])

    with open(tmp_path / ".forge" / "knowledge" / "architecture.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    ids = [item["id"] for item in data["facts"]]
    assert ids == ["a-module", "m-module", "z-module"]


def test_knowledge_store_save_single_fact_and_delete(tmp_path: Path):
    store = KnowledgeStore(tmp_path)
    f1 = KnowledgeFact(id="feat-1", type="feature", title="Feature 1")
    f2 = KnowledgeFact(id="feat-2", type="feature", title="Feature 2")
    store.save_all([f1, f2])

    # Update f1
    f1_updated = KnowledgeFact(id="feat-1", type="feature", title="Feature 1 Updated")
    store.save_fact(f1_updated)

    all_facts = store.load_all()
    assert len(all_facts) == 2
    assert all_facts["feat-1"].title == "Feature 1 Updated"

    # Delete f2
    deleted = store.delete_fact("feat-2")
    assert deleted is True
    assert store.get_fact("feat-2") is None
    assert len(store.load_all()) == 1

    # Delete non-existent
    assert store.delete_fact("feat-nonexistent") is False


def test_knowledge_store_handles_top_level_yaml_list(tmp_path: Path):
    k_dir = tmp_path / ".forge" / "knowledge"
    k_dir.mkdir(parents=True, exist_ok=True)
    raw_yaml = [
        {"id": "feat-manual", "type": "feature", "title": "Manual Feature"}
    ]
    with open(k_dir / "features.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(raw_yaml, f)

    store = KnowledgeStore(tmp_path)
    fact = store.get_fact("feat-manual")
    assert fact is not None
    assert fact.title == "Manual Feature"

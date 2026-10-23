"""Regression and generalization tests for PKB Extraction and Persistence across repositories.

Verifies:
1. Critic findings qualify as unresolved debt and extract into validated proposals.
2. Verified architectural decisions and conventions persist.
3. Findings that do not qualify (minor formatting, low confidence) do not become proposals.
4. Empty proposal output with no qualifying knowledge explicitly records why.
5. Critic with qualifying issues but empty KNOWLEDGE_PROPOSALS has debt extracted automatically.
6. Malformed proposals fail validation and are recorded in rejected_proposals / diff['rejected'].
7. Duplicate and conflicting proposals are handled deterministically.
8. Persistence write failure is observable in diff and run metadata.
9. Successful persistence followed by retrieval via store.load_all() and KnowledgeProjector.
10. Multi-project isolation: separate project roots never contaminate each other.
"""

import json
from pathlib import Path
import pytest
import yaml

from forge.core.extractor import KnowledgeExtractor, slugify_finding
from forge.core.knowledge import (
    FactType,
    FactStatus,
    FactSource,
    ProposalAction,
    KnowledgeFact,
    KnowledgeProposal,
)
from forge.core.reconciler import KnowledgeReconciler
from forge.protocol.validator import MachineReportValidator
from forge.storage.knowledge import KnowledgeStore
from forge.storage.run_manager import RunManager
from forge.cli import reconcile_run_knowledge
from forge.prompts.knowledge_projector import KnowledgeProjector


def test_scenario_1_critic_findings_qualify_and_persist(tmp_path: Path):
    """Scenario 1: Concrete Critic findings (Critical and Major) extract and persist into PKB."""
    store = KnowledgeStore(tmp_path)
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Audit data and model modules")

    # Simulate Critic report with Critical and Major issues (like run-005)
    critic_report_data = {
        "ROLE": "CRITIC",
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
        "CONFIDENCE": "HIGH",
        "ISSUES": {
            "CRITICAL": [
                "[CRIT-1] ModelTrainer.train_model leaks raw ValueError when fitting unencoded non-numeric features."
            ],
            "MAJOR": [
                "[MAJ-1] views/module_01_data.py tab order violates ADR-039 to satisfy brittle mobile test coordinates.",
                "[MAJ-2] Mobile CSS override in views/base.py disables pointer events for unlisted sidebar controls."
            ],
            "MINOR": [
                "[MIN-1] views/module_09_reports.py duplicates Executive Summary headers."
            ]
        }
    }

    report = MachineReportValidator.validate(critic_report_data, expected_role="CRITIC")
    assert report.is_valid is True
    # Exactly 3 qualifying proposals (1 Critical + 2 Major), Minor is excluded
    assert len(report.proposals) == 3

    p0 = report.proposals[0]
    assert p0.action == "ASSERT"
    assert p0.type == "unresolved"
    assert p0.role == "critic"
    assert "debt-" in p0.id

    # Stage proposals to run directory (as stage.py does after validation)
    proposals_dir = run.run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    stage_yaml = {
        "role": "critic",
        "sequence_number": 6,
        "proposals_count": len(report.proposals),
        "proposals": [p.to_dict() for p in report.proposals],
    }
    with open(proposals_dir / "06_critic_proposals.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(stage_yaml, f)

    # Reconcile into PKB
    diff = reconcile_run_knowledge(tmp_path, run.run_id, run.run_dir, run=run)
    assert diff is not None
    assert len(diff["added"]) == 3
    assert diff["rejected"] == []

    # Run metadata accurately records persistence
    assert run.metadata["reconcile_canonical"] is True
    assert run.metadata["pkb"]["status"] == "PERSISTED"
    assert run.metadata["pkb"]["added"] == 3

    # Normal retrieval returns all 3 facts
    all_facts = store.load_all()
    assert len(all_facts) == 3
    unresolved_facts = store.load_by_type("unresolved")
    assert len(unresolved_facts) == 3

    # Verify projection into context works
    projected = KnowledgeProjector.project_context(store=store, role_name="architect", max_chars=10000)
    assert "## PROJECT KNOWLEDGE BASE" in projected
    assert "ModelTrainer.train_model" in projected or "raw ValueError" in projected


def test_scenario_2_non_qualifying_findings_excluded(tmp_path: Path):
    """Scenario 2: Findings that do not qualify (only Minor or LOW confidence) are not proposed."""
    # Subcase A: Only minor issues
    data_minor_only = {
        "ROLE": "CRITIC",
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
        "CONFIDENCE": "HIGH",
        "ISSUES": {
            "CRITICAL": [],
            "MAJOR": [],
            "MINOR": ["Typo in docstring", "Cosmetic indentation mismatch"]
        }
    }
    report_minor = MachineReportValidator.validate(data_minor_only, expected_role="CRITIC")
    assert report_minor.is_valid is True
    assert len(report_minor.proposals) == 0
    assert "no Critical or Major issues" in (report_minor.proposal_reason or "")

    # Subcase B: LOW confidence
    data_low_conf = {
        "ROLE": "CRITIC",
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
        "CONFIDENCE": "LOW",
        "ISSUES": {
            "CRITICAL": ["Possible theoretical leak"]
        }
    }
    report_low = MachineReportValidator.validate(data_low_conf, expected_role="CRITIC")
    assert report_low.is_valid is True
    assert len(report_low.proposals) == 0
    assert "confidence is LOW" in (report_low.proposal_reason or "")


def test_scenario_3_empty_output_explicit_policy_and_metadata(tmp_path: Path):
    """Scenario 3: When no qualifying knowledge exists, framework explicitly reports why and does NOT claim persistence."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Run without findings")

    # Reconcile empty run
    diff = reconcile_run_knowledge(tmp_path, run.run_id, run.run_dir, run=run)
    assert diff is not None
    assert diff["added"] == []

    # Metadata must NOT claim reconcile_canonical: true when 0 proposals existed!
    assert run.metadata.get("reconcile_canonical") is False
    assert run.metadata.get("pkb", {}).get("status") == "NO_CHANGES"


def test_scenario_4_malformed_proposals_recorded_in_rejected_artifacts(tmp_path: Path):
    """Scenario 4: Malformed proposals fail validation and are recorded in rejected artifacts without disappearing."""
    data = {
        "ROLE": "ARCHITECT",
        "STATUS": "APPROVED",
        "HANDOFF": "PLANNER",
        "KNOWLEDGE_PROPOSALS": [
            {
                "action": "INVALID_ACTION",
                "id": "NOT-A-VALID-SLUG!!",
                "type": "architecture",
            }
        ]
    }
    report = MachineReportValidator.validate(data, expected_role="ARCHITECT")
    assert report.is_valid is False
    assert len(report.rejected_proposals) == 1
    assert "INVALID_ACTION" in str(report.rejected_proposals[0]["errors"])

    # Simulate staged proposals file with invalid YAML item
    run_dir = tmp_path / ".forge" / "runs" / "run-malformed"
    proposals_dir = run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    stage_yaml = {
        "role": "architect",
        "sequence_number": 1,
        "proposals": [
            "this-is-not-a-dict-item"
        ],
        "rejected_proposals": report.rejected_proposals
    }
    with open(proposals_dir / "01_architect_proposals.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(stage_yaml, f)

    reconciler = KnowledgeReconciler(tmp_path)
    diff = reconciler.reconcile_run("run-malformed", run_dir)
    assert len(diff["rejected"]) >= 2
    reasons = [r["reason"] for r in diff["rejected"]]
    assert any("INVALID_ACTION" in r for r in reasons)
    assert any("expected dict" in r for r in reasons)


def test_scenario_5_duplicate_slug_deduplication(tmp_path: Path):
    """Scenario 5: Multiple findings generating similar slugs are deduplicated deterministically."""
    data = {
        "ROLE": "CRITIC",
        "STATUS": "CRITIQUE_COMPLETE",
        "HANDOFF": "NONE",
        "ISSUES": {
            "CRITICAL": [
                "Memory leak in SessionHandler during connection reset.",
                "Memory leak in SessionHandler during timeout event."
            ]
        }
    }
    report = MachineReportValidator.validate(data, expected_role="CRITIC")
    assert len(report.proposals) == 2
    ids = [p.id for p in report.proposals]
    # IDs must be distinct
    assert ids[0] != ids[1]
    assert "-2" in ids[1] or ids[1].startswith("debt-memory-leak")


def test_scenario_6_persistence_failure_handling(tmp_path: Path, monkeypatch):
    """Scenario 6: Persistence write failure is observable in diff and run metadata."""
    run_mgr = RunManager(tmp_path)
    run = run_mgr.create_run(task="Run testing write failure")

    # Stage valid proposal
    proposals_dir = run.run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    p_data = {
        "role": "critic",
        "sequence_number": 6,
        "proposals": [
            {
                "action": "ASSERT",
                "id": "debt-unhandled-io",
                "type": "unresolved",
                "title": "Unhandled IO Error",
                "summary": "Disk write fails on full drive.",
            }
        ]
    }
    with open(proposals_dir / "06_critic_proposals.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(p_data, f)

    # Monkeypatch KnowledgeStore.save_all to simulate disk error
    def mock_save_all(*args, **kwargs):
        raise OSError("Read-only file system or disk full")

    store = KnowledgeStore(tmp_path)
    monkeypatch.setattr(store, "save_all", mock_save_all)

    reconciler = KnowledgeReconciler(tmp_path, store=store)
    with pytest.raises(OSError, match="disk full"):
        reconciler.reconcile_run(run.run_id, run.run_dir)

    # Verify knowledge_diff.json was saved before exception propagation
    diff_file = run.run_dir / "knowledge_diff.json"
    assert diff_file.exists()
    diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
    assert diff_data.get("persistence_status") == "FAILED"
    assert "disk full" in diff_data.get("persistence_error", "")


def test_scenario_7_multi_project_isolation(tmp_path: Path):
    """Scenario 7: Multiple projects have independent PKB directories with no cross-project leakage."""
    proj_a = tmp_path / "project_a"
    proj_b = tmp_path / "project_b"
    proj_a.mkdir()
    proj_b.mkdir()

    store_a = KnowledgeStore(proj_a)
    store_b = KnowledgeStore(proj_b)

    fact_a = KnowledgeFact(
        id="arch-project-a-core",
        type="architecture",
        title="Project A Core",
        summary="Core subsystem for A",
    )
    store_a.save_fact(fact_a)

    fact_b = KnowledgeFact(
        id="arch-project-b-core",
        type="architecture",
        title="Project B Core",
        summary="Core subsystem for B",
    )
    store_b.save_fact(fact_b)

    # Verify project A contains ONLY fact A
    facts_a = store_a.load_all()
    assert "arch-project-a-core" in facts_a
    assert "arch-project-b-core" not in facts_a

    # Verify project B contains ONLY fact B
    facts_b = store_b.load_all()
    assert "arch-project-b-core" in facts_b
    assert "arch-project-a-core" not in facts_b


def test_scenario_8_provenance_and_run_id_attribution(tmp_path: Path):
    """Scenario 8: Knowledge facts durably retain originating run ID, role, and verified updates."""
    store = KnowledgeStore(tmp_path)
    run_mgr = RunManager(tmp_path)
    run1 = run_mgr.create_run(task="Run 1 task")

    proposals_dir = run1.run_dir / "knowledge_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    p1 = {
        "role": "critic",
        "sequence_number": 6,
        "proposals": [
            {
                "action": "ASSERT",
                "id": "debt-unhandled-timeout",
                "type": "unresolved",
                "title": "Unhandled Timeout",
                "summary": "Gateway calls omit timeout.",
                "evidence": ["src/gateway.py:55"],
            }
        ]
    }
    with open(proposals_dir / "06_critic_proposals.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(p1, f)

    reconcile_run_knowledge(tmp_path, run1.run_id, run1.run_dir, run=run1)

    fact = store.get_fact("debt-unhandled-timeout")
    assert fact is not None
    assert fact.provenance["originating_run"] == run1.run_id
    assert fact.provenance["originating_role"] == "critic"
    assert fact.provenance["source"] == "observed"
    assert "src/gateway.py:55" in fact.evidence


def test_scenario_9_backward_compatibility_with_existing_pkb_schemas(tmp_path: Path):
    """Scenario 9: Backward compatibility with legacy PKB YAML files (unversioned, list format, extra fields)."""
    k_dir = tmp_path / ".forge" / "knowledge"
    k_dir.mkdir(parents=True, exist_ok=True)

    # Legacy architecture.yaml with plain list format and missing payload
    legacy_yaml = """
- id: "legacy-module-auth"
  type: "architecture"
  title: "Legacy Auth Module"
  status: "PROVISIONAL"
  summary: "Old module format without schema_version"
"""
    (k_dir / "architecture.yaml").write_text(legacy_yaml, encoding="utf-8")

    store = KnowledgeStore(tmp_path)
    facts = store.load_all()
    assert "legacy-module-auth" in facts
    f = facts["legacy-module-auth"]
    assert f.title == "Legacy Auth Module"
    assert f.type == "architecture"

    # Saving again upgrades format safely without losing data
    store.save_all(facts)
    loaded_upgraded = yaml.safe_load((k_dir / "architecture.yaml").read_text(encoding="utf-8"))
    assert loaded_upgraded.get("schema_version") == "1.0"
    assert len(loaded_upgraded.get("facts", [])) == 1

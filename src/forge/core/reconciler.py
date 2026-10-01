"""Knowledge Reconciler engine for Project Knowledge Base (PKB).

Merges staged proposals into .forge/knowledge/ at run completion under exclusive lock.
Enforces role ownership rules, HUMAN_LOCKED immutability, and dispute synthesis.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

from forge.core.knowledge import (
    FactType,
    FactStatus,
    FactSource,
    ProposalAction,
    KnowledgeFact,
    KnowledgeProposal,
)
from forge.storage.knowledge import KnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgeReconciler:
    """Deterministic merge engine that reconciles run knowledge proposals into PKB."""

    def __init__(
        self,
        project_root: Optional[Path] = None,
        store: Optional[KnowledgeStore] = None,
    ):
        self.project_root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
        self.store = store or KnowledgeStore(self.project_root)

    def check_role_permission(self, proposal: KnowledgeProposal) -> Optional[str]:
        """Validate whether proposal.role has authority to emit proposal action & type."""
        role = (proposal.role or "").lower().strip()
        action = proposal.action.upper().strip()
        ftype = proposal.type.lower().strip()

        # INV-PKB-01: Executor prohibition
        if role == "executor":
            return "Protocol invariant violation (INV-PKB-01): Executor cannot propose knowledge."

        # INV-PKB-02: Planner prohibition
        if role == "planner":
            return "Protocol invariant violation (INV-PKB-02): Planner is read-only for knowledge."

        if role == "architect":
            if action in (ProposalAction.ASSERT.value, ProposalAction.VERIFY.value):
                if ftype not in (FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value):
                    return f"Architect cannot {action} fact type '{ftype}'."
            elif action == ProposalAction.DISPUTE.value:
                if ftype != FactType.UNRESOLVED.value:
                    return f"Architect can only dispute unresolved debt, not '{ftype}'."

        elif role == "critic":
            # Critic can audit all types, discover features, or report debt
            if action == ProposalAction.ASSERT.value:
                if ftype not in (FactType.FEATURE.value, FactType.UNRESOLVED.value):
                    return f"Critic can only ASSERT 'feature' (discovered) or 'unresolved' (debt), not '{ftype}'."

        elif role == "tester":
            if action == ProposalAction.ASSERT.value:
                if ftype != FactType.UNRESOLVED.value:
                    return "Tester can only ASSERT 'unresolved' (behavioral defects and bugs)."
            elif action == ProposalAction.VERIFY.value:
                if ftype not in (FactType.FEATURE.value, FactType.ARCHITECTURE.value, FactType.DECISION.value):
                    return f"Tester can only VERIFY 'feature', 'architecture', or 'decision', not '{ftype}'."
            elif action == ProposalAction.DISPUTE.value:
                if ftype not in (FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value):
                    return f"Tester can only DISPUTE 'architecture', 'feature', or 'decision', not '{ftype}'."

        elif role == "reviewer":
            if action == ProposalAction.ASSERT.value:
                if ftype != FactType.DECISION.value:
                    return "Reviewer can only ASSERT 'decision' (conventions)."
            elif action == ProposalAction.VERIFY.value:
                if ftype not in (FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value):
                    return f"Reviewer can only VERIFY architecture, feature, or decision, not '{ftype}'."
            elif action == ProposalAction.DISPUTE.value:
                if ftype not in (FactType.FEATURE.value, FactType.ARCHITECTURE.value):
                    return f"Reviewer can only DISPUTE 'feature' or 'architecture', not '{ftype}'."

        return None

    def load_staged_proposals(self, run_dir: Path) -> List[KnowledgeProposal]:
        """Load all staged proposals from .forge/runs/<run_id>/knowledge_proposals/."""
        proposals: List[KnowledgeProposal] = []
        proposals_dir = run_dir / "knowledge_proposals"
        if not proposals_dir.exists():
            return proposals

        for p_file in sorted(proposals_dir.glob("*.yaml")):
            try:
                with open(p_file, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                if not content or not isinstance(content, dict):
                    continue
                default_role = content.get("role")
                raw_proposals = content.get("proposals", [])
                for item in raw_proposals:
                    if isinstance(item, dict):
                        try:
                            proposal = KnowledgeProposal.from_dict(item, default_role=default_role)
                            proposals.append(proposal)
                        except Exception as e:
                            logger.warning("Skipping invalid proposal in %s: %s", p_file, e)
            except Exception as e:
                logger.warning("Failed to load proposals file %s: %s", p_file, e)

        return proposals

    def reconcile_run(
        self,
        run_id: str,
        run_dir: Path,
        proposals: Optional[List[KnowledgeProposal]] = None,
    ) -> Dict[str, Any]:
        """Reconcile all proposals for a run into the knowledge store.

        Outputs knowledge_diff.json to run_dir and persists updated facts.
        """
        if proposals is None:
            proposals = self.load_staged_proposals(run_dir)

        existing_facts = self.store.load_all()
        now_iso = datetime.now(timezone.utc).isoformat()

        diff: Dict[str, Any] = {
            "run_id": run_id,
            "reconciled_at": now_iso,
            "added": [],
            "modified": [],
            "disputed": [],
            "deprecated": [],
            "rejected": [],
        }

        for p in proposals:
            perm_err = self.check_role_permission(p)
            if perm_err:
                diff["rejected"].append({"proposal_id": p.id, "reason": perm_err})
                continue

            existing = existing_facts.get(p.id)

            # INV-PKB-04: HUMAN_LOCKED invariant enforcement
            if existing and existing.is_locked:
                if p.action == ProposalAction.DISPUTE.value:
                    # Synthesize specification violation anomaly without altering locked fact
                    violation_id = f"violation-{existing.id}"
                    violation_fact = KnowledgeFact(
                        id=violation_id,
                        type=FactType.UNRESOLVED.value,
                        title=f"Specification Violation: {existing.title}",
                        status=FactStatus.DISPUTED.value,
                        summary=p.summary or p.note or f"Runtime behavior violates locked fact '{existing.id}'.",
                        evidence=list(p.evidence),
                        provenance={
                            "originating_run": run_id,
                            "originating_role": p.role,
                            "source": FactSource.VERIFIED.value,
                            "created_at": now_iso,
                        },
                        payload={
                            "category": "SPECIFICATION_VIOLATION",
                            "locked_fact_id": existing.id,
                            "note": p.note,
                        },
                    )
                    existing_facts[violation_id] = violation_fact
                    if violation_id not in diff["added"]:
                        diff["added"].append(violation_id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Target fact '{p.id}' is HUMAN_LOCKED; mutations disallowed by autonomous agents (INV-PKB-04).",
                    })
                continue

            # Process actions on unlocked facts
            if p.action == ProposalAction.ASSERT.value:
                if existing:
                    if p.title:
                        existing.title = p.title
                    if p.summary:
                        existing.summary = p.summary
                    if p.evidence:
                        existing.evidence = sorted(list(set(existing.evidence + p.evidence)))
                    if p.payload:
                        existing.payload.update(p.payload)
                    existing.provenance["last_verified_run"] = run_id
                    existing.provenance["last_verified_role"] = p.role
                    existing.provenance["last_verified_at"] = now_iso
                    if existing.id not in diff["modified"]:
                        diff["modified"].append(existing.id)
                else:
                    src = FactSource.VERIFIED.value if p.role in ("tester", "reviewer") else FactSource.INFERRED.value
                    if p.role == "critic":
                        src = FactSource.OBSERVED.value
                    new_fact = KnowledgeFact(
                        id=p.id,
                        type=p.type,
                        title=p.title,
                        status=FactStatus.PROVISIONAL.value,
                        summary=p.summary,
                        evidence=list(p.evidence),
                        provenance={
                            "originating_run": run_id,
                            "originating_role": p.role,
                            "source": src,
                            "created_at": now_iso,
                        },
                        payload=dict(p.payload),
                    )
                    existing_facts[new_fact.id] = new_fact
                    if new_fact.id not in diff["added"]:
                        diff["added"].append(new_fact.id)

            elif p.action == ProposalAction.VERIFY.value:
                if existing:
                    existing.status = FactStatus.VERIFIED.value
                    existing.provenance["source"] = FactSource.VERIFIED.value
                    existing.provenance["last_verified_run"] = run_id
                    existing.provenance["last_verified_role"] = p.role
                    existing.provenance["last_verified_at"] = now_iso
                    if p.evidence:
                        existing.evidence = sorted(list(set(existing.evidence + p.evidence)))
                    if existing.id not in diff["modified"]:
                        diff["modified"].append(existing.id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Cannot VERIFY non-existent fact '{p.id}'.",
                    })

            elif p.action == ProposalAction.DISPUTE.value:
                if existing:
                    existing.status = FactStatus.DISPUTED.value
                    dispute_record = {
                        "run": run_id,
                        "role": p.role,
                        "claim": p.note or p.summary or "Contradiction observed",
                        "evidence": list(p.evidence),
                        "timestamp": now_iso,
                    }
                    existing.disputes.append(dispute_record)
                    if existing.id not in diff["disputed"]:
                        diff["disputed"].append(existing.id)

                    # Synthesize anomaly in unresolved.yaml
                    dispute_id = f"dispute-{existing.id}"
                    dispute_fact = KnowledgeFact(
                        id=dispute_id,
                        type=FactType.UNRESOLVED.value,
                        title=f"Disputed: {existing.title}",
                        status=FactStatus.DISPUTED.value,
                        summary=p.note or p.summary or f"Contradiction observed by {p.role} in run {run_id}",
                        evidence=list(p.evidence),
                        provenance={
                            "originating_run": run_id,
                            "originating_role": p.role,
                            "source": FactSource.VERIFIED.value,
                            "created_at": now_iso,
                        },
                        payload={
                            "category": "KNOWLEDGE_DISPUTE",
                            "disputed_fact_id": existing.id,
                            "note": p.note,
                        },
                    )
                    existing_facts[dispute_id] = dispute_fact
                    if dispute_id not in diff["added"]:
                        diff["added"].append(dispute_id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Cannot DISPUTE non-existent fact '{p.id}'.",
                    })

            elif p.action == ProposalAction.DEPRECATE.value:
                if existing:
                    existing.status = FactStatus.DEPRECATED.value
                    existing.provenance["deprecated_run"] = run_id
                    existing.provenance["deprecated_role"] = p.role
                    existing.provenance["deprecated_at"] = now_iso
                    if existing.id not in diff["deprecated"]:
                        diff["deprecated"].append(existing.id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Cannot DEPRECATE non-existent fact '{p.id}'.",
                    })

        # Persist updated facts
        self.store.save_all(existing_facts)

        # Write diff artifact to run directory
        diff_file = run_dir / "knowledge_diff.json"
        try:
            with open(diff_file, "w", encoding="utf-8") as f:
                json.dump(diff, f, indent=2)
        except Exception as e:
            logger.warning("Failed to write knowledge_diff.json to %s: %s", diff_file, e)

        return diff

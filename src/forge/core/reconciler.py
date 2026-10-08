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
        initial_fact_ids = set(existing_facts.keys())

        diff: Dict[str, Any] = {
            "run_id": run_id,
            "reconciled_at": now_iso,
            "added": [],
            "modified": [],
            "disputed": [],
            "deprecated": [],
            "rejected": [],
        }

        def record_diff(category: str, fact_id: str) -> None:
            """Record fact_id into diff categories preserving strict exclusivity between added and modified."""
            if category == "disputed":
                if fact_id not in diff["disputed"]:
                    diff["disputed"].append(fact_id)
                return
            if category == "deprecated":
                if fact_id not in diff["deprecated"]:
                    diff["deprecated"].append(fact_id)
                return

            # Mutation lifecycle: added vs modified are strictly mutually exclusive
            if fact_id in diff["added"]:
                # Fact was created/added in this run; subsequent updates stay classified as 'added'
                return
            if fact_id in initial_fact_ids:
                if fact_id not in diff["modified"]:
                    diff["modified"].append(fact_id)
            else:
                if fact_id not in diff["added"]:
                    diff["added"].append(fact_id)

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
                    note_claim = p.summary or p.note or f"Runtime behavior violates locked fact '{existing.id}'."
                    if violation_id in existing_facts:
                        violation_fact = existing_facts[violation_id]
                        if p.evidence:
                            existing_ev = list(getattr(violation_fact, "evidence", []) or [])
                            violation_fact.evidence = sorted(list(set(existing_ev + list(p.evidence))))
                        if isinstance(violation_fact.payload, dict):
                            prev_note = violation_fact.payload.get("note", "")
                            if p.note and p.note not in prev_note:
                                violation_fact.payload["note"] = f"{prev_note}; {p.note}" if prev_note else p.note
                        if p.note and p.note not in violation_fact.summary:
                            violation_fact.summary = f"{violation_fact.summary}; {p.note}"
                        if isinstance(violation_fact.provenance, dict):
                            violation_fact.provenance["last_updated_run"] = run_id
                            violation_fact.provenance["last_updated_role"] = p.role
                            violation_fact.provenance["last_updated_at"] = now_iso
                        record_diff("modified", violation_id)
                    else:
                        violation_fact = KnowledgeFact(
                            id=violation_id,
                            type=FactType.UNRESOLVED.value,
                            title=f"Specification Violation: {existing.title}",
                            status=FactStatus.DISPUTED.value,
                            summary=note_claim,
                            evidence=sorted(list(set(p.evidence))) if p.evidence else [],
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
                        record_diff("added", violation_id)
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
                    record_diff("modified", existing.id)
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
                    record_diff("added", new_fact.id)

            elif p.action == ProposalAction.VERIFY.value:
                if existing:
                    existing.status = FactStatus.VERIFIED.value
                    existing.provenance["source"] = FactSource.VERIFIED.value
                    existing.provenance["last_verified_run"] = run_id
                    existing.provenance["last_verified_role"] = p.role
                    existing.provenance["last_verified_at"] = now_iso
                    if p.evidence:
                        existing.evidence = sorted(list(set(existing.evidence + p.evidence)))
                    record_diff("modified", existing.id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Cannot VERIFY non-existent fact '{p.id}'.",
                    })

            elif p.action == ProposalAction.DISPUTE.value:
                if existing:
                    existing.status = FactStatus.DISPUTED.value
                    claim_text = p.note or p.summary or "Contradiction observed"
                    found_dispute = None
                    for d in existing.disputes:
                        if (
                            isinstance(d, dict)
                            and d.get("run") == run_id
                            and d.get("role") == p.role
                            and d.get("claim") == claim_text
                        ):
                            found_dispute = d
                            break

                    if found_dispute is not None:
                        if p.evidence:
                            existing_ev = found_dispute.get("evidence", [])
                            found_dispute["evidence"] = sorted(list(set(existing_ev + list(p.evidence))))
                        found_dispute["timestamp"] = now_iso
                    else:
                        dispute_record = {
                            "run": run_id,
                            "role": p.role,
                            "claim": claim_text,
                            "evidence": list(p.evidence),
                            "timestamp": now_iso,
                        }
                        existing.disputes.append(dispute_record)

                    record_diff("disputed", existing.id)

                    # Synthesize anomaly in unresolved.yaml with all distinct dispute claims
                    dispute_id = f"dispute-{existing.id}"
                    distinct_claims = []
                    for d in existing.disputes:
                        if isinstance(d, dict) and d.get("claim"):
                            c = str(d["claim"]).strip()
                            if c and c not in distinct_claims:
                                distinct_claims.append(c)
                    synth_summary = "; ".join(distinct_claims) if distinct_claims else (p.note or p.summary or f"Contradiction observed by {p.role} in run {run_id}")

                    if dispute_id in existing_facts:
                        dispute_fact = existing_facts[dispute_id]
                        if p.evidence:
                            existing_ev = list(getattr(dispute_fact, "evidence", []) or [])
                            dispute_fact.evidence = sorted(list(set(existing_ev + list(p.evidence))))
                        dispute_fact.summary = synth_summary
                        if isinstance(dispute_fact.payload, dict):
                            dispute_fact.payload["disputes"] = list(existing.disputes)
                            if p.note and p.note not in dispute_fact.payload.get("note", ""):
                                prev_note = dispute_fact.payload.get("note", "")
                                dispute_fact.payload["note"] = f"{prev_note}; {p.note}" if prev_note else p.note
                        if isinstance(dispute_fact.provenance, dict):
                            dispute_fact.provenance["last_updated_run"] = run_id
                            dispute_fact.provenance["last_updated_role"] = p.role
                            dispute_fact.provenance["last_updated_at"] = now_iso
                        record_diff("modified", dispute_id)
                    else:
                        dispute_fact = KnowledgeFact(
                            id=dispute_id,
                            type=FactType.UNRESOLVED.value,
                            title=f"Disputed: {existing.title}",
                            status=FactStatus.DISPUTED.value,
                            summary=synth_summary,
                            evidence=sorted(list(set(p.evidence))) if p.evidence else [],
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
                                "disputes": list(existing.disputes),
                            },
                        )
                        existing_facts[dispute_id] = dispute_fact
                        record_diff("added", dispute_id)
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
                    record_diff("deprecated", existing.id)
                else:
                    diff["rejected"].append({
                        "proposal_id": p.id,
                        "reason": f"Cannot DEPRECATE non-existent fact '{p.id}'.",
                    })

        # Guarantee strict exclusivity between added and modified
        overlap = set(diff["added"]) & set(diff["modified"])
        if overlap:
            for fid in overlap:
                diff["modified"].remove(fid)

        # Deterministic sorting for output stability
        for k in ("added", "modified", "disputed", "deprecated"):
            diff[k] = sorted(list(dict.fromkeys(diff[k])))

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

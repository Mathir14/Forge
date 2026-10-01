"""Project Knowledge Base (PKB) context projector for prompt compilation.

Structures context injection as explicit pipeline steps:
Knowledge Store -> Knowledge Projection -> Knowledge Selection -> Token Budget -> Prompt Injection.
"""

from typing import Dict, List, Optional, Set
from forge.core.knowledge import FactType, FactStatus, KnowledgeFact
from forge.storage.knowledge import KnowledgeStore


class KnowledgeProjection:
    """Projects raw facts into role-visible candidate sets."""

    ROLE_TYPE_VISIBILITY: Dict[str, Set[str]] = {
        "critic": {FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value, FactType.UNRESOLVED.value},
        "architect": {FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value, FactType.UNRESOLVED.value},
        "planner": {FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.DECISION.value, FactType.UNRESOLVED.value},
        "executor": {FactType.ARCHITECTURE.value, FactType.DECISION.value, FactType.UNRESOLVED.value, FactType.FEATURE.value},
        "tester": {FactType.FEATURE.value, FactType.ARCHITECTURE.value, FactType.UNRESOLVED.value, FactType.DECISION.value},
        "reviewer": {FactType.DECISION.value, FactType.ARCHITECTURE.value, FactType.FEATURE.value, FactType.UNRESOLVED.value},
    }

    @classmethod
    def project(cls, facts: List[KnowledgeFact], role_name: str) -> List[KnowledgeFact]:
        clean_role = role_name.lower().strip()
        allowed_types = cls.ROLE_TYPE_VISIBILITY.get(clean_role)
        if allowed_types is None:
            allowed_types = set(FactType.values())

        # Filter out deprecated facts and types outside role visibility
        return [f for f in facts if f.type in allowed_types and f.status != FactStatus.DEPRECATED.value]


class KnowledgeSelector:
    """Explicit selection abstraction prioritizing relevant facts for the current run/stage."""

    @classmethod
    def score_fact(
        cls,
        fact: KnowledgeFact,
        role_name: str,
        task: str = "",
        changed_files: Optional[List[str]] = None,
    ) -> int:
        score = 0
        changed_set = set(changed_files or [])
        task_words = set(task.lower().split()) if task else set()

        # 1. Unresolved anomalies & active disputes have highest operational priority
        if fact.is_disputed or fact.type == FactType.UNRESOLVED.value:
            score += 1000

        # 2. Human-locked facts represent immutable rules/ADRs
        if fact.is_locked:
            score += 800

        # 3. Direct file matching against touched files
        if changed_set and fact.evidence:
            for ev in fact.evidence:
                if ev in changed_set or any(cf.startswith(ev) or ev.startswith(cf) for cf in changed_set):
                    score += 500
                    break

        # 4. Keyword relevance to task
        fact_tokens = set(fact.id.replace("-", " ").lower().split())
        fact_tokens.update(fact.title.lower().split())
        overlap = task_words.intersection(fact_tokens)
        score += len(overlap) * 150

        # 5. Role alignment
        clean_role = role_name.lower().strip()
        if clean_role == "tester" and fact.type == FactType.FEATURE.value:
            score += 300
        elif clean_role == "reviewer" and fact.type == FactType.DECISION.value:
            score += 300
        elif clean_role == "architect" and fact.type == FactType.ARCHITECTURE.value:
            score += 300

        # 6. Status baseline
        if fact.is_verified:
            score += 200
        else:
            score += 100

        return score

    @classmethod
    def select(
        cls,
        projected_facts: List[KnowledgeFact],
        role_name: str,
        task: str = "",
        changed_files: Optional[List[str]] = None,
    ) -> List[KnowledgeFact]:
        scored_facts = [
            (f, cls.score_fact(f, role_name, task=task, changed_files=changed_files))
            for f in projected_facts
        ]
        # Sort descending by score
        scored_facts.sort(key=lambda x: x[1], reverse=True)
        return [f for f, _ in scored_facts]


class KnowledgeBudgeter:
    """Renders selected facts to markdown while strictly enforcing character/token budgets."""

    @classmethod
    def render_fact_line(cls, fact: KnowledgeFact) -> str:
        status_tag = f"[{fact.status}]"
        if fact.is_locked:
            status_tag = "[LOCKED]"
        elif fact.is_disputed:
            status_tag = "⚠️ [DISPUTED]"

        src_tag = f" via {fact.source}" if fact.source else ""
        title_str = f"**{status_tag} {fact.title}** (`{fact.id}`{src_tag})"
        summary_str = f": {fact.summary}" if fact.summary else ""
        evidence_str = f" (Evidence: {', '.join(fact.evidence)})" if fact.evidence else ""

        line = f"- {title_str}{summary_str}{evidence_str}"

        # If disputed, include dispute notes
        if fact.disputes:
            latest_disp = fact.disputes[-1]
            claim = latest_disp.get("claim", "")
            if claim:
                line += f"\n  - *Dispute ({latest_disp.get('role', 'verifier')}): {claim}*"

        return line

    @classmethod
    def apply_budget(
        cls,
        selected_facts: List[KnowledgeFact],
        max_chars: int = 20000,
    ) -> str:
        if not selected_facts:
            return ""

        # Group selected facts by type
        by_type: Dict[str, List[KnowledgeFact]] = {
            FactType.ARCHITECTURE.value: [],
            FactType.FEATURE.value: [],
            FactType.DECISION.value: [],
            FactType.UNRESOLVED.value: [],
        }
        for f in selected_facts:
            if f.type in by_type:
                by_type[f.type].append(f)
            else:
                by_type[FactType.ARCHITECTURE.value].append(f)

        headers = [
            (FactType.ARCHITECTURE.value, "### Architecture & Subsystems"),
            (FactType.FEATURE.value, "### Verified Features & Capabilities"),
            (FactType.DECISION.value, "### Decisions & Architectural Invariants"),
            (FactType.UNRESOLVED.value, "### Unresolved Debt, Defects & Disputes"),
        ]

        rendered_sections: List[str] = []
        current_chars = len("## PROJECT KNOWLEDGE BASE\n\n")

        for ftype, title in headers:
            type_facts = by_type.get(ftype, [])
            if not type_facts:
                continue

            section_lines = [title]
            for fact in type_facts:
                fact_line = cls.render_fact_line(fact)
                projected_len = current_chars + len(fact_line) + 2
                if projected_len > max_chars:
                    section_lines.append("- [... Additional knowledge facts omitted to respect transport budget ...]")
                    break
                section_lines.append(fact_line)
                current_chars += len(fact_line) + 1

            rendered_sections.append("\n".join(section_lines))
            if current_chars >= max_chars:
                break

        if not rendered_sections:
            return ""

        return "## PROJECT KNOWLEDGE BASE\n\n" + "\n\n".join(rendered_sections)


class KnowledgeProjector:
    """Unified entry point orchestrating the 5-step knowledge projection pipeline."""

    @classmethod
    def project_context(
        cls,
        store: KnowledgeStore,
        role_name: str,
        task: str = "",
        changed_files: Optional[List[str]] = None,
        max_chars: int = 20000,
    ) -> str:
        # Step 1: Knowledge Store
        all_facts = list(store.load_all().values())
        if not all_facts:
            return ""

        # Step 2: Knowledge Projection
        projected = KnowledgeProjection.project(all_facts, role_name=role_name)
        if not projected:
            return ""

        # Step 3: Knowledge Selection
        selected = KnowledgeSelector.select(
            projected,
            role_name=role_name,
            task=task,
            changed_files=changed_files,
        )

        # Step 4 & 5: Token Budget & Prompt Injection Formatting
        return KnowledgeBudgeter.apply_budget(selected, max_chars=max_chars)

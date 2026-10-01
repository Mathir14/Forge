"""Storage layer for the Project Knowledge Base (PKB).

Persists KnowledgeFact entities into canonical YAML projections under .forge/knowledge/:
- architecture.yaml
- features.yaml
- decisions.yaml
- unresolved.yaml
"""

import logging
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Optional, Union
import yaml

from forge.core.knowledge import FactType, KnowledgeFact


class KnowledgeStore:
    """Repository-local store managing PKB YAML projections."""

    FILE_TYPE_MAP: Dict[str, str] = {
        "architecture.yaml": FactType.ARCHITECTURE.value,
        "features.yaml": FactType.FEATURE.value,
        "decisions.yaml": FactType.DECISION.value,
        "unresolved.yaml": FactType.UNRESOLVED.value,
    }

    TYPE_FILE_MAP: Dict[str, str] = {v: k for k, v in FILE_TYPE_MAP.items()}

    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
        self.forge_dir = self.project_root / ".forge"
        self.knowledge_dir = self.forge_dir / "knowledge"

    def ensure_dirs(self) -> None:
        """Ensure .forge/knowledge directory exists."""
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def _file_path_for_type(self, fact_type: str) -> Path:
        clean_type = str(fact_type).lower().strip()
        filename = self.TYPE_FILE_MAP.get(clean_type, f"{clean_type}.yaml")
        return self.knowledge_dir / filename

    def load_all(self) -> Dict[str, KnowledgeFact]:
        """Load all facts across all canonical files, indexed by fact ID."""
        facts: Dict[str, KnowledgeFact] = {}
        if not self.knowledge_dir.exists():
            return facts

        for filename, default_type in self.FILE_TYPE_MAP.items():
            file_path = self.knowledge_dir / filename
            if not file_path.exists():
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = yaml.safe_load(f)
                if not content:
                    continue

                raw_list: List[Any] = []
                if isinstance(content, dict):
                    raw_list = content.get("facts", [])
                elif isinstance(content, list):
                    raw_list = content

                for item in raw_list:
                    if not isinstance(item, dict):
                        continue
                    if "type" not in item:
                        item["type"] = default_type
                    try:
                        fact = KnowledgeFact.from_dict(item)
                        facts[fact.id] = fact
                    except Exception as e:
                        logging.warning("Skipping invalid fact in %s: %s", file_path, e)
            except Exception as e:
                logging.warning("Failed to parse knowledge file %s: %s", file_path, e)

        return facts

    def load_by_type(self, fact_type: str) -> List[KnowledgeFact]:
        """Load facts matching a specific type."""
        all_facts = self.load_all()
        clean_type = str(fact_type).lower().strip()
        return [f for f in all_facts.values() if f.type == clean_type]

    def get_fact(self, fact_id: str) -> Optional[KnowledgeFact]:
        """Retrieve a single fact by ID."""
        return self.load_all().get(fact_id)

    def save_fact(self, fact: KnowledgeFact) -> None:
        """Save or update a single fact, preserving existing facts."""
        facts = self.load_all()
        facts[fact.id] = fact
        self.save_all(facts)

    def delete_fact(self, fact_id: str) -> bool:
        """Delete a fact by ID. Returns True if deleted, False if not found."""
        facts = self.load_all()
        if fact_id not in facts:
            return False
        del facts[fact_id]
        self.save_all(facts)
        return True

    def save_all(self, facts: Union[List[KnowledgeFact], Dict[str, KnowledgeFact]]) -> None:
        """Atomically persist facts into canonical YAML projections."""
        self.ensure_dirs()
        fact_list: List[KnowledgeFact] = list(facts.values()) if isinstance(facts, dict) else list(facts)

        # Group facts by projection type
        grouped: Dict[str, List[KnowledgeFact]] = {t: [] for t in self.TYPE_FILE_MAP}
        for f in fact_list:
            t = f.type.lower()
            if t not in grouped:
                grouped[t] = []
            grouped[t].append(f)

        # Write each canonical file atomically
        for fact_type, filename in self.TYPE_FILE_MAP.items():
            target_path = self.knowledge_dir / filename
            type_facts = grouped.get(fact_type, [])
            # Sort facts deterministically by ID for reproducible Git diffs
            type_facts.sort(key=lambda x: x.id)

            payload = {
                "schema_version": "1.0",
                "facts": [f.to_dict() for f in type_facts],
            }

            self._atomic_yaml_write(target_path, payload)

    def _atomic_yaml_write(self, target_path: Path, data: Dict[str, Any]) -> None:
        """Write YAML data atomically using a temp file swap with fsync."""
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(self.knowledge_dir),
                delete=False,
                encoding="utf-8",
                suffix=".tmp",
            ) as f:
                temp_file = Path(f.name)
                yaml.safe_dump(
                    data,
                    f,
                    sort_keys=False,
                    indent=2,
                    allow_unicode=True,
                    default_flow_style=False,
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(str(temp_file), str(target_path))
            temp_file = None
        finally:
            if temp_file and temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass

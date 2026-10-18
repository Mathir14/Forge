"""Test Fixture Management for Forge Tester.

Provides deterministic, reproducible test-data discovery, generic suitability
validation, and setup for black-box journeys without modifying target application
production code.
"""

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FixtureSuitability:
    """Detailed suitability analysis of a tabular fixture for ML/data workflows."""
    __test__ = False

    is_suitable: bool = True
    row_count: int = 0
    column_count: int = 0
    columns: List[str] = field(default_factory=list)
    candidate_targets: List[str] = field(default_factory=list)
    recommended_target: Optional[str] = None
    target_type: str = "unknown"  # "classification", "regression", "unknown"
    class_distribution: Dict[str, int] = field(default_factory=dict)
    num_classes: int = 0
    min_class_samples: int = 0
    missing_values_count: int = 0
    is_split_compatible: bool = True
    min_samples_required_for_default_split: int = 20
    unsuitability_reason: Optional[str] = None
    adaptive_split_proportions: Optional[Dict[str, float]] = None
    suitability_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_suitable": self.is_suitable,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "columns": self.columns,
            "candidate_targets": self.candidate_targets,
            "recommended_target": self.recommended_target,
            "target_type": self.target_type,
            "class_distribution": self.class_distribution,
            "num_classes": self.num_classes,
            "min_class_samples": self.min_class_samples,
            "missing_values_count": self.missing_values_count,
            "is_split_compatible": self.is_split_compatible,
            "min_samples_required_for_default_split": self.min_samples_required_for_default_split,
            "unsuitability_reason": self.unsuitability_reason,
            "adaptive_split_proportions": self.adaptive_split_proportions,
            "suitability_score": self.suitability_score,
        }


def analyze_fixture_suitability(path: Path) -> FixtureSuitability:
    """Inspect and evaluate the suitability of a tabular file generically.

    Examines:
    - Row and column counts
    - Missing / null values
    - Candidate target columns and class frequencies
    - Minimum sample requirements for stratified train/validation/test splitting
    - Whether default split proportions (e.g. 20% test, 10% val) or adaptive proportions are needed
    """
    suitability = FixtureSuitability()
    if not path.is_file():
        suitability.is_suitable = False
        suitability.unsuitability_reason = f"Fixture file not found: {path}"
        return suitability

    suffix = path.suffix.lower()
    if suffix not in (".csv", ".tsv", ".txt"):
        # Non-delimited or binary format - basic metadata
        suitability.is_suitable = True
        suitability.suitability_score = 50.0
        return suitability

    delimiter = "\t" if suffix == ".tsv" else ","
    rows: List[List[str]] = []
    header: List[str] = []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f, delimiter=delimiter)
            header_row = next(reader, None)
            if not header_row:
                suitability.is_suitable = False
                suitability.unsuitability_reason = "File is empty or contains no header."
                return suitability

            header = [c.strip().strip('"').strip("'") for c in header_row if c.strip()]
            for r in reader:
                if r and any(cell.strip() for cell in r):
                    rows.append([c.strip() for c in r])
    except Exception as e:
        suitability.is_suitable = False
        suitability.unsuitability_reason = f"Failed to parse tabular file: {e}"
        return suitability

    suitability.row_count = len(rows)
    suitability.column_count = len(header)
    suitability.columns = header

    if suitability.row_count == 0:
        suitability.is_suitable = False
        suitability.unsuitability_reason = "Dataset contains header but 0 data rows."
        return suitability

    # 1. Missing values and column profiles
    null_tokens = {"", "na", "nan", "null", "none", "?", "n/a"}
    col_values: Dict[str, List[str]] = {col: [] for col in header}
    missing_count = 0

    for r in rows:
        for idx, col in enumerate(header):
            val = r[idx] if idx < len(r) else ""
            if val.lower() in null_tokens:
                missing_count += 1
            else:
                col_values[col].append(val)

    suitability.missing_values_count = missing_count

    # 2. Candidate target identification
    # Heuristics:
    # - Name contains target/label/class/outcome/y/status/category/churn/default/ground_truth
    # - Cardinality between 2 and 20 unique values
    target_pattern = re.compile(
        r"(?i)\b(target|label|class|y|status|outcome|result|ground_truth|category|churn|default|prediction)\b"
    )
    col_scores: List[Tuple[str, float, int, Dict[str, int]]] = []

    for col in header:
        vals = col_values[col]
        unique_vals = set(vals)
        num_unique = len(unique_vals)
        score = 0.0

        if target_pattern.search(col):
            score += 50.0
            if col.lower() in ("target", "label", "class", "ground_truth"):
                score += 30.0

        if 2 <= num_unique <= 20:
            score += 30.0
            if num_unique == 2:
                score += 20.0  # Binary classification preferred
        elif num_unique > 20:
            score -= 30.0  # High cardinality or continuous float

        # Check if values are discrete (int or category) vs continuous float
        is_float = False
        if num_unique > 5:
            try:
                for v in vals[:20]:
                    float(v)
                is_float = True
            except ValueError:
                is_float = False
            if is_float and num_unique > 20:
                score -= 30.0

        counts: Dict[str, int] = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1

        col_scores.append((col, score, num_unique, counts))

    col_scores.sort(key=lambda x: x[1], reverse=True)
    candidate_targets = [c[0] for c in col_scores if c[1] >= 20.0]
    suitability.candidate_targets = candidate_targets

    recommended_col, best_score, num_unique, class_counts = (
        col_scores[0] if col_scores else ("", 0.0, 0, {})
    )
    suitability.recommended_target = recommended_col
    suitability.class_distribution = class_counts
    suitability.num_classes = num_unique
    min_class_samples = min(class_counts.values()) if class_counts else 0
    suitability.min_class_samples = min_class_samples

    if 2 <= num_unique <= 20:
        suitability.target_type = "classification"
    else:
        suitability.target_type = "regression" if num_unique > 20 else "unknown"

    # 3. Stratified split feasibility
    # Default split standard: test_size = 0.20, val_size = 0.10.
    # Stratified validation split requires validation set samples >= num_classes,
    # meaning N * val_size >= num_classes -> N * 0.10 >= k -> N >= 10*k.
    # For k=2, N >= 20. Each class also needs at least 2 samples (min_class_samples >= 2).
    k = max(2, suitability.num_classes)
    min_required = max(20, k * 10)
    suitability.min_samples_required_for_default_split = min_required

    # Check default split feasibility
    default_test = round(suitability.row_count * 0.20)
    default_temp = suitability.row_count - default_test
    default_val = (
        round(default_temp * (0.10 / 0.80)) if default_temp > 0 else 0
    )

    if (
        suitability.row_count < min_required
        or min_class_samples < 2
        or default_val < k
    ):
        suitability.is_split_compatible = False
        suitability.unsuitability_reason = (
            f"Insufficient samples ({suitability.row_count} rows, {suitability.num_classes} classes, "
            f"min class count {min_class_samples}) for default train/val split (20% test, 10% val). "
            f"Requires at least {min_required} rows."
        )

        # Check if adaptive proportions can accommodate the sample counts
        for t in [0.25, 0.30, 0.20, 0.15]:
            n_test = round(suitability.row_count * t)
            n_temp = suitability.row_count - n_test
            if n_test >= k and n_temp > 0:
                for v in [0.20, 0.15, 0.10, 0.05]:
                    v_adj = v / (1.0 - t)
                    n_val = round(n_temp * v_adj)
                    n_train = n_temp - n_val
                    if n_val >= k and n_train >= k and min_class_samples >= 2:
                        suitability.adaptive_split_proportions = {
                            "test_size": t,
                            "val_size": v,
                        }
                        break
            if suitability.adaptive_split_proportions:
                break
    else:
        suitability.is_split_compatible = True

    # 4. Suitability Scoring
    # Fully suitable: split compatible, adequate sample count (>= 30), balanced classes, no missing values
    if suitability.is_split_compatible:
        if suitability.row_count >= 30 and min_class_samples >= 5 and missing_count == 0:
            suitability.suitability_score = 100.0
            suitability.is_suitable = True
        elif suitability.row_count >= 20 and min_class_samples >= 2:
            suitability.suitability_score = 80.0
            suitability.is_suitable = True
        else:
            suitability.suitability_score = 70.0
            suitability.is_suitable = True
    elif suitability.adaptive_split_proportions:
        # Feasible only if application sliders are adjusted
        suitability.suitability_score = 45.0
        suitability.is_suitable = False
    else:
        # Impossible to split stratify
        suitability.suitability_score = 10.0
        suitability.is_suitable = False

    return suitability


@dataclass
class TestFixture:
    """Represents a test fixture file available for test runs."""
    __test__ = False

    name: str
    path: Path
    description: str = ""
    target_component: Optional[str] = None
    file_type: str = ""
    size_bytes: int = 0
    suitability: Optional[FixtureSuitability] = None
    is_temporary: bool = False
    is_edge_case: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "target_component": self.target_component,
            "file_type": self.file_type,
            "size_bytes": self.size_bytes,
            "suitability": self.suitability.to_dict() if self.suitability else None,
            "is_temporary": self.is_temporary,
            "is_edge_case": self.is_edge_case,
        }


class TestingFixtureManager:
    """Discovers, validates, and manages test fixtures for user journeys."""
    __test__ = False

    SUPPORTED_EXTENSIONS = {".csv", ".json", ".parquet", ".txt", ".tsv", ".xlsx"}

    def __init__(self, project_root: Path, config: Optional[Any] = None):
        self.project_root = Path(project_root).resolve()
        self.config = config
        self._fixtures: Dict[str, TestFixture] = {}
        self.temporary_fixtures: List[Path] = []
        self.discover_fixtures()

    def discover_fixtures(self) -> List[TestFixture]:
        """Discover fixtures from configuration and standard project locations."""
        self._fixtures.clear()

        # 1. Check explicit configuration in forge.yaml
        cfg_fixtures = self._load_fixtures_from_config()
        for fix in cfg_fixtures:
            self._evaluate_and_store_fixture(fix)

        # 2. Check standard directory locations if not explicitly configured or to augment
        standard_dirs = [
            self.project_root / ".forge" / "fixtures",
            self.project_root / "tests" / "fixtures",
            self.project_root / "fixtures",
        ]

        for sdir in standard_dirs:
            if sdir.is_dir():
                for p in sdir.glob("*"):
                    if p.is_file() and p.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                        if p.name not in self._fixtures and str(p) not in self._fixtures:
                            fixture = TestFixture(
                                name=p.name,
                                path=p,
                                description=f"Project fixture '{p.name}' in {sdir.name}",
                                file_type=p.suffix.lstrip(".").lower(),
                                size_bytes=p.stat().st_size,
                            )
                            self._evaluate_and_store_fixture(fixture)

        return self.get_all_fixtures()

    def _evaluate_and_store_fixture(self, fixture: TestFixture) -> None:
        """Analyze suitability and index fixture."""
        if fixture.file_type in ("csv", "tsv", "txt"):
            fixture.suitability = analyze_fixture_suitability(fixture.path)
        self._fixtures[fixture.name] = fixture
        self._fixtures[str(fixture.path)] = fixture

    def _load_fixtures_from_config(self) -> List[TestFixture]:
        """Load fixture definitions from context/stage configuration if available."""
        if not self.config:
            return []

        fixtures_data = None
        # Check config object structures
        if hasattr(self.config, "testing") and hasattr(self.config.testing, "fixtures"):
            fixtures_data = getattr(self.config.testing, "fixtures")
        elif hasattr(self.config, "get_stage_config"):
            stage_cfg = self.config.get_stage_config("tester", phase="pre_run")
            if stage_cfg and hasattr(stage_cfg, "fixtures"):
                fixtures_data = stage_cfg.fixtures

        if not fixtures_data and isinstance(self.config, dict):
            fixtures_data = (
                self.config.get("testing", {}).get("fixtures")
                or self.config.get("stages", {}).get("tester", {}).get("fixtures")
            )

        if not fixtures_data or not isinstance(fixtures_data, list):
            return []

        results = []
        for item in fixtures_data:
            if isinstance(item, dict):
                name = item.get("name", "")
                rel_path = item.get("path", "")
                desc = item.get("description", "")
                target = item.get("target")
            elif isinstance(item, str):
                rel_path = item
                name = Path(item).name
                desc = f"Configured fixture '{name}'"
                target = None
            else:
                continue

            full_path = (self.project_root / rel_path).resolve()
            if full_path.is_file():
                fix = TestFixture(
                    name=name or full_path.name,
                    path=full_path,
                    description=desc,
                    target_component=target,
                    file_type=full_path.suffix.lstrip(".").lower(),
                    size_bytes=full_path.stat().st_size,
                )
                results.append(fix)
            else:
                logger.warning("Configured fixture file does not exist: %s", full_path)

        return results

    def get_all_fixtures(self) -> List[TestFixture]:
        """Return unique list of discovered fixtures."""
        unique: Dict[str, TestFixture] = {}
        for fix in self._fixtures.values():
            unique[str(fix.path)] = fix
        return list(unique.values())

    def get_suitable_fixtures(self, workflow: str = "classification") -> List[TestFixture]:
        """Return discovered fixtures that satisfy suitability requirements for workflow."""
        all_fixtures = self.get_all_fixtures()
        suitable = [f for f in all_fixtures if f.suitability and f.suitability.is_suitable]
        suitable.sort(
            key=lambda f: f.suitability.suitability_score if f.suitability else 0.0,
            reverse=True,
        )
        return suitable

    def get_best_fixture(self, workflow: str = "classification") -> Optional[TestFixture]:
        """Return highest-scoring suitable fixture, or best available fallback."""
        suitable = self.get_suitable_fixtures(workflow=workflow)
        if suitable:
            return suitable[0]

        all_fixtures = self.get_all_fixtures()
        if all_fixtures:
            all_fixtures.sort(
                key=lambda f: f.suitability.suitability_score if f.suitability else 0.0,
                reverse=True,
            )
            return all_fixtures[0]
        return None

    def has_suitable_fixture(self, workflow: str = "classification") -> bool:
        """Return True if at least one discovered fixture is suitable."""
        return len(self.get_suitable_fixtures(workflow=workflow)) > 0

    def get_fixture(self, name_or_path: str) -> Optional[TestFixture]:
        """Lookup fixture by name, path string, or filename."""
        if name_or_path in self._fixtures:
            return self._fixtures[name_or_path]

        base = Path(name_or_path).name
        if base in self._fixtures:
            return self._fixtures[base]

        try:
            resolved = (self.project_root / name_or_path).resolve()
            if str(resolved) in self._fixtures:
                return self._fixtures[str(resolved)]
            if resolved.is_file():
                fix = TestFixture(
                    name=resolved.name,
                    path=resolved,
                    description=f"File fixture {resolved.name}",
                    file_type=resolved.suffix.lstrip(".").lower(),
                    size_bytes=resolved.stat().st_size,
                )
                self._evaluate_and_store_fixture(fix)
                return fix
        except Exception:
            pass

        return None

    def resolve_path(self, name_or_path: str) -> Optional[Path]:
        """Resolve fixture name or path to an absolute Path if existing."""
        fix = self.get_fixture(name_or_path)
        if fix and fix.path.is_file():
            return fix.path

        p = Path(name_or_path)
        if not p.is_absolute():
            p = (self.project_root / p).resolve()
        if p.is_file():
            return p

        return None

    def get_fixtures_prompt_summary(self) -> str:
        """Format discovered fixtures with suitability tags for AI Journey Planner prompt."""
        fixtures = self.get_all_fixtures()
        if not fixtures:
            return "No pre-configured test fixtures discovered."

        # Sort so suitable fixtures appear first
        fixtures.sort(
            key=lambda f: f.suitability.suitability_score if f.suitability else 0.0,
            reverse=True,
        )

        lines = [
            "Available Test Fixtures (Use ActionType: UPLOAD_FILE to populate data workflows):"
        ]
        best_fixture = self.get_best_fixture()
        if best_fixture and best_fixture.suitability and best_fixture.suitability.is_suitable:
            target_str = best_fixture.suitability.recommended_target or "target"
            lines.append(
                f"🌟 RECOMMENDED BEST FIXTURE: '{best_fixture.name}' (Recommended target column: '{target_str}')"
            )

        for fix in fixtures:
            rel = (
                str(fix.path.relative_to(self.project_root))
                if fix.path.is_relative_to(self.project_root)
                else str(fix.path)
            )
            suit = fix.suitability
            tag = "[RECOMMENDED: SUITABLE]" if (suit and suit.is_suitable) else "[UNSUITABLE]"
            if suit and not suit.is_suitable:
                tag = f"[UNSUITABLE: {suit.unsuitability_reason}]"

            target_str = f" | Target hint: {fix.target_component}" if fix.target_component else ""
            lines.append(
                f"- {tag} Name: {fix.name} | Path: {rel} | Type: {fix.file_type} ({fix.size_bytes} bytes){target_str}"
            )
            if fix.description:
                lines.append(f"  Description: {fix.description}")

            if suit and suit.columns:
                lines.append(f"  Columns: {', '.join(suit.columns[:25])}")
                if suit.recommended_target:
                    lines.append(
                        f"  Target Recommendation: '{suit.recommended_target}' "
                        f"({suit.num_classes} classes, target type: {suit.target_type})"
                    )
                if suit.is_split_compatible:
                    lines.append(
                        f"  Split Compatibility: Fully compatible with default train/val split ({suit.row_count} rows >= {suit.min_samples_required_for_default_split} required)"
                    )
                elif suit.adaptive_split_proportions:
                    props = suit.adaptive_split_proportions
                    lines.append(
                        f"  Split Compatibility: WARNING - requires adaptive proportions (test={props.get('test_size')}, val={props.get('val_size')}) due to small row count ({suit.row_count} rows)"
                    )
                else:
                    lines.append(
                        f"  Split Compatibility: INCOMPATIBLE - {suit.unsuitability_reason}"
                    )

        return "\n".join(lines)

    def generate_edge_case_fixtures(
        self,
        target_dir: Optional[Path] = None,
        edge_case_requirements: Optional[List[Any]] = None,
    ) -> List[TestFixture]:
        """Generate test-scoped edge-case fixtures based on diff/domain requirements."""
        if target_dir is None:
            target_dir = self.project_root / ".forge" / "tmp_fixtures"
        target_dir = Path(target_dir).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)

        generated_fixtures: List[TestFixture] = []

        # 1. Missing / NaN values fixture
        nan_file = target_dir / "edge_case_missing_values.csv"
        nan_content = (
            "feature_a,feature_b,feature_c,target\n"
            "1.2,0.5,NaN,0\n"
            "2.3,NaN,1.8,1\n"
            "NaN,3.1,2.0,0\n"
            "4.0,2.5,3.2,1\n"
            "1.5,1.1,NaN,0\n"
            "2.8,NaN,2.9,1\n"
            "NaN,1.9,3.5,0\n"
            "3.7,2.2,4.1,1\n"
            "1.9,0.8,NaN,0\n"
            "2.1,NaN,1.5,1\n"
            "NaN,2.7,2.8,0\n"
            "3.4,1.6,3.9,1\n"
            "2.0,0.9,NaN,0\n"
            "2.5,NaN,2.2,1\n"
            "NaN,3.0,3.1,0\n"
            "4.1,2.1,4.0,1\n"
            "1.8,1.2,NaN,0\n"
            "2.9,NaN,2.6,1\n"
            "NaN,1.8,3.4,0\n"
            "3.6,2.4,4.2,1\n"
        )
        try:
            nan_file.write_text(nan_content, encoding="utf-8")
            self.temporary_fixtures.append(nan_file)
            fix_nan = TestFixture(
                name=nan_file.name,
                path=nan_file,
                description="Edge-case fixture containing missing/NaN values in feature columns.",
                target_component="data_module",
                file_type="csv",
                size_bytes=nan_file.stat().st_size,
                is_temporary=True,
                is_edge_case=True,
            )
            self._evaluate_and_store_fixture(fix_nan)
            generated_fixtures.append(fix_nan)
        except Exception as e:
            logger.warning("Failed creating nan fixture: %s", e)

        # 2. Boundary split dataset (insufficient sample size for default stratified split)
        boundary_file = target_dir / "edge_case_boundary_split.csv"
        boundary_content = (
            "feature_x,feature_y,target\n"
            "0.1,0.2,0\n"
            "0.3,0.4,0\n"
            "0.5,0.6,0\n"
            "0.7,0.8,0\n"
            "0.9,1.0,0\n"
            "1.1,1.2,1\n"
        )
        try:
            boundary_file.write_text(boundary_content, encoding="utf-8")
            self.temporary_fixtures.append(boundary_file)
            fix_bound = TestFixture(
                name=boundary_file.name,
                path=boundary_file,
                description="Edge-case boundary fixture with insufficient samples (6 rows) for default 10% stratified split.",
                target_component="data_module",
                file_type="csv",
                size_bytes=boundary_file.stat().st_size,
                is_temporary=True,
                is_edge_case=True,
            )
            self._evaluate_and_store_fixture(fix_bound)
            generated_fixtures.append(fix_bound)
        except Exception as e:
            logger.warning("Failed creating boundary fixture: %s", e)

        # 3. Empty dataset fixture
        empty_file = target_dir / "edge_case_empty.csv"
        empty_content = "feature_a,feature_b,target\n"
        try:
            empty_file.write_text(empty_content, encoding="utf-8")
            self.temporary_fixtures.append(empty_file)
            fix_empty = TestFixture(
                name=empty_file.name,
                path=empty_file,
                description="Edge-case fixture with header but 0 data rows.",
                target_component="data_module",
                file_type="csv",
                size_bytes=empty_file.stat().st_size,
                is_temporary=True,
                is_edge_case=True,
            )
            self._evaluate_and_store_fixture(fix_empty)
            generated_fixtures.append(fix_empty)
        except Exception as e:
            logger.warning("Failed creating empty fixture: %s", e)

        return generated_fixtures

    def cleanup_temporary_fixtures(self) -> None:
        """Deterministically remove any generated temporary test fixtures."""
        dirs_to_clean: Set[Path] = set()
        for p in list(self.temporary_fixtures):
            try:
                if p.is_file():
                    p.unlink()
                dirs_to_clean.add(p.parent)
                self._fixtures.pop(p.name, None)
                self._fixtures.pop(str(p), None)
            except Exception as e:
                logger.warning("Failed deleting temporary fixture %s: %s", p, e)
        self.temporary_fixtures.clear()

        for d in dirs_to_clean:
            try:
                if d.is_dir() and "tmp_fixtures" in d.name:
                    if not any(d.iterdir()):
                        d.rmdir()
            except Exception:
                pass


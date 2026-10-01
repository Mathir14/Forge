"""Instruction data container passed to the prompt compiler."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Instruction:
    role_name: str
    task: str
    project_docs: Dict[str, str] = field(default_factory=dict)
    previous_stage_outputs: Dict[str, str] = field(default_factory=dict)
    git_diff: Optional[str] = None
    changed_files: List[str] = field(default_factory=list)
    mixed_files: List[str] = field(default_factory=list)
    git_status: Optional[str] = None
    changed_file_summary: Optional[str] = None
    executor_report: Optional[str] = None
    tester_report: Optional[str] = None
    tester_machine_report: Optional[str] = None
    protocol_schema: str = ""
    repair_feedback: Optional[str] = None
    knowledge_context: str = ""


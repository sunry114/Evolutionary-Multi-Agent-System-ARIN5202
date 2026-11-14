"""Limits shared by planning, execution and tool authorization."""

from dataclasses import dataclass
from pathlib import PurePath
from typing import Sequence


CRITICAL_TOOLS = frozenset({"run_python", "delete_file", "git_push"})
DOCUMENT_SUFFIXES = frozenset({".md", ".txt"})


@dataclass(frozen=True)
class Limits:
    max_plan_subtasks: int = 5
    max_total_tasks: int = 12
    max_steps_per_task: int = 20
    max_delegation_depth: int = 2
    max_searches_per_task: int = 3


DEFAULT_LIMITS = Limits()


def validate_plan(subtasks: Sequence, limits: Limits = DEFAULT_LIMITS) -> None:
    """Require sequential IDs and dependencies on earlier subtasks only."""
    if not 1 <= len(subtasks) <= limits.max_plan_subtasks:
        raise ValueError("Plan must contain 1 to 5 subtasks")
    seen = set()
    for index, task in enumerate(subtasks, 1):
        if task.id != "s%d" % index or not task.description.strip():
            raise ValueError("Invalid subtask ID or description")
        if len(task.description) > 1000 or len(set(task.depends_on)) != len(task.depends_on):
            raise ValueError("Invalid subtask description or duplicate dependency")
        if any(dependency not in seen for dependency in task.depends_on):
            raise ValueError("Dependencies must reference earlier subtasks")
        seen.add(task.id)


def validate_execution_budget(total_tasks: int, steps: int, depth: int,
                              searches: int = 0, limits: Limits = DEFAULT_LIMITS) -> None:
    if not (0 <= total_tasks <= limits.max_total_tasks and
            0 <= steps <= limits.max_steps_per_task and
            0 <= depth <= limits.max_delegation_depth and
            0 <= searches <= limits.max_searches_per_task):
        raise ValueError("Execution budget exceeded")


def validate_document_path(path: str) -> None:
    if PurePath(path).suffix.lower() not in DOCUMENT_SUFFIXES:
        raise ValueError("Documents must be Markdown or plain text")

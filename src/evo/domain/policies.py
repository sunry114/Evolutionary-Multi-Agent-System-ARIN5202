"""Limits shared by planning, execution and tool authorization."""

from dataclasses import dataclass
from pathlib import PurePath
from typing import Sequence


CRITICAL_TOOLS = frozenset({"run_python", "delete_file", "git_push"})
DOCUMENT_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm", ".rst", ".xml",
                               ".json", ".csv", ".yaml", ".yml"})


def team_coordinator(agents):
    """One human contact per saved team; prefer an explicitly named lead."""
    agents = tuple(agents)
    if not agents:
        raise ValueError("A team needs at least one agent")
    for preferred in ("lead", "coordinator", "manager"):
        for agent in agents:
            if agent.name.casefold() == preferred:
                return agent.name
    return agents[0].name


@dataclass(frozen=True)
class Limits:
    max_plan_subtasks: int = 5
    max_total_tasks: int = 12
    max_steps_per_task: int = 20
    max_delegation_depth: int = 2
    max_searches_per_task: int = 3


DEFAULT_LIMITS = Limits()
MAX_SUBTASK_DESCRIPTION = 8000


class PlanValidationError(ValueError):
    """Safe validation details built locally, never from model response text."""

    code = "invalid_plan"

    def __init__(self, reason, message, **details):
        super().__init__(message)
        self.details = {"reason": reason, **details}


def validate_plan(subtasks: Sequence, limits: Limits = DEFAULT_LIMITS) -> None:
    """Require sequential IDs and dependencies on earlier subtasks only."""
    if not 1 <= len(subtasks) <= limits.max_plan_subtasks:
        raise PlanValidationError("subtask_count", "Plan must contain 1 to 5 subtasks",
                                  field="subtasks", actual=len(subtasks), limit=limits.max_plan_subtasks)
    seen = set()
    for index, task in enumerate(subtasks, 1):
        field = "subtasks[%d]" % (index - 1)
        if task.id != "s%d" % index:
            raise PlanValidationError("subtask_id", field + ".id must be s%d" % index,
                                      field=field + ".id")
        if not task.description.strip():
            raise PlanValidationError("empty_description", field + ".description must not be blank",
                                      field=field + ".description")
        if len(task.description) > MAX_SUBTASK_DESCRIPTION:
            raise PlanValidationError(
                "description_too_long",
                field + ".description has %d characters; maximum is %d"
                % (len(task.description), MAX_SUBTASK_DESCRIPTION),
                field=field + ".description", actual=len(task.description), limit=MAX_SUBTASK_DESCRIPTION)
        if len(set(task.depends_on)) != len(task.depends_on):
            raise PlanValidationError("duplicate_dependency", field + ".depends_on contains duplicates",
                                      field=field + ".depends_on")
        if any(dependency not in seen for dependency in task.depends_on):
            raise PlanValidationError("dependency_order", field + ".depends_on must reference earlier subtasks",
                                      field=field + ".depends_on")
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
        raise ValueError("Unsupported text document format")

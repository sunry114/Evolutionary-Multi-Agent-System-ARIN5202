"""Transport independent records for the application layer."""

from dataclasses import dataclass
from typing import Optional, Tuple

from .statuses import ReviewStatus, TaskStatus


@dataclass(frozen=True)
class Agent:
    name: str
    specialty: str


@dataclass(frozen=True)
class Subtask:
    id: str
    description: str
    skills: Tuple[str, ...] = ()
    depends_on: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Task:
    id: str
    description: str
    owner: Optional[str] = None
    parent_id: Optional[str] = None
    status: TaskStatus = TaskStatus.CREATED
    result: str = ""


@dataclass(frozen=True)
class Message:
    id: str
    kind: str
    sender: str
    content: str
    recipient: Optional[str] = None
    task_id: Optional[str] = None


@dataclass(frozen=True)
class Review:
    task_id: str
    status: ReviewStatus = ReviewStatus.PENDING
    feedback: str = ""
    revision_task_id: Optional[str] = None


@dataclass(frozen=True)
class Experience:
    agent: str
    task: str
    outcome: TaskStatus
    lesson: str
    evidence: str = ""
    failure_category: Optional[str] = None


@dataclass(frozen=True)
class Metric:
    task_id: str
    elapsed_seconds: float
    completed_subtasks: int
    total_subtasks: int
    objective_passed: Optional[bool] = None

"""Technical and human review states are separate."""

from enum import Enum


class TaskStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    CHANGES_REQUESTED = "changes_requested"
    REVISING = "revising"


TASK_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.PLANNING, TaskStatus.READY, TaskStatus.BLOCKED},
    TaskStatus.PLANNING: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.FAILED},
    TaskStatus.READY: {TaskStatus.ASSIGNED, TaskStatus.BLOCKED},
    TaskStatus.ASSIGNED: {TaskStatus.RUNNING, TaskStatus.BLOCKED},
    TaskStatus.RUNNING: {TaskStatus.WAITING_HUMAN, TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.WAITING_HUMAN: {TaskStatus.RUNNING, TaskStatus.BLOCKED},
    TaskStatus.SUCCESS: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.BLOCKED: set(),
}

REVIEW_TRANSITIONS = {
    ReviewStatus.PENDING: {ReviewStatus.ACCEPTED, ReviewStatus.CHANGES_REQUESTED},
    ReviewStatus.CHANGES_REQUESTED: {ReviewStatus.REVISING},
    ReviewStatus.REVISING: set(),
    ReviewStatus.ACCEPTED: set(),
}


def validate_task_transition(current, new):
    if TaskStatus(new) not in TASK_TRANSITIONS[TaskStatus(current)]:
        raise ValueError("Invalid task status transition")


def validate_review_transition(current, new):
    if ReviewStatus(new) not in REVIEW_TRANSITIONS[ReviewStatus(current)]:
        raise ValueError("Invalid review status transition")

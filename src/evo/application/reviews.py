"""Review decisions never alter technical execution status.

Repository supplied by B. Atomic operations must validate current state in the
same transaction as the update/link; the preflight read is not a lock.
"""
from typing import Protocol


class ReviewRepository(Protocol):
    def get_task(self, task_id: str) -> dict: ...
    def decide_review(self, task_id: str, *, expected: str, decision: str, feedback: str) -> dict: ...
    def link_revision(self, task_id: str, *, expected: str, prompt: str) -> dict:
        """Atomically create a new task and link it; a duplicate must conflict."""
        ...


class ReviewService:
    def __init__(self, repository):
        self.repository = repository

    def decide(self, task_id, decision, feedback=""):
        if decision not in ("accepted", "changes_requested"):
            raise ValueError("Invalid review decision")
        task = self.repository.get_task(task_id)
        if task["status"] != "success" or task["review_status"] != "pending":
            raise ValueError("Only successful tasks with pending review may be reviewed")
        if decision == "changes_requested" and not feedback.strip():
            raise ValueError("Changes requested requires feedback")
        return self.repository.decide_review(
            task_id, expected="pending", decision=decision, feedback=feedback.strip())

    def revise(self, task_id, prompt):
        task = self.repository.get_task(task_id)
        if task["review_status"] != "changes_requested":
            raise ValueError("Revision requires changes_requested")
        if not prompt.strip():
            raise ValueError("Revision prompt is required")
        return self.repository.link_revision(task_id, expected="changes_requested", prompt=prompt.strip())

"""Application service for human review and revision.

Technical execution status and human review status remain independent.
Repository implementations must atomically recheck the expected review
state in the same transaction as the update.
"""

from evo.ports.repositories import ReviewRepository


class ReviewService:
    def __init__(self, repository: ReviewRepository):
        self.repository = repository

    def decide(
        self,
        task_id: str,
        decision: str,
        feedback: str = "",
    ):
        if decision not in ("accepted", "changes_requested"):
            raise ValueError("Invalid review decision")

        task = self.repository.get_task(task_id)

        if (
            task["status"] != "success"
            or task["review_status"] != "pending"
        ):
            raise ValueError(
                "Only successful tasks with pending review may be reviewed"
            )

        feedback = feedback.strip()

        if decision == "changes_requested" and not feedback:
            raise ValueError("Changes requested requires feedback")

        return self.repository.decide_review(
            task_id,
            expected="pending",
            decision=decision,
            feedback=feedback,
        )

    def revise(self, task_id: str, prompt: str):
        task = self.repository.get_task(task_id)

        if task["review_status"] != "changes_requested":
            raise ValueError("Revision requires changes_requested")

        prompt = prompt.strip()

        if not prompt:
            raise ValueError("Revision prompt is required")

        return self.repository.link_revision(
            task_id,
            expected="changes_requested",
            prompt=prompt,
        )

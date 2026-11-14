"""Human clarification and critical action authorization boundary."""

from typing import Mapping, Optional, Protocol


class ApprovalPort(Protocol):
    def request_approval(self, task_id: str, agent: str, action: str,
                         payload: Mapping[str, object]) -> str:
        """Persist a pending request and return its ID; never assume approval."""
        ...

    def request_clarification(self, task_id: str, question: str) -> str:
        """Persist a pending question and return its ID."""
        ...

    def get_answer(self, request_id: str) -> Optional[Mapping[str, object]]:
        """Return None while unanswered; a denial is an explicit answer."""
        ...

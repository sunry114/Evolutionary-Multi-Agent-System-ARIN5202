"""Persistence contracts used by the application layer."""

from typing import Mapping, Protocol, Sequence

from evo.domain.models import Experience, Message, Task


Record = Mapping[str, object]


class TaskRepository(Protocol):
    def save_task(self, task: Task) -> None:
        """Create a task.

        A successful task receives a pending human review record.
        """
        ...

    def get_task(self, task_id: str) -> Record:
        """Return a task together with its optional review state."""
        ...

    def list_children(self, parent_id: str) -> Sequence[Record]:
        ...


class MessageRepository(Protocol):
    def save_message(self, message: Message) -> None:
        ...

    def list_direct_messages(
        self,
        human: str,
        agent: str,
    ) -> Sequence[Record]:
        ...

    def list_group_messages(self) -> Sequence[Record]:
        ...


class EventRepository(Protocol):
    def append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        actor: str = "",
        payload: Mapping[str, object] | None = None,
    ) -> int:
        ...

    def list_events(self, task_id: str) -> Sequence[Record]:
        ...


class ActionRepository(Protocol):
    def save_action(
        self,
        *,
        action_id: str,
        task_id: str,
        agent: str,
        tool_name: str,
        arguments: Mapping[str, object],
        status: str = "requested",
    ) -> None:
        ...


class ApprovalRepository(Protocol):
    def save_approval_request(
        self,
        *,
        request_id: str,
        task_id: str,
        agent: str,
        kind: str,
        action: str = "",
        payload: Mapping[str, object] | None = None,
    ) -> None:
        ...

    def get_approval_request(self, request_id: str) -> Record:
        ...


class ReviewRepository(Protocol):
    def get_task(self, task_id: str) -> Record:
        ...

    def decide_review(
        self,
        task_id: str,
        *,
        expected: str,
        decision: str,
        feedback: str,
    ) -> Record:
        ...

    def link_revision(
        self,
        task_id: str,
        *,
        expected: str,
        prompt: str,
    ) -> Record:
        ...


class MemoryRepository(Protocol):
    def recall(
        self,
        agent: str,
        query: str,
        limit: int = 3,
    ) -> Sequence[Experience]:
        ...

    def add(self, experience: Experience) -> None:
        ...

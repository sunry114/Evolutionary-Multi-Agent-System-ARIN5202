"""Persistence contracts for task and conversation state."""

from typing import Protocol, Sequence

from evo.domain.models import Experience, Message, Task


class TaskRepository(Protocol):
    def save_task(self, task: Task) -> None: ...

    def get_task(self, task_id: str) -> Task: ...

    def list_children(self, parent_id: str) -> Sequence[Task]: ...


class MessageRepository(Protocol):
    def save_message(self, message: Message) -> None: ...

    def list_direct_messages(self, human: str, agent: str) -> Sequence[Message]: ...

    def list_group_messages(self) -> Sequence[Message]: ...


class MemoryRepository(Protocol):
    def recall(self, agent: str, query: str, limit: int = 3) -> Sequence[Experience]: ...

    def add(self, experience: Experience) -> None: ...

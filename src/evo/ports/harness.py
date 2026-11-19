"""One-agent execution boundary. Harness success is not objective task success."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol


class HarnessError(RuntimeError):
    """Safe-to-report lifecycle, configuration or protocol failure."""


class HarnessCleanupError(HarnessError):
    """The runtime may still exist; quarantine the task until operator reconciliation."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid harness scope identifier")
    return value


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class RunRequest:
    task_id: str
    agent: str
    session_scope: str
    workspace: Path
    prompt: str
    role: str = ""
    memory: tuple[str, ...] = ()
    actions: tuple[ToolCall, ...] = ()  # Baseline's explicit single-action sequence.
    acceptance: Mapping[str, object] | None = None

    def __post_init__(self):
        for value in (self.task_id, self.agent, self.session_scope):
            identifier(value)
        object.__setattr__(self, "workspace", Path(self.workspace))
        if not self.workspace.is_absolute():
            raise ValueError("Harness workspace must be absolute")
        if (not isinstance(self.prompt, str) or not self.prompt.strip()
                or len(self.prompt.encode("utf-8")) > 32768
                or not isinstance(self.role, str) or len(self.role.encode("utf-8")) > 8192
                or not isinstance(self.memory, tuple) or len(self.memory) > 8
                or any(not isinstance(x, str) or len(x.encode("utf-8")) > 8192 for x in self.memory)
                or not isinstance(self.actions, tuple) or len(self.actions) > 20
                or any(not isinstance(x, ToolCall) for x in self.actions)):
            raise ValueError("Invalid or oversized harness context")
        if self.acceptance is not None and (not isinstance(self.acceptance, Mapping)
                or set(self.acceptance) - {"path", "purpose", "contains", "exact"}
                or not isinstance(self.acceptance.get("path"), str)):
            raise ValueError("Invalid harness acceptance check")


@dataclass(frozen=True)
class ResumeRef:
    run_id: str
    task_id: str
    agent: str
    session_scope: str


@dataclass(frozen=True)
class RunHandle:
    run_id: str


@dataclass(frozen=True)
class HarnessEvent:
    sequence: int
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)
    untrusted: bool = True


@dataclass(frozen=True)
class ArtifactRef:
    path: str  # Relative to the bound workspace, never a host path.
    purpose: str
    sha256: str


@dataclass(frozen=True)
class RunResult:
    status: str
    text: str = ""
    error: str = ""
    native_session_id: str = ""
    artifacts: tuple[ArtifactRef, ...] = ()
    resume_ref: ResumeRef | None = None
    exit_code: int | None = None
    resume_strategy: str = "context_restart"


class HarnessAdapter(Protocol):
    def capabilities(self) -> Mapping[str, object]: ...
    def start(self, request: RunRequest) -> RunHandle: ...
    def events(self, handle: RunHandle, after: int = 0) -> tuple[HarnessEvent, ...]: ...
    def wait(self, handle: RunHandle, timeout: float | None = None) -> RunResult | None: ...
    def cancel(self, handle: RunHandle) -> RunResult: ...
    def resume(self, reference: ResumeRef, request: RunRequest) -> RunHandle: ...

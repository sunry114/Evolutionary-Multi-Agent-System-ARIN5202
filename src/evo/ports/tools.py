"""Tool dispatch contract; implementations enforce isolation and path policy."""

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class Artifact:
    """Published snapshot metadata; contains no host filesystem paths."""

    id: str
    owner: str
    name: str
    purpose: str
    size: int
    sha256: str
    recipients: Tuple[str, ...]


class WorkspacePort(Protocol):
    """Bound to one agent by the trusted broker, never selected by an LLM."""

    agent: str

    def input_digest(self) -> str:
        """Hash bounded workspace inputs for execution approval scope."""
        ...

    def read_file(self, path: str, *, purpose: str = "document",
                  start_line: int = 1, max_lines: int | None = None) -> str:
        ...

    def edit_file(self, path: str, old_text: str, new_text: str, *, purpose: str = "document") -> None:
        ...

    def search_files(self, query: str, path: str = ".", max_results: int = 20) -> Mapping[str, object]:
        ...

    def check_python(self, path: str) -> Mapping[str, object]:
        ...

    def write_file(self, path: str, content: str, *, purpose: str = "document",
                   overwrite: bool = True, append: bool = False) -> None:
        ...

    def list_files(self, path: str = ".") -> Sequence[str]:
        ...

    def delete_file(self, path: str, *, purpose: str = "document",
                    approved: bool = False) -> None:
        """Approval is trusted application state, never a model-supplied flag."""
        ...


class ArtifactPort(Protocol):
    def publish(self, path: str, recipients: Sequence[str], *,
                purpose: str = "document") -> Artifact:
        ...

    def list_available(self) -> Sequence[Artifact]:
        ...

    def import_artifact(self, artifact_id: str, destination: str) -> Artifact:
        """Import a permitted snapshot as a new file; never overwrite."""
        ...


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    untrusted: bool = field(default=True, init=False)


@dataclass(frozen=True)
class WebPage:
    url: str
    text: str
    media_type: str
    untrusted: bool = field(default=True, init=False)


class WebPort(Protocol):
    """One trusted instance per task; retrieved content is never instructions."""

    def search(self, query: str) -> Sequence[SearchResult]:
        ...

    def fetch_url(self, url: str) -> WebPage:
        ...


class ToolPort(Protocol):
    def call(self, agent: str, name: str, arguments: Mapping[str, object]) -> object:
        """Execute one validated tool call for the named agent."""
        ...


class ToolError(RuntimeError):
    """Safe-to-report tool infrastructure or policy failure."""


class ToolCleanupError(ToolError):
    """A tool runtime may still exist; quarantine instead of retrying."""


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False


class PythonRunnerPort(Protocol):
    def run_python(self, path: str, *, approved: bool = False) -> ExecutionResult:
        """Approval must come from trusted application state, never model arguments."""
        ...


class GitPort(Protocol):
    def validate_push_target(self, repository: str, branch: str) -> None:
        ...

    def validate_commit(self, paths: Sequence[str], message: str) -> None:
        ...

    def init(self) -> ExecutionResult:
        ...

    def status(self) -> ExecutionResult:
        ...

    def head(self) -> str:
        ...

    def commit(self, paths: Sequence[str], message: str) -> ExecutionResult:
        ...

    def push(self, repository: str, branch: str, revision: str, *, approved: bool = False) -> ExecutionResult:
        ...

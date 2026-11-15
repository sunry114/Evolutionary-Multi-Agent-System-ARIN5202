"""Tool dispatch contract; implementations enforce isolation and path policy."""

from dataclasses import dataclass
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

    def read_file(self, path: str, *, purpose: str = "document") -> str:
        ...

    def write_file(self, path: str, content: str, *, purpose: str = "document",
                   overwrite: bool = True) -> None:
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


class ToolPort(Protocol):
    def call(self, agent: str, name: str, arguments: Mapping[str, object]) -> object:
        """Execute one validated tool call for the named agent."""
        ...

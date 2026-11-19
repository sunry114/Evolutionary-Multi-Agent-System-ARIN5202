"""Independent file evidence shared by orchestration and execution adapters."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactCheck:
    path: str
    purpose: str = "document"
    contains: str = ""
    exact: str | None = None

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or not isinstance(self.contains, str) or (
                self.exact is not None and not isinstance(self.exact, str)):
            raise ValueError("Artifact check needs a path and valid expected content")

    def verify(self, workspace, artifacts: tuple) -> bool:
        # Neither an old file alone nor a model claim proves delivery.
        artifact = next((item for item in artifacts
                         if item.path == self.path and item.purpose == self.purpose), None)
        if artifact is None:
            return False
        try:
            content = workspace.read_file(self.path, purpose=self.purpose)
        except (OSError, ValueError):
            return False
        return (hashlib.sha256(content.encode("utf-8")).hexdigest() == artifact.sha256
                and (self.exact is None or content == self.exact)
                and self.contains in content)

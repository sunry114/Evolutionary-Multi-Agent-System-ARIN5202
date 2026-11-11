"""Tool dispatch contract; implementations enforce isolation and path policy."""

from typing import Mapping, Protocol


class ToolPort(Protocol):
    def call(self, agent: str, name: str, arguments: Mapping[str, object]) -> object:
        """Execute one validated tool call for the named agent."""
        ...

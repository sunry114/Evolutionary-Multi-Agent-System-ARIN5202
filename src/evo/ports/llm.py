"""Language model boundary; credentials stay with adapters."""

from typing import Mapping, Protocol, Sequence


class LLMPort(Protocol):
    def complete(self, messages: Sequence[Mapping[str, str]],
                 json_object: bool = False) -> object:
        """Return model text or a JSON object; raise an adapter error on failure."""
        ...

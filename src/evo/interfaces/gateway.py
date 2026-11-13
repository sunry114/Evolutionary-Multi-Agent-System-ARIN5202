"""Integration boundary owned by D; implementations supplied by A/B/C."""
import importlib
import os
from typing import Protocol


class ApplicationUnavailable(RuntimeError):
    pass


class ApplicationGateway(Protocol):
    def snapshot(self) -> dict:
        """JSON-safe tasks, agents, messages, memory, pending requests; no secrets."""
        ...

    def dispatch(self, operation: str, payload: dict) -> dict:
        """Run/DM/group/eval/metrics/trace/memory/review/revision/answer."""
        ...


def load_application(state, demo=False):
    if demo:
        from evo.interfaces.demo import DemoApplication
        return DemoApplication()
    target = os.environ.get("EVO_APPLICATION_FACTORY", "")
    if not target:
        from evo.interfaces.sqlite_application import create_application
        return create_application(state=state)
    try:
        module, name = target.split(":")
        factory = getattr(importlib.import_module(module), name)
        return factory(state=state)
    except Exception:
        raise ApplicationUnavailable("Application factory failed to load; check backend installation") from None

"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.interfaces.gateway import ApplicationUnavailable

CAPABILITIES = {name: name in ("tasks", "task", "trace", "review", "revision")
                for name in ("tasks", "task", "trace", "review", "revision",
                             "run", "dm", "group", "answer", "memory", "eval", "metrics")}


def required_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Required text field is missing or invalid")
    return value.strip()


class SQLiteApplication:
    def __init__(self, state):
        self.store = SQLiteStore(Path(state) / "events.sqlite3")
        self.reviews = ReviewService(self.store)

    def snapshot(self):
        tasks = self.store.list_tasks()
        return {
            "mode": "SQLite 持久化工作台：可查询、审查、创建修订；Agent 执行后端尚未接入",
            "capabilities": dict(CAPABILITIES),
            "tasks": tasks, "task_limit": 100,
            "agents": sorted({t["owner"] for t in tasks if t["owner"]}),
            "messages": [], "memory": [],
            "requests": self.store.list_pending_requests(),
        }

    def dispatch(self, operation, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        if operation not in CAPABILITIES:
            raise ValueError("Unknown operation")
        if not CAPABILITIES[operation]:
            raise ApplicationUnavailable(
                "Agent execution backend not connected; SQLite mode supports "
                "tasks, task, trace, review and revision only.")
        if operation == "tasks":
            return {"tasks": self.store.list_tasks(payload.get("limit", 100))}
        task_id = required_text(payload, "task_id")
        if operation == "task":
            return {"task": self.store.get_task(task_id),
                    "children": self.store.list_children(task_id)}
        if operation == "trace":
            self.store.get_task(task_id)  # Unknown IDs must not look like empty traces.
            return {"task_id": task_id, "events": self.store.list_events(task_id)}
        if operation == "review":
            feedback = payload.get("feedback", "")
            if not isinstance(feedback, str):
                raise ValueError("Feedback must be text")
            return dict(self.reviews.decide(
                task_id, required_text(payload, "decision"), feedback))
        child = dict(self.reviews.revise(task_id, required_text(payload, "prompt")))
        # Store remains authoritative; no task scheduling or status mutation here.
        child["message"] = "Revision saved; waiting for the agent execution backend."
        return child


def create_application(*, state):
    return SQLiteApplication(state)

"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path
from threading import RLock

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.application.approvals import ApprovalService
from evo.application.chat import ChatService
from evo.domain.models import Agent
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable

DEFAULT_AGENTS = (
    Agent("researcher", "Research and gather evidence"),
    Agent("builder", "Implement code and produce artifacts"),
    Agent("reviewer", "Review results and verification evidence"),
)

CAPABILITIES = {name: name in ("tasks", "task", "trace", "review", "revision", "answer", "dm", "group", "messages")
                for name in ("tasks", "task", "trace", "review", "revision",
                             "run", "dm", "group", "answer", "memory", "eval", "metrics", "messages")}


def required_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Required text field is missing or invalid")
    return value.strip()


class SQLiteApplication:
    def __init__(self, state, *, provider=None, agents=DEFAULT_AGENTS):
        self.store = SQLiteStore(Path(state) / "events.sqlite3")
        self.reviews = ReviewService(self.store)
        self.approvals = ApprovalService(self.store)
        self.agents = tuple(agents)
        if not self.agents or len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("Agent registry must be nonempty and unique")
        self._provider = provider
        self._chat_lock = RLock()

    def messages(self, *, agent=None):
        if agent is not None:
            if agent not in {item.name for item in self.agents}:
                raise ValueError("Unknown agent")
            return list(self.store.list_direct_messages("human", agent))[-100:]
        records = [m for m in self.store.list_group_messages() if not m.get("task_id")]
        for item in self.agents:
            records.extend(self.store.list_direct_messages("human", item.name)[-100:])
        return sorted(records, key=lambda m: (m["created_at"], m["id"]))[-400:]

    def chat(self, operation, payload):
        message = required_text(payload, "message")
        if len(message) > 8000:
            raise ValueError("Message is too long")
        agent = required_text(payload, "agent") if operation == "dm" else None
        if agent is not None and agent not in {item.name for item in self.agents}:
            raise ValueError("Unknown agent")
        with self._chat_lock:
            if self._provider is None:
                from evo.adapters.llm.credentials import CredentialError, load_key
                from evo.adapters.llm.factory import create_provider
                try:
                    provider = create_provider()
                    load_key(provider.config.provider)
                except (CredentialError, ValueError):
                    raise ApplicationUnavailable(
                        "Chat requires valid provider configuration and credentials; use provider-info and setup-key.") from None
            else:
                provider = self._provider
            service = ChatService(provider, self.store, self.agents)
            try:
                reply = service.direct_message(agent, message) if agent else service.group_message(message)
            except Exception:
                raise ApplicationFailure(
                    "Agent reply failed; the message and any completed replies may already be saved. Refresh history before retrying.") from None
        return {"status": "success", "agent": agent, "reply" if agent else "replies": reply}

    def snapshot(self):
        tasks = self.store.list_tasks()
        return {
            "mode": "SQLite 工作台：聊天、审查与审批已接入；自动任务执行、记忆和评测尚未接入",
            "capabilities": dict(CAPABILITIES),
            "tasks": tasks, "task_limit": 100,
            "agents": [agent.name for agent in self.agents],
            "messages": self.messages(), "memory": [],
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
                "stored tasks, trace, review, revision, chat and human answers. "
                "Automatic execution, memory and evaluation require C11/C12/C13.")
        if operation in ("dm", "group"):
            return self.chat(operation, payload)
        if operation == "messages":
            return {"messages": self.messages(agent=payload.get("agent"))}
        if operation == "tasks":
            return {"tasks": self.store.list_tasks(payload.get("limit", 100))}
        if operation == "answer":
            if set(payload) not in ({"request_id", "approved"}, {"request_id", "answer"}):
                raise ValueError("Invalid human response")
            request_id = required_text(payload, "request_id")
            result = self.approvals.answer(request_id, {k: v for k, v in payload.items() if k != "request_id"})
            result["message"] = (
                "Request expired; no action executed." if result["status"] == "expired" else
                "Human decision saved. Approval or clarification does not automatically resume execution.")
            return result
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

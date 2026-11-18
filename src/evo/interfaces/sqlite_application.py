"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path
from threading import RLock
import uuid

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.application.approvals import ApprovalService
from evo.application.chat import ChatService
from evo.domain.models import Agent
from evo.application.orchestrator import ArtifactCheck
from evo.adapters.tools.filesystem import validate_file_path
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable
from evo.interfaces.agent_registry import load_agents, parse_agents, save_agents

DEFAULT_AGENTS = (
    Agent("researcher", "Research and gather evidence"),
    Agent("builder", "Implement code and produce artifacts"),
    Agent("reviewer", "Review results and verification evidence"),
)

CAPABILITIES = {name: name in ("tasks", "task", "trace", "review", "revision", "answer", "dm", "group", "messages", "run", "resume", "agents", "eval")
                for name in ("tasks", "task", "trace", "review", "revision",
                             "run", "resume", "dm", "group", "answer", "memory", "eval", "metrics", "messages", "agents")}


def required_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Required text field is missing or invalid")
    return value.strip()


class SQLiteApplication:
    def __init__(self, state, *, provider=None, agents=DEFAULT_AGENTS):
        self.state = Path(state).absolute()
        self.store = SQLiteStore(self.state / "events.sqlite3")
        self.reviews = ReviewService(self.store)
        self.approvals = ApprovalService(self.store)
        self._uses_registry = agents == DEFAULT_AGENTS
        self.agents = load_agents(self.state, agents) if self._uses_registry else tuple(agents)
        if not self.agents or len({agent.name for agent in self.agents}) != len(self.agents):
            raise ValueError("Agent registry must be nonempty and unique")
        self._provider = provider
        self._chat_lock = RLock()

    def _refresh_agents(self):
        if self._uses_registry:
            self.agents = load_agents(self.state, DEFAULT_AGENTS)

    def _coordinator(self, agents=None):
        from evo.interfaces.execution_factory import create_orchestrator
        return create_orchestrator(self.state, agents=agents or self.agents, store=self.store,
                                   provider=self._provider)

    @staticmethod
    def _checks(value):
        if not isinstance(value, dict) or not 1 <= len(value) <= 5:
            raise ValueError("Provide 1 to 5 objective subtask checks")
        checks = {}
        for key, raw in value.items():
            if key not in ("s1", "s2", "s3", "s4", "s5") or not isinstance(raw, dict):
                raise ValueError("Invalid subtask check")
            if "path" not in raw or set(raw) - {"path", "purpose", "contains", "exact"}:
                raise ValueError("Invalid subtask check fields")
            check = ArtifactCheck(**raw)
            validate_file_path(check.path, check.purpose)
            if len(check.contains.encode()) > 8192 or (
                    check.exact is not None and len(check.exact.encode()) > 8192):
                raise ValueError("Objective check is oversized")
            checks[key] = check
        return checks

    def run_task(self, payload):
        self._refresh_agents()
        if set(payload) - {"prompt", "checks", "no_memory"}:
            raise ValueError("Invalid run fields")
        prompt = required_text(payload, "prompt")
        if type(payload.get("no_memory", False)) is not bool:
            raise ValueError("Invalid memory mode")
        checks = self._checks(payload.get("checks"))
        try:
            coordinator = self._coordinator()
        except Exception:
            raise ApplicationUnavailable(
                "Execution needs a permitted provider, credential, and configured Harness runtime.") from None
        task_id = uuid.uuid4().hex
        try:
            result = coordinator.run(prompt, checks=checks, task_id=task_id,
                                     memory_enabled=not payload.get("no_memory", False))
        except Exception:
            return {"status": "failed", "task_id": task_id,
                    "message": "Task failed; inspect its trace for the recorded state."}
        return {"status": result["task"]["status"], "task": result["task"],
                "subtasks": result["subtasks"]}

    def resume_task(self, task_id):
        task = self.store.get_task(task_id)
        if task["parent_id"]:
            raise ValueError("Resume the root task")
        configs = [event["payload"] for event in self.store.list_events(task_id)
                   if event["event_type"] == "run_config"]
        saved_agents = parse_agents(configs[-1]["agents"]) if configs and "agents" in configs[-1] else None
        try:
            coordinator = self._coordinator(saved_agents)
        except Exception:
            raise ApplicationUnavailable(
                "Execution needs a permitted provider, credential, and configured Harness runtime.") from None
        try:
            result = coordinator.resume(task_id)
        except ValueError:
            raise
        except Exception:
            raise ApplicationFailure("Task could not resume; inspect its saved state and trace.") from None
        return {"status": result["task"]["status"], "task": result["task"],
                "subtasks": result["subtasks"]}

    def evaluate(self, payload):
        if set(payload) - {"dataset", "output", "no_memory"}:
            raise ValueError("Invalid evaluation fields")
        dataset = required_text(payload, "dataset")
        output = Path(required_text(payload, "output")).absolute()
        reports = (self.state / "reports").resolve()
        if (not reports.is_relative_to(self.state.resolve()) or output.suffix != ".json"
                or not output.resolve().is_relative_to(reports)):
            raise ValueError("Evaluation output must be a JSON file under the state reports directory")
        if type(payload.get("no_memory", False)) is not bool:
            raise ValueError("Invalid memory mode")
        self._refresh_agents()
        agents = self.agents
        from evo.evaluation.runner import evaluate_jsonl
        from evo.interfaces.execution_factory import create_orchestrator
        def factory(state):
            return create_orchestrator(state, agents=agents, provider=self._provider)
        try:
            return evaluate_jsonl(dataset, output, factory,
                                  memory_enabled=not payload.get("no_memory", False))
        except (ValueError, OSError):
            raise
        except Exception:
            raise ApplicationFailure("Evaluation failed; inspect dataset and runtime configuration.") from None

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
        self._refresh_agents()
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
        self._refresh_agents()
        tasks = self.store.list_tasks()
        return {
            "mode": "SQLite 工作台：聊天、审查、审批、任务执行与评测已接入",
            "capabilities": dict(CAPABILITIES),
            "tasks": tasks, "task_limit": 100,
            "agents": [agent.name for agent in self.agents],
            "agent_details": [{"name": agent.name, "specialty": agent.specialty}
                              for agent in self.agents],
            "messages": self.messages(), "memory": [],
            "requests": self.store.list_pending_requests(),
        }

    def dispatch(self, operation, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        if operation not in CAPABILITIES:
            raise ValueError("Unknown operation")
        if operation == "run":
            return self.run_task(payload)
        if operation == "eval":
            return self.evaluate(payload)
        if operation == "agents":
            if set(payload) != {"agents"}:
                raise ValueError("Invalid agent registry request")
            with self._chat_lock:
                agents = parse_agents(payload["agents"])
                save_agents(self.state, agents)
                self.agents = agents
            return {"status": "success", "agents": [agent.name for agent in agents]}
        if operation == "resume":
            return self.resume_task(required_text(payload, "task_id"))
        if not CAPABILITIES[operation]:
            raise ApplicationUnavailable(
                "This operation needs the evaluation or memory gateway.")
        if operation in ("dm", "group"):
            if operation == "group" and payload.get("execute") is True:
                if set(payload) - {"message", "execute", "checks", "no_memory"}:
                    raise ValueError("Invalid group execution fields")
                prompt = required_text(payload, "message")
                self._checks(payload.get("checks"))
                if type(payload.get("no_memory", False)) is not bool:
                    raise ValueError("Invalid memory mode")
                discussion = self.chat("group", {"message": prompt})
                run = self.run_task({"prompt": prompt, "checks": payload["checks"],
                                     "no_memory": payload.get("no_memory", False)})
                return {**run, "replies": discussion["replies"]}
            if operation == "group" and set(payload) != {"message"}:
                raise ValueError("Invalid group fields")
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

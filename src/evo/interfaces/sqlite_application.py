"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path
from threading import RLock
import uuid
from datetime import datetime, timezone
from dataclasses import asdict

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.application.approvals import ApprovalService
from evo.application.chat import ChatService
from evo.domain.models import Agent, Task
from evo.application.orchestrator import ArtifactCheck
from evo.adapters.tools.filesystem import validate_file_path
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable
from evo.interfaces.agent_registry import load_agents, parse_agents, save_agents

DEFAULT_AGENTS = (
    Agent("researcher", "Research and gather evidence"),
    Agent("builder", "Implement code and produce artifacts"),
    Agent("reviewer", "Review results and verification evidence"),
)

CAPABILITIES = {name: True
                for name in ("tasks", "task", "trace", "review", "revision",
                             "run", "create", "terminate", "clear_finished", "resume", "dm", "group", "answer", "memory", "eval", "metrics", "messages", "agents")}


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
            if not check.contains and check.exact is None:
                raise ValueError("Subtask check needs expected content")
            validate_file_path(check.path, check.purpose)
            if len(check.contains.encode()) > 8192 or (
                    check.exact is not None and len(check.exact.encode()) > 8192):
                raise ValueError("Objective check is oversized")
            checks[key] = check
        return checks

    @staticmethod
    def _final_check(value):
        if not isinstance(value, dict) or "path" not in value or set(value) - {
                "path", "purpose", "contains", "exact"}:
            raise ValueError("Provide one final delivery artifact")
        check = ArtifactCheck(**value)
        validate_file_path(check.path, check.purpose)
        if len(check.contains.encode()) > 8192 or (
                check.exact is not None and len(check.exact.encode()) > 8192):
            raise ValueError("Final delivery check is oversized")
        return check

    def run_task(self, payload):
        self._refresh_agents()
        if set(payload) - {"prompt", "checks", "final_check", "no_memory", "task_id"}:
            raise ValueError("Invalid run fields")
        prompt = required_text(payload, "prompt")
        if type(payload.get("no_memory", False)) is not bool:
            raise ValueError("Invalid memory mode")
        if "final_check" in payload:
            if "checks" in payload:
                raise ValueError("Choose one acceptance mode")
            final_check = self._final_check(payload["final_check"])
            checks = {}
        else:
            final_check = None
            checks = self._checks(payload.get("checks"))
        try:
            coordinator = self._coordinator()
        except Exception:
            raise ApplicationUnavailable(
                "Execution needs a permitted provider, credential, and configured Harness runtime.") from None
        task_id = payload.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str):
                raise ValueError("Invalid task ID")
            task = self.store.get_task(task_id)
            if task["status"] != "created" or task["parent_id"] or task["prompt"] != prompt:
                raise ValueError("Only a matching draft task can be started")
        else:
            task_id = uuid.uuid4().hex
        try:
            options = {"final_check": final_check} if final_check is not None else {}
            result = coordinator.run(prompt, checks=checks, task_id=task_id,
                                     memory_enabled=not payload.get("no_memory", False), **options)
        except Exception:
            try:
                if self.store.get_task(task_id)["result"] == "Terminated by human":
                    return {"status": "blocked", "task_id": task_id,
                            "message": "Task was terminated by the human."}
            except KeyError:
                pass
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
        records = [m for m in self.store.list_group_messages()
                   if not m.get("task_id") or m.get("recipient") == "chat"]
        for item in self.agents:
            records.extend(self.store.list_direct_messages("human", item.name)[-100:])
        return sorted(records, key=lambda m: (m["created_at"], m["id"]))[-400:]

    def memory(self, payload):
        self._refresh_agents()
        agent = required_text(payload, "agent")
        if agent not in {item.name for item in self.agents}:
            raise ValueError("Unknown agent")
        query = payload.get("query", "")
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("Invalid memory query")
        path = self.state / "agents" / agent / "memory.sqlite3"
        if not path.exists():
            return {"agent": agent, "memory": []}
        if not path.resolve().is_relative_to(self.state.resolve()) or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Unsafe memory path")
        from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
        memory = SQLiteMemoryStore(self.state, agent)
        records = ([asdict(item) for item in memory.recall(query)] if query.strip()
                   else memory.list_recent())
        return {"agent": agent, "memory": records}

    def metrics(self, task_id):
        task = self.store.get_task(task_id)
        children = list(self.store.list_children(task_id))
        started = datetime.fromisoformat(task["created_at"])
        terminal = task["status"] in ("success", "failed", "blocked")
        ended = datetime.fromisoformat(task["updated_at"]) if terminal else datetime.now(timezone.utc)
        completed = sum(child["status"] == "success" for child in children)
        return {"task_id": task_id, "status": task["status"],
                "basis": "stored_task_state", "goal_completed": task["status"] == "success",
                "completed_subtasks": completed, "total_subtasks": len(children),
                "subtask_completion_rate": completed / len(children) if children else None,
                "elapsed_seconds": max(0, (ended - started).total_seconds()),
                "timing_final": terminal, "includes_human_wait": True,
                "note": "Stored task wall time and direct-child states; not an official dataset quality score."}

    def chat(self, operation, payload):
        self._refresh_agents()
        message = required_text(payload, "message")
        if len(message) > 8000:
            raise ValueError("Message is too long")
        agent = required_text(payload, "agent") if operation == "dm" else None
        task_id = payload.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str):
                raise ValueError("Invalid task ID")
            task = self.store.get_task(task_id)
            if task["parent_id"] or task["status"] in ("success", "failed", "blocked"):
                raise ValueError("Task is not active")
        if agent is not None and agent not in {item.name for item in self.agents}:
            raise ValueError("Unknown agent")
        with self._chat_lock:
            if task_id is not None:
                task = self.store.get_task(task_id)
                if task["parent_id"] or task["status"] in ("success", "failed", "blocked"):
                    raise ValueError("Task is not active")
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
                reply = service.direct_message(agent, message, task_id) if agent else service.group_message(message, task_id)
            except Exception:
                raise ApplicationFailure(
                    "Agent reply failed; the message and any completed replies may already be saved. Refresh history before retrying.") from None
        result = {"status": "success", "agent": agent, "reply" if agent else "replies": reply}
        if not agent:
            result["directed_replies"] = service.directed_replies
        return result

    def snapshot(self):
        self._refresh_agents()
        tasks = self.store.list_board_tasks()
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
        if operation == "create":
            if set(payload) - {"prompt", "agents"}:
                raise ValueError("Invalid task fields")
            prompt = required_text(payload, "prompt")
            if len(prompt) > 8000:
                raise ValueError("Task is too long")
            with self._chat_lock:
                agents = parse_agents(payload["agents"]) if "agents" in payload else None
                task_id = uuid.uuid4().hex
                self.store.save_task(Task(task_id, prompt))
                if agents is not None:
                    save_agents(self.state, agents)
                    self.agents = agents
            return {"status": "created", "task_id": task_id}
        if operation == "terminate":
            if set(payload) != {"task_id"}:
                raise ValueError("Invalid termination request")
            with self._chat_lock:
                return self.store.terminate_task(required_text(payload, "task_id"))
        if operation == "clear_finished":
            if payload:
                raise ValueError("Invalid clear request")
            return self.store.clear_finished_board()
        if operation == "eval":
            return self.evaluate(payload)
        if operation == "memory":
            return self.memory(payload)
        if operation == "metrics":
            return self.metrics(required_text(payload, "task_id"))
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
                if set(payload) - {"message", "execute", "checks", "final_check", "no_memory"}:
                    raise ValueError("Invalid group execution fields")
                prompt = required_text(payload, "message")
                if "final_check" in payload:
                    self._final_check(payload["final_check"])
                else:
                    self._checks(payload.get("checks"))
                if type(payload.get("no_memory", False)) is not bool:
                    raise ValueError("Invalid memory mode")
                discussion = self.chat("group", {"message": prompt})
                acceptance = ("final_check" if "final_check" in payload else "checks")
                run = self.run_task({"prompt": prompt, acceptance: payload[acceptance],
                                     "no_memory": payload.get("no_memory", False)})
                return {**run, "replies": discussion["replies"]}
            if operation == "group" and set(payload) not in ({"message"}, {"message", "task_id"}):
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
            request = self.store.get_approval_request(request_id)
            if request["action"] == "delivery_format" and (
                    set(payload) != {"request_id", "answer"} or
                    payload["answer"] not in ("accept", "decline")):
                raise ValueError("Choose accept or decline for delivery format")
            result = self.approvals.answer(request_id, {k: v for k, v in payload.items() if k != "request_id"})
            if request["action"] == "delivery_format" and result["status"] == "answered":
                delivery = self.resume_task(request["task_id"])
                return {**result, "message": "交付格式已确认；任务状态：" + delivery["status"],
                        "task": delivery["task"]}
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

"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path
from threading import RLock
import uuid
from datetime import datetime, timezone
from dataclasses import asdict

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.application.approvals import ApprovalService
from evo.application.chat import ChatService, parse_group_chat
from evo.application.issues import IssueService
from evo.domain.models import Agent, Task
from evo.application.artifact_checks import parse_checks
from evo.domain.routing import issue_route
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable
from evo.interfaces.agent_registry import default_registry, load_registry, migrate_registry, parse_agents, update_registry

DEFAULT_AGENTS = (
    Agent("researcher", "Research and gather evidence"),
    Agent("builder", "Implement code and produce artifacts"),
    Agent("reviewer", "Review results and verification evidence"),
)

CAPABILITIES = {name: True
                for name in ("tasks", "task", "trace", "review", "revision",
                             "create", "start", "resume", "dm", "group", "answer", "memory",
                             "eval", "metrics", "messages", "configure", "assign", "comment", "artifact",
                             "chat-threads", "new-chat", "new-group")}


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
        self.issues = IssueService(self.store)
        self.approvals = ApprovalService(self.store)
        self._default_registry = default_registry(agents)
        migrate_registry(self.state)
        self.registry = load_registry(self.state, self._default_registry)
        self._provider = provider
        self._chat_lock = RLock()

    def _refresh_registry(self):
        self.registry = load_registry(self.state, self._default_registry)

    @property
    def agents(self):
        return self.registry.agents

    def _coordinator(self, agents, *, squad, direct_assignee=None):
        from evo.interfaces.execution_factory import create_orchestrator
        try:
            return create_orchestrator(self.state, agents=agents, squad=squad,
                                       direct_assignee=direct_assignee, store=self.store, provider=self._provider)
        except Exception:
            raise ApplicationUnavailable(
                "Execution needs a permitted provider, credential, and configured Harness runtime.") from None

    def _participants(self, assignee_type, assignee_id, *, check_squad_name=False):
        self._refresh_registry()
        registry = self.registry
        if assignee_type == "agent":
            if any(agent.name == assignee_id for agent in registry.agents):
                return registry.agents, None
        # An existing issue points to the workspace's only squad, whose label can change.
        elif assignee_type == "squad" and (not check_squad_name or registry.squad.name == assignee_id):
            return (tuple(agent for agent in registry.agents if agent.name in registry.squad.members),
                    registry.payload()["squad"])
        raise ValueError("Unknown issue assignee")

    def _assignment(self, payload, *, required):
        assignee = {"agent", "squad"} & payload.keys()
        if not assignee and not required:
            return None, None, None
        if len(assignee) != 1:
            raise ValueError("Choose one agent or Squad")
        assignee_type = assignee.pop()
        assignee_id = required_text(payload, assignee_type)
        mode = issue_route(assignee_type)
        self._participants(assignee_type, assignee_id, check_squad_name=True)
        return assignee_type, assignee_id, mode.value

    def create_task(self, payload, *, after_insert=None):
        if set(payload) - {"prompt", "agent", "squad", "checks", "no_memory", "from_group"}:
            raise ValueError("Invalid issue fields")
        prompt = required_text(payload, "prompt")
        assignee_type, assignee_id, mode = self._assignment(payload, required=False)
        no_memory = payload.get("no_memory", False)
        from_group = payload.get("from_group", False)
        if type(no_memory) is not bool or type(from_group) is not bool:
            raise ValueError("Invalid issue options")
        checks = parse_checks(payload["checks"]) if "checks" in payload else {}
        task_id = uuid.uuid4().hex
        config = {"checks": {key: asdict(value) for key, value in checks.items()},
                  "memory_enabled": not no_memory}
        with self._chat_lock:
            self.store.save_task(Task(task_id, prompt, assignee_type=assignee_type,
                                      assignee_id=assignee_id, routing_mode=mode),
                                 issue_config=config, from_group=from_group, after_insert=after_insert)
        return {"status": "created", "task": self.store.get_task(task_id)}

    def start_task(self, task_id):
        task = self.store.get_task(task_id)
        if not task["assignee_type"] or task["parent_id"] is not None:
            raise ValueError("Start an assigned root issue")
        if task["status"] != "created":
            return self._execution_result(task_id)
        config = self.store.latest_event_payload(task_id, "issue_config")
        if config is None:
            raise ValueError("Issue is missing its execution settings")
        checks = parse_checks(config["checks"]) if config["checks"] else {}
        agents, squad = self._participants(task["assignee_type"], task["assignee_id"])
        direct_assignee = task["assignee_id"] if task["assignee_type"] == "agent" else None
        coordinator = self._coordinator(agents, squad=squad, direct_assignee=direct_assignee)
        discussion = self.store.list_group_messages(task_id=self.store.issue_id(task_id))
        context = [{key: message[key] for key in ("sender", "content")} for message in discussion]
        try:
            coordinator.start(task_id, checks=checks, memory_enabled=config["memory_enabled"],
                              group_context=context if context or not checks else None,
                              expected_updated_at=task["updated_at"])
        except Exception:
            raise ApplicationFailure("Task execution failed; inspect its saved state and trace.") from None
        result = self._execution_result(task_id)
        if result["status"] == "created":
            raise ValueError("Issue assignment changed before start")
        return result

    def _execution_result(self, task_id):
        task = self.store.get_task(task_id)
        return {"status": task["status"], "task": task, "subtasks": self.store.list_children(task_id)}

    def resume_task(self, task_id):
        task = self.store.get_task(task_id)
        if task["parent_id"] is not None or task["status"] != "waiting_human":
            raise ValueError("Resume a root task waiting for a human")
        config = self.store.latest_event_payload(task_id, "run_config")
        if config is None:
            raise ValueError("No saved run configuration")
        coordinator = self._coordinator(parse_agents(config["agents"]), squad=config["squad"],
                                        direct_assignee=config.get("direct_assignee"))
        try:
            coordinator.resume(task_id)
        except ValueError:
            raise
        except Exception:
            raise ApplicationFailure("Task could not resume; inspect its saved state and trace.") from None
        return self._execution_result(task_id)

    def evaluate(self, payload):
        if set(payload) - {"dataset", "output", "no_memory", "compare_memory"}:
            raise ValueError("Invalid evaluation fields")
        dataset = required_text(payload, "dataset")
        output = Path(required_text(payload, "output")).absolute()
        reports = (self.state / "reports").resolve()
        if (not reports.is_relative_to(self.state.resolve()) or output.suffix != ".json"
                or not output.resolve().is_relative_to(reports)):
            raise ValueError("Evaluation output must be a JSON file under the state reports directory")
        disabled = payload.get("no_memory", False)
        compare = payload.get("compare_memory", False)
        if type(disabled) is not bool or type(compare) is not bool or (disabled and compare):
            raise ValueError("Invalid memory mode")
        self._refresh_registry()
        registry = self.registry
        agents = tuple(agent for agent in registry.agents if agent.name in registry.squad.members)
        squad = registry.payload()["squad"]
        from evo.config import ProviderConfig
        from evo.evaluation.runner import evaluate_jsonl
        from evo.interfaces.execution_factory import create_orchestrator
        config = ProviderConfig.from_environment(evaluation=True)

        def factory(state, *, memory_state):
            return create_orchestrator(state, agents=agents, squad=squad, provider=self._provider,
                                       provider_config=config, memory_state=memory_state)
        try:
            return evaluate_jsonl(dataset, output, factory, model=config.model,
                                  memory_enabled=not disabled, compare_memory=compare)
        except (ValueError, OSError):
            raise
        except Exception:
            raise ApplicationFailure("Evaluation failed; inspect dataset and runtime configuration.") from None

    def messages(self, *, agent=None):
        if agent is not None:
            if agent not in {item.name for item in self.agents}:
                raise ValueError("Unknown agent")
            return list(self.store.list_direct_messages("human", agent))[-100:]
        records = list(self.store.list_group_messages())
        for item in self.agents:
            records.extend(self.store.list_direct_messages("human", item.name)[-100:])
        return sorted(records, key=lambda m: (m["created_at"], m["id"]))[-400:]

    def memory(self, payload):
        self._refresh_registry()
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
        message = required_text(payload, "message")
        if len(message) > 8000:
            raise ValueError("Message is too long")
        chat = self.store.get_chat(required_text(payload, "chat_id")) if "chat_id" in payload else None
        agent = (chat["agent"] if chat else required_text(payload, "agent")) if operation == "dm" else None
        task_id = self.store.issue_id(required_text(payload, "task_id")) if "task_id" in payload else None
        group_id = required_text(payload, "group_id") if "group_id" in payload else None
        with self._chat_lock:
            self._refresh_registry()
            agents = {item.name: item for item in self.agents}
            if agent is not None and agent not in agents:
                raise ValueError("Unknown agent")
            participants = tuple(agents.values())
            if group_id is not None:
                group = self.store.get_group_chat(group_id)
                participants = tuple(agents[name] for name in group["members"] if name in agents)
                if not participants:
                    raise ValueError("No group members are available")
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
            service = ChatService(provider, self.store, participants)
            try:
                if agent:
                    chat_id = chat["id"] if chat else self.store.create_chat(agent, default=True)["id"]
                    reply = service.direct_message(agent, message, chat_id=chat_id)
                else:
                    reply = service.group_message(message, task_id=task_id, group_id=group_id)
            except Exception:
                raise ApplicationFailure(
                    "Agent reply failed; the message and any completed replies may already be saved. Refresh history before retrying.") from None
        return {"status": "success", "agent": agent, "reply" if agent else "replies": reply}

    def snapshot(self):
        self._refresh_registry()
        registry = self.registry.payload()
        return {
            "mode": "本地工作区 · SQLite",
            "capabilities": dict(CAPABILITIES),
            "tasks": self.issues.list(squad_name=registry["squad"]["name"]),
            "agents": [agent["name"] for agent in registry["agents"]],
            "agent_details": registry["agents"],
            "squad": registry["squad"],
        }

    def dispatch(self, operation, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        if operation not in CAPABILITIES:
            raise ValueError("Unknown operation")
        if operation == "create":
            return self.create_task(payload)
        if operation == "assign":
            if set(payload) - {"task_id", "agent", "squad"}:
                raise ValueError("Invalid assignment fields")
            task_id = required_text(payload, "task_id")
            return self.store.assign_issue(task_id, *self._assignment(payload, required=True))
        if operation == "start":
            if set(payload) != {"task_id"}:
                raise ValueError("Start requires only an issue ID")
            return self.start_task(required_text(payload, "task_id"))
        if operation == "eval":
            return self.evaluate(payload)
        if operation == "memory":
            return self.memory(payload)
        if operation == "metrics":
            return self.metrics(required_text(payload, "task_id"))
        if operation == "configure":
            with self._chat_lock:
                registry = update_registry(self.state, self._default_registry, payload)
                self.registry = registry
            return {"status": "success", **registry.payload()}
        if operation == "resume":
            return self.resume_task(required_text(payload, "task_id"))
        if operation in ("dm", "group"):
            valid = (set(payload) in ({"message"}, {"message", "task_id"}, {"message", "group_id"}) if operation == "group"
                     else set(payload) in ({"message", "agent"}, {"message", "chat_id"}))
            if not valid:
                raise ValueError("Invalid chat fields")
            return self.chat(operation, payload)
        if operation == "chat-threads" and not payload:
            return {"chats": self.store.list_chats(), "groups": self.store.list_group_chats()}
        if operation == "new-chat" and set(payload) == {"agent"}:
            self._refresh_registry()
            agent = required_text(payload, "agent")
            if agent not in {item.name for item in self.agents}:
                raise ValueError("Unknown agent")
            return {"chat": self.store.create_chat(agent)}
        if operation == "new-group":
            self._refresh_registry()
            name, members = parse_group_chat(payload, {agent.name for agent in self.agents})
            return {"group": self.store.create_group_chat(name, members)}
        if operation == "messages":
            if set(payload) == {"chat_id"}:
                chat = self.store.get_chat(required_text(payload, "chat_id"))
                return {"messages": self.store.list_direct_messages("human", chat["agent"], chat_id=chat["id"])[-100:]}
            if payload == {"scope": "group"}:
                return {"messages": self.store.list_group_messages()[-100:]}
            if set(payload) == {"group_id"}:
                group = self.store.get_group_chat(required_text(payload, "group_id"))
                return {"messages": self.store.list_group_messages(group_id=group["id"])[-100:]}
            if set(payload) <= {"agent"}:
                return {"messages": self.messages(agent=payload.get("agent"))}
            raise ValueError("Invalid message scope")
        if operation == "tasks":
            owner = required_text(payload, "owner") if "owner" in payload else None
            owner_type = required_text(payload, "owner_type") if "owner_type" in payload else None
            if owner_type is not None and (owner is None or owner_type not in ("squad", "agent")):
                raise ValueError("Owner type requires a named agent or Squad")
            self._refresh_registry()
            return {"tasks": self.issues.list(payload.get("limit", 100), owner=owner, owner_type=owner_type,
                                             squad_name=self.registry.squad.name)}
        if operation == "answer":
            if set(payload) not in ({"task_id", "request_id", "approved"}, {"task_id", "request_id", "answer"}):
                raise ValueError("Invalid human response")
            request_id = required_text(payload, "request_id")
            issue_id = self.store.issue_id(required_text(payload, "task_id"))
            request = self.store.get_approval_request(request_id)
            if self.store.issue_id(request["task_id"]) != issue_id:
                raise ValueError("Approval does not belong to this issue")
            result = self.approvals.answer(
                request_id, {key: payload[key] for key in ("approved", "answer") if key in payload})
            result["message"] = (
                "Request expired; no action executed." if result["status"] == "expired" else
                "Human decision saved. Approval or clarification does not automatically resume execution.")
            return result
        task_id = required_text(payload, "task_id")
        if operation == "task":
            self._refresh_registry()
            return self.issues.detail(task_id, squad_name=self.registry.squad.name)
        if operation == "comment":
            content = required_text(payload, "message")
            if set(payload) != {"task_id", "message"} or len(content) > 8000:
                raise ValueError("Invalid comment")
            return self.issues.comment(task_id, content)
        if operation == "artifact":
            from evo.adapters.tools.artifacts import ArtifactExchange
            from evo.adapters.tools.filesystem import WorkspaceFilesystem
            event_id = payload.get("event_id")
            if set(payload) != {"task_id", "event_id"} or type(event_id) is not int:
                raise ValueError("Artifact event ID is required")
            artifacts = self.issues.detail(task_id)["artifacts"]
            item = next((row for row in artifacts if row["event_id"] == event_id), None)
            if item is None or item["archive"] is None:
                raise ValueError("Verified snapshot is unavailable for this issue")
            exchange = ArtifactExchange(WorkspaceFilesystem(self.state, item["owner"]))
            return exchange.preview(item["archive"]["id"], item["archive"]["sha256"])
        if operation == "trace":
            self.store.get_task(task_id)  # Unknown IDs must not look like empty traces.
            events = self.store.list_events(task_id)
            for event in events:
                if event["event_type"] == "memory_context":
                    event["payload"].pop("lessons", None)
            return {"task_id": task_id, "events": events}
        if operation == "review":
            feedback = payload.get("feedback", "")
            if not isinstance(feedback, str):
                raise ValueError("Feedback must be text")
            return dict(self.reviews.decide(
                task_id, required_text(payload, "decision"), feedback))
        return dict(self.reviews.revise(task_id, required_text(payload, "prompt")))


def create_application(*, state):
    return SQLiteApplication(state)

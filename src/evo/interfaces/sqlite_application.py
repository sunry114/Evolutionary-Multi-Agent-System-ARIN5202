"""Real SQLite human-review workspace; no agent execution is simulated."""
from pathlib import Path
from threading import RLock, Thread
import uuid
from datetime import datetime, timezone
from dataclasses import asdict

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.reviews import ReviewService
from evo.application.approvals import ApprovalService
from evo.application.chat import ChatService, human_question
from evo.domain.models import Agent, Task
from evo.domain.policies import team_coordinator
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
                             "run", "start", "create", "terminate", "clear_finished", "resume", "continue", "dm", "group", "discuss", "answer", "memory", "eval", "metrics", "messages", "agents")}


def required_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Required text field is missing or invalid")
    return value.strip()


def private_message_view(records):
    """Keep agent-to-agent sections out of private history, including legacy DMs."""
    visible = []
    for record in records:
        if record["kind"] == "dm" and record["sender"] != "human":
            content = human_question(record["content"])
            if not content:
                continue
            record = {**record, "content": content}
        visible.append(record)
    return visible


def private_request_view(request):
    if request["action"] == "chat_question":
        question = human_question(request["payload"].get("question", ""))
        return {**request, "payload": {**request["payload"],
                "question": question or "请补充你对当前任务的需求或限制。"}}
    return request


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
        # Serialize each task's conversation; controls and other tasks stay responsive.
        self._state_lock = RLock()
        self._active_runs = set()
        self._active_discussions = set()
        self._conversation_locks = {}
        # Existing unanswered text questions use the same human contact after restart.
        for scope in {r["task_id"] for r in self.store.list_pending_requests() if r["kind"] == "clarification"}:
            root = self.store.get_task(scope)
            while root["parent_id"]:
                root = self.store.get_task(root["parent_id"])
            saved_agents = self._task_agents(root["id"])
            self.store.route_pending_questions(scope, team_coordinator(saved_agents), [asdict(a) for a in saved_agents])

    def _task_agents(self, task_id):
        configs = [e["payload"]["agents"] for e in self.store.list_events(task_id)
                   if e["event_type"] in ("task_agents", "run_config") and "agents" in e["payload"]]
        return parse_agents(configs[-1]) if configs else self.agents

    def _conversation_lock(self, task_id):
        with self._state_lock:
            return self._conversation_locks.setdefault(task_id, RLock())

    def _require_discussion_ready(self, task_id):
        with self._state_lock:
            active = any(key[0] == task_id for key in self._active_discussions)
        if active:
            raise ApplicationUnavailable("团队仍在讨论，请等本轮讨论结束后开始执行。")
        if any(r["task_id"] == task_id and r["action"] == "chat_question"
               for r in self.store.list_pending_requests()):
            raise ApplicationUnavailable("Agent 正在等待人类回答，请先到私聊回复。")

    def start_discussion(self, payload):
        """Acknowledge work promptly; committed messages are readable during model calls."""
        if set(payload) - {"task_id", "message", "request_id"}:
            raise ValueError("Invalid discussion fields")
        task_id = required_text(payload, "task_id")
        task = self.store.get_task(task_id)
        if task["parent_id"] or task["status"] in ("success", "failed", "blocked"):
            raise ValueError("Task is not active")
        request_id = payload.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str):
                raise ValueError("Invalid request ID")
            request = self.store.get_approval_request(request_id)
            if (request["task_id"] != task_id or request["action"] != "chat_question"
                    or request["status"] != "answered"):
                raise ValueError("Only an answered chat question may wake an agent")
            if any(e["event_type"] == "chat_completed" and e["payload"].get("request_id") == request_id
                   for e in self.store.list_events(task_id)):
                raise ValueError("This reply has already been processed")
            message = None
        else:
            message = required_text(payload, "message")
            if len(message) > 8000:
                raise ValueError("Message is too long")
        key = (task_id, request_id)
        with self._state_lock:
            if key in self._active_discussions:
                return {"status": "started", "task_id": task_id}
            self._active_discussions.add(key)

        def work():
            try:
                self.store.append_event(task_id=task_id, event_type="chat_started", actor="chat",
                                        payload={"request_id": request_id})
                self.chat("group", {"task_id": task_id, "message": message or "Resume private answer"},
                          resume_request=request_id)
                self.store.append_event(task_id=task_id, event_type="chat_completed", actor="chat",
                                        payload={"request_id": request_id})
            except Exception as error:
                hint = str(error) if isinstance(error, (ApplicationFailure, ApplicationUnavailable)) else "讨论处理失败；已保存的消息和私聊回答会保留。"
                self.store.append_event(task_id=task_id, event_type="chat_failed", actor="chat",
                                        payload={"request_id": request_id, "message": hint})
            finally:
                with self._state_lock:
                    self._active_discussions.discard(key)

        try:
            Thread(target=work, name="evo-chat-" + task_id[:8], daemon=True).start()
        except Exception:
            with self._state_lock:
                self._active_discussions.discard(key)
            raise
        return {"status": "started", "task_id": task_id}

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
        task_id = payload.get("task_id")
        if task_id is not None:
            if not isinstance(task_id, str):
                raise ValueError("Invalid task ID")
            task = self.store.get_task(task_id)
            if task["status"] != "created" or task["parent_id"] or task["prompt"] != prompt:
                raise ValueError("Only a matching draft task can be started")
            self._require_discussion_ready(task_id)
        else:
            task_id = uuid.uuid4().hex
        try:
            coordinator = self._coordinator(self._task_agents(task_id))
        except Exception:
            raise ApplicationUnavailable(
                "Execution needs a permitted provider, credential, and configured Harness runtime.") from None
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

    def start_task(self, payload):
        """Start a board task without keeping its HTTP request open for minutes."""
        if set(payload) - {"task_id", "prompt", "final_check", "no_memory"} or "final_check" not in payload:
            raise ValueError("Invalid start fields")
        task_id = required_text(payload, "task_id")
        prompt = required_text(payload, "prompt")
        self._final_check(payload["final_check"])
        if type(payload.get("no_memory", False)) is not bool:
            raise ValueError("Invalid memory mode")
        with self._state_lock:
            task = self.store.get_task(task_id)
            if (task["status"] != "created" or task["parent_id"]
                    or task["prompt"] != prompt or task_id in self._active_runs):
                raise ValueError("Only a matching draft task can be started once")
            self._refresh_agents()
            self._require_discussion_ready(task_id)
            try:
                self._coordinator(self._task_agents(task_id))  # Validate before acknowledging start.
            except Exception:
                raise ApplicationUnavailable("Execution needs a permitted provider and credential.") from None
            self._active_runs.add(task_id)

        def work():
            try:
                result = self.run_task(payload)
                if result["status"] == "failed":
                    task = self.store.get_task(task_id)
                    if task["status"] in ("created", "planning", "ready", "assigned"):
                        self.store.transition_task(task_id, "failed", expected=task["status"],
                                                   result="Execution failed; inspect trace")
            except Exception:
                try:
                    task = self.store.get_task(task_id)
                    if task["status"] in ("created", "planning", "ready", "assigned"):
                        self.store.transition_task(task_id, "failed", expected=task["status"],
                                                   result="Execution failed; inspect trace")
                except Exception:
                    pass
            finally:
                with self._state_lock:
                    self._active_runs.discard(task_id)

        try:
            Thread(target=work, name="evo-task-" + task_id[:8], daemon=True).start()
        except Exception:
            with self._state_lock:
                self._active_runs.discard(task_id)
            raise
        return {"status": "started", "task_id": task_id}

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

    def continue_task(self, task_id):
        """Resume after human input without holding the HTTP connection open."""
        with self._state_lock:
            task = self.store.get_task(task_id)
            if (task["parent_id"] or task["status"] != "waiting_human"
                    or task_id in self._active_runs):
                raise ValueError("Only one paused root task can resume")
            if any(request["task_id"] == task_id or request["task_id"].startswith(task_id + "-")
                   for request in self.store.list_pending_requests()):
                raise ValueError("Answer pending human requests before continuing")
            self._active_runs.add(task_id)

        def work():
            try:
                self.resume_task(task_id)
            except Exception:
                try:
                    self.store.append_event(task_id=task_id, event_type="resume_error", actor="web",
                                            payload={"code": "resume_failed"})
                except Exception:
                    pass
            finally:
                with self._state_lock:
                    self._active_runs.discard(task_id)

        try:
            Thread(target=work, name="evo-resume-" + task_id[:8], daemon=True).start()
        except Exception:
            with self._state_lock:
                self._active_runs.discard(task_id)
            raise
        return {"status": "started", "task_id": task_id}

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

    def messages(self, *, agent=None, task_id=None):
        if agent is not None:
            agents = self._task_agents(task_id) if task_id else self.agents
            if agent not in {item.name for item in agents}:
                raise ValueError("Unknown agent")
        if task_id is not None:
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Invalid task ID")
            self.store.get_task(task_id)
            return private_message_view([message for message in self.store.list_task_messages(task_id)
                    if (message["kind"] == "group" and message.get("recipient") == "chat")
                    or (message["kind"] == "dm" and
                        (message["sender"] == "human" or message["recipient"] == "human"))
                    if agent is None or message["kind"] != "dm" or
                    message["sender"] == agent or message["recipient"] == agent])
        if agent is not None:
            return private_message_view(list(self.store.list_direct_messages("human", agent))[-100:])
        records = [m for m in self.store.list_group_messages()
                   if not m.get("task_id") or m.get("recipient") == "chat"]
        for item in self.agents:
            records.extend(self.store.list_direct_messages("human", item.name)[-100:])
        return private_message_view(sorted(records, key=lambda m: (m["created_at"], m["id"]))[-400:])

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

    def _save_chat_message(self, message):
        # Termination clears messages under the same lock, so a reply arriving
        # later cannot recreate a terminated task's conversation.
        with self._state_lock:
            if message.task_id is not None:
                task = self.store.get_task(message.task_id)
                if task["parent_id"] or task["status"] in ("success", "failed", "blocked"):
                    raise ValueError("Task is not active")
            self.store.save_message(message)

    def chat(self, operation, payload, *, resume_request=None):
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
        agents = self._task_agents(task_id) if task_id else self.agents
        if agent is not None and agent not in {item.name for item in agents}:
            raise ValueError("Unknown agent")
        with self._conversation_lock(task_id):
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
            agents = self._task_agents(task_id) if task_id else self.agents
            if resume_request:
                request = self.store.get_approval_request(resume_request)
                agents = parse_agents(request["payload"]["agents"])
            waiting = set()
            for pending in self.store.list_pending_requests():
                if pending["task_id"] == task_id and pending["action"] == "chat_question":
                    waiting.add(pending["agent"])
                    waiting.update(pending["payload"].get("sources", []))
            def question_writer(name, question, scope):
                if scope:
                    with self._state_lock:
                        if name != team_coordinator(agents):
                            raise ValueError("Only the team coordinator may ask the human")
                        self.store.save_chat_question(scope, name, question, [asdict(a) for a in agents],
                                                      sources=service.question_sources)
            service = ChatService(provider, self.store, agents,
                                  message_writer=self._save_chat_message,
                                  question_writer=question_writer, waiting_agents=waiting)
            try:
                if resume_request:
                    reply = service.resume_question(request["agent"], task_id,
                                                     sources=request["payload"].get("sources", []))
                else:
                    reply = service.direct_message(agent, message, task_id) if agent else service.group_message(message, task_id)
            except Exception as error:
                from evo.adapters.llm.client import ProviderError
                hints = {"connection": "模型服务连接失败或超时", "authentication": "模型 API 密钥无效",
                         "permission": "模型服务拒绝访问", "rate_limit": "模型服务限流",
                         "invalid_completion": "模型返回了空白、截断或格式错误的回复"}
                hint = hints.get(error.code, "模型服务调用失败") if isinstance(error, ProviderError) else "Agent 回复处理失败"
                raise ApplicationFailure(
                    hint + "；已保存的消息会保留。这不是人类回复超时，请查看历史后再重试。") from None
        result = {"status": "success", "agent": agent, "reply" if agent else "replies": reply}
        result["waiting_for_human"] = service.waiting_for_human
        if not agent:
            result["directed_replies"] = service.directed_replies
        return result

    def _discussion_state(self, task_id):
        events = [e for e in self.store.list_events(task_id)
                  if e["event_type"] in ("chat_started", "chat_completed", "chat_failed")]
        with self._state_lock:
            active = any(key[0] == task_id for key in self._active_discussions)
        return {"active": active, "last_event": events[-1] if events else None}

    def snapshot(self):
        self._refresh_agents()
        tasks = self.store.list_board_tasks()
        requests = [private_request_view(r) for r in self.store.list_pending_requests()]
        request_roots = {}
        for request in requests:
            scope = self.store.get_task(request["task_id"])
            while scope["parent_id"]:
                scope = self.store.get_task(scope["parent_id"])
            request_roots[request["id"]] = scope["id"]
        for task in tasks:
            if not task["parent_id"]:
                task["agent_details"] = [asdict(a) for a in self._task_agents(task["id"])]
                task["coordinator"] = team_coordinator(self._task_agents(task["id"]))
                task["discussion"] = self._discussion_state(task["id"])
                pending = [r for r in requests if request_roots[r["id"]] == task["id"]]
                task["pending_questions"] = sum(r["kind"] == "clarification" for r in pending)
                task["pending_approvals"] = sum(r["kind"] == "approval" for r in pending)
        return {
            "mode": "SQLite 工作台：聊天、审查、审批、任务执行与评测已接入",
            "capabilities": dict(CAPABILITIES),
            "tasks": tasks, "task_limit": 100,
            "agents": [agent.name for agent in self.agents],
            "agent_details": [{"name": agent.name, "specialty": agent.specialty}
                              for agent in self.agents],
            "coordinator": team_coordinator(self.agents),
            "messages": self.messages(), "memory": [],
            "requests": requests,
        }

    def dispatch(self, operation, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request must be an object")
        if operation not in CAPABILITIES:
            raise ValueError("Unknown operation")
        if operation == "run":
            return self.run_task(payload)
        if operation == "start":
            return self.start_task(payload)
        if operation == "discuss":
            return self.start_discussion(payload)
        if operation == "create":
            if set(payload) - {"prompt", "agents"}:
                raise ValueError("Invalid task fields")
            prompt = required_text(payload, "prompt")
            if len(prompt) > 8000:
                raise ValueError("Task is too long")
            with self._state_lock:
                agents = parse_agents(payload["agents"]) if "agents" in payload else None
                task_id = uuid.uuid4().hex
                self.store.save_task(Task(task_id, prompt))
                if agents is not None:
                    save_agents(self.state, agents)
                    self.agents = agents
                self.store.append_event(task_id=task_id, event_type="task_agents", actor="human",
                                        payload={"agents": [asdict(a) for a in self.agents]})
            return {"status": "created", "task_id": task_id}
        if operation == "terminate":
            if set(payload) != {"task_id"}:
                raise ValueError("Invalid termination request")
            with self._state_lock:
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
            with self._state_lock:
                agents = parse_agents(payload["agents"])
                save_agents(self.state, agents)
                self.agents = agents
            return {"status": "success", "agents": [agent.name for agent in agents]}
        if operation == "resume":
            return self.resume_task(required_text(payload, "task_id"))
        if operation == "continue":
            if set(payload) != {"task_id"}:
                raise ValueError("Invalid continue fields")
            return self.continue_task(required_text(payload, "task_id"))
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
            if set(payload) - {"agent", "task_id"}:
                raise ValueError("Invalid message fields")
            task_id = payload.get("task_id")
            result = {"messages": self.messages(agent=payload.get("agent"), task_id=task_id)}
            if task_id:
                result["discussion"] = self._discussion_state(task_id)
                requests = []
                for request in self.store.list_pending_requests():
                    scope = self.store.get_task(request["task_id"])
                    while scope["parent_id"]:
                        scope = self.store.get_task(scope["parent_id"])
                    if scope["id"] == task_id:
                        requests.append(private_request_view(request))
                result["requests"] = requests
            return result
        if operation == "tasks":
            return {"tasks": self.store.list_tasks(payload.get("limit", 100))}
        if operation == "answer":
            if set(payload) not in ({"request_id", "approved"}, {"request_id", "answer"}):
                raise ValueError("Invalid human response")
            request_id = required_text(payload, "request_id")
            request = self.store.get_approval_request(request_id)
            if request["action"] == "chat_question":
                result = self.approvals.answer(request_id, {k: v for k, v in payload.items() if k != "request_id"})
                self.start_discussion({"task_id": request["task_id"], "request_id": request_id})
                return {**result, "message": "私聊回答已保存，总负责人正在向相关 Agent 同步决定。"}
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

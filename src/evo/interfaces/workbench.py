"""Issue-first frontend for the local agent runtime."""

import fcntl
from contextlib import contextmanager
from dataclasses import asdict
from threading import Event, Thread
from uuid import uuid4

from evo.adapters.persistence.issue_runs import IssueRunStore, issue_status
from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import ConflictError
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.domain.models import Experience
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import required_text
from evo.ports.harness import HarnessCleanupError, HarnessError, RunRequest, ToolInputError


class Workbench:
    def __init__(self, application):
        self.application = application
        self.store = application.store
        self.queue = IssueRunStore(self.store)
        self.shutdown = Event()
        self.wake = Event()

    @contextmanager
    def worker(self):
        with (self.application.state / ".issue-worker.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.queue.recover_interrupted()
            thread = Thread(target=self._work, name="evo-issue-worker", daemon=True)
            thread.start()
            try:
                yield
            finally:
                self.shutdown.set()
                self.wake.set()
                thread.join()

    def _agents(self):
        self.application._refresh_registry()
        return {agent.name: agent for agent in self.application.agents}

    def _assignee(self, issue, agents=None):
        agents = self._agents() if agents is None else agents
        if issue["assignee_type"] == "squad":
            return agents.get(self.application.registry.squad.lead)
        return agents.get(issue["assignee_id"])

    def _issue(self, task_id):
        return self.store.get_task(self.store.issue_id(task_id))

    def _work(self):
        while not self.shutdown.is_set():
            self.wake.clear()
            run = self.queue.claim()
            if run is None:
                self.wake.wait(0.5)
                continue
            self._execute(run)

    def _execute(self, run):
        from evo.interfaces.execution_factory import create_issue_harness

        run_id, issue_id = run["id"], run["issue_id"]
        agent = run["agent"]
        status, result = "failed", "Execution failed"
        stage = "preparing the run"
        handle = None
        try:
            issue = self._issue(issue_id)
            agents = self._agents()
            assignee = self._assignee(issue, agents)
            squad = self.application.registry.squad
            coordinating = (issue["assignee_type"] == "squad" and assignee and assignee.name == agent
                            and run["kind"] != "mention")
            targets = {name: member for name, member in agents.items() if name in squad.members} if coordinating else agents

            def comment(content):
                if not content.strip() or len(content) > 8000:
                    raise ToolInputError("Provide a comment of 1–8000 characters")
                return self.queue.comment(issue_id, content, targets, sender=agent, parent_run_id=run_id)

            def change_status(status):
                current = self._assignee(self._issue(issue_id))
                if not current or current.name != agent:
                    raise ToolInputError("Only the issue assignee may set its status")
                try:
                    return self.queue.set_run_status(run, status)
                except (ConflictError, ValueError) as error:
                    raise ToolInputError(str(error)) from None

            detail = self.application.issues.detail(issue_id)
            workspace, harness = create_issue_harness(
                self.application.state, self.store, run, comment=comment,
                change_status=change_status if run["kind"] != "mention" else None,
                issue_artifacts=detail["artifacts"])
            trigger = next((row["content"] for row in detail["messages"] if row["id"] == run["comment_id"]), "")
            issue_text = issue["description"].encode()[:5000].decode(errors="ignore")
            trigger = trigger.encode()[:14000].decode(errors="ignore")
            comments = "\n".join(f"{m['sender']}: {m['content'][:1500]}" for m in detail["messages"][-8:])
            comments = comments.encode()[-5000:].decode(errors="ignore")
            prompt = f"Issue:\n{issue_text}\n\nTriggering comment:\n{trigger}\n\nRecent comments:\n{comments}"
            role = run["agent_role"] + " Follow the triggering comment when present. Report results on this issue."
            if coordinating:
                roster = ", ".join(f"@{name}" for name in targets if name != agent)
                role += (f" You lead the Squad. Use post_issue_comment with one of {roster} to hand off work. "
                         "After dispatching, stop and leave the issue in progress. When member results arrive, "
                         "coordinate the next step or move the issue to in_review once its goal is met.")
            config = self.store.latest_event_payload(issue_id, "issue_config") or {}
            memory = SQLiteMemoryStore(self.application.state, agent) if config.get("memory_enabled", True) else None
            lessons = tuple(item.lesson for item in memory.recall(issue_text)) if memory else ()
            request = RunRequest(run_id, agent, issue_id, workspace.workspace, prompt, role=role, memory=lessons)
            if self.queue.stopping(run_id) or self.shutdown.is_set():
                self.queue.finish(run, "stopped", "Stopped before execution", assignee=assignee)
                return
            handle = harness.start(request)
            stage = "reading runtime progress"
            cursor = 0
            while True:
                stopping = self.shutdown.is_set() or self.queue.stopping(run_id)
                outcome = harness.cancel(handle) if stopping else harness.wait(handle, timeout=0.25)
                for event in harness.events(handle, after=cursor):
                    cursor = event.sequence
                    if event.kind == "assistant" and event.payload.get("text"):
                        self.store.append_event(task_id=run_id, event_type="issue_run_output", actor=agent,
                                                payload={"result": event.payload["text"]})
                    elif event.kind == "native_tool":
                        self.store.append_event(task_id=run_id, event_type="issue_run_tool", actor=agent,
                                                payload=event.payload)
                if outcome is not None:
                    break
            if outcome.error == "cleanup_failed":
                status, result = "interrupted", "Runtime cleanup could not be confirmed"
            elif stopping:
                status, result = "stopped", "Stopped by user or server shutdown"
            elif outcome.status == "success":
                stage = "archiving artifacts"
                for evidence in outcome.artifacts:
                    snapshot = ArtifactExchange(workspace).publish_verified(
                        evidence.path, agent, purpose=evidence.purpose, artifact_id=uuid4().hex, digest=evidence.sha256)
                    self.store.append_event(task_id=run_id, event_type="artifact_published", actor=agent,
                                            payload={"path": evidence.path, "purpose": evidence.purpose,
                                                     "artifact": asdict(snapshot)})
                status, result = "completed", outcome.text
                if memory and outcome.artifacts:
                    artifact = outcome.artifacts[0].path
                    memory.add(Experience(agent, issue_text[:1000], TaskStatus.SUCCESS, "Produced " + artifact,
                                          artifact, source_issue_id=issue_id))
            else:
                result = "Execution failed: " + (outcome.error or outcome.status)
        except HarnessCleanupError:
            status, result = "interrupted", "Runtime cleanup could not be confirmed"
        except Exception as error:
            if status != "completed":
                result = "Execution failed: " + str(error) if isinstance(error, HarnessError) else "Execution failed while " + stage
                if handle is not None:
                    try:
                        cleanup = harness.cancel(handle)
                        if cleanup.error == "cleanup_failed":
                            status, result = "interrupted", "Runtime cleanup could not be confirmed"
                    except Exception:
                        status, result = "interrupted", "Runtime cleanup could not be confirmed"
        self.queue.finish(run, status, result, assignee=self._assignee(self._issue(issue_id)))

    def _list(self, rows):
        return [{**row, "lane": issue_status(row)} for row in rows]

    def snapshot(self):
        result = self.application.snapshot()
        result["tasks"] = self._list(result["tasks"])
        result["capabilities"]["issue_runs"] = True
        return result

    def dispatch(self, operation, payload):
        if operation in ("answer", "resume", "review", "revision"):
            raise ValueError("Use the issue status and run controls")
        if operation == "create":
            if set(payload) - {"prompt", "agent", "squad", "from_group", "no_memory"}:
                raise ValueError("Invalid issue fields")
            agents = self._agents()
            squad = self.application.registry.squad
            run_id = None

            def enqueue(connection, issue_id):
                nonlocal run_id
                run_id = self.queue.create(connection, issue_id, agents, squad)

            draft = self.application.create_task(payload, after_insert=enqueue)
            if run_id:
                draft["run_id"] = run_id
                self.wake.set()
            return draft
        if operation == "tasks":
            return {"tasks": self._list(self.application.dispatch(operation, payload)["tasks"])}
        if operation == "task":
            detail = self.application.dispatch(operation, payload)
            detail["lane"] = issue_status({**detail["current"], "issue_status": detail["issue"].get("issue_status")})
            detail["executions"] = self.queue.list(detail["issue"]["id"])
            return detail
        if operation not in ("assign", "start", "comment", "issue-status", "stop-run", "retry-run"):
            return self.application.dispatch(operation, payload)
        issue_id = self.store.issue_id(required_text(payload, "task_id"))
        issue = self._issue(issue_id)
        agents = self._agents()
        agent = self._assignee(issue, agents)
        if operation == "assign":
            assignee_type, assignee_id, mode = self.application._assignment(payload, required=True)
            if set(payload) != {"task_id", assignee_type}:
                raise ValueError("Invalid assignment")
            target = self._assignee({"assignee_type": assignee_type, "assignee_id": assignee_id}, agents)
            result = {"run_id": self.queue.assign(issue_id, assignee_type, assignee_id, mode, target)}
        elif operation == "comment":
            content = required_text(payload, "message")
            if set(payload) != {"task_id", "message"} or len(content) > 8000:
                raise ValueError("Invalid comment")
            result = self.queue.comment(issue_id, content, agents)
        elif operation == "issue-status":
            if set(payload) != {"task_id", "status"}:
                raise ValueError("Invalid status change")
            result = {"run_id": self.queue.change_status(issue_id, required_text(payload, "status"), agent=agent)}
        elif operation == "start":
            if set(payload) != {"task_id"} or not agent:
                raise ValueError("Assign the issue first")
            result = {"run_id": self.queue.start(issue_id, agent)}
        else:
            if set(payload) != {"task_id", "run_id"}:
                raise ValueError("Invalid run operation")
            run_id = required_text(payload, "run_id")
            result = ({"status": self.queue.stop(issue_id, run_id)} if operation == "stop-run" else
                      {"run_id": self.queue.retry(issue_id, run_id)})
        self.wake.set()
        return result

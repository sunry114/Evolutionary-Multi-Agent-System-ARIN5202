"""Issue-first frontend for the local agent runtime."""

import fcntl
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from evo.adapters.persistence.issue_runs import IssueRunStore, issue_status
from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import ConflictError
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.domain.models import Experience
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import required_text
from evo.ports.harness import MAX_RUN_PROMPT_BYTES, HarnessCleanupError, HarnessError, RunRequest, ToolInputError


class Workbench:
    def __init__(self, application, *, project):
        self.application = application
        self.project = dict(project)
        self.directory = Path(self.project["directory"])
        self.store = application.store
        self.queue = IssueRunStore(self.store)
        self.shutdown = Event()
        self.wake = Event()

    @contextmanager
    def worker(self):
        with (self.application.state / ".issue-worker.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.queue.recover_interrupted()
            self.shutdown.clear()
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

    def _outputs(self, issue_id, run_id):
        return self.directory / ".evo" / "artifacts" / issue_id / run_id

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
        reply = ""
        stage = "preparing the run"
        handle = None
        try:
            issue = self._issue(issue_id)
            self.application._refresh_registry()
            registry = self.application.registry
            agents = {member.name: member for member in registry.agents}
            assignee = self._assignee(issue, agents)
            squad = registry.squad
            assigned_to_squad = issue["assignee_type"] == "squad"
            owns_status = assignee is not None and agent == assignee.name
            coordinating = assigned_to_squad and owns_status
            targets = {name: agents[name] for name in squad.members} if coordinating else {}

            def comment(content):
                if not content.strip() or len(content) > 8000:
                    raise ToolInputError("Provide a comment of 1–8000 characters")
                return self.queue.comment(issue_id, content, {}, sender=agent, parent_run_id=run_id)

            def assign_work(agent, content):
                if agent not in targets or agent == squad.lead:
                    raise ToolInputError("Choose a teammate other than the squad lead")
                if not content.strip() or len(content) > 8000:
                    raise ToolInputError("Provide a subtask of 1–8000 characters")
                current_issue = self._issue(issue_id)
                current = self._assignee(current_issue)
                if current_issue["assignee_type"] != "squad" or current is None or current.name != squad.lead:
                    raise ToolInputError("Only the current squad lead may assign work")
                try:
                    return self.queue.comment(issue_id, f"Assigned to @{agent}: {content}", targets,
                                              sender=squad.lead, parent_run_id=run_id, targets=(agent,))
                except ConflictError as error:
                    raise ToolInputError(str(error)) from None

            def change_status(status):
                current = self._assignee(self._issue(issue_id))
                if not current or current.name != agent:
                    raise ToolInputError("Only the issue assignee may set its status")
                try:
                    return self.queue.set_run_status(run, status)
                except (ConflictError, ValueError) as error:
                    raise ToolInputError(str(error)) from None

            detail = self.application.issues.detail(issue_id)
            completed = {row["id"] for row in self.queue.list(issue_id)
                         if row["agent"] == agent and row["status"] == "completed"}
            resume_session = next((event["payload"]["session_id"] for event in reversed(detail["logs"])
                                   if event["event_type"] == "issue_run_native_session"
                                   and event["task_id"] in completed), None) if not run["retry_of"] else None
            workspace, harness = create_issue_harness(
                self.application.state, self.store, run, project_dir=self.directory,
                output_dir=self._outputs(issue_id, run_id), resume_session=resume_session, comment=comment,
                change_status=change_status if owns_status else None,
                assign_work=assign_work if coordinating else None,
                issue_artifacts=detail["artifacts"])
            triggers = list(dict.fromkeys(f"{row['sender']}: {row['content']}" for row in self.queue.triggers(run_id)))
            trigger = "\n\n".join(triggers).encode()[-14000:].decode(errors="ignore")
            follow_up = self.store.latest_event_payload(issue_id, "issue_follow_up_requested") or {}
            scope = next((row["content"] for row in detail["messages"] if row["id"] == follow_up.get("comment_id")),
                         issue["description"])
            issue_text = scope.encode()[:5000].decode(errors="ignore")
            mode = "Assignment" if run["kind"] == "assignment" else "Reply"
            prompt = (f"Turn mode: {mode}\nIssue status: {issue_status(issue)}\n"
                      f"\nCurrent requester scope:\n{issue_text}\n\nTriggering comments:\n{trigger}")
            role = (run["agent_role"] + " Work within the current requester scope. "
                    "Use the triggering comments to identify the concrete work for this turn. "
                    "Add formats, features, or extra rounds only when needed to satisfy the current request. "
                    "Reuse existing work. If a reply only acknowledges, thanks, or signs off and needs no action, "
                    "return only NO_ACTION and do not post a comment.")
            if assigned_to_squad:
                roster = "\n".join(f"@{name}{' (lead)' if name == squad.lead else ''}: {agents[name].specialty}"
                                   for name in squad.members)
                prompt += f"\n\nSquad: {squad.name}\nMembers and responsibilities:\n{roster}"
                role += (f" Your handle is @{agent}. An ordinary comment never dispatches another agent. "
                         "Queued teammate runs start after your current run ends.")
                if coordinating:
                    role += (" As squad lead, own scoping, decisions, delegation, and sign-off. "
                             "Use assign_issue_work to give each necessary subtask to a suitable teammate. "
                             "Keep delegation bounded to the requested deliverable and let teammates do the work. "
                             "After dispatching, end this turn. Teammate results will wake you. "
                             "On a reply, assess new results or requester input and delegate only uncovered work. "
                             "When the requested deliverables are ready, call set_issue_status with in_review "
                             "and finish with a concise sign-off. Reuse the files already delivered by teammates.")
                else:
                    role += (" Complete only the assigned subtask or requested clarification. "
                             "Summarize the result in your final response so the squad lead can follow up.")
            prompt += "\n\nRecent comments:\n"
            history_limit = min(5000, MAX_RUN_PROMPT_BYTES - len(prompt.encode()))
            comments = "\n".join(f"{m['sender']}: {m['content'][:1500]}" for m in detail["messages"][-8:]).encode()
            prompt += comments[max(0, len(comments) - history_limit):].decode(errors="ignore")
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
                    if event.kind == "terminal_output":
                        reply = event.payload["text"]
                    elif event.kind == "started" and event.payload.get("native_session_id"):
                        self.store.append_event(task_id=run_id, event_type="issue_run_native_session", actor=agent,
                                                payload={"session_id": event.payload["native_session_id"]})
                    elif event.kind in ("assistant", "api_retry") and event.payload.get("text"):
                        event_type = "issue_run_api_retry" if event.kind == "api_retry" else "issue_run_output"
                        self.store.append_event(task_id=run_id, event_type=event_type, actor=agent,
                                                payload={"result": event.payload["text"]})
                    elif event.kind == "native_tool":
                        self.store.append_event(task_id=run_id, event_type="issue_run_tool", actor=agent,
                                                payload=event.payload)
                    elif event.kind == "tool_result":
                        self.store.append_event(task_id=run_id, event_type="issue_run_platform_tool", actor=agent,
                                                payload=event.payload)
                if outcome is not None:
                    reply = reply or outcome.text
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
                status, result = "completed", reply
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
                        reply = reply or cleanup.text
                        if cleanup.error == "cleanup_failed":
                            status, result = "interrupted", "Runtime cleanup could not be confirmed"
                    except Exception:
                        status, result = "interrupted", "Runtime cleanup could not be confirmed"
        self.queue.finish(run, status, result, reply="" if reply.strip() == "NO_ACTION" else reply,
                          assignee=self._assignee(self._issue(issue_id)))

    def _list(self, rows):
        return [{**row, "lane": issue_status(row), "project_id": self.project["id"]} for row in rows]

    def snapshot(self):
        result = self.application.snapshot()
        result["project"] = self.project
        result["mode"] = "本地项目 · SQLite"
        result["tasks"] = self._list(result["tasks"])
        result["capabilities"]["issue_runs"] = True
        return result

    def dispatch(self, operation, payload):
        if operation in ("answer", "resume", "review", "revision"):
            raise ValueError("Use the issue status and run controls")
        if operation == "create":
            if set(payload) - {"title", "prompt", "agent", "squad", "from_group", "group_id", "no_memory"}:
                raise ValueError("Invalid issue fields")
            agents = self._agents()
            squad = self.application.registry.squad
            run_id = None

            def enqueue(connection, issue_id):
                nonlocal run_id
                run_id = self.queue.create(connection, issue_id, agents, squad)

            draft = self.application.create_task(payload, after_insert=enqueue)
            draft["task"]["project_id"] = self.project["id"]
            if run_id:
                draft["run_id"] = run_id
                self.wake.set()
            return draft
        if operation == "tasks":
            return {"tasks": self._list(self.application.dispatch(operation, payload)["tasks"])}
        if operation == "task":
            detail = self.application.dispatch(operation, payload)
            detail["lane"] = issue_status({**detail["current"], "issue_status": detail["issue"].get("issue_status")})
            issue_id = detail["issue"]["id"]
            detail["project"] = self.project
            detail["issue"]["project_id"] = self.project["id"]
            detail["executions"] = [{**run, "output_dir": str(self._outputs(issue_id, run["id"]))}
                                    for run in self.queue.list(issue_id)]
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
            notify = payload.get("notify", True)
            if set(payload) - {"task_id", "message", "notify"} or len(content) > 8000 or type(notify) is not bool:
                raise ValueError("Invalid comment")
            result = self.queue.comment(issue_id, content, agents, targets=None if notify else (),
                                        assignee=agent.name if agent else None)
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

"""C11 serial DAG coordinator. Harness completion needs independent evidence."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from evo.domain.models import Experience, Message, Task
from evo.domain.statuses import TaskStatus
from evo.domain.policies import DEFAULT_LIMITS, validate_execution_budget
from evo.ports.harness import ArtifactRef, ResumeRef, RunRequest, ToolCall, identifier


@dataclass(frozen=True)
class ArtifactCheck:
    path: str
    purpose: str = "document"
    contains: str = ""
    exact: str | None = None

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path or not isinstance(self.contains, str) or (
                self.exact is not None and not isinstance(self.exact, str)):
            raise ValueError("Artifact check needs a path and valid expected content")

    def verify(self, workspace, artifacts: tuple[ArtifactRef, ...]) -> bool:
        # A pre-existing file and a model-provided evidence string are insufficient.
        artifact = next((item for item in artifacts
                         if item.path == self.path and item.purpose == self.purpose), None)
        if artifact is None:
            return False
        try:
            content = workspace.read_file(self.path, purpose=self.purpose)
        except (OSError, ValueError):
            return False
        return (hashlib.sha256(content.encode("utf-8")).hexdigest() == artifact.sha256
                and (self.exact is None or content == self.exact)
                and self.contains in content)


class Orchestrator:
    """Persist task states; call one scoped Harness per ready subtask."""

    def __init__(self, store, planner, assignment, harness_factory, workspace_factory,
                 *, agents, limits=DEFAULT_LIMITS, memory_factory=None):
        self.store = store
        self.planner = planner
        self.assignment = assignment
        self.harness_factory = harness_factory
        self.workspace_factory = workspace_factory
        agents = tuple(agents)
        self.agents = {agent.name: agent for agent in agents}
        if not self.agents or len(self.agents) != len(agents):
            raise ValueError("Agent registry must be nonempty and unique")
        self.limits = limits
        self.memory_factory = memory_factory
        if memory_factory is not None:
            self.assignment.memory_adjustment = lambda agent, query: self._memory_store(agent).adjustment(query)

    def _memory_store(self, owner):
        memory = self.memory_factory(owner)
        if memory.agent != owner:
            raise ValueError("Memory repository agent scope mismatch")
        return memory

    def _memory_enabled(self, task_id):
        task = self.store.get_task(task_id)
        while task["parent_id"]:
            task = self.store.get_task(task["parent_id"])
        config = self._last_event(task["id"], "run_config")
        return self.memory_factory is not None and (config is None or config.get("memory_enabled", True))

    def _remember(self, task_id):
        task = self.store.get_task(task_id)
        if (not self._memory_enabled(task_id) or task["owner"] not in self.agents
                or task["status"] not in ("success", "failed", "blocked")
                or self._last_event(task_id, "memory_recorded") is not None):
            return
        verified = self._last_event(task_id, "objective_verified")
        status = TaskStatus(task["status"])
        evidence = verified["path"] if verified else ""
        if status == TaskStatus.SUCCESS:
            lesson = ("Verified artifact at " + evidence if evidence else
                      "Subtask completed; final delivery is verified at the root task")
            category = None
        else:
            category = "verification" if task["result"] == "Objective evidence missing" else (
                "approval" if status == TaskStatus.BLOCKED else "execution")
            lesson = "Caution: " + category + " failure; verify the task and its evidence before retrying."
        self._memory_store(task["owner"]).add(Experience(
            task["owner"], task["description"], status, lesson, evidence, category))
        self._event(task_id, "memory_recorded", {"outcome": status.value, "failure_category": category})

    def _event(self, task_id, kind, payload):
        self.store.append_event(task_id=task_id, event_type=kind, actor="orchestrator",
                                payload=payload)

    def _last_event(self, task_id, kind):
        matches = [item for item in self.store.list_events(task_id)
                   if item["event_type"] == kind]
        return matches[-1]["payload"] if matches else None

    def _move(self, task_id, target, *, owner=None, result=""):
        current = self.store.get_task(task_id)["status"]
        return self.store.transition_task(task_id, target, expected=current,
                                          owner=owner, result=result)

    def run(self, goal: str, *, checks: dict[str, ArtifactCheck] | None = None,
            final_check: ArtifactCheck | None = None,
            actions: dict[str, tuple[ToolCall, ...]] | None = None,
            required_inputs=None, task_id: str | None = None,
            memory_enabled: bool = True):
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Goal cannot be empty")
        checks = {} if checks is None else checks
        if final_check is not None and not isinstance(final_check, ArtifactCheck):
            raise ValueError("Invalid final delivery check")
        if final_check is not None and checks:
            raise ValueError("Use either final delivery or subtask checks")
        if not isinstance(checks, dict) or not all(
                isinstance(k, str) and isinstance(v, ArtifactCheck)
                for k, v in checks.items()):
            raise ValueError("Checks must name artifact checks by subtask ID")
        actions = actions or {}
        if not isinstance(actions, dict) or not all(
                isinstance(k, str) and isinstance(v, tuple)
                and all(isinstance(item, ToolCall) for item in v)
                for k, v in actions.items()):
            raise ValueError("Invalid action plan")
        task_id = task_id or uuid.uuid4().hex
        identifier(task_id)
        if len(task_id) > 110:
            raise ValueError("Task identifier is too long for subtasks")
        if type(memory_enabled) is not bool:
            raise ValueError("Invalid memory mode")
        try:
            existing = self.store.get_task(task_id)
        except KeyError:
            existing = None
        if existing is None:
            self.store.save_task(Task(task_id, goal.strip()))
        elif (existing["status"] != "created" or existing["parent_id"]
              or existing["prompt"] != goal.strip()):
            raise ValueError("Only a matching draft task can be started")
        self._event(task_id, "run_config", {
            "memory_enabled": memory_enabled,
            "agents": [{"name": agent.name, "specialty": agent.specialty}
                       for agent in self.agents.values()],
            "checks": {key: asdict(value) for key, value in checks.items()},
            "final_check": asdict(final_check) if final_check else None,
            "actions": {key: [asdict(x) for x in value] for key, value in actions.items()},
        })
        self._move(task_id, "planning")
        return self._plan(task_id, goal.strip(), checks, actions, required_inputs, final_check)

    def _plan(self, root_id, goal, checks, actions, required_inputs=None, final_check=None):
        try:
            plan = self.planner.plan(goal, task_id=root_id, required_inputs=required_inputs)
            if plan.question:
                self._event(root_id, "clarification_requested",
                            {"request_id": plan.request_id, "question": plan.question})
                self._move(root_id, "waiting_human")
                return self.snapshot(root_id)
            if len(plan.subtasks) > self.limits.max_total_tasks:
                raise ValueError("Task limit exceeded")
            unknown = set(checks) - {s.id for s in plan.subtasks}
            if unknown or set(actions) - {s.id for s in plan.subtasks}:
                raise ValueError("Check or action references an unknown subtask")
            self._event(root_id, "plan_created", {
                "subtasks": [asdict(s) for s in plan.subtasks],
                "checks": {key: asdict(value) for key, value in checks.items()},
                "final_check": asdict(final_check) if final_check else None,
                "actions": {key: [asdict(x) for x in value] for key, value in actions.items()},
            })
            self._move(root_id, "ready")
            self._move(root_id, "assigned", owner="collective")
            self._move(root_id, "running")
            return self._drive(root_id)
        except Exception:
            if self.store.get_task(root_id)["status"] == "planning":
                self._move(root_id, "failed", result="Planning failed")
            raise

    def _request(self, task_id, owner, description, actions):
        workspace = self.workspace_factory(owner)
        memory = ()
        if self._memory_enabled(task_id):
            saved = self._last_event(task_id, "memory_context")
            if saved is not None:
                memory = tuple(saved["lessons"])
            else:
                recalled = self._memory_store(owner).recall(description)
                memory = tuple(("Prior verified success: " if item.outcome == TaskStatus.SUCCESS
                                else "Caution from prior " + item.outcome.value + ": ") + item.lesson
                               for item in recalled)
                self._event(task_id, "memory_context", {"count": len(memory),
                                                          "outcomes": [item.outcome.value for item in recalled],
                                                          "lessons": list(memory)})
        request = RunRequest(task_id, owner, task_id, workspace.workspace,
                             description, role=self.agents[owner].specialty,
                             memory=memory,
                             actions=tuple(ToolCall(**item) for item in actions))
        return workspace, request

    def _execute(self, task_id, owner, description, check, actions, *, resume=False):
        workspace, request = self._request(task_id, owner, description, actions)
        validate_execution_budget(0, len(request.actions), 0,
                                  sum(x.name == "search" for x in request.actions), self.limits)
        harness = self.harness_factory(task_id, owner, workspace)
        try:
            if resume:
                paused = self._last_event(task_id, "harness_paused")
                if paused is None:
                    raise ValueError("No recorded Harness pause")
                handle = harness.resume(ResumeRef(**paused["resume_ref"]), request)
            else:
                handle = harness.start(request)
            self._event(task_id, "harness_started", {"run_id": handle.run_id})
            while True:
                outcome = harness.wait(handle, timeout=0.5)
                if outcome is not None:
                    break
                child = self.store.get_task(task_id)
                root = self.store.get_task(child["parent_id"]) if child["parent_id"] else child
                if child["status"] == "blocked" or root["status"] == "blocked":
                    harness.cancel(handle)
                    return
            events = harness.events(handle)
            steps = sum(e.kind == "tool_call" for e in events)
            searches = sum(e.kind == "tool_call" and e.payload.get("name") == "search"
                           for e in events)
            validate_execution_budget(0, steps, 0, searches, self.limits)
            if outcome.status == "waiting_human" and outcome.resume_ref:
                pending_ids = [e.payload.get("request_id") for e in events
                               if e.kind == "tool_result" and e.payload.get("request_id")]
                self._event(task_id, "harness_paused",
                            {"resume_ref": asdict(outcome.resume_ref),
                             "request_id": pending_ids[-1] if pending_ids else None})
                if self.store.get_task(task_id)["status"] == "running":
                    self._move(task_id, "waiting_human")
                return
            if outcome.status == "success":
                self._event(task_id, "artifacts_produced",
                            {"artifacts": [asdict(item) for item in outcome.artifacts]})
                if check is not None and check.verify(workspace, outcome.artifacts):
                    self._event(task_id, "objective_verified", asdict(check))
                    self._move(task_id, "success", result=outcome.text[:4000])
                elif check is None:
                    self._move(task_id, "success", result=outcome.text[:4000])
                else:
                    self._move(task_id, "failed", result="Objective evidence missing")
            elif outcome.status == "blocked":
                self._move(task_id, "blocked", result="Execution blocked")
            else:
                self._move(task_id, "failed", result="Harness " + outcome.status[:100])
        except Exception:
            if self.store.get_task(task_id)["status"] == "running":
                self._move(task_id, "failed", result="Execution or verification failed")
            raise

    def _drive(self, root_id):
        plan = self._last_event(root_id, "plan_created")
        if plan is None:
            raise ValueError("No durable task plan")
        for item in plan["subtasks"]:
            if self.store.get_task(root_id)["status"] == "blocked":
                return self.snapshot(root_id)
            child_id = root_id + "-" + item["id"]
            try:
                child = self.store.get_task(child_id)
            except KeyError:
                self.store.save_task(Task(child_id, item["description"], parent_id=root_id))
                child = self.store.get_task(child_id)
            if child["status"] in ("success", "failed", "blocked"):
                continue
            if child["status"] == "running" and self._last_event(child_id, "harness_started"):
                # A process may have died after side effects but before recording
                # the result. Never start a second Harness for that task.
                self._move(child_id, "blocked", result="In-flight outcome needs reconciliation")
                continue
            dependencies = [self.store.get_task(root_id + "-" + dep)
                            for dep in item["depends_on"]]
            if any(dep["status"] != "success" for dep in dependencies):
                self._move(child_id, "blocked", result="Dependency did not succeed")
                continue
            from evo.domain.models import Subtask
            if child["status"] == "created":
                try:
                    self._move(child_id, "ready")
                    owner = self.assignment.assign(
                        Subtask(item["id"], item["description"], tuple(item["skills"]),
                                tuple(item["depends_on"])), root_id,
                        memory_enabled=self._memory_enabled(root_id)).owner
                    if owner not in self.agents:
                        raise ValueError("Assignment selected unknown agent")
                    self._move(child_id, "assigned", owner=owner)
                    self._move(child_id, "running")
                except Exception as exc:
                    from evo.application.assignment import AssignmentError
                    detail = ({"reason": exc.code, "agent": exc.agent}
                              if isinstance(exc, AssignmentError) else {"reason": "internal_error"})
                    self._event(child_id, "orchestration_error",
                                {"code": "assignment_failed", **detail})
                    self._move(child_id, "failed", result="Assignment failed")
                    continue
            else:
                owner = child["owner"]
                if child["status"] == "assigned":
                    self._move(child_id, "running")
            resume = child["status"] == "waiting_human"
            if resume:
                self._move(child_id, "running")
            check = ArtifactCheck(**plan["checks"][item["id"]]) if item["id"] in plan["checks"] else None
            description = item["description"]
            if plan.get("final_check") and item["id"] == plan["subtasks"][-1]["id"]:
                delivery = ArtifactCheck(**plan["final_check"])
                description += ("\nFinal delivery: write " + delivery.path +
                                " as a " + delivery.purpose + " artifact. " +
                                ("It must contain: " + delivery.contains if delivery.contains else ""))
            try:
                self._execute(child_id, owner, description, check,
                              plan["actions"].get(item["id"], ()), resume=resume)
            except Exception:
                self._event(child_id, "orchestration_error", {"code": "execution_failed"})
                if self.store.get_task(child_id)["status"] not in ("failed", "blocked"):
                    self._move(child_id, "failed", result="Execution failed")
            self._remember(child_id)
            if self.store.get_task(child_id)["status"] == "waiting_human":
                self._move(root_id, "waiting_human")
                return self.snapshot(root_id)
        statuses = [row["status"] for row in self.store.list_children(root_id)]
        if self.store.get_task(root_id)["status"] == "blocked":
            return self.snapshot(root_id)
        final = "success" if statuses and all(x == "success" for x in statuses) else (
            "blocked" if any(x == "blocked" for x in statuses) else "failed")
        if final == "success" and plan.get("final_check"):
            return self._verify_final_delivery(root_id, ArtifactCheck(**plan["final_check"]))
        self._move(root_id, final, result="Subtasks: " + ", ".join(statuses))
        return self.snapshot(root_id)

    def _verify_final_delivery(self, root_id, check):
        """Verify a recorded artifact after all subtasks; ask before a format change."""
        candidates = []
        for child in self.store.list_children(root_id):
            recorded = self._last_event(child["id"], "artifacts_produced")
            if not recorded:
                continue
            workspace = self.workspace_factory(child["owner"])
            for raw in recorded["artifacts"]:
                if raw["purpose"] != check.purpose:
                    continue
                actual = ArtifactCheck(raw["path"], raw["purpose"], check.contains, check.exact)
                if actual.verify(workspace, (ArtifactRef(**raw),)):
                    candidates.append((child, raw))
        exact = next(((child, raw) for child, raw in reversed(candidates)
                      if raw["path"] == check.path), None)
        if exact:
            self._event(root_id, "objective_verified",
                        {**asdict(check), "agent": exact[0]["owner"]})
            self._move(root_id, "success", result="Final artifact: " + check.path)
            return self.snapshot(root_id)
        stem = PurePosixPath(check.path).stem
        alternative = next(((child, raw) for child, raw in reversed(candidates)
                            if PurePosixPath(raw["path"]).stem == stem), None)
        if alternative is None:
            alternative = next(reversed(candidates), None)
        if alternative is None:
            self._move(root_id, "failed", result="Final delivery evidence missing")
            return self.snapshot(root_id)
        child, raw = alternative
        prior = self._last_event(root_id, "format_confirmation_requested")
        if prior:
            decision = self.store.get_approval_request(prior["request_id"])
            if (prior["path"] != raw["path"] or prior["sha256"] != raw["sha256"]):
                self._move(root_id, "failed", result="Final artifact changed after format decision")
            elif decision["status"] == "answered" and decision["answer"]["answer"] == "accept":
                self._event(root_id, "objective_verified", {**asdict(check),
                            "actual_path": raw["path"], "agent": child["owner"],
                            "format_approved": True})
                self._move(root_id, "success", result="Final artifact: " + raw["path"])
            elif decision["status"] == "answered":
                self._move(root_id, "failed", result="Alternative delivery format declined")
            else:
                self._move(root_id, "waiting_human")
            return self.snapshot(root_id)
        request_id = str(uuid.uuid4())
        self.store.save_approval_request(request_id=request_id, task_id=root_id,
                                         agent=child["owner"], kind="clarification",
                                         action="delivery_format",
                                         payload={"expected": check.path, "actual": raw["path"],
                                                  "question": "预期 " + check.path + "，实际得到 " + raw["path"] +
                                                              "。是否接受该格式交付？"})
        self._event(root_id, "format_confirmation_requested",
                    {"request_id": request_id, "path": raw["path"], "sha256": raw["sha256"]})
        self._move(root_id, "waiting_human")
        return self.snapshot(root_id)

    def resume(self, root_id):
        root = self.store.get_task(root_id)
        if root["status"] != "waiting_human":
            raise ValueError("Task is not waiting for a human")
        if self._last_event(root_id, "plan_created") is None:
            requested = self._last_event(root_id, "clarification_requested")
            answer = self.store.get_approval_request(requested["request_id"])
            if answer["status"] != "answered" or not answer["answer"]:
                raise ValueError("Clarification has not been answered")
            config = self._last_event(root_id, "run_config")
            self._move(root_id, "planning")
            return self._plan(root_id, root["description"] + "\nClarification: " +
                              answer["answer"]["answer"],
                              {k: ArtifactCheck(**v) for k, v in config["checks"].items()},
                              {k: tuple(ToolCall(**x) for x in v) for k, v in config["actions"].items()},
                              final_check=(ArtifactCheck(**config["final_check"])
                                           if config.get("final_check") else None))
        pending = next((row for row in self.store.list_children(root_id)
                        if row["status"] == "waiting_human"), None)
        if pending:
            paused = self._last_event(pending["id"], "harness_paused")
            if paused and paused.get("request_id"):
                decision = self.store.get_approval_request(paused["request_id"])
                if decision["status"] == "pending":
                    raise ValueError("Human decision is still pending")
        format_request = self._last_event(root_id, "format_confirmation_requested")
        if format_request and self.store.get_approval_request(format_request["request_id"])["status"] == "pending":
            raise ValueError("Delivery format decision is still pending")
        self._move(root_id, "running")
        return self._drive(root_id)

    def delegate(self, parent_id: str, target: str, description: str,
                 check: ArtifactCheck):
        """Execute one bounded agent-to-agent delegation with durable messages."""
        parent = self.store.get_task(parent_id)
        if parent["status"] != "running" or parent["owner"] not in self.agents:
            raise ValueError("Only a running agent task can delegate")
        if target not in self.agents or target == parent["owner"]:
            raise ValueError("Invalid delegation target")
        if not isinstance(description, str) or not description.strip() or len(description) > 1000:
            raise ValueError("Invalid delegation description")
        if not isinstance(check, ArtifactCheck):
            raise ValueError("Delegation requires objective evidence")
        prior = self._last_event(parent_id, "delegation_created")
        depth = prior["depth"] if prior else 0
        validate_execution_budget(0, 0, depth + 1, limits=self.limits)
        root = parent
        while root["parent_id"]:
            root = self.store.get_task(root["parent_id"])
        def count(task_id):
            return 1 + sum(count(row["id"]) for row in self.store.list_children(task_id))
        validate_execution_budget(count(root["id"]) + 1, 0, depth + 1, limits=self.limits)
        child_id = uuid.uuid4().hex
        self.store.save_task(Task(child_id, description.strip(), parent_id=parent_id))
        self._event(child_id, "delegation_created", {"depth": depth + 1,
                    "from": parent["owner"], "to": target})
        self.store.save_message(Message(uuid.uuid4().hex, "agent", parent["owner"],
                                        description.strip(), target, child_id))
        self._move(child_id, "ready")
        self._move(child_id, "assigned", owner=target)
        self._move(child_id, "running")
        try:
            self._execute(child_id, target, description.strip(), check, ())
        except Exception:
            if self.store.get_task(child_id)["status"] == "running":
                self._move(child_id, "failed", result="Delegated execution failed")
        self._remember(child_id)
        outcome = self.store.get_task(child_id)
        self.store.save_message(Message(uuid.uuid4().hex, "agent", target,
                                        outcome["status"] + ": " + outcome["result"][:1000],
                                        parent["owner"], child_id))
        return outcome

    def snapshot(self, root_id):
        return {"task": self.store.get_task(root_id),
                "subtasks": list(self.store.list_children(root_id))}

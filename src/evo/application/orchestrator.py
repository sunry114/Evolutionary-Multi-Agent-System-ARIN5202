"""C11 serial DAG coordinator. Harness completion needs independent evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from evo.adapters.llm.client import ProviderError
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.adapters.tools.filesystem import validate_file_path
from evo.application.artifact_checks import ArtifactCheck, SLIDE_FORMAT
from evo.application.negotiation import AssignmentBlock, AssignmentClarification, NegotiationService

from evo.domain.models import Experience, Subtask, Task
from evo.domain.routing import RoutingMode
from evo.domain.statuses import TaskStatus
from evo.domain.policies import DEFAULT_LIMITS, validate_execution_budget
from evo.ports.harness import ResumeRef, RunRequest, ToolCall, identifier


class Orchestrator:
    """Persist task states; call one scoped Harness per ready subtask."""

    def __init__(self, store, planner, harness_factory, workspace_factory,
                 *, agents, negotiation_provider, squad=None, direct_assignee=None,
                 limits=DEFAULT_LIMITS, memory_factory=None):
        self.store = store
        self.planner = planner
        self.negotiation_provider = negotiation_provider
        self.harness_factory = harness_factory
        self.workspace_factory = workspace_factory
        agents = tuple(agents)
        self.agents = {agent.name: agent for agent in agents}
        if not self.agents or len(self.agents) != len(agents):
            raise ValueError("Agent registry must be nonempty and unique")
        self.squad = squad
        self.routing_mode = RoutingMode.SQUAD if squad is not None else RoutingMode.DIRECT
        self.direct_assignee = direct_assignee
        self.limits = limits
        self.memory_factory = memory_factory

    def _memory_store(self, owner):
        memory = self.memory_factory(owner)
        if memory.agent != owner:
            raise ValueError("Memory repository agent scope mismatch")
        return memory

    def _memory_enabled(self, root_id):
        return (self.memory_factory is not None
                and self._last_event(root_id, "run_config")["memory_enabled"])

    def _recall(self, root_id, owner, description):
        recalled = self._memory_store(owner).recall(description) if self._memory_enabled(root_id) else ()
        lessons = tuple(("Prior verified success: " if item.outcome == TaskStatus.SUCCESS
                         else "Caution from prior " + item.outcome.value + ": ") + item.lesson
                        for item in recalled)
        issue_id = self.store.issue_id(root_id) if recalled else root_id
        cross_task = sum(item.source_issue_id is not None and item.source_issue_id != issue_id
                         for item in recalled)
        return lessons, cross_task

    def _remember(self, task_id):
        task = self.store.get_task(task_id)
        root_id = self.store.task_chain(task_id)[-1]["id"]
        if (not self._memory_enabled(root_id) or task["owner"] not in self.agents
                or task["status"] not in ("success", "failed", "blocked")
                or self._last_event(task_id, "memory_recorded") is not None):
            return
        verified = self._last_event(task_id, "objective_verified")
        status = TaskStatus(task["status"])
        evidence = verified["path"] if verified else ""
        if status == TaskStatus.SUCCESS:
            lesson = "Verified artifact at " + evidence
            category = None
        else:
            category = "verification" if task["result"] == "Objective evidence missing" else (
                "approval" if status == TaskStatus.BLOCKED else "execution")
            lesson = "Caution: " + category + " failure; verify the task and its evidence before retrying."
        self._memory_store(task["owner"]).add(Experience(
            task["owner"], task["description"][:1000], status, lesson, evidence, category,
            source_issue_id=self.store.issue_id(root_id)))
        self._event(task_id, "memory_recorded", {"outcome": status.value, "failure_category": category})

    def _event(self, task_id, kind, payload, *, actor="orchestrator"):
        self.store.append_event(task_id=task_id, event_type=kind, actor=actor,
                                payload=payload)

    def _last_event(self, task_id, kind):
        return self.store.latest_event_payload(task_id, kind)

    def _move(self, task_id, target, *, owner=None, result=""):
        current = self.store.get_task(task_id)["status"]
        return self.store.transition_task(task_id, target, expected=current,
                                          owner=owner, result=result)

    def start(self, task_id: str, *, checks: dict[str, ArtifactCheck],
              actions: dict[str, tuple[ToolCall, ...]] | None = None,
              required_inputs=None, memory_enabled: bool = True, group_context=None,
              expected_updated_at=None):
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
        identifier(task_id)
        if len(task_id) > 110:
            raise ValueError("Task identifier is too long for subtasks")
        if type(memory_enabled) is not bool:
            raise ValueError("Invalid memory mode")
        task = self.store.get_task(task_id)
        if task["parent_id"] is not None:
            raise ValueError("Start the root issue")
        config = {
            "group_context": group_context,
            "memory_enabled": memory_enabled,
            "agents": [asdict(agent) for agent in self.agents.values()],
            "squad": self.squad,
            "routing_mode": self.routing_mode.value,
            "checks": {key: asdict(value) for key, value in checks.items()},
            "actions": {key: [asdict(x) for x in value] for key, value in actions.items()},
        }
        if self.routing_mode == RoutingMode.DIRECT:
            config["direct_assignee"] = self.direct_assignee
        revision = task["updated_at"] if expected_updated_at is None else expected_updated_at
        if not self.store.claim_start(task_id, config, expected_updated_at=revision):
            return self.snapshot(task_id)
        return self._plan(task_id, task["description"], checks, actions, required_inputs, group_context)

    def _plan(self, root_id, goal, checks, actions, required_inputs=None, group_context=None):
        try:
            options = {}
            if group_context is not None or checks:
                options = {"group_context": group_context,
                           "acceptance": {key: asdict(check) for key, check in checks.items()}}
            actor = self.planner.actor.name if self.planner.actor else "planner"
            plan = self.planner.plan(
                goal, task_id=root_id, required_inputs=required_inputs, **options,
                on_model_call=lambda: self._event(root_id, "routing_call", {"phase": "planning"}, actor=actor))
            if group_context is not None and not checks and not plan.question:
                checks = plan.checks
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
                "goal": goal,
                "subtasks": [asdict(s) for s in plan.subtasks],
                "checks": {key: asdict(value) for key, value in checks.items()},
                "actions": {key: [asdict(x) for x in value] for key, value in actions.items()},
            }, actor=actor)
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
        root_id = self.store.task_chain(task_id)[-1]["id"]
        if self._memory_enabled(root_id):
            saved = self._last_event(task_id, "memory_context")
            if saved is not None:
                memory = tuple(saved["lessons"])
            else:
                memory, cross_task = self._recall(root_id, owner, description)
                self._event(task_id, "memory_context", {
                    "count": len(memory), "cross_task": cross_task, "lessons": list(memory)})
        request = RunRequest(task_id, owner, task_id, workspace.workspace,
                             description, role=self.agents[owner].specialty,
                             memory=memory,
                             actions=tuple(ToolCall(**item) for item in actions))
        return workspace, request

    def _execute(self, task_id, owner, description, check, actions, *, paused=None, delivery=None):
        if check is not None:
            description += "\nArtifact acceptance: " + json.dumps(asdict(check), ensure_ascii=False)
            if check.purpose == "slides":
                description += "\n" + SLIDE_FORMAT
        workspace, request = self._request(task_id, owner, description, actions)
        validate_execution_budget(0, len(request.actions), 0,
                                  sum(x.name == "search" for x in request.actions), self.limits)
        harness = self.harness_factory(task_id, owner, workspace)
        try:
            if paused is not None:
                handle = harness.resume(ResumeRef(**paused["resume_ref"]), request)
            else:
                handle = harness.start(request)
            self._event(task_id, "harness_started", {"run_id": handle.run_id})
            outcome = harness.wait(handle, timeout=None)
            if outcome is None:
                outcome = harness.cancel(handle)
            events = harness.events(handle)
            steps = sum(e.kind == "tool_call" for e in events)
            searches = sum(e.kind == "tool_call" and e.payload.get("name") == "search"
                           for e in events)
            validate_execution_budget(0, steps, 0, searches, self.limits)
            if outcome.status in ("waiting_human", "waiting_dependency") and outcome.resume_ref:
                key = "delegation_id" if outcome.status == "waiting_dependency" else "request_id"
                continuation = next((e.payload[key] for e in reversed(events)
                                     if e.kind == "tool_result" and e.payload.get(key)), None)
                if outcome.status == "waiting_dependency" and continuation is None:
                    raise ValueError("Delegate checkpoint does not identify the requested task")
                self._event(task_id, "harness_paused",
                            {"resume_ref": asdict(outcome.resume_ref),
                             key: continuation})
                if outcome.status == "waiting_human":
                    reason = "Waiting for human input"
                    if continuation is not None:
                        approval = self.store.get_approval_request(continuation)
                        reason = f"Waiting for approval {continuation} to {approval['action']} ({approval['agent']})"
                    if self.store.get_task(task_id)["status"] == "running":
                        self._move(task_id, "waiting_human", result=reason)
                    else:
                        self.store.describe_wait(task_id, reason)
                return outcome.status
            if outcome.status == "success":
                if check is not None and check.verify(workspace, outcome.artifacts):
                    verified = asdict(check)
                    evidence = next(ref for ref in outcome.artifacts
                                    if ref.path == check.path and ref.purpose == check.purpose)
                    recipient = delivery["sender"] if delivery else owner
                    archive_id = task_id if delivery else hashlib.sha256(
                        ("verification:" + task_id).encode()).hexdigest()[:32]
                    artifact = ArtifactExchange(workspace).publish_verified(
                        check.path, recipient, purpose=check.purpose, artifact_id=archive_id, digest=evidence.sha256)
                    verified["artifact"] = {"id": artifact.id, "name": artifact.name,
                                            "purpose": artifact.purpose, "sha256": artifact.sha256}
                    self._event(task_id, "objective_verified", verified)
                    self._move(task_id, "success", result=outcome.text[:4000])
                else:
                    self._move(task_id, "failed", result="Objective evidence missing")
            else:
                task = self.store.get_task(task_id)
                if task["status"] == "running":
                    status = "blocked" if outcome.status == "blocked" else "failed"
                    reason = "Execution blocked" if status == "blocked" else "Harness " + outcome.status[:100]
                    if paused and paused.get("delegation_id"):
                        child = self.store.get_delegation(paused["delegation_id"])["outcome"]
                        if child is not None and child["status"] != "success":
                            reason = f"Delegated task {child['id']} {child['status']}: {child['result']}"
                    self._move(task_id, status, result=reason[:4000])
        except Exception:
            if self.store.get_task(task_id)["status"] == "running":
                self._move(task_id, "failed", result="Execution or verification failed")
            raise

    def _assign_child(self, root_id, child_id, goal, subtask, check, *, delegation=None):
        try:
            if delegation is not None:
                ancestors = self.store.task_chain(delegation["parent_id"])
                negotiation = NegotiationService(
                    self.negotiation_provider, self.store, self.agents.values(),
                    coordinator=delegation["sender"], initial_member=delegation["recipient"], recall=self._recall,
                    excluded={task["owner"] for task in ancestors})
            else:
                coordinator = self.squad["lead"] if self.squad is not None else self.direct_assignee
                negotiation = NegotiationService(
                    self.negotiation_provider, self.store, self.agents.values(),
                    coordinator=coordinator, direct=self.routing_mode == RoutingMode.DIRECT, recall=self._recall)
            outcome = negotiation.assign(root_id, child_id, goal, subtask, check,
                                         max_rounds=self.limits.max_assignment_rounds)
        except ProviderError:
            self._event(child_id, "orchestration_error", {"code": "assignment_provider_error"})
            return self._move(child_id, "failed", result="Provider failed during assignment")
        except Exception:
            self._event(child_id, "orchestration_error", {"code": "assignment_failed"})
            return self._move(child_id, "failed", result="Assignment failed")

        if isinstance(outcome, AssignmentBlock):
            self._event(root_id, "assignment_blocked",
                        {"task_id": child_id, "subtask_id": subtask.id, "round": outcome.round_number,
                         "code": outcome.code, "reason": outcome.reason}, actor=outcome.actor)
            return self._move(child_id, "blocked", result=outcome.reason)
        if isinstance(outcome, AssignmentClarification):
            self.store.pause_assignment(root_id, child_id, actor=outcome.actor,
                                        question=outcome.question, round_number=outcome.round_number)
            return None
        return self.store.transition_task(
            child_id, "assigned", expected="ready", owner=outcome.owner,
            assignment={"description": outcome.description, "round": outcome.round_number,
                        "reason": outcome.reason, "subtask_id": subtask.id})

    def _drive_execution(self, task, check, actions=(), *, delivery=None):
        task_id = task["id"]
        while task["status"] in ("assigned", "running", "waiting_human"):
            started = self._last_event(task_id, "harness_started")
            paused = self._last_event(task_id, "harness_paused")
            if paused and (started is None or paused["resume_ref"]["run_id"] != started["run_id"]):
                paused = None
            if paused and paused.get("delegation_id"):
                dependency = self._drive_delegation(paused["delegation_id"])
                if dependency["status"] == "waiting_human":
                    reason = f"Delegated task {dependency['id']}: {dependency['result']}"
                    if task["status"] == "running":
                        return self._move(task_id, "waiting_human", result=reason[:4000])
                    self.store.describe_wait(task_id, reason)
                    return self.store.get_task(task_id)
            elif task["status"] == "running" and started:
                return self._move(task_id, "blocked", result="In-flight outcome needs reconciliation")

            if task["status"] in ("assigned", "waiting_human"):
                self._move(task_id, "running")
            try:
                if task["status"] == "waiting_human" and paused is None:
                    raise ValueError("No recorded Harness pause")
                status = self._execute(task_id, task["owner"], task["description"], check, actions,
                                       paused=paused, delivery=delivery)
            except Exception:
                self._event(task_id, "orchestration_error", {"code": "execution_failed"})
                if self.store.get_task(task_id)["status"] == "running":
                    self._move(task_id, "failed", result="Execution failed")
                status = "failed"
            task = self.store.get_task(task_id)
            if status != "waiting_dependency":
                break
        return task

    def _drive_delegation(self, delegation_id):
        delegation = self.store.get_delegation(delegation_id)
        if delegation["outcome"] is not None:
            return delegation["outcome"]
        child = self.store.get_task(delegation_id)
        check = ArtifactCheck(**delegation["check"])
        if child["status"] == "created":
            child = self._move(delegation_id, "ready")
        if child["status"] == "ready":
            parent = self.store.get_task(delegation["parent_id"])
            child = self._assign_child(delegation["issue_id"], delegation_id, parent["description"],
                                       Subtask(delegation_id, child["description"]), check, delegation=delegation)
            if child is None:
                request = self._last_event(delegation["issue_id"], "assignment_clarification_requested")
                return {"id": delegation_id, "status": "waiting_human",
                        "result": "Assignment clarification: " + request["question"]}
        child = self._drive_execution(child, check, delivery=delegation)
        if child["status"] == "waiting_human":
            return child
        verified = self._last_event(delegation_id, "objective_verified")
        result = self.store.complete_delegation(delegation_id, verified["artifact"] if verified else None)
        self._remember(delegation_id)
        return result

    def _drive(self, root_id):
        plan = self._last_event(root_id, "plan_created")
        if plan is None:
            raise ValueError("No durable task plan")
        # Reserve the full plan before a member can consume the shared task budget.
        for item in plan["subtasks"]:
            child_id = root_id + "-" + item["id"]
            try:
                self.store.get_task(child_id)
            except KeyError:
                self.store.save_task(Task(child_id, item["description"], parent_id=root_id))
        for item in plan["subtasks"]:
            child_id = root_id + "-" + item["id"]
            child = self.store.get_task(child_id)
            if child["status"] in ("success", "failed", "blocked"):
                continue
            dependencies = [self.store.get_task(root_id + "-" + dep) for dep in item["depends_on"]]
            failed_dependency = next((dep for dep in dependencies if dep["status"] != "success"), None)
            if failed_dependency:
                self._move(child_id, "blocked", result=(
                    f"Dependency did not succeed: {failed_dependency['id']} ({failed_dependency['status']}). "
                    f"{failed_dependency['result']}")[:4000])
                continue
            if child["status"] == "created":
                child = self._move(child_id, "ready")
            check = ArtifactCheck(**plan["checks"][item["id"]]) if item["id"] in plan["checks"] else None
            if child["status"] == "ready":
                subtask = Subtask(item["id"], item["description"], tuple(item["skills"]), tuple(item["depends_on"]))
                child = self._assign_child(root_id, child_id, plan["goal"], subtask, check)
                if child is None:
                    return self.snapshot(root_id)
            child = self._drive_execution(child, check, plan["actions"].get(item["id"], ()))
            self._remember(child_id)
            if child["status"] == "waiting_human":
                self._move(root_id, "waiting_human", result=child["result"])
                return self.snapshot(root_id)
        children = self.store.list_children(root_id)
        statuses = [row["status"] for row in children]
        final = "success" if statuses and all(x == "success" for x in statuses) else (
            "blocked" if any(x == "blocked" for x in statuses) else "failed")
        summary = "Subtasks: " + ", ".join(statuses)
        if final != "success":
            reasons = [row["id"].removeprefix(root_id + "-") + ": " + row["result"]
                       for row in children if row["status"] in ("blocked", "failed") and row["result"]]
            summary += "\n" + "\n".join(reasons)
        self._move(root_id, final, result=summary[:4000])
        return self.snapshot(root_id)

    def resume(self, root_id):
        root = self.store.get_task(root_id)
        if root["status"] != "waiting_human":
            raise ValueError("Task is not waiting for a human")
        resume_id = hashlib.sha256(f"{root_id}:{root['updated_at']}".encode()).hexdigest()[:32]
        if self._last_event(root_id, "plan_created") is None:
            requested = self._last_event(root_id, "clarification_requested")
            answer = self.store.get_approval_request(requested["request_id"])
            if answer["status"] != "answered" or not answer["answer"]:
                raise ValueError("Clarification has not been answered")
            config = self._last_event(root_id, "run_config")
            clarified_goal = root["description"]
            for event in self.store.list_events(root_id):
                if event["event_type"] == "clarification_requested":
                    resolved = self.store.get_approval_request(event["payload"]["request_id"])
                    if resolved["status"] == "answered":
                        clarified_goal += ("\nPlanner question: " + event["payload"]["question"]
                                           + "\nClarification: " + resolved["answer"]["answer"])
            self.store.transition_task(root_id, "planning", expected="waiting_human",
                                       expected_updated_at=root["updated_at"], resume_id=resume_id)
            return self._plan(root_id, clarified_goal,
                              {k: ArtifactCheck(**v) for k, v in config["checks"].items()},
                              {k: tuple(ToolCall(**x) for x in v) for k, v in config["actions"].items()},
                              group_context=config["group_context"])
        if self.store.pending_in_tree(root_id):
            raise ValueError("Human decision is still pending")
        self.store.transition_task(root_id, "running", expected="waiting_human",
                                   expected_updated_at=root["updated_at"], resume_id=resume_id)
        return self._drive(root_id)

    def request_delegation(self, parent_id, sender, target, description, check, *, request_id=None):
        """Register a member's request. The coordinator executes it after that member parks."""
        if sender not in self.agents or target not in self.agents or sender == target:
            raise ValueError("Invalid delegation member")
        if not isinstance(description, str) or not description.strip() or len(description.strip()) > 1000:
            raise ValueError("Invalid delegation description")
        if not isinstance(check, ArtifactCheck):
            raise ValueError("Delegation requires objective evidence")
        validate_file_path(check.path, check.purpose)
        if len(check.contains.encode()) > 8192 or (check.exact is not None and len(check.exact.encode()) > 8192):
            raise ValueError("Delegation evidence check is oversized")
        description = description.strip()
        check_record = asdict(check)
        identity = json.dumps([parent_id, sender, target, description, check_record], sort_keys=True)
        delegation_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
        if request_id is not None and request_id != delegation_id:
            raise ValueError("Delegation continuation does not match the original request")
        result = self.store.register_delegation(
            delegation_id, parent_id, sender, target, description, check_record,
            max_depth=self.limits.max_delegation_depth, max_tasks=self.limits.max_total_tasks,
            replay=request_id is not None)
        if result is None:
            return {"status": "waiting_dependency", "delegation_id": delegation_id}
        return {"status": result["status"], "delegation_id": delegation_id, "result": result}

    def snapshot(self, root_id):
        return {"task": self.store.get_task(root_id),
                "subtasks": list(self.store.list_children(root_id))}

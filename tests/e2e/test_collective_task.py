"""Scripted C11 flow against real SQLite, with a deterministic Harness."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent, Task
from evo.domain.statuses import TaskStatus
from evo.ports.harness import ArtifactRef, HarnessEvent, ResumeRef, RunHandle, RunResult, ToolCall


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, messages, json_object=False):
        return self.responses.pop(0)


class NegotiationProvider:
    def complete(self, messages, json_object=False):
        if "Nominate one listed member" in messages[0]["content"]:
            return {"decision": "nominate", "member": "builder", "scope": "", "reason": "Suitable role"}
        return {"decision": "accept", "reason": "Can perform this task"}


class Workspace:
    def __init__(self, root, agent):
        self._state_root = root
        self.agent = agent
        self.workspace = root / agent
        self.workspace.mkdir(exist_ok=True)

    def read_file(self, path, *, purpose="document"):
        return (self.workspace / path).read_text()

    def _read_bytes(self, path, *, purpose):
        return (self.workspace / path).read_bytes()

    def write_file(self, path, content):
        (self.workspace / path).write_text(content)


class ScriptedHarness:
    def __init__(self, workspace, script):
        self.workspace = workspace
        self.script = script
        self.request = None

    def start(self, request):
        self.request = request
        return RunHandle(request.task_id)

    def resume(self, reference, request):
        return self.start(request)

    def wait(self, handle, timeout=None):
        status, path, content = self.script(self.request)
        if status == "waiting_human":
            return RunResult(status, resume_ref=ResumeRef(handle.run_id,
                            self.request.task_id, self.request.agent,
                            self.request.session_scope))
        artifacts = ()
        if path:
            self.workspace.write_file(path, content)
            artifacts = (ArtifactRef(path, "document",
                                     hashlib.sha256(content.encode()).hexdigest()),)
        return RunResult(status, text="Model says complete", artifacts=artifacts)

    def events(self, handle):
        return tuple(HarnessEvent(i + 1, "tool_call", {"name": action.name})
                     for i, action in enumerate(self.request.actions))


class CollectiveTaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SQLiteStore(self.root / "events.sqlite3")
        self.agents = (Agent("builder", "Python"), Agent("reviewer", "review"))
        self.workspaces = {}
        self.script = lambda request: ("success", "result.md", "done")

    def workspace(self, agent):
        return self.workspaces.setdefault(agent, Workspace(self.root, agent))

    def make(self, *plan_responses):
        return Orchestrator(self.store,
                            Planner(Provider(*plan_responses), self.agents,
                                    ApprovalService(self.store)),
                            lambda task_id, agent, workspace: ScriptedHarness(workspace, self.script),
                            self.workspace, agents=self.agents, negotiation_provider=NegotiationProvider(),
                            squad={"name": "default", "lead": "reviewer", "members": ["builder", "reviewer"]})

    def test_plan_negotiate_execute_verify_and_trace(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Write", "skills": ["Python"], "depends_on": []},
            {"id": "s2", "description": "Check", "depends_on": ["s1"]}]}
        orchestrator = self.make(plan)
        orchestrator.store.save_task(Task("root", "Produce an artifact"))
        result = orchestrator.start("root", checks={
            "s1": ArtifactCheck("result.md", contains="done"),
            "s2": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual([x["status"] for x in result["subtasks"]], ["success", "success"])
        self.assertEqual(len([x for x in self.store.list_events(result["task"]["id"])
                              if x["event_type"] == "routing_call"]), 5)
        self.assertTrue(all(any(e["event_type"] == "objective_verified"
                                for e in self.store.list_events(child["id"]))
                            for child in result["subtasks"]))

    def test_failure_blocks_dependent_task(self):
        self.script = lambda request: ("failed", "", "")
        plan = {"subtasks": [
            {"id": "s1", "description": "Write"},
            {"id": "s2", "description": "Check", "depends_on": ["s1"]}]}
        orchestrator = self.make(plan)
        orchestrator.store.save_task(Task("root", "Goal"))
        result = orchestrator.start("root", checks={key: ArtifactCheck("result.md", exact="done")
                                                   for key in ("s1", "s2")})
        self.assertEqual([x["status"] for x in result["subtasks"]], ["failed", "blocked"])
        self.assertEqual(result["task"]["status"], "blocked")

    def test_model_success_without_artifact_fails(self):
        self.script = lambda request: ("success", "", "")
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        orchestrator.store.save_task(Task("root", "Goal"))
        result = orchestrator.start("root", checks={"s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(result["subtasks"][0]["status"], "failed")
        self.assertEqual(result["task"]["status"], "failed")

    def test_waiting_human_resumes_once_from_checkpoint(self):
        calls = []
        def script(request):
            calls.append(request.task_id)
            return ("waiting_human", "", "") if len(calls) == 1 else (
                "success", "result.md", "done")
        self.script = script
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        orchestrator.store.save_task(Task("root", "Goal"))
        first = orchestrator.start("root", checks={"s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(first["task"]["status"], "waiting_human")
        second = orchestrator.resume(first["task"]["id"])
        self.assertEqual(second["task"]["status"], "success")
        self.assertEqual(len(calls), 2)

    def test_uncertain_inflight_run_is_not_started_again(self):
        calls = []
        def script(request):
            calls.append(request.task_id)
            return "waiting_human", "", ""
        self.script = script
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        orchestrator.store.save_task(Task("root", "Goal"))
        first = orchestrator.start("root", checks={"s1": ArtifactCheck("result.md", exact="done")})
        child_id = first["subtasks"][0]["id"]
        self.store.transition_task(child_id, "running", expected="waiting_human")
        final = orchestrator.resume(first["task"]["id"])
        self.assertEqual(final["subtasks"][0]["status"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_search_and_step_budgets_fail_before_harness(self):
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Search"}]})
        orchestrator.store.save_task(Task("root", "Goal"))
        result = orchestrator.start("root", checks={"s1": ArtifactCheck("result.md", exact="done")},
                                  actions={"s1": tuple(ToolCall("search", {"query": "x"})
                                                       for _ in range(4))})
        self.assertEqual(result["subtasks"][0]["status"], "failed")
        self.assertEqual(result["task"]["status"], "failed")

    def test_twenty_one_explicit_steps_are_rejected(self):
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        orchestrator.store.save_task(Task("root", "Goal"))
        result = orchestrator.start("root", checks={"s1": ArtifactCheck("result.md", exact="done")},
            actions={"s1": tuple(ToolCall("list_files", {}) for _ in range(21))})
        self.assertEqual(result["subtasks"][0]["status"], "failed")

    def test_assignment_failure_is_recorded_and_propagated(self):
        orchestrator = self.make({"subtasks": [
            {"id": "s1", "description": "One"},
            {"id": "s2", "description": "Two", "depends_on": ["s1"]}]})
        orchestrator.negotiation_provider = Provider({"decision": "invalid", "reason": "x"})
        orchestrator.store.save_task(Task("root", "Goal"))
        result = orchestrator.start("root", checks={})
        self.assertEqual([x["status"] for x in result["subtasks"]], ["failed", "blocked"])

    def test_clarification_answer_replans_with_preserved_check(self):
        orchestrator = self.make(
            {"question": "What filename?", "subtasks": []},
            {"subtasks": [{"id": "s1", "description": "Write"}]})
        orchestrator.store.save_task(Task("root", "Write a report"))
        first = orchestrator.start("root", checks={
            "s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(first["task"]["status"], "waiting_human")
        request = self.store.list_pending_requests()[0]
        ApprovalService(self.store).answer(request["id"], {"answer": "result.md"})
        final = orchestrator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")

    def test_delegate_registration_is_bound_to_owner_and_identical_requests_share_one_child(self):
        self.store.save_task(Task("root", "Goal", status=TaskStatus.RUNNING))
        self.store.save_task(Task("executor", "Work", owner="builder", parent_id="root", status=TaskStatus.RUNNING))
        orchestrator = self.make()
        check = ArtifactCheck("result.md", contains="done")
        with self.assertRaisesRegex(ValueError, "owner"):
            orchestrator.request_delegation("executor", "reviewer", "builder", "delegate", check)
        with self.assertRaisesRegex(ValueError, "member"):
            orchestrator.request_delegation("executor", "builder", "builder", "delegate", check)
        executor = SimpleNamespace(agent="builder", workspace=self.workspace("builder"),
                                   repository=self.store)
        broker = PlatformToolBroker(executor, delegation=lambda task_id, sender, agent, description,
                                    path, contains, *, request_id: orchestrator.request_delegation(
                                        task_id, sender, agent, description,
                                        ArtifactCheck(path, contains=contains), request_id=request_id))
        args = {"agent": "reviewer", "description": "Write delegated artifact", "path": "result.md", "contains": "done"}
        first = broker.call("executor", "delegate", args)
        self.assertEqual(broker.call("executor", "delegate", args), first)
        self.assertEqual(first["status"], "waiting_dependency")
        self.assertEqual(len(self.store.list_children("executor")), 1)
        events = [event for event in self.store.list_events("root") if event["event_type"] == "delegation_requested"]
        self.assertEqual([(event["actor"], event["payload"]["to"]) for event in events], [("builder", "reviewer")])

    def test_delegation_depth_limit_is_enforced(self):
        self.store.save_task(Task("root", "Goal"))
        self.store.save_task(Task("planned", "Planned", parent_id="root"))
        self.store.save_task(Task("first", "First", parent_id="planned"))
        self.store.save_task(Task("deep", "Nested", owner="reviewer", parent_id="first", status=TaskStatus.RUNNING))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.make().request_delegation("deep", "reviewer", "builder", "Too deep",
                                           ArtifactCheck("result.md", exact="done"))

    def test_total_task_limit_rejects_extra_delegation(self):
        self.store.save_task(Task("root", "Goal", owner="builder", status=TaskStatus.RUNNING))
        for index in range(11):
            self.store.save_task(Task("existing%d" % index, "Existing", parent_id="root"))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.make().request_delegation("root", "builder", "reviewer", "One more",
                                           ArtifactCheck("result.md", exact="done"))


if __name__ == "__main__":
    unittest.main()

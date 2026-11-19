"""Scripted C11 flow against real SQLite, with a deterministic Harness."""

import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.assignment import AssignmentService
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent, Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.harness import ArtifactRef, HarnessEvent, ResumeRef, RunHandle, RunResult, ToolCall


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, messages, json_object=False):
        return self.responses.pop(0)


class BidProvider:
    def complete(self, messages, json_object=False):
        return {"score": 4, "reason": "Can perform this task"}


class Workspace:
    def __init__(self, root, agent):
        self.workspace = root / agent
        self.workspace.mkdir(exist_ok=True)

    def read_file(self, path, *, purpose="document"):
        return (self.workspace / path).read_text()

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
                            AssignmentService(BidProvider(), self.agents, self.store),
                            lambda task_id, agent, workspace: ScriptedHarness(workspace, self.script),
                            self.workspace, agents=self.agents)

    def test_plan_bid_execute_verify_and_trace(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Write", "skills": ["Python"], "depends_on": []},
            {"id": "s2", "description": "Check", "depends_on": ["s1"]}]}
        result = self.make(plan).run("Produce an artifact", checks={
            "s1": ArtifactCheck("result.md", contains="done"),
            "s2": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual([x["status"] for x in result["subtasks"]], ["success", "success"])
        self.assertEqual(len([x for x in self.store.list_events(result["task"]["id"])
                              if x["event_type"] == "agent_bid"]), 4)
        self.assertTrue(all(any(e["event_type"] == "objective_verified"
                                for e in self.store.list_events(child["id"]))
                            for child in result["subtasks"]))

    def test_final_delivery_only_after_unchecked_subtasks_complete(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Research"},
            {"id": "s2", "description": "Deliver", "depends_on": ["s1"]}]}
        self.script = lambda request: ("success", "", "") if request.task_id.endswith("-s1") else (
            "success", "report.md", "done")
        result = self.make(plan).run("Goal", final_check=ArtifactCheck("report.md"))
        self.assertEqual([row["status"] for row in result["subtasks"]], ["success", "success"])
        self.assertEqual(result["task"]["status"], "success")
        self.assertTrue(any(event["event_type"] == "objective_verified"
                            for event in self.store.list_events(result["task"]["id"])))
        self.assertFalse(any(event["event_type"] == "objective_verified"
                             for event in self.store.list_events(result["subtasks"][0]["id"])))

    def test_alternative_format_requires_human_acceptance(self):
        self.script = lambda request: ("success", "report.html", "done")
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Deliver"}]})
        first = coordinator.run("Goal", final_check=ArtifactCheck("report.md", contains="done"))
        self.assertEqual(first["task"]["status"], "waiting_human")
        request = self.store.list_pending_requests()[0]
        self.assertEqual(request["payload"]["actual"], "report.html")
        with self.assertRaises(ValueError):
            coordinator.resume(first["task"]["id"])
        ApprovalService(self.store).answer(request["id"], {"answer": "accept"})
        final = coordinator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertEqual(final["task"]["result"], "Final artifact: report.html")

    def test_web_answer_accepts_format_and_finishes_without_second_click(self):
        self.script = lambda request: ("success", "report.html", "done")
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Deliver"}]})
        first = coordinator.run("Goal", final_check=ArtifactCheck("report.md"))
        app = SQLiteApplication(self.root, provider=BidProvider(), agents=self.agents)
        request = app.snapshot()["requests"][0]
        with patch.object(app, "_coordinator", return_value=coordinator):
            with self.assertRaises(ValueError):
                app.dispatch("answer", {"request_id": request["id"], "answer": "maybe"})
            result = app.dispatch("answer", {"request_id": request["id"], "answer": "accept"})
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(app.store.get_task(first["task"]["id"])["status"], "success")

    def test_alternative_format_decline_and_missing_content(self):
        self.script = lambda request: ("success", "report.html", "done")
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Deliver"}]})
        first = coordinator.run("Goal", final_check=ArtifactCheck("report.md"))
        request = self.store.list_pending_requests()[0]
        ApprovalService(self.store).answer(request["id"], {"answer": "decline"})
        self.assertEqual(coordinator.resume(first["task"]["id"])["task"]["status"], "failed")
        second = self.make({"subtasks": [{"id": "s1", "description": "Deliver"}]}).run(
            "Another goal", final_check=ArtifactCheck("report.md", contains="missing"))
        self.assertEqual(second["task"]["status"], "failed")

    def test_terminating_running_task_cancels_harness(self):
        waiting, cancelled = threading.Event(), threading.Event()
        class WaitingHarness(ScriptedHarness):
            def wait(self, handle, timeout=None):
                waiting.set()
                cancelled.wait(timeout)
                return None
            def cancel(self, handle):
                cancelled.set()
                return RunResult("cancelled")
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        coordinator.harness_factory = lambda task_id, agent, workspace: WaitingHarness(workspace, self.script)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(coordinator.run, "Goal", checks={"s1": ArtifactCheck("result.md", exact="done")},
                                 task_id="stop-test")
            self.assertTrue(waiting.wait(3))
            self.store.terminate_task("stop-test")
            result = future.result(timeout=3)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["task"]["status"], "blocked")
        self.assertEqual(self.store.get_task("stop-test-s1")["status"], "blocked")

    def test_failure_blocks_dependent_task(self):
        self.script = lambda request: ("failed", "", "")
        plan = {"subtasks": [
            {"id": "s1", "description": "Write"},
            {"id": "s2", "description": "Check", "depends_on": ["s1"]}]}
        result = self.make(plan).run("Goal", checks={"s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual([x["status"] for x in result["subtasks"]], ["failed", "blocked"])
        self.assertEqual(result["task"]["status"], "blocked")

    def test_model_success_without_artifact_fails(self):
        self.script = lambda request: ("success", "", "")
        result = self.make({"subtasks": [{"id": "s1", "description": "Write"}]}).run(
            "Goal", checks={"s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(result["subtasks"][0]["status"], "failed")
        self.assertEqual(result["task"]["status"], "failed")

    def test_harness_failure_records_safe_error_code(self):
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        class ModelFailureHarness(ScriptedHarness):
            def wait(self, handle, timeout=None):
                return RunResult("failed", error="model_unavailable", exit_code=1)
        coordinator.harness_factory = lambda task_id, agent, workspace: ModelFailureHarness(workspace, self.script)
        result = coordinator.run("Goal", final_check=ArtifactCheck("result.md"))
        child = result["subtasks"][0]
        self.assertEqual(child["result"], "Harness failed: model_unavailable")
        events = self.store.list_events(child["id"])
        self.assertIn({"code": "model_unavailable", "exit_code": 1},
                      [event["payload"] for event in events if event["event_type"] == "harness_error"])

    def test_harness_event_limit_is_not_hidden_as_execution_failed(self):
        coordinator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})

        class EventLimitHarness(ScriptedHarness):
            def wait(self, handle, timeout=None):
                return RunResult("failed", error="event_limit")

        coordinator.harness_factory = lambda task_id, agent, workspace: EventLimitHarness(workspace, self.script)
        result = coordinator.run("Goal", final_check=ArtifactCheck("result.md"))
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: event_limit")

    def test_waiting_human_resumes_once_from_checkpoint(self):
        calls = []
        def script(request):
            calls.append(request.task_id)
            return ("waiting_human", "", "") if len(calls) == 1 else (
                "success", "result.md", "done")
        self.script = script
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Write"}]})
        first = orchestrator.run("Goal", checks={"s1": ArtifactCheck("result.md", exact="done")})
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
        first = orchestrator.run("Goal", checks={"s1": ArtifactCheck("result.md", exact="done")})
        child_id = first["subtasks"][0]["id"]
        self.store.transition_task(child_id, "running", expected="waiting_human")
        final = orchestrator.resume(first["task"]["id"])
        self.assertEqual(final["subtasks"][0]["status"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_search_and_step_budgets_fail_before_harness(self):
        orchestrator = self.make({"subtasks": [{"id": "s1", "description": "Search"}]})
        result = orchestrator.run("Goal", checks={"s1": ArtifactCheck("result.md", exact="done")},
                                  actions={"s1": tuple(ToolCall("search", {"query": "x"})
                                                       for _ in range(4))})
        self.assertEqual(result["subtasks"][0]["status"], "failed")
        self.assertEqual(result["task"]["status"], "failed")

    def test_twenty_one_explicit_steps_are_rejected(self):
        result = self.make({"subtasks": [{"id": "s1", "description": "Write"}]}).run(
            "Goal", checks={"s1": ArtifactCheck("result.md", exact="done")},
            actions={"s1": tuple(ToolCall("list_files", {}) for _ in range(21))})
        self.assertEqual(result["subtasks"][0]["status"], "failed")

    def test_invalid_bid_fallback_allows_dependent_subtasks(self):
        orchestrator = self.make({"subtasks": [
            {"id": "s1", "description": "One"},
            {"id": "s2", "description": "Two", "depends_on": ["s1"]}]})
        class InvalidBids:
            def complete(self, messages, json_object=False):
                return {"score": "invalid", "reason": "x"}
        orchestrator.assignment.provider = InvalidBids()
        result = orchestrator.run("Goal", final_check=ArtifactCheck("result.md"))
        self.assertEqual([x["status"] for x in result["subtasks"]], ["success", "success"])
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(len([event for event in self.store.list_events(result["task"]["id"])
                              if event["event_type"] == "agent_bid_fallback"]), 4)

    def test_clarification_answer_replans_with_preserved_check(self):
        orchestrator = self.make(
            {"question": "What filename?", "subtasks": []},
            {"subtasks": [{"id": "s1", "description": "Write"}]})
        first = orchestrator.run("Write a report", checks={
            "s1": ArtifactCheck("result.md", exact="done")})
        self.assertEqual(first["task"]["status"], "waiting_human")
        request = self.store.list_pending_requests()[0]
        ApprovalService(self.store).answer(request["id"], {"answer": "result.md"})
        final = orchestrator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")

    def test_delegation_checks_owner_depth_and_messages(self):
        self.store.save_task(Task("root", "Goal", owner="builder", status=TaskStatus.RUNNING))
        orchestrator = self.make()
        with self.assertRaises(ValueError):
            orchestrator.delegate("root", "builder", "delegate", ArtifactCheck("result.md", exact="done"))
        with self.assertRaises(ValueError):
            orchestrator.delegate("root", "unknown", "delegate", ArtifactCheck("result.md", exact="done"))
        child = orchestrator.delegate("root", "reviewer", "delegate", ArtifactCheck("result.md", exact="done"))
        self.assertEqual(child["status"], "success")
        self.assertEqual(child["owner"], "reviewer")
        messages = self.store.list_events(child["id"])
        self.assertEqual(len([e for e in messages if e["event_type"] == "message_created"]), 2)

    def test_harness_broker_exposes_scoped_delegate_tool(self):
        self.store.save_task(Task("root", "Goal", owner="builder", status=TaskStatus.RUNNING))
        orchestrator = self.make()
        executor = SimpleNamespace(agent="builder", workspace=self.workspace("builder"),
                                   repository=self.store)
        broker = PlatformToolBroker(executor, delegation=lambda task_id, agent, description,
                                    path, contains: orchestrator.delegate(
                                        task_id, agent, description,
                                        ArtifactCheck(path, contains=contains)))
        result = broker.call("root", "delegate", {"agent": "reviewer",
                             "description": "Write delegated artifact", "path": "result.md",
                             "contains": "done"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["result"]["status"], "success")

    def test_delegation_depth_limit_is_enforced(self):
        self.store.save_task(Task("root", "Goal", owner="builder", status=TaskStatus.RUNNING))
        self.store.save_task(Task("deep", "Nested", owner="reviewer",
                                  parent_id="root", status=TaskStatus.RUNNING))
        self.store.append_event(task_id="deep", event_type="delegation_created",
                                payload={"depth": 2})
        with self.assertRaisesRegex(ValueError, "budget"):
            self.make().delegate("deep", "builder", "Too deep",
                                 ArtifactCheck("result.md", exact="done"))

    def test_total_task_limit_rejects_extra_delegation(self):
        self.store.save_task(Task("root", "Goal", owner="builder", status=TaskStatus.RUNNING))
        for index in range(11):
            self.store.save_task(Task("existing%d" % index, "Existing", parent_id="root"))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.make().delegate("root", "reviewer", "One more",
                                 ArtifactCheck("result.md", exact="done"))


if __name__ == "__main__":
    unittest.main()

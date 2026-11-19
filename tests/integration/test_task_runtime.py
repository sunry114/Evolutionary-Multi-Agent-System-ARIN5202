"""Offline task acceptance through real persistence, routing, broker and harness."""

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from evo.adapters.llm.client import ProviderError
from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.web import WebTools
from evo.application.approvals import ApprovalService
from evo.application.orchestrator import ArtifactCheck
from evo.config import HarnessConfig
from evo.domain.models import Agent
from evo.interfaces.execution_factory import create_orchestrator
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.tools import ExecutionResult


def tool(name, **arguments):
    return {"type": "tool", "name": name, "arguments": arguments}


def done(text="Delivered"):
    return {"type": "done", "text": text}


class ScriptedModel:
    def __init__(self, actions, plan=None):
        self.actions = iter(actions)
        self.plan = plan or {"subtasks": [{"id": "s1", "description": "Write report.md", "skills": ["documents"]}]}
        self.contexts = []
        self.planning_calls = 0

    def complete(self, messages, json_object=False):
        assert json_object
        if "Decompose the goal" in messages[0]["content"]:
            self.planning_calls += 1
            return self.plan
        self.contexts.append(json.loads(messages[-1]["content"]))
        action = next(self.actions)
        if isinstance(action, Exception):
            raise action
        return action(self.contexts[-1]) if callable(action) else action


class TaskRuntimeTests(unittest.TestCase):
    def test_planner_questions_use_the_team_coordinator(self):
        model = ScriptedModel([], plan={"question": "Which source file should we use?"})
        coordinator = create_orchestrator(self.state, agents=(Agent("dev", "Build"), Agent("lead", "Coordinate")),
                                          provider=model, harness_config=HarnessConfig(adapter="provider"))
        result = coordinator.run("Build report", final_check=ArtifactCheck("report.md"))
        self.assertEqual(result["task"]["status"], "waiting_human")
        request = coordinator.store.list_pending_requests()[0]
        self.assertEqual(request["kind"], "clarification")
        self.assertEqual(request["agent"], "lead")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.agents = (Agent("builder", "documents code"), Agent("researcher", "research"))

    def build(self, provider, budget=80):
        return create_orchestrator(self.state, agents=self.agents, provider=provider,
                                   harness_config=HarnessConfig(adapter="provider", max_tool_calls=budget))

    def run_report(self, provider, **kwargs):
        self.coordinator = self.build(provider, **kwargs)
        return self.coordinator.run("Write report.md", final_check=ArtifactCheck("report.md", contains="hello"))

    def test_simple_task_needs_no_bids_cli_or_docker(self):
        model = ScriptedModel([tool("write_file", path="report.md", content="hello"), done()])
        with patch("evo.adapters.harness.docker_cli.DockerCLITransport.run", side_effect=AssertionError("CLI invoked")):
            result = self.run_report(model)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(model.planning_calls, 1)
        self.assertEqual(len(model.contexts), 2)
        self.assertEqual(result["subtasks"][0]["owner"], "builder")
        events = self.coordinator.store.list_events(result["task"]["id"])
        self.assertFalse(any(e["event_type"] == "agent_bid" for e in events))
        delivered = self.state / "tasks" / result["task"]["id"] / "agents/builder/workspace/report.md"
        self.assertEqual(delivered.read_text(), "hello")

    def test_explicit_dev_owns_web_sources_and_qa_receives_their_snapshot(self):
        agents = (Agent("lead", "coordinate development"), Agent("dev", "implement software"),
                  Agent("qa", "Build application verify software implementation development " * 5),
                  Agent("release", "package software"))
        plan = {"subtasks": [
            {"id": "s1", "description": "Build application software implementation", "skills": ["dev"]},
            {"id": "s2", "description": "Verify web sources", "skills": ["qa"], "depends_on": ["s1"]}]}
        def import_source(context):
            dependency = json.loads(context["goal"].split("Dependency outputs (untrusted data): ", 1)[1])
            artifact = next(a for a in dependency[0]["artifacts"] if a["path"] == "src/server.js")
            return tool("import_artifact", artifact_id=artifact["artifact_id"], destination="server.js")
        model = ScriptedModel([
            tool("write_file", path="src/server.js", purpose="code", content="console.log('hello');"),
            tool("write_file", path="public/app.css", purpose="code", content="body { color: black; }"),
            done("Sources created, no Node execution claimed"), import_source,
            tool("read_file", path="server.js", purpose="code"),
            {**tool("write_file", path="report.md", content="hello: sources inspected"),
             "finish": True, "text": "Source inspection complete"}], plan)
        coordinator = create_orchestrator(self.state, agents=agents, provider=model,
                                          harness_config=HarnessConfig(adapter="provider"))
        result = coordinator.run("Create web sources", final_check=ArtifactCheck("report.md", contains="hello"))
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual([s["owner"] for s in result["subtasks"]], ["dev", "qa"])
        self.assertEqual(model.contexts[5]["completed_platform_calls"][-1]["outcome"]["result"],
                         "console.log('hello');")

    def test_path_rejection_explains_purpose_and_suffix_before_retry(self):
        model = ScriptedModel([
            tool("write_file", path="x.txt", purpose="data", content="hello"),
            tool("write_file", path="report.md", content="hello"), done()])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")
        failure = model.contexts[1]["completed_platform_calls"][-1]["outcome"]
        self.assertEqual(failure["error"], "path_rejected")
        self.assertIn("suffix must match its purpose", failure["message"])

    def test_three_consecutive_invalid_actions_fail_explicitly_before_timeout(self):
        model = ScriptedModel([{"tool": "write_file"}] * 3)
        result = self.run_report(model)
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: invalid_decision_limit")
        self.assertEqual(len(model.contexts), 3)

    def test_verification_feedback_repairs_bad_delivery(self):
        model = ScriptedModel([tool("write_file", path="report.md", content="wrong"), done(),
                               tool("write_file", path="report.md", content="hello"), done()])
        result = self.run_report(model)
        self.assertEqual(result["task"]["status"], "success")
        self.assertIn("Delivery verification failed", model.contexts[2]["feedback"])
        self.assertEqual(len(model.contexts[2]["completed_platform_calls"]), 1)

    def test_final_write_finishes_without_another_model_roundtrip(self):
        final = {**tool("write_file", path="report.md", content="hello"),
                 "finish": True, "text": "Report delivered"}
        model = ScriptedModel([final])
        result = self.run_report(model, budget=1)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 1)
        child = result["subtasks"][0]
        self.assertEqual(child["result"], "Report delivered")
        events = self.coordinator.store.list_events(child["id"])
        run_id = next(e["payload"]["run_id"] for e in events if e["event_type"] == "harness_started")
        checkpoint = json.loads((self.state / "harness" / (run_id + ".json")).read_text())
        events = checkpoint["events"]
        responses = checkpoint["model_timings"]
        self.assertEqual(len(responses), 1)
        self.assertGreaterEqual(responses[0]["duration_ms"], 0)
        self.assertTrue(any(e["kind"] == "delivery_verified" for e in events))

    def test_final_write_with_bad_content_is_repaired(self):
        model = ScriptedModel([
            {**tool("write_file", path="report.md", content="wrong"), "finish": True, "text": "Done"},
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Fixed"}])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 2)
        self.assertIn("Delivery verification failed", model.contexts[1]["feedback"])

    def test_intermediate_write_cannot_finish_delivery(self):
        model = ScriptedModel([
            {**tool("write_file", path="draft.md", content="hello"), "finish": True, "text": "Draft"},
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Done"}])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 2)

    def test_rejected_final_write_does_not_finish_delivery(self):
        model = ScriptedModel([
            {**tool("write_file", path="../report.md", content="hello"), "finish": True, "text": "Done"},
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Fixed"}])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 2)

    def test_without_acceptance_final_write_still_needs_done(self):
        model = ScriptedModel([
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Done"}, done()])
        result = self.build(model).run("Write report.md")
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 2)

    def test_matching_file_does_not_finish_without_explicit_declaration(self):
        model = ScriptedModel([tool("write_file", path="report.md", content="hello"),
                               tool("read_file", path="report.md"), done()])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 3)

    def test_latency_recording_preserves_maximum_tool_budget(self):
        model = ScriptedModel([tool("list_files")] * 127 + [
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Done"}])
        self.assertEqual(self.run_report(model, budget=128)["task"]["status"], "success")

    def test_fast_delivery_still_checks_exact_content(self):
        model = ScriptedModel([
            {**tool("write_file", path="report.md", content="hello draft"), "finish": True, "text": "Done"},
            {**tool("write_file", path="report.md", content="hello"), "finish": True, "text": "Fixed"}])
        result = self.build(model).run("Write report.md", final_check=ArtifactCheck("report.md", exact="hello"))
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(len(model.contexts), 2)

    def test_bad_tool_arguments_can_be_corrected(self):
        model = ScriptedModel([tool("write_file", path="../report.md", content="hello"),
                               tool("write_file", path="report.md", content="hello"), done()])
        result = self.run_report(model)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(model.contexts[1]["completed_platform_calls"][0]["outcome"]["status"], "failed")

    def test_missing_script_is_feedback_then_repaired_before_approval(self):
        model = ScriptedModel([
            tool("run_python", path="qa/_inspect.py"),
            tool("write_file", path="qa/_inspect.py", purpose="code", content="print('ok')"),
            tool("run_python", path="qa/_inspect.py"),
            tool("write_file", path="report.md", content="hello"), done()])
        with patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(0, "ok", "")) as python:
            first = self.run_report(model)
            self.assertEqual(first["task"]["status"], "waiting_human")
            python.assert_not_called()
            failure = model.contexts[1]["completed_platform_calls"][0]["outcome"]
            self.assertEqual(failure["error"], "tool_file_missing")
            self.assertIn("Create it", failure["message"])
            child_id = first["subtasks"][0]["id"]
            trace = self.coordinator.store.list_events(child_id)
            self.assertTrue(any(e["event_type"] == "tool_preparation_rejected"
                                and e["payload"] == {"name": "run_python", "code": "tool_file_missing"}
                                for e in trace))
            requests = self.coordinator.store.list_pending_requests()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["payload"]["arguments"]["path"], "qa/_inspect.py")
            ApprovalService(self.coordinator.store).answer(requests[0]["id"], {"approved": True})
            final = self.coordinator.resume(first["task"]["id"])
            self.assertEqual(final["task"]["status"], "success")
            python.assert_called_once_with("qa/_inspect.py", approved=True)

    def test_critical_path_validation_is_recoverable_without_approval(self):
        model = ScriptedModel([
            tool("run_python", path="../inspect.py"),
            tool("write_file", path="report.md", content="hello"), done()])
        with patch.object(DockerPythonRunner, "run_python") as python:
            self.assertEqual(self.run_report(model)["task"]["status"], "success")
            python.assert_not_called()
        failure = model.contexts[1]["completed_platform_calls"][0]["outcome"]
        self.assertEqual(failure["error"], "tool_preparation_failed")
        self.assertEqual(self.coordinator.store.list_pending_requests(), [])

    def test_script_missing_after_approval_can_be_repaired_with_new_approval(self):
        model = ScriptedModel([
            tool("write_file", path="check.py", purpose="code", content="print('old')"),
            tool("run_python", path="check.py"),
            tool("write_file", path="check.py", purpose="code", content="print('fixed')"),
            tool("run_python", path="check.py"),
            tool("write_file", path="report.md", content="hello"), done()])
        with patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(0, "fixed", "")) as python:
            first = self.run_report(model)
            request = self.coordinator.store.list_pending_requests()[0]
            ApprovalService(self.coordinator.store).answer(request["id"], {"approved": True})
            workspace = self.state / "tasks" / first["task"]["id"] / "agents/builder/workspace"
            (workspace / "check.py").unlink()
            second = self.coordinator.resume(first["task"]["id"])
            self.assertEqual(second["task"]["status"], "waiting_human")
            python.assert_not_called()
            failure = model.contexts[2]["completed_platform_calls"][-1]["outcome"]
            self.assertEqual(failure["error"], "tool_file_missing")
            replacement = self.coordinator.store.list_pending_requests()[0]
            self.assertNotEqual(request["id"], replacement["id"])
            ApprovalService(self.coordinator.store).answer(replacement["id"], {"approved": True})
            final = self.coordinator.resume(first["task"]["id"])
            self.assertEqual(final["task"]["status"], "success")
            python.assert_called_once_with("check.py", approved=True)

    def test_invalid_json_completion_can_be_corrected(self):
        model = ScriptedModel([ProviderError("malformed", code="invalid_completion"),
                               tool("write_file", path="report.md", content="hello"), done()])
        self.assertEqual(self.run_report(model)["task"]["status"], "success")

    def test_model_claim_alone_never_succeeds(self):
        model = ScriptedModel([done()] * 10)
        result = self.run_report(model, budget=1)
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: turn_limit")
        self.assertEqual(len(model.contexts), 9)

    def test_provider_error_is_safe_and_not_blindly_retried(self):
        model = ScriptedModel([ProviderError("secret detail", code="authentication")])
        result = self.run_report(model)
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: provider_authentication")
        self.assertEqual(len(model.contexts), 1)
        self.assertNotIn("secret detail", str(self.coordinator.store.list_events(result["subtasks"][0]["id"])))

    def test_more_than_twenty_tool_calls_respect_configured_budget(self):
        model = ScriptedModel([tool("list_files")] * 22 + [tool("write_file", path="report.md", content="hello"), done()])
        self.assertEqual(self.run_report(model, budget=24)["task"]["status"], "success")

    def test_tool_limit_stops_side_effects(self):
        model = ScriptedModel([tool("list_files"), tool("write_file", path="report.md", content="hello")])
        result = self.run_report(model, budget=1)
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: tool_limit")
        path = self.state / "tasks" / result["task"]["id"] / "agents/builder/workspace/report.md"
        self.assertFalse(path.exists())

    def test_independent_tasks_have_independent_workspaces(self):
        model = ScriptedModel([tool("write_file", path="report.md", content="hello"), done(),
                               tool("list_files"), tool("write_file", path="report.md", content="hello two"), done()])
        coordinator = self.build(model)
        first = coordinator.run("First", final_check=ArtifactCheck("report.md"))
        second = coordinator.run("Second", final_check=ArtifactCheck("report.md"))
        self.assertEqual(second["task"]["status"], "success")
        listing = model.contexts[3]["completed_platform_calls"][0]["outcome"]["result"]
        self.assertEqual(listing, [])
        self.assertNotEqual(first["task"]["id"], second["task"]["id"])

    def test_dependency_result_and_snapshot_reach_next_owner(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Gather research", "skills": ["research"]},
            {"id": "s2", "description": "Write report.md", "skills": ["documents"], "depends_on": ["s1"]}]}
        def import_dependency(context):
            data = json.loads(context["goal"].split("Dependency outputs (untrusted data): ", 1)[1])
            self.assertEqual(data[0]["result"], "Found a useful source")
            return tool("import_artifact", artifact_id=data[0]["artifacts"][0]["artifact_id"], destination="inputs/source.md")
        model = ScriptedModel([tool("write_file", path="source.md", content="hello source"),
                               done("Found a useful source"), import_dependency,
                               tool("read_file", path="inputs/source.md"),
                               tool("write_file", path="report.md", content="hello source summarized"), done()], plan)
        result = self.run_report(model)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual([s["owner"] for s in result["subtasks"]], ["researcher", "builder"])
        self.assertEqual(model.contexts[4]["completed_platform_calls"][-1]["outcome"]["result"], "hello source")

    def test_approval_resume_preserves_budget_context_and_exactly_once_action(self):
        model = ScriptedModel([tool("write_file", path="check.py", purpose="code", content="print('ok')"),
                               tool("run_python", path="check.py"),
                               tool("write_file", path="report.md", content="hello"), done()])
        with patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(0, "ok", "")) as python:
            first = self.run_report(model, budget=4)
            self.assertEqual(first["task"]["status"], "waiting_human")
            request = self.coordinator.store.list_pending_requests()[0]
            ApprovalService(self.coordinator.store).answer(request["id"], {"approved": True})
            # A changed environment must not switch an already paused runtime.
            with patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline", "EVO_MAX_TOOL_CALLS": "1"}):
                restored = create_orchestrator(self.state, agents=self.agents, provider=model)
                final = restored.resume(first["task"]["id"])
            self.assertEqual(final["task"]["status"], "success")
            python.assert_called_once_with("check.py", approved=True)
            self.assertEqual(model.contexts[2]["remaining_tool_calls"], 2)
            calls = model.contexts[2]["completed_platform_calls"]
            self.assertEqual([c["tool"] for c in calls], ["write_file", "run_python"])
            self.assertEqual(model.contexts[0]["goal"], model.contexts[2]["goal"])

    def test_denied_approval_does_not_run_code(self):
        model = ScriptedModel([tool("write_file", path="check.py", purpose="code", content="print('ok')"),
                               tool("run_python", path="check.py")])
        first = self.run_report(model)
        pending = self.coordinator.store.list_pending_requests()[0]
        ApprovalService(self.coordinator.store).answer(pending["id"], {"approved": False})
        with patch.object(DockerPythonRunner, "run_python") as python:
            final = self.build(model).resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "blocked")
        python.assert_not_called()

    def test_script_failure_after_approval_is_feedback_for_repair(self):
        model = ScriptedModel([tool("write_file", path="check.py", purpose="code", content="assert False"),
                               tool("run_python", path="check.py"),
                               tool("write_file", path="report.md", content="hello"), done()])
        first = self.run_report(model)
        pending = self.coordinator.store.list_pending_requests()[0]
        ApprovalService(self.coordinator.store).answer(pending["id"], {"approved": True})
        with patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(1, "", "AssertionError")) as python:
            final = self.build(model).resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertEqual(model.contexts[2]["completed_platform_calls"][-1]["outcome"]["result"]["exit_code"], 1)
        python.assert_called_once()

    def test_existing_gateway_payload_uses_new_runtime(self):
        model = ScriptedModel([tool("write_file", path="report.md", content="hello"), done()])
        app = SQLiteApplication(self.state, provider=model, agents=self.agents)
        with patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "provider"}):
            result = app.dispatch("run", {"prompt": "Write report.md", "final_check": {"path": "report.md", "contains": "hello"}})
        self.assertEqual(result["status"], "success")

    def test_same_owner_can_execute_two_isolated_tasks_concurrently(self):
        barrier = threading.Barrier(2)
        def write(context):
            barrier.wait(timeout=3)
            return tool("write_file", path="report.md", content="hello")
        first = self.build(ScriptedModel([write, done()]))
        second = self.build(ScriptedModel([write, done()]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(c.run, "Write report.md", final_check=ArtifactCheck("report.md"))
                       for c in (first, second)]
            self.assertEqual([r.result(timeout=5)["task"]["status"] for r in results], ["success", "success"])

    def test_deadline_prevents_late_model_response_from_writing(self):
        def late(context):
            time.sleep(0.03)
            return tool("write_file", path="report.md", content="hello")
        coordinator = create_orchestrator(
            self.state, agents=self.agents, provider=ScriptedModel([late]),
            harness_config=HarnessConfig(adapter="provider", timeout=0.01))
        result = coordinator.run("Write report.md", final_check=ArtifactCheck("report.md"))
        self.assertEqual(result["subtasks"][0]["result"], "Harness failed: timeout")
        self.assertFalse(list((self.state / "tasks").rglob("report.md")))
        child = result["subtasks"][0]
        run_id = next(e["payload"]["run_id"] for e in coordinator.store.list_events(child["id"])
                      if e["event_type"] == "harness_started")
        record = json.loads((self.state / "harness" / (run_id + ".json")).read_text())
        deadline = next(e for e in record["events"] if e["kind"] == "execution_deadline")
        self.assertEqual(deadline["payload"]["tool_calls"], 0)

    def test_snapshot_survives_later_overwrite_in_same_owner_workspace(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Write draft", "skills": ["documents"]},
            {"id": "s2", "description": "Rewrite draft", "skills": ["documents"], "depends_on": ["s1"]},
            {"id": "s3", "description": "Research original draft", "skills": ["research"], "depends_on": ["s1", "s2"]}]}
        def import_original(context):
            dependencies = json.loads(context["goal"].split("Dependency outputs (untrusted data): ", 1)[1])
            return tool("import_artifact", artifact_id=dependencies[0]["artifacts"][0]["artifact_id"], destination="original.md")
        model = ScriptedModel([tool("write_file", path="draft.md", content="original"), done(),
                               tool("write_file", path="draft.md", content="revised"), done(),
                               import_original, tool("read_file", path="original.md"),
                               tool("write_file", path="report.md", content="hello original"), done()], plan)
        result = self.run_report(model)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(model.contexts[6]["completed_platform_calls"][-1]["outcome"]["result"], "original")

    def test_task_artifacts_are_not_visible_from_another_task(self):
        source = WorkspaceFilesystem(self.state, "builder", task_scope="one")
        source.write_file("note.md", "private")
        artifact = ArtifactExchange(source).publish("note.md", ["researcher"])
        same_task = ArtifactExchange(WorkspaceFilesystem(self.state, "researcher", task_scope="one"))
        other_task = ArtifactExchange(WorkspaceFilesystem(self.state, "researcher", task_scope="two"))
        self.assertEqual(len(same_task.list_available()), 1)
        self.assertEqual(other_task.list_available(), ())
        with self.assertRaises(ValueError):
            other_task.import_artifact(artifact.id, "note.md")

    def test_search_budget_survives_approval_resume(self):
        model = ScriptedModel([tool("search", query="example")] * 3 + [
            tool("write_file", path="check.py", purpose="code", content="print('ok')"),
            tool("run_python", path="check.py"), tool("search", query="fourth")])
        with patch.object(WebTools, "search", return_value=[]) as search:
            first = self.run_report(model)
            request = self.coordinator.store.list_pending_requests()[0]
            ApprovalService(self.coordinator.store).answer(request["id"], {"approved": True})
            with patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(0, "ok", "")):
                final = self.build(model).resume(first["task"]["id"])
        self.assertEqual(final["subtasks"][0]["result"], "Harness failed: search_limit")
        self.assertEqual(search.call_count, 3)

    def test_saved_routing_applies_to_later_subtasks_after_resume(self):
        plan = {"subtasks": [
            {"id": "s1", "description": "Check code", "skills": ["code"]},
            {"id": "s2", "description": "Write report.md", "skills": ["documents"], "depends_on": ["s1"]}]}
        model = ScriptedModel([tool("write_file", path="check.py", purpose="code", content="print('ok')"),
                               tool("run_python", path="check.py"), done(),
                               tool("write_file", path="report.md", content="hello"), done()], plan)
        first = self.run_report(model)
        pending = self.coordinator.store.list_pending_requests()[0]
        ApprovalService(self.coordinator.store).answer(pending["id"], {"approved": True})
        with patch.dict("os.environ", {"EVO_ASSIGNMENT_STRATEGY": "bidding"}), \
                patch.object(DockerPythonRunner, "run_python", return_value=ExecutionResult(0, "ok", "")):
            final = self.build(model).resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertFalse(any(e["event_type"] == "agent_bid"
                             for e in self.coordinator.store.list_events(first["task"]["id"])))

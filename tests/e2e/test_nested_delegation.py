"""Exercise durable nested waits through the Claude protocol and local workspaces."""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from evo.adapters.harness.claude_code import ClaudeCodeAdapter
from evo.config import HarnessConfig
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.tools import ExecutionResult
from tests.fixtures.harness import FakeCLITransport


class DelegationProvider:
    def __init__(self, *, broken=False):
        self.broken = broken

    def complete(self, messages, *, json_object):
        system = messages[0]["content"]
        context = json.loads(messages[1]["content"])
        if "Decompose the goal" in system:
            suffix = "-fails" if self.broken else ""
            return {"subtasks": [{"id": "s1", "description": "fixture-delegation-parent" + suffix},
                                 {"id": "s2", "description": "fixture-delegation-dependent", "depends_on": ["s1"]}]}
        if "Act as the delegating member" in system:
            return {"decision": "nominate", "member": "reviewer",
                    "scope": "Include the collector's evidence.", "reason": "Accepted the reviewer’s scope."}
        proposal = context["proposal"]
        if proposal["to"] == "reviewer" and context["round"] == 1:
            return {"decision": "revise", "scope": "Include the collector's evidence.",
                    "reason": "The review needs a supporting artifact."}
        return {"decision": "accept", "reason": "I can deliver and verify the work."}


class NestedDelegationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.provider = DelegationProvider()
        app = self.application()
        names = ["analyst", "reviewer", "collector"]
        app.dispatch("configure", {"agents": [{"name": name, "specialty": "reports"} for name in names],
                                   "squad": {"name": "team", "lead": "analyst", "members": names}})
        harness = patch("evo.interfaces.execution_factory.create_harness", side_effect=self.harness)
        harness.start()
        self.addCleanup(harness.stop)
        python = patch("evo.adapters.tools.host.HostPythonRunner.run_python",
                       return_value=ExecutionResult(0, "checked", ""))
        self.python = python.start()
        self.addCleanup(python.stop)

    def application(self):
        return SQLiteApplication(self.state, provider=self.provider)

    @staticmethod
    def harness(config, state, broker, *, model, credential_loader):
        strict = HarnessConfig(adapter="claude_code", timeout=8)
        adapter = ClaudeCodeAdapter(state, broker, config=strict, model=model,
                                    credential_loader=lambda: "fixture-credential")
        adapter.transport = FakeCLITransport(strict)
        return adapter

    def prepare(self):
        app = self.application()
        issue = app.dispatch("create", {
            "prompt": "Analyze the logs and include independent evidence.", "agent": "analyst", "no_memory": True,
            "checks": {"s1": {"path": "report.md", "contains": "Analysis: Review: verified evidence"},
                       "s2": {"path": "followup.md", "contains": "review complete"}},
        })["task"]["id"]
        first = app.dispatch("start", {"task_id": issue})
        self.assertEqual(first["status"], "waiting_human", first)
        traces = app.dispatch("trace", {"task_id": issue})["events"]
        requests = [event for event in traces if event["event_type"] == "delegation_requested"]
        self.assertEqual([(event["actor"], event["payload"]["to"]) for event in requests],
                         [("analyst", "reviewer"), ("reviewer", "collector")])
        subtask = issue + "-s1"
        child, grandchild = [event["payload"]["delegation_id"] for event in requests]
        self.assertEqual([event["payload"]["parent_id"] for event in requests], [subtask, child])
        approval, = app.store.list_pending_requests()
        self.assertEqual((approval["task_id"], approval["agent"], approval["action"]),
                         (grandchild, "collector", "run_python"))
        reason = first["task"]["result"]
        for value in (child, grandchild, approval["id"], "run_python", "collector"):
            self.assertIn(value, reason)
        self.assertEqual(app.store.get_task(issue + "-s2")["status"], "created")
        self.assertIn("Include the collector's evidence.", app.store.get_task(child)["description"])
        with self.assertRaisesRegex(ValueError, "still pending"):
            app.dispatch("resume", {"task_id": issue})
        self.python.assert_not_called()
        return issue, subtask, child, grandchild, approval["id"]

    def duplicate(self, operation, payload):
        def dispatch(_):
            try:
                return self.application().dispatch(operation, payload)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [result for result in executor.map(dispatch, range(2)) if result is not None]
        self.assertEqual(len(results), 1)
        return results[0]

    def assert_delivered_once(self, issue, child, grandchild):
        app = self.application()
        events = app.dispatch("trace", {"task_id": issue})["events"]
        self.assertEqual(sum(event["event_type"] == "resume_claimed" for event in events), 1)
        self.assertEqual(sum(event["event_type"] == "delegation_requested" for event in events), 2)
        self.assertEqual([event["payload"]["id"] for event in events
                          if event["event_type"] == "delegation_completed"], [grandchild, child])
        self.assertEqual(len(app.store.list_children(child)), 1)

    def test_approve_resumes_original_chain_and_integrates_shared_artifacts_once(self):
        issue, subtask, child, grandchild, request_id = self.prepare()
        answer = self.duplicate("answer", {"task_id": issue, "request_id": request_id, "approved": True})
        self.assertEqual(answer["status"], "approved")
        result = self.duplicate("resume", {"task_id": issue})
        self.assertEqual(result["status"], "success", result)
        self.python.assert_called_once_with("check.py", approved=True)
        self.assert_delivered_once(issue, child, grandchild)
        workspace = self.state / "agents" / "analyst" / "workspace"
        self.assertEqual((workspace / "report.md").read_text(), "Analysis: Review: verified evidence")
        self.assertEqual((workspace / "followup.md").read_text(), "review complete")
        store = self.application().store
        self.assertEqual(store.get_delegation(grandchild)["outcome"]["artifact"]["id"], grandchild)
        self.assertEqual(store.get_delegation(child)["outcome"]["artifact"]["id"], child)
        for task_id in (subtask, child, grandchild):
            self.assertEqual(store.get_task(task_id)["status"], "success")

    def test_deny_propagates_through_original_chain_without_executing_approved_tool(self):
        issue, subtask, child, grandchild, request_id = self.prepare()
        self.assertEqual(self.duplicate("answer", {"task_id": issue, "request_id": request_id, "approved": False})["status"], "denied")
        result = self.duplicate("resume", {"task_id": issue})
        self.assertEqual(result["status"], "blocked")
        self.python.assert_not_called()
        self.assert_delivered_once(issue, child, grandchild)
        store = self.application().store
        for task_id in (grandchild, child, subtask, issue):
            record = store.get_task(task_id)
            self.assertEqual(record["status"], "blocked")
            self.assertIn("Approval denied: run_python", record["result"])
        self.assertIn("Dependency did not succeed", store.get_task(issue + "-s2")["result"])

    def test_verification_failure_returns_to_original_parent_and_blocks_dag_dependent(self):
        self.provider.broken = True
        issue, subtask, child, grandchild, request_id = self.prepare()
        self.application().dispatch("answer", {"task_id": issue, "request_id": request_id, "approved": True})
        result = self.application().dispatch("resume", {"task_id": issue})
        self.assertEqual(result["status"], "blocked")
        self.python.assert_called_once_with("check.py", approved=True)
        self.assert_delivered_once(issue, child, grandchild)
        store = self.application().store
        for task_id in (grandchild, child, subtask):
            self.assertEqual(store.get_task(task_id)["status"], "failed")
            self.assertIn("Objective evidence missing", store.get_task(task_id)["result"])
        self.assertIn("Dependency did not succeed", result["task"]["result"])
        self.assertIn("Objective evidence missing", result["task"]["result"])
        self.assertIsNone(store.get_delegation(grandchild)["outcome"]["artifact"])

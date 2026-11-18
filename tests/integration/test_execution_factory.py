import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.application.orchestrator import ArtifactCheck
from evo.application.approvals import ApprovalService
from evo.adapters.tools.docker import DockerPythonRunner
from evo.config import HarnessConfig
from evo.domain.models import Agent
from evo.interfaces.execution_factory import create_orchestrator
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.harness import ToolCall
from evo.ports.tools import ExecutionResult


class Provider:
    def complete(self, messages, json_object=False):
        if "Decompose the goal" in messages[0]["content"]:
            return {"subtasks": [{"id": "s1", "description": "Write report.md"}]}
        return {"score": 5, "reason": "Can write the report"}


class ExecutionFactoryTests(unittest.TestCase):
    def test_real_store_workspace_broker_and_baseline_harness_are_wired(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent("builder", "documents")
            coordinator = create_orchestrator(
                temp, agents=(agent,), provider=Provider(),
                harness_config=HarnessConfig(adapter="baseline"))
            result = coordinator.run("Write report.md", checks={
                "s1": ArtifactCheck("report.md", exact="done")},
                actions={"s1": (ToolCall("write_file", {"path": "report.md", "content": "done"}),)})
            self.assertEqual(result["task"]["status"], "success")
            self.assertEqual(result["subtasks"][0]["status"], "success")
            self.assertEqual((Path(temp) / "agents" / "builder" / "workspace" / "report.md").read_text(), "done")
            events = coordinator.store.list_events(result["subtasks"][0]["id"])
            self.assertTrue(any(item["event_type"] == "objective_verified" for item in events))

    def test_recreated_factory_resumes_one_approved_action(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent("builder", "documents and Python")
            def build():
                return create_orchestrator(
                    temp, agents=(agent,), provider=Provider(),
                    harness_config=HarnessConfig(adapter="baseline"))
            with patch.object(DockerPythonRunner, "run_python",
                              return_value=ExecutionResult(0, "ok", "")) as python:
                coordinator = build()
                first = coordinator.run("Write report.md", checks={
                    "s1": ArtifactCheck("report.md", exact="done")}, actions={"s1": (
                    ToolCall("write_file", {"path": "report.md", "content": "done"}),
                    ToolCall("write_file", {"path": "check.py", "content": "print('ok')", "purpose": "code"}),
                    ToolCall("run_python", {"path": "check.py"}),
                )})
                self.assertEqual(first["task"]["status"], "waiting_human")
                request = coordinator.store.list_pending_requests()[0]
                ApprovalService(coordinator.store).answer(request["id"], {"approved": True})
                final = build().resume(first["task"]["id"])
                self.assertEqual(final["task"]["status"], "success")
                python.assert_called_once_with("check.py", approved=True)

    def test_default_gateway_run_and_resume_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider(), agents=(Agent("builder", "documents"),))
            called = []
            class Coordinator:
                def run(self, prompt, *, checks, task_id, memory_enabled):
                    called.append((prompt, checks["s1"].path, memory_enabled))
                    return {"task": {"id": task_id, "status": "waiting_human"}, "subtasks": []}
                def resume(self, task_id):
                    called.append(("resume", task_id))
                    return {"task": {"id": task_id, "status": "success"}, "subtasks": []}
            with patch.object(app, "_coordinator", return_value=Coordinator()):
                first = app.dispatch("run", {"prompt": "Write report.md", "no_memory": True,
                                             "checks": {"s1": {"path": "report.md", "exact": "done"}}})
                final = app.dispatch("resume", {"task_id": first["task"]["id"]})
            self.assertEqual(first["status"], "waiting_human")
            self.assertEqual(final["status"], "success")
            self.assertEqual(called[0], ("Write report.md", "report.md", False))
            self.assertEqual(called[1], ("resume", first["task"]["id"]))

    def test_invalid_checks_rejected_before_provider_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            for checks in ({}, {"s1": {"path": "../report.md", "exact": "done"}},
                           {"s1": {"path": "report.md"}}):
                with self.subTest(checks=checks), self.assertRaises(ValueError):
                    app.dispatch("run", {"prompt": "Write report.md", "checks": checks})
            self.assertEqual(app.store.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()

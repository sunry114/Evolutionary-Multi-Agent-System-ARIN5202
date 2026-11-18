"""Real C10a Baseline Harness approval pause and C11 resume flow."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from evo.adapters.harness.baseline import BaselineHarnessAdapter
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.application.approvals import ApprovalService
from evo.application.assignment import AssignmentService
from evo.application.executor import ActionExecutor
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent
from evo.ports.harness import ToolCall
from evo.ports.tools import ExecutionResult


class Provider:
    def complete(self, messages, json_object=False):
        return {"subtasks": [{"id": "s1", "description": "Write and check hello"}]}


class Bidder:
    def complete(self, messages, json_object=False):
        return {"score": 5, "reason": "Can do it"}


class HarnessResumeTests(unittest.TestCase):
    def test_approved_python_resumes_and_file_is_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteStore(root / "events.sqlite3")
            approval = ApprovalService(store)
            agent = Agent("builder", "Python and documents")
            python = Mock()
            python.run_python.return_value = ExecutionResult(0, "ok", "")

            def workspace(name):
                return WorkspaceFilesystem(root, name)

            def harness(task_id, name, fs):
                executor = ActionExecutor(store, approval, agent=name, workspace=fs,
                                          python=python, git=Mock())
                return BaselineHarnessAdapter(root, PlatformToolBroker(executor))

            coordinator = Orchestrator(store, Planner(Provider(), (agent,), approval),
                                       AssignmentService(Bidder(), (agent,), store),
                                       harness, workspace, agents=(agent,))
            first = coordinator.run("Create hello.md and run a check", checks={
                "s1": ArtifactCheck("hello.md", exact="hello")}, actions={"s1": (
                ToolCall("write_file", {"path": "check.py", "content": "print('ok')", "purpose": "code"}),
                ToolCall("write_file", {"path": "hello.md", "content": "hello"}),
                ToolCall("run_python", {"path": "check.py"}),
            )})
            self.assertEqual(first["task"]["status"], "waiting_human")
            self.assertEqual(first["subtasks"][0]["status"], "waiting_human")
            pending = store.list_pending_requests()
            self.assertEqual(len(pending), 1)
            with self.assertRaisesRegex(ValueError, "pending"):
                coordinator.resume(first["task"]["id"])
            approval.answer(pending[0]["id"], {"approved": True})
            final = coordinator.resume(first["task"]["id"])
            self.assertEqual(final["task"]["status"], "success")
            self.assertEqual(final["subtasks"][0]["status"], "success")
            python.run_python.assert_called_once_with("check.py", approved=True)


if __name__ == "__main__":
    unittest.main()

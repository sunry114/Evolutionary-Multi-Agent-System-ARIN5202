import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.ports.tools import ExecutionResult, ToolError


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = SQLiteStore(self.state / "events.sqlite3")
        self.store.save_task(Task("t1", "Execute action", owner="alice", status=TaskStatus.RUNNING))
        self.fs = WorkspaceFilesystem(self.state, "alice")
        self.fs.write_file("main.py", "print('hello')", purpose="code")
        self.fs.write_file("report.md", "report")
        self.approval = ApprovalService(self.store)
        self.python = Mock()
        self.python.run_python.return_value = ExecutionResult(0, "hello", "")
        self.git = Mock()
        self.git.head.return_value = "a" * 40
        self.git.commit.return_value = ExecutionResult(0, "committed", "")
        self.git.push.return_value = ExecutionResult(0, "Push completed", "")
        self.executor = ActionExecutor(self.store, self.approval, agent="alice", workspace=self.fs,
                                       python=self.python, git=self.git)

    def request(self, name="run_python", arguments=None):
        return self.executor.execute("t1", name, arguments if arguments is not None else {"path": "main.py"})["request_id"]

    def age(self, request_id):
        with self.store._transaction() as connection:
            connection.execute("UPDATE approval_requests SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (request_id,))

    def test_pending_persists_and_never_executes(self):
        request_id = self.request()
        self.python.run_python.assert_not_called()
        restored = SQLiteStore(self.store.path)
        self.assertEqual(restored.get_task("t1")["status"], "waiting_human")
        self.assertEqual(restored.get_action(request_id)["status"], "requested")
        self.assertIsNone(ApprovalService(restored).get_answer(request_id))
        self.assertEqual(restored.schema_version(), 12)

    def test_concurrent_identical_requests_share_one_pending_request(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(lambda _: self.request(), range(4)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.store.list_pending_requests()), 1)
        self.python.run_python.assert_not_called()

    def test_approved_python_resumes_after_restart_and_records_only_safe_metadata(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.executor.approval = ApprovalService(SQLiteStore(self.store.path))
        result = self.executor.execute("t1", "run_python", {"path": "main.py"}, request_id=request_id)
        self.assertEqual(result["status"], "success")
        json.dumps(result)
        self.python.run_python.assert_called_once_with("main.py", approved=True)
        action = self.store.get_action(request_id)
        self.assertEqual(action["status"], "success")
        self.assertEqual(action["result"], {"exit_code": 0, "timed_out": False, "output_limited": False})
        self.assertEqual(self.store.get_task("t1")["status"], "running")
        self.assertTrue(self.store.get_approval_request(request_id)["answered_at"])

    def test_denied_python_and_repeated_calls_are_blocked_without_effects(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": False})
        for _ in range(3):
            self.assertEqual(self.executor.execute("t1", "run_python", {"path": "main.py"})["status"], "blocked")
        self.assertEqual(self.store.get_action(request_id)["status"], "blocked")
        self.assertEqual(len(self.store.list_pending_requests()), 0)
        self.python.run_python.assert_not_called()

    def test_delete_approved_and_denied(self):
        request_id = self.request("delete_file", {"path": "report.md"})
        self.assertEqual(self.fs.read_file("report.md"), "report")
        self.approval.answer(request_id, {"approved": True})
        self.assertEqual(self.executor.execute("t1", "delete_file", {"path": "report.md"}, request_id=request_id)["status"], "success")
        self.assertFalse((self.fs.workspace / "report.md").exists())

    def test_delete_denied_retains_file(self):
        request_id = self.request("delete_file", {"path": "report.md"})
        self.approval.answer(request_id, {"approved": False})
        self.assertEqual(self.executor.execute("t1", "delete_file", {"path": "report.md"})["status"], "blocked")
        self.assertEqual(self.fs.read_file("report.md"), "report")

    def test_changed_file_invalidates_approval(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.fs.write_file("main.py", "print('changed')", purpose="code")
        with self.assertRaises(ValueError):
            self.executor.execute("t1", "run_python", {"path": "main.py"}, request_id=request_id)
        self.python.run_python.assert_not_called()

    def test_cross_task_agent_action_or_payload_cannot_consume_approval(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        payload = self.store.get_approval_request(request_id)["payload"]
        self.store.save_task(Task("t2", "Other", owner="alice", status=TaskStatus.RUNNING))
        for task, agent, name, args in [("t2", "alice", "run_python", payload),
                                        ("t1", "bob", "run_python", payload),
                                        ("t1", "alice", "delete_file", payload),
                                        ("t1", "alice", "run_python", {"arguments": {}})]:
            with self.assertRaises(ValueError):
                self.approval.claim(request_id, task, agent, name, args)
        self.assertEqual(self.store.get_action(request_id)["status"], "requested")

    def test_concurrent_consumers_execute_only_once(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        def resume(_):
            try:
                return self.executor.execute("t1", "run_python", {"path": "main.py"}, request_id=request_id)["status"]
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(resume, range(2)))
        self.assertEqual(sorted(results), ["conflict", "success"])
        self.python.run_python.assert_called_once()

    def test_duplicate_and_concurrent_human_decisions_write_one_event(self):
        request_id = self.request()
        def answer(_):
            try:
                return self.approval.answer(request_id, {"approved": True})["status"]
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(answer, range(2)))
        self.assertEqual(sorted(results), ["approved", "conflict"])
        self.assertEqual(sum(e["event_type"] == "request_answered" for e in self.store.list_events("t1")), 1)

    def test_invalid_answers_never_write(self):
        request_id = self.request()
        before = self.store.list_events("t1")
        for response in ({"approved": 1}, {"approved": "true"}, {"answer": "yes"}, {},
                         {"approved": True, "answer": "yes"}, None):
            with self.assertRaises(ValueError):
                self.approval.answer(request_id, response)
        self.assertEqual(before, self.store.list_events("t1"))

    def test_expired_pending_request_and_late_answer_block_task(self):
        request_id = self.request()
        self.age(request_id)
        self.assertEqual(self.approval.answer(request_id, {"approved": True})["status"], "expired")
        self.assertEqual(self.store.get_task("t1")["status"], "blocked")
        self.python.run_python.assert_not_called()

    def test_unanswered_request_expires_when_polled(self):
        request_id = self.request()
        self.age(request_id)
        self.assertEqual(self.approval.get_answer(request_id)["status"], "expired")
        self.assertEqual(self.store.get_action(request_id)["status"], "blocked")

    def test_expired_approved_request_cannot_be_consumed(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.age(request_id)
        self.assertEqual(self.executor.execute("t1", "run_python", {"path": "main.py"}, request_id=request_id)["status"], "blocked")
        self.python.run_python.assert_not_called()

    def test_expiry_is_rechecked_inside_atomic_claim(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.age(request_id)
        payload = self.store.get_approval_request(request_id)["payload"]
        self.assertFalse(self.approval.claim(request_id, "t1", "alice", "run_python", payload))
        self.assertEqual(self.store.get_approval_request(request_id)["status"], "expired")

    def test_unrelated_pending_request_cannot_be_polled_through_executor(self):
        self.store.save_task(Task("t2", "Other", owner="alice", status=TaskStatus.RUNNING))
        request_id = self.approval.request_approval("t2", "alice", "run_python", {"arguments": {"path": "other.py"}})
        with self.assertRaises(ValueError):
            self.executor.execute("t1", "run_python", {"path": "main.py"}, request_id=request_id)
        self.assertEqual(self.store.get_task("t1")["status"], "running")

    def test_clarification_answer_persists_and_rejects_wrong_schema(self):
        request_id = self.approval.request_clarification("t1", "Which output?")
        with self.assertRaises(ValueError):
            self.approval.answer(request_id, {"approved": True})
        self.assertEqual(self.approval.answer(request_id, {"answer": "report.md"})["status"], "answered")
        self.assertEqual(ApprovalService(SQLiteStore(self.store.path)).get_answer(request_id)["answer"], {"answer": "report.md"})

    def test_model_cannot_supply_approval_or_credentials_or_unknown_action(self):
        for name, args in [("run_python", {"path": "main.py", "approved": True}),
                           ("git_push", {"repository": "repo", "branch": "main", "token": "fixture"}),
                           ("shell", {"command": "anything"})]:
            with self.assertRaises(ValueError):
                self.executor.execute("t1", name, args)
        self.python.run_python.assert_not_called()
        self.assertEqual(self.store.list_pending_requests(), [])

    def test_tool_failures_persist_without_raw_output_or_exception_secrets(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.python.run_python.side_effect = ToolError("fixture-secret")
        self.assertEqual(self.executor.execute("t1", "run_python", {"path": "main.py"})["status"], "failed")
        self.assertEqual(self.store.get_task("t1")["status"], "failed")
        self.assertNotIn("fixture-secret", json.dumps(self.store.list_events("t1")))
        self.assertEqual(self.store.get_action(request_id)["result"], {"error": "tool_failed"})

    def test_nonzero_timeout_and_output_limit_never_count_as_success(self):
        for index, outcome in enumerate((ExecutionResult(7, "fixture-secret", "fixture-secret"),
                                         ExecutionResult(0, "", "", timed_out=True),
                                         ExecutionResult(0, "", "", output_limited=True))):
            task_id = "failure%d" % index
            self.store.save_task(Task(task_id, "Fail", owner="alice", status=TaskStatus.RUNNING))
            self.python.run_python.return_value = outcome
            request_id = self.executor.execute(task_id, "run_python", {"path": "main.py"})["request_id"]
            self.approval.answer(request_id, {"approved": True})
            self.assertEqual(self.executor.execute(task_id, "run_python", {"path": "main.py"})["status"], "failed")
            self.assertNotIn("fixture-secret", json.dumps(self.store.list_events(task_id)))

    def test_malformed_tool_result_fails_closed(self):
        request_id = self.request()
        self.approval.answer(request_id, {"approved": True})
        self.python.run_python.return_value = None
        self.assertEqual(self.executor.execute("t1", "run_python", {"path": "main.py"})["status"], "failed")
        self.assertEqual(self.store.get_task("t1")["status"], "failed")

    def test_noncritical_git_actions_are_audited(self):
        self.git.status.return_value = ExecutionResult(0, "", "")
        result = self.executor.execute("t1", "git_status", {})
        self.assertEqual(result["status"], "success")
        self.assertEqual(self.store.get_action(result["action_id"])["tool_name"], "git_status")

    def test_high_risk_git_commit_requires_approval(self):
        self.executor.require_commit_approval = True
        args = {"paths": ["report.md"], "message": "Report"}
        request_id = self.request("git_commit", args)
        self.git.commit.assert_not_called()
        self.approval.answer(request_id, {"approved": True})
        self.assertEqual(self.executor.execute("t1", "git_commit", args)["status"], "success")

    def test_push_is_bound_to_approved_commit(self):
        args = {"repository": "https://github.com/example/project.git", "branch": "main"}
        request_id = self.request("git_push", args)
        self.approval.answer(request_id, {"approved": True})
        self.git.head.return_value = "b" * 40
        with self.assertRaises(ValueError):
            self.executor.execute("t1", "git_push", args, request_id=request_id)
        self.git.push.assert_not_called()

    def test_wrong_owner_and_unbounded_approval_lifetimes_are_rejected(self):
        self.store.save_task(Task("t2", "Other", owner="bob", status=TaskStatus.RUNNING))
        with self.assertRaises(ValueError):
            self.executor.execute("t2", "run_python", {"path": "main.py"})
        for age in (0, -1, True, float("inf"), float("nan"), 86401):
            with self.assertRaises(ValueError):
                ApprovalService(self.store, max_age_seconds=age)

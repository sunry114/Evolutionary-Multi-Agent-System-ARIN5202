import copy
import hashlib
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock

from evo.adapters.harness.context import model_history
from evo.adapters.harness.provider import ProviderHarnessAdapter
from evo.config import HarnessConfig
from evo.ports.harness import HarnessError
from evo.ports.tools import ToolCleanupError
from tests.fixtures.harness import HarnessFixture


def action(name, **arguments):
    return {"type": "tool", "name": name, "arguments": arguments}


class NativeModel:
    native_tools = True

    def __init__(self, actions):
        self.actions = iter(actions)
        self.contexts = []

    def complete(self, *args, **kwargs):
        raise AssertionError("Native execution must not use the text JSON protocol")

    def decide(self, messages, tools):
        self.contexts.append(json.loads(messages[-1]["content"]))
        result = next(self.actions)
        return result() if callable(result) else result


class RealisticHarnessTests(HarnessFixture):
    def native(self, model, **settings):
        return ProviderHarnessAdapter(self.state, self.broker, provider=model,
                                      config=HarnessConfig(adapter="provider", **settings))

    def test_search_range_edit_syntax_and_delivery_share_final_artifact_digest(self):
        source = "# line\n" * 300 + "def total(q, p):\n    return q + p\n"
        self.fs.write_file("inventory.py", source, purpose="code")
        model = NativeModel([
            action("search_files", query="return q + p"),
            action("read_file", path="inventory.py", purpose="code", start_line=301, max_lines=2),
            action("edit_file", path="inventory.py", purpose="code", old_text="return q + p", new_text="return q * p"),
            action("check_python", path="inventory.py"),
            action("write_file", path="report.md", content="source checked"),
            {"type": "done", "text": "Source checked; runtime tests were not executed"}])
        request = replace(self.request, acceptance={"path": "report.md", "exact": "source checked"})
        _, result = self.complete(request, self.native(model))
        self.assertEqual(result.status, "success")
        self.assertEqual(model.contexts[2]["completed_platform_calls"][-1]["outcome"]["result"],
                         "def total(q, p):\n    return q + p\n")
        self.assertTrue(model.contexts[4]["completed_platform_calls"][-1]["outcome"]["result"]["valid"])
        refs = {ref.path: ref.sha256 for ref in result.artifacts}
        expected = self.fs.read_file("inventory.py", purpose="code").encode()
        self.assertEqual(refs["inventory.py"], hashlib.sha256(expected).hexdigest())
        self.python.run_python.assert_not_called()

    def test_ambiguous_edit_changes_nothing_and_can_be_repaired(self):
        self.fs.write_file("report.md", "x x")
        model = NativeModel([action("edit_file", path="report.md", old_text="x", new_text="lost"),
                             action("read_file", path="report.md"),
                             action("edit_file", path="report.md", old_text="x x", new_text="fixed"),
                             {"type": "done", "text": "Fixed"}])
        _, result = self.complete(adapter=self.native(model))
        self.assertEqual(result.status, "success")
        first = model.contexts[1]["completed_platform_calls"][0]["outcome"]
        self.assertEqual(first["error"], "edit_rejected")
        self.assertEqual(model.contexts[2]["completed_platform_calls"][-1]["outcome"]["result"], "x x")
        self.assertEqual(self.fs.read_file("report.md"), "fixed")

    def test_native_approval_restart_executes_once_and_preserves_budget(self):
        self.fs.write_file("check.py", "print('ok')", purpose="code")
        model = NativeModel([action("run_python", path="check.py"), action("write_file", path="report.md", content="tested"),
                             {"type": "done", "text": "Tested"}])
        _, first = self.complete(adapter=self.native(model, max_tool_calls=2))
        self.assertEqual(first.status, "waiting_human")
        self.python.run_python.assert_not_called()
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": True})
        restored = self.native(model, max_tool_calls=2)
        result = restored.wait(restored.resume(first.resume_ref, self.request), 5)
        self.assertEqual(result.status, "success")
        self.python.run_python.assert_called_once_with("check.py", approved=True)
        self.assertEqual(model.contexts[-1]["remaining_tool_calls"], 0)

    def test_native_cancellation_prevents_a_late_write(self):
        entered, release = threading.Event(), threading.Event()
        def late():
            entered.set()
            release.wait(3)
            return action("write_file", path="late.md", content="must not exist")
        adapter = self.native(NativeModel([late]))
        handle = adapter.start(self.request)
        self.assertTrue(entered.wait(2))
        control = adapter._active[handle.run_id]
        control.stop.set()
        release.set()
        result = adapter.cancel(handle)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse((self.fs.workspace / "late.md").exists())

    def test_unverified_tool_cleanup_quarantines_task_and_never_replays(self):
        self.fs.write_file("check.py", "print('ok')", purpose="code")
        self.python.run_python.side_effect = ToolCleanupError("runtime still exists")
        model = NativeModel([action("run_python", path="check.py")])
        _, first = self.complete(adapter=self.native(model))
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": True})
        restored = self.native(model)
        result = restored.wait(restored.resume(first.resume_ref, self.request), 5)
        self.assertEqual(result.error, "cleanup_failed")
        self.assertTrue((restored.root / "task-t1.quarantine").exists())
        self.python.run_python.assert_called_once()
        with self.assertRaises(HarnessError):
            restored.resume(result.resume_ref, self.request)
        with self.assertRaises(HarnessError):
            restored.start(self.request)

    def test_legacy_malformed_tool_name_is_diagnosed_without_crashing(self):
        class Legacy:
            def complete(self, *args, **kwargs):
                return {"type": "tool", "name": ["write_file"], "arguments": {}}
        _, result = self.complete(adapter=self.native(Legacy()))
        self.assertEqual(result.error, "invalid_decision_limit")
        self.assertEqual(self.fs.list_files(), ())

    def test_cleanup_failure_during_action_preparation_also_quarantines(self):
        self.executor.git.head.side_effect = ToolCleanupError("Git runtime still exists")
        model = NativeModel([action("git_push", repository="https://github.com/example/project.git", branch="main")])
        adapter = self.native(model)
        _, result = self.complete(adapter=adapter)
        self.assertEqual(result.error, "cleanup_failed")
        self.assertTrue((adapter.root / "task-t1.quarantine").exists())
        self.executor.git.push.assert_not_called()
        self.assertEqual(self.store.list_pending_requests(), [])

    def test_unexpected_tool_exception_preserves_safe_diagnostics_and_unknown_outcome_guard(self):
        model = NativeModel([action("list_files")])
        adapter = self.native(model)
        original = self.broker.call
        self.broker.call = Mock(side_effect=ValueError("private provider or file details"))
        try:
            handle, result = self.complete(adapter=adapter)
        finally:
            self.broker.call = original
        self.assertEqual(result.error, "platform_tool_error")
        record = json.loads((adapter.root / (handle.run_id + ".json")).read_text())
        detail = next(e["payload"] for e in record["events"] if e["kind"] == "platform_tool_exception")
        self.assertEqual(detail, {"name": "list_files", "exception_type": "ValueError"})
        self.assertNotIn("private provider or file details", str(record))
        self.assertEqual(record["inflight"], {"name": "list_files"})
        with self.assertRaises(HarnessError):
            adapter.resume(result.resume_ref, self.request)


class HistoryTests(unittest.TestCase):
    def test_old_content_is_compacted_without_mutating_evidence_or_failure_metadata(self):
        history = [{"tool": "write_file", "arguments": {"path": "file.md", "content": "x" * 16000},
                    "outcome": {"status": "success", "result": {"path": "file.md", "sha256": "digest"}}}
                   for _ in range(30)]
        history[0] = {"tool": "run_python", "arguments": {"path": "test.py"}, "outcome": {
            "status": "failed", "result": {"exit_code": 1, "stdout": "a" * 20000 + "FAIL", "timed_out": False}}}
        original = copy.deepcopy(history)
        compact = model_history(history)
        self.assertEqual(history, original)
        self.assertLess(len(json.dumps(compact)), 100000)
        self.assertEqual(compact[-4:], history[-4:])
        self.assertEqual(compact[0]["outcome"]["result"]["exit_code"], 1)
        self.assertIn("FAIL", compact[0]["outcome"]["result"]["stdout"])

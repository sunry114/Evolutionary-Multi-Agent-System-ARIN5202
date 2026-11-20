import copy
import json
import unittest
from unittest.mock import Mock

from evo.adapters.harness.context import native_messages
from evo.adapters.harness.progress import ProgressGuard


class ProgressTests(unittest.TestCase):
    def test_new_artifact_visibility_allows_a_fresh_listing(self):
        workspace = Mock()
        workspace.input_digest.return_value = "unchanged-workspace"
        loader = Mock(return_value=[])
        guard = ProgressGuard({}, workspace, loader)
        guard.refresh()
        guard.note("list_artifacts", {}, {"status": "success"})
        guard.note("list_artifacts", {}, {"status": "success"})
        self.assertTrue(guard.repeated("list_artifacts", {}))
        loader.return_value = [{"id": "newly-shared", "sha256": "new-content"}]
        self.assertFalse(guard.repeated("list_artifacts", {}))

    def test_unavailable_fingerprint_does_not_leave_stale_repetition_counts(self):
        workspace = Mock()
        workspace.input_digest.side_effect = ["version", ValueError("unreadable"), "version"]
        guard = ProgressGuard({}, workspace)
        guard.refresh()
        guard.state["counts"][guard.key("list_files", {})] = 2
        self.assertTrue(guard.repeated("list_files", {"path": "."}))
        guard.refresh()
        self.assertFalse(guard.repeated("list_files", {}))
        guard.refresh()
        self.assertFalse(guard.repeated("list_files", {}))

    def test_native_history_has_paired_ids_correct_order_and_no_mutation(self):
        history = [{"tool": "read_file", "arguments": {"path": "a.md"},
                    "outcome": {"status": "success", "result": "first"}},
                   {"tool": "write_file", "arguments": {"path": "b.md", "content": "second"},
                    "outcome": {"status": "success", "result": {"path": "b.md"}}}]
        original = copy.deepcopy(history)
        messages = native_messages("system", "current context", history)
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "tool", "assistant", "tool", "user"])
        self.assertEqual(messages[-1]["content"], "current context")
        self.assertEqual(messages[2]["tool_calls"][0]["id"], messages[3]["tool_call_id"])
        self.assertEqual(messages[4]["tool_calls"][0]["id"], messages[5]["tool_call_id"])
        self.assertEqual(json.loads(messages[3]["content"])["result"], "first")
        self.assertEqual(history, original)

    def test_native_history_budget_preserves_current_context(self):
        history = [{"tool": "write_file", "arguments": {"path": "a.md", "content": "x" * 70000},
                    "outcome": {"status": "success"}}]
        messages = native_messages("system", "current context", history)
        self.assertEqual(messages[-1], {"role": "user", "content": "current context"})
        self.assertLess(len(json.dumps(messages)), 65536)

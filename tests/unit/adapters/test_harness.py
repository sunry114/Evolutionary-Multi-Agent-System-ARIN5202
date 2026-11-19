import json
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from evo.adapters.harness import create_harness
from evo.adapters.harness.backend import local_claude_backend
from evo.adapters.harness.baseline import BaselineHarnessAdapter
from evo.config import HarnessConfig, HarnessModelConfig
from evo.ports.harness import HarnessError, ToolCall
from evo.adapters.harness.mcp_bridge import MCPBridge
from evo.adapters.harness.runtime import ManagedHarness
from evo.adapters.tools.web import WebToolError
from evo.ports.tools import ExecutionResult

from tests.fixtures.harness import HarnessFixture


class BaselineTests(HarnessFixture):
    def test_baseline_runs_explicit_actions_and_preserves_task_and_audit(self):
        request = replace(self.request, actions=(ToolCall("write_file", {"path": "report.md", "content": "done"}),
                                                 ToolCall("read_file", {"path": "report.md"})))
        handle, result = self.complete(request)
        self.assertEqual(result.status, "success")
        self.assertEqual(self.fs.read_file("report.md"), "done")
        self.assertEqual(result.artifacts[0].path, "report.md")
        self.assertEqual(self.store.get_task("t1")["status"], "running")
        events = self.adapter.events(handle)
        self.assertEqual(len(self.adapter.events(handle, events[-2].sequence)), 1)
        self.assertTrue(all(e.untrusted for e in events))
        self.assertIn("harness_finished", [e["event_type"] for e in self.store.list_events("t1")])

    def test_missing_model_loop_is_explicit(self):
        _, result = self.complete()
        self.assertEqual(result.error, "baseline_requires_explicit_actions")
        self.assertEqual(result.status, "failed")

    def test_approval_wait_survives_restart_then_executes_exactly_once(self):
        self.fs.write_file("main.py", "print('ok')", purpose="code")
        request = replace(self.request, actions=(ToolCall("run_python", {"path": "main.py"}),))
        _, first = self.complete(request)
        self.assertEqual(first.status, "waiting_human")
        self.python.run_python.assert_not_called()
        restored = BaselineHarnessAdapter(self.state, self.broker)
        pending = restored.resume(first.resume_ref, request)
        still_waiting = restored.wait(pending, 5)
        self.assertEqual(still_waiting.status, "waiting_human")
        self.python.run_python.assert_not_called()
        item = self.store.list_pending_requests()[0]
        self.approval.answer(item["id"], {"approved": True})
        resumed = restored.resume(still_waiting.resume_ref, request)
        result = restored.wait(resumed, 5)
        self.assertEqual(result.status, "success")
        self.assertNotEqual(first.native_session_id, result.native_session_id)
        self.python.run_python.assert_called_once_with("main.py", approved=True)
        # Reusing an old checkpoint cannot consume an already-used approval.
        replay = restored.resume(first.resume_ref, request)
        self.assertEqual(restored.wait(replay, 5).status, "failed")
        self.python.run_python.assert_called_once()

    def test_denial_blocks_without_side_effects(self):
        self.fs.write_file("report.md", "keep")
        request = replace(self.request, actions=(ToolCall("delete_file", {"path": "report.md"}),))
        _, first = self.complete(request)
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": False})
        with self.assertRaises(HarnessError):
            self.adapter.resume(first.resume_ref, request)
        self.assertEqual(self.fs.read_file("report.md"), "keep")

    def test_changed_content_cannot_reuse_approval(self):
        self.fs.write_file("main.py", "print(1)", purpose="code")
        request = replace(self.request, actions=(ToolCall("run_python", {"path": "main.py"}),))
        _, first = self.complete(request)
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": True})
        self.fs.write_file("main.py", "print(2)", purpose="code")
        handle = self.adapter.resume(first.resume_ref, request)
        self.assertEqual(self.adapter.wait(handle, 5).status, "failed")
        self.python.run_python.assert_not_called()

    def test_cross_agent_task_session_and_modified_context_are_rejected(self):
        _, first = self.complete()
        for request in (replace(self.request, agent="bob"), replace(self.request, task_id="t2"),
                        replace(self.request, session_scope="other"), replace(self.request, prompt="changed"),
                        replace(self.request, workspace=self.state)):
            with self.subTest(request=request), self.assertRaises(HarnessError):
                self.adapter.resume(first.resume_ref, request)
        forged = replace(first.resume_ref, agent="bob")
        with self.assertRaises(HarnessError):
            self.adapter.resume(forged, self.request)

    def test_interrupted_side_effect_is_never_replayed(self):
        handle, result = self.complete()
        path = self.adapter.root / (handle.run_id + ".json")
        record = json.loads(path.read_text())
        record["inflight"] = {"name": "run_python"}
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(HarnessError, "unknown"):
            self.adapter.resume(result.resume_ref, self.request)

    def test_scope_path_and_resource_limits_reject_invalid_input(self):
        for kwargs in ({"task_id": "../escape"}, {"workspace": Path("relative")}, {"prompt": ""},
                       {"memory": ("x" * 9000,)}):
            with self.assertRaises(ValueError):
                replace(self.request, **kwargs)
        for kwargs in ({"timeout": float("nan")}, {"max_tool_calls": 129}, {"cli_version": "latest"}):
            with self.assertRaises(ValueError):
                HarnessConfig(**kwargs)
        self.assertEqual(HarnessConfig().max_tool_calls, 80)
        self.assertEqual(HarnessConfig(max_tool_calls=128).max_tool_calls, 128)
        with self.assertRaises(HarnessError):
            self.adapter.events(type("Handle", (), {"run_id": "../secret"})())

    def test_registry_does_not_silently_fallback(self):
        self.assertIsInstance(create_harness(HarnessConfig(), self.state, self.broker), BaselineHarnessAdapter)
        for name in ("pi", "custom"):
            with self.assertRaises(HarnessError):
                create_harness(HarnessConfig(adapter=name), self.state, self.broker)

    def test_tool_limit_is_enforced_across_resume(self):
        adapter = BaselineHarnessAdapter(self.state, self.broker, config=HarnessConfig(max_tool_calls=1))
        request = replace(self.request, actions=(ToolCall("list_files", {}), ToolCall("list_files", {})))
        handle, result = self.complete(request, adapter)
        self.assertNotEqual(result.status, "success")
        self.assertEqual(json.loads((adapter.root / (handle.run_id + ".json")).read_text())["tool_calls"], 1)
        actions = [e for e in self.store.list_events("t1") if e["event_type"] == "action_started"]
        self.assertEqual(len(actions), 1)

    def test_default_budget_allows_more_than_twenty_platform_calls(self):
        class ProbeHarness(ManagedHarness):
            adapter_name = "baseline"

            def _execute(self, control, request):
                for _ in range(21):
                    if self._call(control, request, "list_files", {})["status"] != "success":
                        return "failed", "", "platform_action_stopped", None
                return "success", "done", "", 0

        adapter = ProbeHarness(self.state, self.broker)
        handle, result = self.complete(self.request, adapter)
        self.assertEqual(result.status, "success")
        self.assertEqual(json.loads((adapter.root / (handle.run_id + ".json")).read_text())["tool_calls"], 21)


class BridgeTests(HarnessFixture):
    def test_large_failed_test_output_keeps_exit_code_and_failure_tail(self):
        class ProbeHarness(ManagedHarness):
            adapter_name = "baseline"

            def _execute(self, control, request):
                result = self._call(control, request, "run_python", {"path": "check.py"})
                detail = result["result"]
                return "success", str(detail["exit_code"]) + ":" + (
                    "FAIL C7" if "FAIL C7" in detail["output_excerpt"] else "missing failure"), "", 0

        output = {"status": "failed", "action_id": "fake-action",
                  "result": {"exit_code": 1, "stdout": "x" * 20000 + "FAIL C7",
                             "stderr": "", "timed_out": False, "output_limited": False}}
        harness = ProbeHarness(self.state, self.broker)
        with patch.object(self.broker, "call", return_value=output):
            _, result = self.complete(adapter=harness)
        self.assertEqual(result.status, "success")
        self.assertIn("1:", result.text)
        self.assertIn("FAIL C7", result.text)

    def test_claude_resume_receives_failed_test_output_for_repair(self):
        class ProbeHarness(ManagedHarness):
            adapter_name = "claude_code"

            def _execute(self, control, request):
                context = control.record["context"]
                if context and context[-1]["tool"] == "run_python":
                    return "success", context[-1]["outcome"]["result"]["stdout"], "", 0
                result = self._call(control, request, "run_python", {"path": "check.py"})
                return result["status"], "", "", None

        self.fs.write_file("check.py", "raise SystemExit(1)", purpose="code")
        self.python.run_python.return_value = ExecutionResult(1, "FAIL C7", "")
        harness = ProbeHarness(self.state, self.broker,
                               config=HarnessConfig(adapter="claude_code"))
        _, first = self.complete(adapter=harness)
        self.assertEqual(first.status, "waiting_human")
        self.approval.answer(self.store.list_pending_requests()[0]["id"], {"approved": True})
        handle = harness.resume(first.resume_ref, self.request)
        second = harness.wait(handle, 5)
        self.assertEqual((second.status, second.text), ("success", "FAIL C7"))
        self.assertEqual(self.store.get_task("t1")["status"], "running")
        self.python.run_python.assert_called_once_with("check.py", approved=True)

    def test_recoverable_search_failure_does_not_stop_other_tools(self):
        class OfflineWeb:
            def search(self, query):
                raise WebToolError("Web request timed out")

        class ProbeHarness(ManagedHarness):
            adapter_name = "baseline"

            def _execute(self, control, request):
                self._call(control, request, "search", {"query": "meeting rooms"})
                self._call(control, request, "list_files", {"path": "."})
                return "success", "continued", "", 0

        self.broker.web = OfflineWeb()
        harness = ProbeHarness(self.state, self.broker)
        handle, result = self.complete(adapter=harness)
        self.assertEqual((result.status, result.text), ("success", "continued"), repr(result))
        outcomes = [event.payload["outcome"] for event in harness.events(handle)
                    if event.kind == "tool_result"]
        self.assertEqual([item["status"] for item in outcomes], ["failed", "success"])
        self.assertEqual(outcomes[0]["error"], "web_tool_failed")
        actions = [event for event in self.store.list_events("t1")
                   if event["event_type"] == "action_completed"]
        self.assertEqual([event["payload"]["status"] for event in actions], ["failed", "success"])

    def test_tools_cannot_supply_identity_approval_or_arbitrary_paths(self):
        for name, args in (("run_python", {"path": "main.py", "approved": True}),
                           ("write_file", {"path": "report.md", "content": "x", "agent": "bob"}),
                           ("Bash", {"command": "id"}), ("list_files", {"path": "/"}),
                           ("write_file", {"path": "../escape.md", "content": "x"}),
                           ("write_file", {"path": "report.md", "content": 12})):
            with self.subTest(name=name, args=args), self.assertRaises(HarnessError):
                self.broker.call("t1", name, args)
        self.python.run_python.assert_not_called()

    def test_real_socket_initialize_tools_call_and_notification(self):
        with MCPBridge(self.state / "mcp.sock", self.broker,
                       lambda name, arguments: self.broker.call("t1", name, arguments)):
            with socket.socket(socket.AF_UNIX) as channel:
                channel.connect(str(self.state / "mcp.sock"))
                with channel.makefile("rb") as reader:
                    def rpc(method, params=None, ident=1):
                        channel.sendall(json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}).encode() + b"\n")
                        return json.loads(reader.readline())
                    self.assertIn("tools", rpc("initialize")["result"]["capabilities"])
                    channel.sendall(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                    self.assertIn("write_file", [x["name"] for x in rpc("tools/list")["result"]["tools"]])
                    self.assertTrue(rpc("tools/call", {"name": "delete_file", "arguments": {"path": "a.md", "approved": True}})["result"]["isError"])
                    result = rpc("tools/call", {"name": "write_file", "arguments": {"path": "a.md", "content": "ok"}})
                    self.assertFalse(result["result"]["isError"])
                    self.assertEqual(self.fs.read_file("a.md"), "ok")


class BackendTests(unittest.TestCase):
    def test_third_party_and_http_are_explicit_dev_only(self):
        HarnessModelConfig(model="deepseek-model")
        for kwargs in ({"provider": "development", "endpoint": "http://127.0.0.1:1234"},
                       {"endpoint": "https://other.example"},
                       {"provider": "development", "endpoint": "http://127.0.0.1:1234", "development": True, "evaluation": True},
                       {"endpoint": "https://user:password@example.com"}):
            with self.assertRaises(ValueError):
                HarnessModelConfig(model="test-model", **kwargs)

    def test_local_config_import_is_opt_in_bounded_and_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1234", "ANTHROPIC_AUTH_TOKEN": "fixture-credential",
                                               "ANTHROPIC_MODEL": "test-model"}, "hooks": {"ignored": "bad"}}))
            path.chmod(0o600)
            with self.assertRaises(HarnessError):
                local_claude_backend(path)
            model, loader = local_claude_backend(path, development=True)
            self.assertNotIn("credential", repr(model))
            self.assertEqual(loader(), "fixture-credential")
            path.chmod(0o666)
            with self.assertRaises(HarnessError):
                loader()
            path.chmod(0o600)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(HarnessError):
                local_claude_backend(link, development=True)

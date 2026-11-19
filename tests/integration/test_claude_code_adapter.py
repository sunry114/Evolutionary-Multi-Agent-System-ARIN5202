"""Offline CLI subprocess/protocol tests; real native CLI is separately opt-in."""

import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from evo.adapters.harness.claude_code import ClaudeCodeAdapter
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.adapters.tools.git import DockerGitTools
from evo.config import HarnessConfig, HarnessModelConfig
from evo.ports.harness import HarnessCleanupError, HarnessError
from tests.fixtures.harness import FakeCLITransport, HarnessFixture


class ClaudeProtocolTests(HarnessFixture):
    def setUp(self):
        super().setUp()
        self.config = HarnessConfig(adapter="claude_code", timeout=3)
        self.adapter = ClaudeCodeAdapter(self.state, self.broker, config=self.config,
                                         model=HarnessModelConfig(model="fixture-model"),
                                         credential_loader=lambda: "fixture-credential")
        self.transport = FakeCLITransport(self.config)
        self.adapter.transport = self.transport

    def test_structured_events_results_and_cli_whitelist(self):
        handle, result = self.complete()
        self.assertEqual(result.status, "success")
        self.assertEqual(result.text, "fixture success")
        command = self.transport.command
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--bare", command)
        self.assertNotIn("fixture-credential", " ".join(command))
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertIn("assistant", [event.kind for event in self.adapter.events(handle)])

    def test_system_status_updates_do_not_record_repeated_starts(self):
        handle, result = self.complete(replace(self.request, prompt="system_status_updates"))
        self.assertEqual(result.status, "success")
        self.assertEqual([event.kind for event in self.adapter.events(handle)].count("started"), 1)

    def test_mcp_tool_call_reaches_bound_workspace_and_audit(self):
        _, result = self.complete(replace(self.request, prompt="mcp"))
        self.assertEqual(result.status, "success")
        self.assertEqual(self.fs.read_file("fake.md"), "from MCP")
        self.assertEqual(result.artifacts[0].path, "fake.md")

    def test_missing_git_repository_is_audited_and_file_execution_continues(self):
        self.executor.git = DockerGitTools(self.fs)
        handle, result = self.complete(replace(self.request, prompt="missing_git_repository"))
        self.assertEqual(result.status, "success")
        self.assertEqual(self.fs.read_file("fake.md"), "from MCP")
        self.assertFalse((self.fs.workspace / ".git").exists())
        probe = next(event for event in self.adapter.events(handle)
                     if event.kind == "tool_result" and event.payload["name"] == "git_status")
        action = self.store.get_action(probe.payload["action_id"])
        self.assertEqual(action["status"], "failed")
        self.assertEqual(action["result"]["error"], "git_repository_missing")
        self.assertEqual(self.store.get_task(self.request.task_id)["status"], "running")

    def test_native_issue_tools_report_progress_and_export_a_file(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        self.broker.issue_controls_only = True
        self.broker.issue_comment = lambda content: {}
        self.fs.write_file("before.md", "Previous output")
        prior = ArtifactExchange(self.fs).publish("before.md", ["alice"])
        self.adapter.issue_artifacts = ({"event_id": 1, "owner": "alice", "path": "before.md",
                                         "purpose": "document", "archive": {"id": prior.id, "size": prior.size,
                                                                              "sha256": prior.sha256}},)
        handle, result = self.complete(replace(self.request, prompt="native_issue"))
        self.assertEqual(result.status, "success")
        self.assertEqual([item.path for item in result.artifacts], ["hello-world.md"])
        self.assertEqual(self.fs.read_file("hello-world.md"), "# Hello World\n")
        calls = [event.payload for event in self.adapter.events(handle) if event.kind == "native_tool"]
        self.assertEqual(calls, [{"name": name, "status": status} for name in ("Bash", "Write", "Read")
                                 for status in ("started", "completed")])
        command = self.transport.command
        self.assertEqual(command[command.index("--tools") + 1], "Bash,Read,Write")
        self.assertEqual(command[command.index("--allowedTools") + 1], "Bash,Read,Write,mcp__evo__post_issue_comment")
        self.assertIn("--restricted", command)
        self.assertIn("/broker/inputs", command)
        self.assertNotIn("--bare", command)

    def test_native_issue_rejects_artifact_outside_work_directory(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        _, result = self.complete(replace(self.request, prompt="native_issue_bad_export"))
        self.assertEqual((result.status, result.error), ("failed", "protocol_error"))
        self.assertEqual(result.artifacts, ())
        self.assertFalse((self.fs.workspace.parent / "outside.md").exists())

    def test_protocol_exit_and_native_tool_failures(self):
        for mode in ("invalid", "missing", "nonzero", "wrong_session", "error_result", "blocked_native", "failed_mcp"):
            with self.subTest(mode=mode):
                _, result = self.complete(replace(self.request, prompt=mode))
                self.assertEqual(result.status, "failed")
                self.assertNotIn("sensitive diagnostic", result.text)

    def test_timeout_and_stderr_flood_are_bounded(self):
        self.adapter.config = replace(self.config, timeout=0.2, max_output_bytes=1024)
        self.adapter.transport = FakeCLITransport(self.adapter.config)
        for mode, status, error in (("hang", "timed_out", "timeout"), ("flood", "failed", "output_limit")):
            with self.subTest(mode=mode):
                _, result = self.complete(replace(self.request, prompt=mode))
                self.assertEqual((result.status, result.error), (status, error))

    def test_reading_live_events_does_not_reread_the_checkpoint(self):
        handle = self.adapter.start(replace(self.request, prompt="hang"))
        self.addCleanup(lambda: self.adapter.cancel(handle))
        with patch.object(self.adapter, "_load", side_effect=HarnessError("Concurrent checkpoint replacement")) as load:
            self.assertEqual(self.adapter.events(handle), ())
            load.assert_not_called()
        self.assertEqual(self.adapter.cancel(handle).status, "cancelled")

    def test_cancel_kills_child_process_group_and_releases_task_lease(self):
        pid_file = self.state / "child.pid"
        self.adapter.transport = FakeCLITransport(self.config, child_pid=pid_file)
        handle = self.adapter.start(replace(self.request, prompt="descendant"))
        self.addCleanup(lambda: self.adapter.cancel(handle))
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(pid_file.exists())
        child_pid = int(pid_file.read_text())
        self.assertIsNone(self.adapter.wait(handle, timeout=0.01))
        with self.assertRaisesRegex(HarnessError, "active"):
            self.adapter.start(self.request)
        # A separate adapter instance/process must use the same task lease.
        from evo.adapters.harness.baseline import BaselineHarnessAdapter
        with self.assertRaisesRegex(HarnessError, "active"):
            BaselineHarnessAdapter(self.state, self.broker).start(self.request)
        from evo.domain.models import Task
        from evo.domain.statuses import TaskStatus
        self.store.save_task(Task("t2", "Other task for same Agent", owner="alice", status=TaskStatus.RUNNING))
        with self.assertRaisesRegex(HarnessError, "workspace"):
            self.adapter.start(replace(self.request, task_id="t2"))
        result = self.adapter.cancel(handle)
        self.assertEqual(result.status, "cancelled")
        child_status = Path(f"/proc/{child_pid}/status")
        if child_status.exists():
            self.assertIn("State:\tZ", child_status.read_text())
        self.complete()

    def test_restart_replays_saved_context_only_in_same_backend_scope(self):
        _, first = self.complete()
        restored = ClaudeCodeAdapter(self.state, self.broker, config=self.config, model=self.adapter.model,
                                     credential_loader=lambda: "fixture-credential")
        restored.transport = FakeCLITransport(self.config)
        handle = restored.resume(first.resume_ref, self.request)
        second = restored.wait(handle, 5)
        self.assertEqual(second.status, "success")
        self.assertNotEqual(first.native_session_id, second.native_session_id)
        restored.model = HarnessModelConfig(model="another-model")
        with self.assertRaises(HarnessError):
            restored.resume(second.resume_ref, self.request)

    def test_secret_result_is_redacted_in_output_and_checkpoint(self):
        handle, result = self.complete(replace(self.request, prompt="secret"))
        self.assertEqual(result.text, "[REDACTED]")
        self.assertNotIn("fixture-credential", (self.adapter.root / (handle.run_id + ".json")).read_text())

    def test_start_failure_is_safe_and_not_a_host_fallback(self):
        self.adapter.transport.run = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("credential secret"))
        _, result = self.complete()
        self.assertEqual(result.status, "failed")
        self.assertNotIn("secret", repr(result))

    def test_failed_cleanup_quarantines_task_across_restart(self):
        def fail(*args, **kwargs):
            raise HarnessCleanupError("Cannot verify runtime cleanup")
        self.adapter.transport.run = fail
        _, result = self.complete()
        self.assertEqual((result.status, result.error), ("failed", "cleanup_failed"))
        self.assertTrue((self.adapter.root / "task-t1.quarantine").exists())
        with self.assertRaisesRegex(HarnessError, "reconciliation"):
            self.adapter.start(self.request)
        restored = ClaudeCodeAdapter(self.state, self.broker, config=self.config, model=self.adapter.model,
                                     credential_loader=lambda: "fixture-credential")
        with self.assertRaisesRegex(HarnessError, "reconciliation"):
            restored.resume(result.resume_ref, self.request)

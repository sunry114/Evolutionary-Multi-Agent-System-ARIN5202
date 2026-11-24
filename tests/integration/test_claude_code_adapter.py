"""Offline CLI subprocess/protocol tests; real native CLI is separately opt-in."""

import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from evo.adapters.harness.claude_code import ClaudeCodeAdapter
from evo.adapters.tools.git import HostGitTools
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
        self.executor.git = HostGitTools(self.fs)
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

    def test_native_issue_tools_run_in_project_and_index_real_files(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        self.broker.issue_controls_only = True
        self.broker.issue_comment = lambda content: {}
        project = self.state / "project"
        project.mkdir()
        (project / "AGENT.md").write_text("Keep articles in docs/.\n", encoding="utf-8")
        self.adapter.project_dir = project
        previous = self.fs.workspace.parent / "prior"
        previous.mkdir()
        (previous / "before.md").write_text("Previous output")
        self.adapter.issue_artifacts = ({"task_id": "prior", "owner": "alice", "path": "before.md", "archive": {}},)
        handle, result = self.complete(replace(self.request, prompt="native_issue"))
        self.assertEqual(result.status, "success")
        self.assertEqual([item.path for item in result.artifacts], ["docs/hello-world.md"])
        self.assertEqual((project / "docs/hello-world.md").read_text(), "# Hello World\n")
        self.assertEqual(self.fs.read_file("docs/hello-world.md"), "# Hello World\n")
        prompt = json.loads(self.transport.prompt["message"]["content"])
        self.assertEqual(prompt["project_instructions"], {"AGENT.md": "Keep articles in docs/.\n"})
        self.assertNotIn("deliverables_directory", prompt)
        calls = [event.payload for event in self.adapter.events(handle) if event.kind == "native_tool"]
        self.assertEqual([(call["name"], call["status"]) for call in calls],
                         [(name, status) for name in ("Bash", "Write", "Read") for status in ("started", "completed")])
        self.assertTrue(all(call["output"] == "fixture result" and not call["output_truncated"]
                            for call in calls if call["status"] == "completed"))
        command = self.transport.command
        self.assertEqual(command[command.index("--input-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertEqual(settings["pluginConfigs"]["cc-plugin-agents-md@builtin"]["options"]["instructionFiles"],
                         "claude-md-and-agents-md")
        self.assertEqual(self.transport.cwd, project)

    def test_native_issue_does_not_index_symlinks_outside_the_deliverables_directory(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        (self.fs.workspace.parent / "outside.md").write_text("External file")
        _, result = self.complete(replace(self.request, prompt="native_issue_output_symlink"))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.artifacts, ())

    def test_native_webfetch_http_status_is_recorded_as_a_failed_tool_result(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        handle, result = self.complete(replace(self.request, prompt="native_issue_webfetch_http_error"))
        self.assertEqual(result.status, "success")
        _, outcome = [event.payload for event in self.adapter.events(handle) if event.kind == "native_tool"]
        self.assertEqual((outcome["name"], outcome["status"], outcome["http_status"]), ("WebFetch", "failed", 403))

    def test_failed_native_run_leaves_its_work_on_disk(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        project = self.state / "project"
        project.mkdir()
        self.adapter.project_dir = project
        _, result = self.complete(replace(self.request, prompt="native_issue_failed_with_output"))
        self.assertEqual((result.status, result.error), ("failed", "cli_failed"))
        self.assertEqual(result.artifacts, ())
        self.assertEqual((project / "hello-world.md").read_text(), "# Hello World\n")

    def test_native_issue_can_archive_after_platform_tool_budget_runs_out(self):
        self.adapter.config = replace(self.config, native_issue_tools=True, max_tool_calls=1)
        self.broker.web = Mock()
        self.broker.web.search.return_value = []
        _, result = self.complete(replace(self.request, prompt="native_issue_tool_limit"))
        self.assertEqual(result.status, "success")
        self.assertEqual([item.path for item in result.artifacts], ["hello-world.md"])
        self.broker.web.search.assert_called_once_with(query="fixture")
        self.broker.web.fetch_url.assert_not_called()

    def test_native_issue_tool_failures_do_not_cut_off_the_final_reply(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        handle, result = self.complete(replace(self.request, prompt="native_issue_many_tools"))
        self.assertEqual(result.status, "success")
        events = self.adapter.events(handle)
        tool_statuses = [event.payload["status"] for event in events if event.kind == "native_tool"]
        self.assertEqual(tool_statuses.count("started"), 24)
        self.assertEqual(tool_statuses.count("failed"), 1)
        final, = [event.payload["text"] for event in events if event.kind == "terminal_output"]
        self.assertEqual(final, "x" * 10000 + "\nThe first tool failed. The remaining steps finished.")

    def test_native_issue_preserves_cli_errors_without_leaking_credentials(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        for mode, expected in (
            ("native_issue_api_error", "API Error: 401 [REDACTED] authentication rejected"),
            ("native_issue_result_errors", "Connection closed by the model API\n\nRequest ID: fixture"),
            ("native_issue_stderr", "Unable to connect with [REDACTED]"),
        ):
            with self.subTest(mode=mode):
                handle, result = self.complete(replace(self.request, prompt=mode))
                self.assertEqual((result.status, result.error, result.exit_code), ("failed", "cli_failed", 1))
                final, = [event.payload["text"] for event in self.adapter.events(handle)
                          if event.kind == "terminal_output"]
                self.assertTrue(final.endswith(expected))
                self.assertNotIn("fixture-credential", final)
                self.assertEqual(result.artifacts, ())
                if mode == "native_issue_stderr":
                    self.assertTrue(final.startswith("[Earlier stderr truncated]"))

    def test_native_issue_keeps_api_retry_error_if_runtime_times_out(self):
        self.adapter.config = replace(self.config, native_issue_tools=True, timeout=1)
        self.adapter.transport = FakeCLITransport(self.adapter.config)
        handle, result = self.complete(replace(self.request, prompt="native_issue_api_timeout"))
        self.assertEqual((result.status, result.error), ("timed_out", "timeout"))
        self.assertEqual(result.text, "Claude Code API retry: connection_error")
        self.assertIn("api_retry", [event.kind for event in self.adapter.events(handle)])

    def test_protocol_exit_and_native_tool_failures(self):
        for mode in ("invalid", "missing", "nonzero", "wrong_session", "error_result", "blocked_native", "failed_mcp"):
            with self.subTest(mode=mode):
                _, result = self.complete(replace(self.request, prompt=mode))
                self.assertEqual(result.status, "failed")
                self.assertNotIn("sensitive diagnostic", result.text)

    def test_timeout_and_single_event_limits_are_independent(self):
        self.adapter.config = replace(self.config, timeout=0.5, max_event_bytes=1024)
        self.adapter.transport = FakeCLITransport(self.adapter.config)
        for mode, status, error in (("hang", "timed_out", "timeout"), ("flood", "timed_out", "timeout"),
                                    ("oversized_event", "failed", "event_size_limit")):
            with self.subTest(mode=mode):
                _, result = self.complete(replace(self.request, prompt=mode))
                self.assertEqual((result.status, result.error), (status, error))

    def test_long_stdout_and_stderr_streams_complete_and_keep_events(self):
        self.adapter.config = replace(self.config, timeout=12, max_event_bytes=32 * 1024)
        self.adapter.transport = FakeCLITransport(self.adapter.config)
        handle = self.adapter.start(replace(self.request, prompt="large_stream"))
        self.addCleanup(lambda: self.adapter.cancel(handle))
        result = self.adapter.wait(handle, 15)
        self.assertEqual(result.status, "success")
        tail = self.adapter.events(handle, after=520)
        self.assertEqual([(event.sequence, event.kind) for event in tail],
                         [(521, "assistant"), (522, "assistant"), (523, "result")])
        self.assertLess((self.adapter.root / (handle.run_id + ".json")).stat().st_size, 4096)

    def test_native_tool_output_preview_is_utf8_bounded_and_redacted(self):
        self.adapter.config = replace(self.config, native_issue_tools=True)
        handle, result = self.complete(replace(self.request, prompt="native_issue_long_tool_output"))
        self.assertEqual(result.status, "success")
        preview = next(event.payload for event in self.adapter.events(handle)
                       if event.kind == "native_tool" and event.payload["name"] == "Bash"
                       and event.payload["status"] == "completed")
        self.assertTrue(preview["output_truncated"])
        self.assertTrue(preview["output"].startswith("[REDACTED] 长"))
        self.assertLessEqual(len(preview["output"].encode()), 8192)
        self.assertNotIn("tail", preview["output"])
        self.assertEqual([item.path for item in result.artifacts], ["hello-world.md"])

    def test_long_mcp_tool_output_reaches_claude_and_only_records_preview(self):
        self.fs.write_file("large.md", "a" * 65536 + "end of tool result")
        handle, result = self.complete(replace(self.request, prompt="large_mcp"))
        self.assertEqual(result.status, "success")
        recorded = next(event.payload["outcome"] for event in self.adapter.events(handle)
                        if event.kind == "tool_result" and event.payload["name"] == "read_file")
        self.assertTrue(recorded["output_truncated"])
        self.assertLessEqual(len(recorded["result"].encode()), 8192)
        self.assertNotIn("end of tool result", recorded["result"])

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

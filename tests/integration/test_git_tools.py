import base64
import os
import tempfile
import unittest
from pathlib import Path

from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.git import DockerGitTools
from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus


@unittest.skipUnless(os.environ.get("EVO_DOCKER_TESTS") == "1", "Set EVO_DOCKER_TESTS=1 for real Git containers")
class GitIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fs = WorkspaceFilesystem(Path(temp.name), "alice")
        self.git = DockerGitTools(self.fs)
        result = self.git.init()
        self.assertEqual(result.exit_code, 0, result.stderr)

    def test_init_status_commit_only_explicit_files(self):
        self.fs.write_file("report.md", "first")
        self.fs.write_file("other.md", "not committed")
        result = self.git.commit(["report.md"], "Initial report")
        self.assertEqual(result.exit_code, 0, result.stderr)
        first = self.git.head()
        self.assertEqual(self.git.status().stdout, "?? other.md\n")
        self.fs.write_file("report.md", "second")
        self.assertEqual(self.git.commit(["report.md"], "Update report").exit_code, 0)
        self.assertNotEqual(self.git.head(), first)
        self.assertEqual(self.git.status().stdout, "?? other.md\n")

    def test_hooks_are_never_executed(self):
        hooks = self.fs.workspace / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        for name in ("pre-commit", "post-commit"):
            hook = hooks / name
            hook.write_text("#!/bin/sh\necho unsafe > /workspace/hook-ran.txt\nexit 1\n")
            hook.chmod(0o755)
        self.fs.write_file("report.md", "safe")
        result = self.git.commit(["report.md"], "No hooks")
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertFalse((self.fs.workspace / "hook-ran.txt").exists())

    def test_pre_staged_unlisted_files_are_not_committed(self):
        self.fs.write_file("report.md", "selected")
        self.fs.write_file("other.md", "not selected")
        # Test setup still runs Git inside the real container.
        self.assertEqual(self.git._run(["add", "--", "other.md"]).exit_code, 0)
        self.assertEqual(self.git.commit(["report.md"], "Selected only").exit_code, 0)
        self.assertEqual(self.git.status().stdout, "A  other.md\n")

    def test_nonzero_commit_and_container_identity(self):
        self.fs.write_file("report.md", "same")
        self.assertEqual(self.git.commit(["report.md"], "Initial").exit_code, 0)
        self.assertNotEqual(self.git.commit(["report.md"], "No changes").exit_code, 0)
        self.assertEqual((self.fs.workspace / ".git").stat().st_uid, os.getuid())

    def test_real_python_action_waits_for_durable_approval_and_executes_once(self):
        store = SQLiteStore(self.fs.workspace.parent.parent.parent / "events.sqlite3")
        store.save_task(Task("t1", "Create result", owner="alice", status=TaskStatus.RUNNING))
        service = ApprovalService(store)
        self.fs.write_file("main.py", "from pathlib import Path; Path('result.txt').write_text('approved')", purpose="code")
        executor = ActionExecutor(store, service, agent="alice", workspace=self.fs,
                                  python=DockerPythonRunner(self.fs), git=self.git)
        waiting = executor.execute("t1", "run_python", {"path": "main.py"})
        self.assertEqual(waiting["status"], "waiting_human")
        self.assertFalse((self.fs.workspace / "result.txt").exists())
        service.answer(waiting["request_id"], {"approved": True})
        self.assertFalse((self.fs.workspace / "result.txt").exists())
        self.assertEqual(executor.execute("t1", "run_python", {"path": "main.py"})["status"], "success")
        self.assertEqual(self.fs.read_file("result.txt"), "approved")
        # The script created a new input file, so another invocation has a new
        # workspace scope. It must pause for fresh approval, never run silently.
        timestamp = (self.fs.workspace / "result.txt").stat().st_mtime_ns
        again = executor.execute("t1", "run_python", {"path": "main.py"})
        self.assertEqual(again["status"], "waiting_human")
        self.assertNotEqual(again["request_id"], waiting["request_id"])
        self.assertEqual((self.fs.workspace / "result.txt").stat().st_mtime_ns, timestamp)

    def test_real_push_wrapper_limits_temporary_header_to_exact_repository(self):
        repository = "https://github.com/example/project.git"
        secret = base64.b64encode(b"x-access-token:fixture_token") + b"\n"
        before = (self.fs.workspace / ".git" / "config").read_bytes()
        result = self.git._run(["config", "--get-urlmatch", "http.extraHeader", repository + "/info/refs"],
                               repository=repository, input_data=secret)
        self.assertEqual(result.exit_code, 0, result.stderr)
        other = self.git._run(["config", "--get-urlmatch", "http.extraHeader", "https://github.com/example/other.git"],
                              repository=repository, input_data=secret)
        self.assertNotEqual(other.exit_code, 0)
        self.assertEqual((self.fs.workspace / ".git" / "config").read_bytes(), before)
        self.assertNotIn("fixture_token", str(result))

"""Test-only local fake CLI transport. Production always uses Docker."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock
from evo.adapters.harness.baseline import BaselineHarnessAdapter
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.ports.harness import RunRequest

from pathlib import Path

from evo.adapters.harness.process import capture


class FakeCLITransport:
    def __init__(self, config, *, child_pid=None):
        self.config = config
        self.child_pid = child_pid
        self.command = None

    def run(self, args, prompt, directory, *, stop, on_line):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FAKE_MCP_SOCKET": str(Path(directory) / "mcp.sock")}
        if self.child_pid:
            env["FAKE_CHILD_PID"] = str(self.child_pid)
        self.command = [sys.executable, str(Path(__file__).with_name("fake_claude_cli.py"))] + args
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=env, start_new_session=True)
        return capture(process, prompt, timeout=self.config.timeout, max_event_bytes=self.config.max_event_bytes,
                       stop=stop, on_line=on_line, keep_stderr=True)


class HarnessFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.store = SQLiteStore(self.state / "events.sqlite3")
        self.store.save_task(Task("t1", "Harness test", owner="alice", status=TaskStatus.RUNNING))
        self.fs = WorkspaceFilesystem(self.state, "alice")
        self.approval = ApprovalService(self.store)
        self.python = Mock()
        from evo.ports.tools import ExecutionResult
        self.python.run_python.return_value = ExecutionResult(0, "executed", "")
        self.executor = ActionExecutor(self.store, self.approval, agent="alice", workspace=self.fs,
                                       python=self.python, git=Mock())
        self.broker = PlatformToolBroker(self.executor)
        self.request = RunRequest("t1", "alice", "session1", self.fs.workspace, "test")
        self.adapter = BaselineHarnessAdapter(self.state, self.broker)

    def complete(self, request=None, adapter=None):
        adapter = adapter or self.adapter
        handle = adapter.start(request or self.request)
        self.addCleanup(lambda: adapter.cancel(handle))
        result = adapter.wait(handle, timeout=5)
        self.assertIsNotNone(result)
        return handle, result

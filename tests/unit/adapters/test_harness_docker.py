import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evo.adapters.harness.docker_cli import DockerCLITransport
from evo.adapters.harness.process import ProcessOutcome
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.config import HarnessConfig
from evo.ports.harness import HarnessError
from evo.ports.tools import ExecutionResult


class DockerHarnessCommandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        self.workspace = WorkspaceFilesystem(self.state, "alice")
        self.transport = DockerCLITransport(self.workspace, HarnessConfig(adapter="claude_code", cli_path="/usr/bin/true"))
        self.result = ExecutionResult(0, "", "")

    @patch("evo.adapters.harness.docker_cli.capture", return_value=ProcessOutcome(0))
    @patch("evo.adapters.harness.docker_cli.subprocess.Popen")
    def test_container_is_networkless_has_no_agent_state_or_real_secrets(self, popen, capture):
        self.transport.helper._capture = Mock(side_effect=[ExecutionResult(0, "sha256:" + "a" * 64, ""), self.result,
                                                          ExecutionResult(0, "exited 0", ""), self.result])
        result = self.transport.run(["--version"], "prompt", self.state, stop=lambda: "", on_line=lambda x: None)
        self.assertEqual(result.exit_code, 0)
        command = self.transport.helper._capture.call_args_list[1].args[0]
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--read-only", command)
        self.assertIn("no-new-privileges", command)
        self.assertIn("--pids-limit", command)
        self.assertNotIn(str(self.workspace.workspace), " ".join(command))
        self.assertNotIn("events.sqlite3", " ".join(command))
        self.assertNotIn("GITHUB_TOKEN", " ".join(command))
        self.assertNotIn("/var/run/docker.sock,dst", " ".join(command))
        self.assertIn("ANTHROPIC_API_KEY=evo-broker-placeholder", command)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(set(env), {"PATH", "DOCKER_CONFIG", "LC_ALL"})
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIn("rm", self.transport.helper._capture.call_args.args[0])

    def test_missing_runtime_fails_without_host_execution(self):
        with patch("evo.adapters.harness.docker_cli.shutil.which", return_value=None), \
                patch("evo.adapters.harness.docker_cli.subprocess.Popen") as process:
            with self.assertRaises(HarnessError):
                self.transport.run([], "", self.state, stop=lambda: "", on_line=lambda x: None)
            process.assert_not_called()

    def test_unverified_image_never_starts_a_container(self):
        self.transport.helper._capture = Mock(return_value=ExecutionResult(0, "not-an-image-id", ""))
        with self.assertRaises(HarnessError):
            self.transport.run([], "", self.state, stop=lambda: "", on_line=lambda x: None)
        self.assertEqual(self.transport.helper._capture.call_count, 1)

    def test_cleanup_failure_is_reported(self):
        self.transport.helper._capture = Mock(side_effect=[ExecutionResult(0, "sha256:" + "a" * 64, ""),
                                                          ExecutionResult(1, "", ""), ExecutionResult(1, "", "")])
        with self.assertRaisesRegex(HarnessError, "cleanup"):
            self.transport.run([], "", self.state, stop=lambda: "", on_line=lambda x: None)

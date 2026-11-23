import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evo.adapters.harness.host_cli import HostCLITransport
from evo.adapters.harness.process import ProcessOutcome, capture
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.host import HostPythonRunner
from evo.config import HarnessConfig
from evo.ports.harness import HarnessError


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_cli_starts_in_the_bound_directory_with_a_process_group(self):
        config = HarnessConfig(adapter="claude_code", cli_path="/usr/bin/claude")
        transport = HostCLITransport(config)
        settings = self.root / "settings"
        with patch("evo.adapters.harness.host_cli.shutil.which", return_value=config.cli_path), \
                patch("evo.adapters.harness.host_cli.os.getuid", return_value=1000), \
                patch("evo.adapters.harness.host_cli.os.getgid", return_value=1000), \
                patch("evo.adapters.harness.host_cli.subprocess.run",
                      return_value=Mock(stdout=config.cli_version + " (Claude Code)\n")), \
                patch("evo.adapters.harness.host_cli.subprocess.Popen") as popen, \
                patch("evo.adapters.harness.host_cli.capture", return_value=ProcessOutcome(0)) as stream, \
                patch.dict(os.environ, {"GITHUB_TOKEN": "unrelated", "CLAUDECODE": "parent"}):
            result = transport.run(["--print"], "prompt", self.root, settings=settings, endpoint="http://127.0.0.1:3333",
                                   credential="fixture-key", stop=lambda: "", on_line=lambda line: None)
        self.assertEqual(result, ProcessOutcome(0))
        self.assertEqual(popen.call_args.args[0], [config.cli_path, "--print"])
        self.assertEqual(popen.call_args.kwargs["cwd"], self.root)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(settings))
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "fixture-key")
        self.assertEqual(stream.call_args.args[1], "prompt")

    def test_missing_cli_is_reported_before_starting_any_process(self):
        transport = HostCLITransport(HarnessConfig(adapter="claude_code"))
        with patch("evo.adapters.harness.host_cli.shutil.which", return_value=None), \
                patch("evo.adapters.harness.host_cli.subprocess.Popen") as process:
            with self.assertRaises(HarnessError):
                transport.run([], "", self.root, settings=self.root, endpoint="", credential="",
                              stop=lambda: "", on_line=lambda line: None)
        process.assert_not_called()

    def test_stream_counts_each_record_even_when_one_read_contains_multiple_records(self):
        program = "import sys; sys.stdout.buffer.write(b'a' * 1024 + b'\\n' + b'b' * 1024)"
        process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        lines = []
        result = capture(process, "", timeout=3, max_event_bytes=1024, stop=lambda: "", on_line=lines.append)
        self.assertEqual(result, ProcessOutcome(0))
        self.assertEqual(lines, [b"a" * 1024, b"b" * 1024])

    def test_uncapped_process_can_be_cancelled_after_its_pipes_close(self):
        program = "import os, time; os.close(0); os.close(1); os.close(2); time.sleep(10)"
        process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        started = time.monotonic()
        result = capture(process, "", timeout=None, max_event_bytes=1024,
                         stop=lambda: "cancelled" if time.monotonic() - started >= 0.2 else "",
                         on_line=lambda line: None)
        self.assertEqual(result.reason, "cancelled")
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(process.poll())

    def test_python_tool_uses_real_workspace_with_approval_and_a_bounded_environment(self):
        fs = WorkspaceFilesystem(self.root, "alice")
        fs.write_file("main.py", "import os\nfrom pathlib import Path\n"
                      "Path('result.txt').write_text(os.getenv('GITHUB_TOKEN', 'local'))", purpose="code")
        runner = HostPythonRunner(fs)
        with self.assertRaises(PermissionError):
            runner.run_python("main.py")
        self.assertFalse((fs.workspace / "result.txt").exists())
        with patch.dict(os.environ, {"GITHUB_TOKEN": "unrelated"}):
            self.assertEqual(runner.run_python("main.py", approved=True).exit_code, 0)
        self.assertEqual((fs.workspace / "result.txt").read_text(), "local")

    def test_python_timeout_and_output_limit_stop_the_process(self):
        fs = WorkspaceFilesystem(self.root, "alice")
        fs.write_file("slow.py", "import time\ntime.sleep(10)", purpose="code")
        fs.write_file("wide.py", "print('x' * 10000)", purpose="code")
        runner = HostPythonRunner(fs, timeout=1, max_output_bytes=32)
        self.assertTrue(runner.run_python("slow.py", approved=True).timed_out)
        result = runner.run_python("wide.py", approved=True)
        self.assertTrue(result.output_limited)
        self.assertEqual(len(result.stdout), 32)

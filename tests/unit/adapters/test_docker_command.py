import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.linux import create_arguments, validate_image
from evo.adapters.tools.docker import DockerPythonRunner
from evo.ports.tools import ExecutionResult, ToolError


class DockerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fs = WorkspaceFilesystem(Path(temp.name), "alice")
        self.fs.write_file("main.py", "print('ok')", purpose="code")
        self.runner = DockerPythonRunner(self.fs)
        self.ok = ExecutionResult(0, "", "")
        self.image = ExecutionResult(0, "sha256:" + "a" * 64, "")
        self.state = ExecutionResult(0, "exited 0", "")

    def test_command_is_restricted_and_mounts_only_workspace(self):
        args = create_arguments(self.fs.workspace, "test", "image", "main.py")
        for flag, value in [("--network", "none"), ("--cap-drop", "ALL"),
                            ("--security-opt", "no-new-privileges"), ("--pids-limit", "64"),
                            ("--memory", "256m"), ("--cpus", "1"), ("--pull", "never")]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--read-only", args)
        self.assertEqual(args.count("--mount"), 1)
        self.assertEqual(args[args.index("--mount") + 1],
                         f"type=bind,src={self.fs.workspace},dst=/workspace")
        self.assertEqual(args[-1], "/workspace/main.py")

    def test_platform_root_mount_and_image_validation(self):
        with patch("evo.adapters.tools.linux.sys.platform", "win32"):
            with self.assertRaisesRegex(ToolError, "Unsupported"):
                create_arguments(self.fs.workspace, "test", "image", "main.py")
        with patch("evo.adapters.tools.linux.os.getuid", return_value=0):
            with self.assertRaises(ToolError):
                create_arguments(self.fs.workspace, "test", "image", "main.py")
        for path in [Path("relative"), self.fs.workspace.parent, self.fs.workspace.parent.parent]:
            with self.assertRaises(ToolError):
                create_arguments(path, "test", "image", "main.py")
        for image in ["python:latest", "python:3.11-slim", "evil:3.11", "--privileged"]:
            with self.assertRaises(ToolError):
                validate_image(image)

    def test_denied_and_invalid_paths_never_start_process(self):
        with patch.object(self.runner, "_capture") as capture:
            with self.assertRaises(PermissionError):
                self.runner.run_python("main.py")
            for path in ["../main.py", "/tmp/main.py", ".git/main.py"]:
                with self.assertRaises(ValueError):
                    self.runner.run_python(path, approved=True)
            capture.assert_not_called()

    def test_missing_docker_and_daemon_fail_closed(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(ToolError, "CLI unavailable"):
                self.runner.run_python("main.py", approved=True)
        with patch("shutil.which", return_value="/usr/bin/docker"), patch.object(
            self.runner, "_capture", return_value=ExecutionResult(1, "", "secret")):
            with self.assertRaisesRegex(ToolError, "daemon") as error:
                self.runner.run_python("main.py", approved=True)
            self.assertNotIn("secret", str(error.exception))

    def test_success_and_nonzero_exit_use_verified_container_status(self):
        for code in (0, 7):
            with patch("shutil.which", return_value="/usr/bin/docker"), patch.object(
                self.runner, "_capture", side_effect=[self.image, self.ok,
                    ExecutionResult(code, "hello", "err"),
                    ExecutionResult(0, f"exited {code}", ""), self.ok]) as capture:
                result = self.runner.run_python("main.py", approved=True)
                self.assertEqual((result.exit_code, result.stdout), (code, "hello"))
                self.assertEqual(capture.call_args_list[-1].args[0][-3:-1], ["rm", "--force"])

    def test_timeout_output_limit_and_start_error_always_cleanup(self):
        for outcome in [ExecutionResult(-9, "", "", True),
                        ExecutionResult(-9, "x", "", False, True), ToolError("start failed")]:
            with patch("shutil.which", return_value="/usr/bin/docker"), patch.object(
                self.runner, "_capture", side_effect=[self.image, self.ok, outcome, self.ok]) as capture:
                if isinstance(outcome, Exception):
                    with self.assertRaises(ToolError):
                        self.runner.run_python("main.py", approved=True)
                else:
                    self.assertEqual(self.runner.run_python("main.py", approved=True), outcome)
                self.assertIn("--force", capture.call_args.args[0])

    def test_creation_and_cleanup_failures_are_reported(self):
        for results in [[self.image, ExecutionResult(1, "", ""), self.ok],
                        [self.image, self.ok, self.ok, self.state, ExecutionResult(1, "", "")]]:
            with patch("shutil.which", return_value="/usr/bin/docker"), patch.object(
                self.runner, "_capture", side_effect=results):
                with self.assertRaises(ToolError):
                    self.runner.run_python("main.py", approved=True)

    def test_capture_bounds_both_pipes_and_wall_time(self):
        result = self.runner._capture([sys.executable, "-c",
            "import os; os.write(1,b'x'*10000); os.write(2,b'y'*10000)"], 5, 100)
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 100)
        result = self.runner._capture([sys.executable, "-c", "import time; time.sleep(5)"], .05, 100)
        self.assertTrue(result.timed_out)

    def test_configuration_rejects_unbounded_limits(self):
        for timeout in [0, -1, float('nan'), float('inf'), True, 301]:
            with self.assertRaises(ValueError):
                DockerPythonRunner(self.fs, timeout=timeout)
        for limit in [0, True, 1048577]:
            with self.assertRaises(ValueError):
                DockerPythonRunner(self.fs, max_output_bytes=limit)

    def test_capture_does_not_inherit_credentials_or_docker_config(self):
        code = """
import os
from pathlib import Path
config = Path(os.environ['DOCKER_CONFIG'])
assert config.is_dir() and not list(config.iterdir())
assert not any(key in os.environ for key in (
    'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY',
    'HTTP_PROXY', 'HTTPS_PROXY', 'HKGAI_API_KEY', 'GITHUB_TOKEN'))
print(config)
"""
        with patch.dict(os.environ, {
            "DOCKER_CONFIG": "/untrusted/docker-config", "DOCKER_HOST": "tcp://untrusted:2375",
            "DOCKER_CONTEXT": "untrusted", "DOCKER_TLS_VERIFY": "1",
            "HTTP_PROXY": "http://untrusted", "HTTPS_PROXY": "http://untrusted",
            "HKGAI_API_KEY": "fixture-only", "GITHUB_TOKEN": "fixture-only",
        }):
            result = self.runner._capture([sys.executable, "-c", code], 5, 4096)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertFalse(Path(result.stdout.strip()).exists())

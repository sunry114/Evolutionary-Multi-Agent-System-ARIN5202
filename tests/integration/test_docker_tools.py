"""Opt in with EVO_DOCKER_TESTS=1; when enabled, failures must not skip."""
import os
import tempfile
import subprocess
import unittest
from pathlib import Path

from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.linux import DEFAULT_IMAGE


@unittest.skipUnless(os.environ.get("EVO_DOCKER_TESTS") == "1", "Set EVO_DOCKER_TESTS=1 for real Docker tests")
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fs = WorkspaceFilesystem(self.root, "alice")
        other = WorkspaceFilesystem(self.root, "bob")
        other.write_file("secret.txt", "private")
        (self.root / "events.sqlite3").write_text("private")
        (self.fs.workspace.parent / "memory.sqlite3").write_text("private")
        self.runner = DockerPythonRunner(self.fs, image=os.environ.get("EVO_DOCKER_IMAGE", DEFAULT_IMAGE))

    def run_code(self, code, **options):
        self.fs.write_file("main.py", code, purpose="code")
        return self.runner.run_python("main.py", **options)

    def test_success_nonroot_workspace_and_isolation(self):
        result = self.run_code(f'''
import os, socket
from pathlib import Path
assert os.getuid() != 0
assert os.getgid() != 0
assert not Path({str(self.root / 'events.sqlite3')!r}).exists()
assert not Path({str(self.fs.workspace.parent / 'memory.sqlite3')!r}).exists()
assert not Path('/workspace/../bob/workspace/secret.txt').exists()
assert not Path('/var/run/docker.sock').exists()
assert not any(name in os.environ for name in ('HKGAI_API_KEY', 'GITHUB_TOKEN'))
assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
try:
    Path('/forbidden.txt').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
s = socket.socket()
s.settimeout(1)
try:
    s.connect(('1.1.1.1', 443))
except OSError:
    pass
else:
    raise AssertionError('network available')
finally:
    s.close()
Path('result.txt').write_text('ok')
print('isolated')
''', approved=True)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(self.fs.read_file("result.txt"), "ok")

    def test_nonzero_timeout_and_output_limit(self):
        self.assertEqual(self.run_code("raise SystemExit(7)", approved=True).exit_code, 7)
        self.runner.timeout = 1
        names = []
        original = self.runner._capture
        def record(command, timeout, limit):
            if "--name" in command:
                names.append(command[command.index("--name") + 1])
            return original(command, timeout, limit)
        self.runner._capture = record
        self.assertTrue(self.run_code("while True: pass", approved=True).timed_out)
        self.runner.timeout = 10
        self.assertTrue(self.run_code("while True: print('x'*10000)", approved=True).output_limited)
        for name in names:
            checked = subprocess.run(["docker", "--host", "unix:///var/run/docker.sock",
                                      "container", "ls", "--all", "--quiet", "--filter", "name=" + name],
                                     capture_output=True, text=True, timeout=10)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(checked.stdout.strip(), "", "Container leaked after cancellation")

    def test_denial_has_no_side_effect(self):
        with self.assertRaises(PermissionError):
            self.run_code("open('bad.txt','w').write('bad')")
        self.assertFalse((self.fs.workspace / "bad.txt").exists())

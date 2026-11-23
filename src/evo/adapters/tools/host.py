"""Bounded host subprocess tools for the approval-based execution path."""

import os
import selectors
import subprocess
import sys
import tempfile
import time

from evo.ports.tools import ExecutionResult, ToolError
from evo.adapters.harness.process import terminate_group


class HostPythonRunner:
    def __init__(self, filesystem, *, timeout=30, max_output_bytes=65536):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Timeout must be within (0, 300] seconds")
        if type(max_output_bytes) is not int or not 0 < max_output_bytes <= 1048576:
            raise ValueError("Invalid output limit")
        self.filesystem = filesystem
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def _capture(self, command, *, environment=None):
        buffers = (bytearray(), bytearray())
        timed_out = limited = False
        with tempfile.TemporaryDirectory(prefix="evo-tool-") as home:
            env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home, "LC_ALL": "C", **(environment or {})}
            try:
                process = subprocess.Popen(command, cwd=self.filesystem.workspace, env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
            except OSError as error:
                raise ToolError("Could not start local tool") from error
            deadline = time.monotonic() + self.timeout
            try:
                with selectors.DefaultSelector() as selector:
                    for index, stream in enumerate((process.stdout, process.stderr)):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(stream, selectors.EVENT_READ, index)
                    while selector.get_map() and not limited:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        for key, _ in selector.select(remaining):
                            data = os.read(key.fileobj.fileno(), 8192)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            available = self.max_output_bytes - sum(map(len, buffers))
                            buffers[key.data].extend(data[:available])
                            if len(data) > available:
                                limited = True
                                break
                if not timed_out and not limited:
                    try:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
            finally:
                exit_code = terminate_group(process)
                process.stdout.close()
                process.stderr.close()
        if exit_code is None:
            raise ToolError("Local tool process did not exit after termination")
        return ExecutionResult(exit_code, *(part.decode("utf-8", errors="replace") for part in buffers),
                               timed_out, limited)

    def run_python(self, path, *, approved=False):
        if approved is not True:
            raise PermissionError("Python execution requires human approval")
        self.filesystem.read_file(path, purpose="code")
        return self._capture([sys.executable, "-I", "-B", "-u", str(self.filesystem.workspace / path)])

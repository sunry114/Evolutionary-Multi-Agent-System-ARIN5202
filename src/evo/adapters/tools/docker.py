"""Bounded Docker execution. Docker failure never enables host execution."""
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import uuid

from evo.adapters.tools.filesystem import WorkspaceFilesystem, validate_file_path
from evo.adapters.tools.linux import DEFAULT_IMAGE, create_arguments, validate_image
from evo.ports.tools import ExecutionResult, ToolError, ToolCleanupError


class DockerPythonRunner:
    """Trusted broker only. C10 supplies approval; C11 supplies tool dispatch.

    Uses the local system Docker socket deliberately; remote Docker contexts are
    unsupported because bind paths must refer to this host's agent workspace.
    """

    def __init__(self, filesystem: WorkspaceFilesystem, *, image: str = DEFAULT_IMAGE,
                 timeout: float = 30, max_output_bytes: int = 65536):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise ValueError("Timeout must be within (0, 300] seconds")
        if type(max_output_bytes) is not int or not 0 < max_output_bytes <= 1048576:
            raise ValueError("Invalid output limit")
        self.filesystem = filesystem
        self.image = validate_image(image)
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes

    def _capture(self, command, timeout, limit, *, input_data=None):
        """Drain both pipes with a total byte bound and a monotonic deadline."""
        # Docker config can automatically inject proxy credentials into containers.
        # Keep it empty for the entire CLI lifetime; never read ~/.docker/config.json.
        with tempfile.TemporaryDirectory(prefix="evo-docker-config-") as config:
            env = {"DOCKER_CONFIG": config, "LC_ALL": "C"}
            if "PATH" in os.environ:
                env["PATH"] = os.environ["PATH"]
            return self._capture_process(command, timeout, limit, env, input_data)

    def _capture_process(self, command, timeout, limit, env, input_data=None):
        if input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > 4096):
            raise ValueError("Invalid bounded process input")
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env)
        except OSError:
            raise ToolError("Docker CLI unavailable or could not start") from None
        buffers = [bytearray(), bytearray()]
        timed_out = limited = False
        deadline = time.monotonic() + timeout
        try:
            if input_data is not None:
                try:
                    process.stdin.write(input_data)
                    process.stdin.close()
                except BrokenPipeError:
                    process.stdin.close()
            with selectors.DefaultSelector() as selector:
                for index, stream in enumerate((process.stdout, process.stderr)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, index)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        available = limit - sum(map(len, buffers))
                        buffers[key.data].extend(chunk[:available])
                        if len(chunk) > available:
                            limited = True
                            break
                    if limited:
                        break
                if not timed_out and not limited:
                    try:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        timed_out = True
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()
        return ExecutionResult(process.returncode,
                               buffers[0].decode("utf-8", errors="replace"),
                               buffers[1].decode("utf-8", errors="replace"),
                               timed_out, limited)

    def run_python(self, path: str, *, approved: bool = False) -> ExecutionResult:
        if approved is not True:
            raise PermissionError("Python execution requires human approval")
        if not isinstance(path, str) or not path.lower().endswith(".py"):
            raise ToolError("Python execution requires a .py script")
        validate_file_path(path, "code")
        self.filesystem.read_file(path, purpose="code")
        # Validate platform and mount before contacting Docker.
        name = "evo-python-" + uuid.uuid4().hex
        args = create_arguments(self.filesystem.workspace, name, "placeholder", path)
        executable = shutil.which("docker")
        if executable is None:
            raise ToolError("Docker CLI unavailable; host execution is disabled")
        base = [executable, "--host", "unix:///var/run/docker.sock"]
        inspected = self._capture(base + ["image", "inspect", "--format", "{{.Id}}", self.image], 10, 4096)
        image_id = inspected.stdout.strip()
        if (inspected.exit_code or inspected.timed_out or inspected.output_limited
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
            raise ToolError("Docker daemon or configured local image unavailable; pre-pull the image")
        args[args.index("placeholder")] = image_id
        try:
            created = self._capture(base + args, 10, 4096)
            if created.exit_code or created.timed_out or created.output_limited:
                raise ToolError("Docker container creation failed; required isolation unavailable")
            result = self._capture(base + ["start", "--attach", name], self.timeout, self.max_output_bytes)
            if result.timed_out or result.output_limited:
                return result
            state = self._capture(base + ["inspect", "--format",
                                          "{{.State.Status}} {{.State.ExitCode}}", name], 10, 4096)
            match = re.fullmatch(r"exited (\d+)", state.stdout.strip())
            if state.exit_code or state.timed_out or state.output_limited or not match:
                raise ToolError("Docker execution failed to reach a verified exit state")
            return ExecutionResult(int(match.group(1)), result.stdout, result.stderr)
        finally:
            removed = self._capture(base + ["rm", "--force", name], 10, 4096)
            if removed.exit_code or removed.timed_out or removed.output_limited:
                raise ToolCleanupError("Docker cleanup failed; inspect containers named evo-python-* before retrying")

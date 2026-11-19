"""Native Claude Code in a pinned, networkless Linux Docker container."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.filesystem import PURPOSE_SUFFIXES
from evo.adapters.tools.linux import DEFAULT_IMAGE
from evo.ports.harness import HarnessCleanupError, HarnessError

from .process import ProcessOutcome, capture


class DockerCLITransport:
    def __init__(self, workspace, config):
        self.helper = DockerPythonRunner(workspace)
        self.config = config

    def run(self, args, prompt, directory, *, stop, on_line):
        executable = shutil.which(self.config.cli_path)
        docker = shutil.which("docker")
        if executable is None or docker is None or os.getuid() == 0 or os.getgid() == 0:
            raise HarnessError("A native CLI, local Docker and non-root Linux identity are required")
        binary = Path(executable).resolve()
        info = binary.stat()
        with binary.open("rb") as stream:
            native = stream.read(4) == b"\x7fELF"
        if (not native or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or info.st_uid not in (0, os.getuid()) or "," in str(binary)):
            raise HarnessError("Use an owner-controlled native Linux Claude CLI binary")
        base = [docker, "--host", "unix:///var/run/docker.sock"]
        inspected = self.helper._capture(base + ["image", "inspect", "--format", "{{.Id}}", DEFAULT_IMAGE], 10, 4096)
        image_id = inspected.stdout.strip()
        if inspected.exit_code or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise HarnessError("Pinned local Python image/Docker daemon is unavailable")
        name = "evo-harness-" + uuid.uuid4().hex
        client = Path(__file__).with_name("container_client.py").resolve()
        native = []
        if self.config.native_issue_tools:
            suffixes = sorted(set().union(*PURPOSE_SUFFIXES.values()))
            native = ["--env", "EVO_ARTIFACT_SUFFIXES=" + ",".join(suffixes)]
        command = base + ["create", "--name", name, "--pull", "never", "--interactive", "--network", "none",
                          "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                          "--user", f"{os.getuid()}:{os.getgid()}", "--cpus", "1", "--memory", "1g",
                          "--memory-swap", "1g", "--pids-limit", "128", "--ulimit", "nofile=256:256",
                          "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                          "--tmpfs", "/home/evo:rw,noexec,nosuid,nodev,size=64m,mode=1777",
                          "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=8m,mode=1777",
                          "--mount", f"type=bind,src={binary},dst=/opt/claude,readonly",
                          "--mount", f"type=bind,src={client},dst=/opt/container_client.py,readonly",
                          "--mount", f"type=bind,src={directory},dst=/broker,readonly",
                          "--env", "HOME=/home/evo", "--env", "CLAUDE_CONFIG_DIR=/home/evo/.claude",
                          "--env", "ANTHROPIC_BASE_URL=http://127.0.0.1:18080",
                          "--env", "ANTHROPIC_API_KEY=evo-broker-placeholder",
                          "--env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                          "--env", "DISABLE_AUTOUPDATER=1", "--env", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1",
                          "--env", "EVO_CLI_VERSION=" + self.config.cli_version] + native + [
                          "--workdir", "/workspace", "--log-driver", "none", image_id,
                          "python", "/opt/container_client.py", "launch"] + args
        try:
            created = self.helper._capture(command, 15, 4096)
            if created.exit_code or created.timed_out or created.output_limited:
                raise HarnessError("Harness container creation failed")
            with tempfile.TemporaryDirectory(prefix="evo-harness-docker-") as docker_config:
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "DOCKER_CONFIG": docker_config, "LC_ALL": "C"}
                process = subprocess.Popen(base + ["start", "--attach", "--interactive", name],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, start_new_session=True)
                outcome = capture(process, prompt, timeout=self.config.timeout,
                                  limit=self.config.max_output_bytes, stop=stop, on_line=on_line,
                                  max_line_bytes=2 * 1024 * 1024 if native else 1024 * 1024)
            if outcome.reason:
                return outcome
            state = self.helper._capture(base + ["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name], 10, 4096)
            match = re.fullmatch(r"exited (\d+)", state.stdout.strip())
            if state.exit_code or not match:
                raise HarnessError("Harness container did not reach a verified exit state")
            return ProcessOutcome(int(match.group(1)))
        finally:
            removed = self.helper._capture(base + ["rm", "--force", name], 10, 4096)
            if removed.exit_code or removed.timed_out or removed.output_limited:
                raise HarnessCleanupError("Harness cleanup failed; inspect evo-harness-* containers")

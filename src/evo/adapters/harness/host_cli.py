"""Start the installed Claude CLI in the selected local directory."""

import os
import shutil
import subprocess

from evo.ports.harness import HarnessError

from .process import capture


class HostCLITransport:
    def __init__(self, config):
        self.config = config

    def run(self, args, prompt, cwd, *, settings, endpoint, credential, stop, on_line):
        cli = shutil.which(self.config.cli_path)
        if cli is None:
            raise HarnessError("Claude Code executable unavailable; configure EVO_CLAUDE_CLI")
        if self.config.native_issue_tools and (os.getuid() == 0 or os.getgid() == 0):
            raise HarnessError("Run the Claude Code workbench as a non-root user")
        env = os.environ.copy()
        for key in ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY",
                    "HKGAI_API_KEY", "GITHUB_TOKEN"):
            env.pop(key, None)
        env.update({
            "CLAUDE_CONFIG_DIR": str(settings),
            "ANTHROPIC_BASE_URL": endpoint,
            "ANTHROPIC_AUTH_TOKEN": credential,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        })
        try:
            version = subprocess.run([cli, "--version"], cwd=cwd, env=env, capture_output=True,
                                     text=True, timeout=10, check=True)
            if version.stdout.strip() != self.config.cli_version + " (Claude Code)":
                raise HarnessError("Installed Claude Code version differs from the configured version")
            process = subprocess.Popen([cli, *args], cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise HarnessError("Could not start the local Claude Code CLI") from error
        return capture(process, prompt, timeout=self.config.timeout, max_event_bytes=self.config.max_event_bytes,
                       stop=stop, on_line=on_line, keep_stderr=True)

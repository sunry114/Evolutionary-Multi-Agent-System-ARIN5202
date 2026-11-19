"""Container-only Git with conservative repository validation and scoped push."""

import base64
import configparser
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.filesystem import _directory, _read_at, validate_file_path
from evo.adapters.tools.linux import isolation_arguments
from evo.ports.tools import ExecutionResult, GitRepositoryMissing, ToolError

DEFAULT_GIT_IMAGE = (
    "alpine/git:2.49.1@sha256:"
    "c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26"
)


def validate_repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.git", repository):
        raise ValueError("Use an exact GitHub HTTPS repository ending in .git")
    if ".." in repository or repository.rsplit("/", 1)[1] == ".git":
        raise ValueError("Invalid repository name")
    return repository


def validate_branch(branch):
    if (not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch)
            or branch == "HEAD" or ".." in branch or "//" in branch
            or any(part.startswith(".") or part.endswith((".", ".lock")) for part in branch.split("/"))
            or branch.endswith("/")):
        raise ValueError("Invalid branch")
    return branch


class RepositoryToken:
    """Trusted exact-repository binding; loader is never supplied by the model."""
    def __init__(self, repository, loader):
        self.repository = validate_repository(repository)
        self.loader = loader

    def read(self, repository):
        if repository != self.repository:
            raise ToolError("No credential authorized for this repository")
        try:
            token = self.loader()
        except Exception:
            raise ToolError("Repository credential could not be loaded") from None
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,2048}", token):
            raise ToolError("A valid repository-scoped token is required")
        return token


class DockerGitTools(DockerPythonRunner):
    @staticmethod
    def validate_push_target(repository, branch):
        validate_repository(repository)
        validate_branch(branch)

    def __init__(self, filesystem, *, token_provider=None, image=DEFAULT_GIT_IMAGE,
                 timeout=30, max_output_bytes=65536):
        super().__init__(filesystem, timeout=timeout, max_output_bytes=max_output_bytes)
        if not isinstance(image, str) or not re.fullmatch(r"alpine/git:2\.\d+\.\d+(?:@sha256:[0-9a-f]{64})?", image):
            raise ToolError("Use a fixed Alpine Git image")
        self.image = image
        self.token_provider = token_provider

    def validate_local_repository(self, *, allow_missing=False):
        """Never ask Git to interpret executable config or external gitdir links."""
        root = self.filesystem.workspace / ".git"
        if not root.exists() and not root.is_symlink() and allow_missing:
            return
        try:
            with _directory(root) as directory:
                config = _read_at(directory, "config", 65536).decode("utf-8")
            count = 0
            for parent, directories, names in os.walk(root, followlinks=False):
                for name in directories + names:
                    count += 1
                    path = Path(parent) / name
                    info = path.lstat()
                    if (count > 10000 or stat.S_ISLNK(info.st_mode)
                            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                        raise ValueError("Unsafe Git metadata")
            if any((root / name).exists() for name in (
                "commondir", "gitdir", "config.worktree", "objects/info/alternates", "objects/info/http-alternates")):
                raise ValueError("External Git metadata is forbidden")
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(config)
            allowed = {"repositoryformatversion": {"0"}, "filemode": {"true", "false"},
                       "bare": {"false"}, "logallrefupdates": {"true"}}
            if set(parser.sections()) != {"core"} or parser.defaults():
                raise ValueError("Untrusted Git configuration")
            if not set(parser["core"]).issubset(allowed):
                raise ValueError("Untrusted Git configuration")
            if any(value not in allowed[key] for key, value in parser["core"].items()):
                raise ValueError("Untrusted Git configuration")
        except (OSError, UnicodeError, configparser.Error, ValueError):
            raise ToolError("Unsafe or unsupported Git repository configuration") from None

    @staticmethod
    def _git_arguments(arguments):
        return ["--literal-pathspecs", "-c", "core.hooksPath=/dev/null",
                "-c", "core.fsmonitor=false", "-c", "credential.helper=",
                "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
                "-c", "http.proxy=", "-c", "push.recurseSubmodules=no",
                "-c", "commit.gpgSign=false", "-c", "maintenance.auto=false",
                "-c", "gc.auto=0", "-c", "user.name=Evo Agent",
                "-c", "user.email=evo-agent@localhost"] + arguments

    def _run(self, arguments, *, repository=None, input_data=None):
        name = "evo-git-" + uuid.uuid4().hex
        args = isolation_arguments(self.filesystem.workspace, name, network="bridge" if repository else "none")
        executable = shutil.which("docker")
        if executable is None:
            raise ToolError("Docker CLI unavailable; host Git execution is disabled")
        base = [executable, "--host", "unix:///var/run/docker.sock"]
        inspected = self._capture(base + ["image", "inspect", "--format", "{{.Id}}", self.image], 10, 4096)
        image_id = inspected.stdout.strip()
        if (inspected.exit_code or inspected.timed_out or inspected.output_limited
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
            raise ToolError("Docker daemon or configured Git image unavailable; pre-pull the image")
        args += ["--env", "HOME=/tmp", "--env", "GIT_CONFIG_NOSYSTEM=1",
                 "--env", "GIT_CONFIG_GLOBAL=/dev/null", "--env", "GIT_TERMINAL_PROMPT=0",
                 "--env", "GIT_NO_REPLACE_OBJECTS=1", "--env", "GIT_ATTR_NOSYSTEM=1"]
        command = self._git_arguments(arguments)
        if repository:
            # Only this fixed wrapper reads the secret, after container creation.
            # Neither Docker metadata nor the host command line contains the token.
            wrapper = ('IFS= read -r header || exit 125; '
                       'export GIT_CONFIG_COUNT=1; export GIT_CONFIG_KEY_0="http.$1.extraHeader"; '
                       'export GIT_CONFIG_VALUE_0="Authorization: Basic $header"; '
                       'unset header; shift; exec git "$@"')
            args += ["--interactive", "--entrypoint", "/bin/sh", image_id,
                     "-c", wrapper, "evo-push", repository] + command
        else:
            args += ["--entrypoint", "git", image_id] + command
        try:
            created = self._capture(base + args, 10, 4096)
            if created.exit_code or created.timed_out or created.output_limited:
                raise ToolError("Git container creation failed; required isolation unavailable")
            start = base + ["start", "--attach"] + (["--interactive"] if repository else []) + [name]
            if repository:
                result = self._capture(start, self.timeout, self.max_output_bytes, input_data=input_data)
            else:
                result = self._capture(start, self.timeout, self.max_output_bytes)
            if result.timed_out or result.output_limited:
                return ExecutionResult(result.exit_code, "", "Git execution interrupted", result.timed_out, result.output_limited)
            state = self._capture(base + ["inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name], 10, 4096)
            match = re.fullmatch(r"exited (\d+)", state.stdout.strip())
            if state.exit_code or state.timed_out or state.output_limited or not match:
                raise ToolError("Git execution failed to reach a verified exit state")
            code = int(match.group(1))
            if repository:
                return ExecutionResult(code, "Push completed" if code == 0 else "", "" if code == 0 else "Git push failed")
            return ExecutionResult(code, result.stdout, result.stderr)
        finally:
            removed = self._capture(base + ["rm", "--force", name], 10, 4096)
            if removed.exit_code or removed.timed_out or removed.output_limited:
                raise ToolError("Git cleanup failed; inspect containers named evo-git-* before retrying")

    def init(self):
        self.validate_local_repository(allow_missing=True)
        return self._run(["init", "--initial-branch=main", "--template="])

    def status(self):
        try:
            (self.filesystem.workspace / ".git").lstat()
        except FileNotFoundError:
            raise GitRepositoryMissing("No Git repository in this workspace") from None
        self.validate_local_repository()
        return self._run(["status", "--porcelain=v1", "--untracked-files=normal"])

    def head(self):
        self.validate_local_repository()
        result = self._run(["rev-parse", "--verify", "HEAD^{commit}"])
        if result.exit_code or result.timed_out or result.output_limited or not re.fullmatch(r"[0-9a-f]{40}\n?", result.stdout):
            raise ToolError("A valid existing commit is required")
        return result.stdout.strip()

    def commit(self, paths, message):
        self.validate_commit(paths, message)
        self.validate_local_repository()
        added = self._run(["add", "--"] + list(paths))
        if added.exit_code or added.timed_out or added.output_limited:
            return added
        return self._run(["commit", "--only", "-m", message, "--"] + list(paths))

    def validate_commit(self, paths, message):
        if (not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 100
                or not all(isinstance(path, str) for path in paths)
                or not isinstance(message, str) or not message.strip() or len(message) > 1000
                or any(ord(c) < 32 for c in message)):
            raise ValueError("Commit requires an explicit file list and bounded message")
        for path in paths:
            # Explicit purposes let code/data/config coexist with Markdown reports.
            purpose = {".html": "slides", ".py": "code", ".json": "data", ".csv": "data", ".yaml": "config", ".yml": "config"}.get(Path(path).suffix.lower(), "document")
            validate_file_path(path, purpose)
            self.filesystem.read_file(path, purpose=purpose)
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate commit paths")

    def push(self, repository, branch, revision, *, approved=False):
        if approved is not True:
            raise PermissionError("Git push requires human approval")
        validate_repository(repository)
        validate_branch(branch)
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision) or self.head() != revision:
            raise ToolError("Approved commit no longer matches HEAD")
        if self.token_provider is None:
            raise ToolError("No repository-scoped token configured")
        token = self.token_provider.read(repository)
        secret = base64.b64encode(("x-access-token:" + token).encode()) + b"\n"
        return self._run(["push", "--porcelain", "--no-verify", "--recurse-submodules=no",
                          repository, revision + ":refs/heads/" + branch], repository=repository, input_data=secret)

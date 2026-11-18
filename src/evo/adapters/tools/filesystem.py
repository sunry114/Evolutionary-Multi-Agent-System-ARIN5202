"""Linux workspace broker with descriptor-relative, no-follow file access."""

from __future__ import annotations

import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from evo.domain.policies import DOCUMENT_SUFFIXES


MAX_FILE_BYTES = 1024 * 1024
MAX_DIRECTORY_ENTRIES = 1000
PURPOSE_SUFFIXES = {
    "document": DOCUMENT_SUFFIXES,
    "slides": frozenset({".html"}),
    "code": frozenset({".py"}),
    "config": frozenset({".json", ".yaml", ".yml"}),
    "data": frozenset({".json", ".csv"}),
}
_ALLOWED_SUFFIXES = frozenset().union(*PURPOSE_SUFFIXES.values())
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class PathPolicyError(ValueError):
    """A safe-to-report workspace policy violation."""


def validate_agent(agent: str) -> str:
    if not isinstance(agent, str) or not _AGENT_ID.fullmatch(agent):
        raise PathPolicyError("Invalid agent identifier")
    return agent


def _parts(path: str, *, directory: bool = False) -> tuple[str, ...]:
    if directory and path == ".":
        return ()
    if (not isinstance(path, str) or not path or len(path) > 4096
            or path.startswith("/") or "\\" in path or ":" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise PathPolicyError("Expected a relative workspace path")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255
           or part.lower() == ".git" or part.startswith(".evo-") for part in parts):
        raise PathPolicyError("Forbidden workspace path component")
    return parts


def validate_file_path(path: str, purpose: str) -> tuple[str, ...]:
    parts = _parts(path)
    if not isinstance(purpose, str) or purpose not in PURPOSE_SUFFIXES:
        raise PathPolicyError("Unknown file purpose")
    if PurePosixPath(path).suffix.lower() not in PURPOSE_SUFFIXES[purpose]:
        raise PathPolicyError("File suffix is not allowed for this purpose")
    return parts


@contextmanager
def _directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Open each absolute path component without following symlinks."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError:
        os.close(fd)
        raise PathPolicyError("Unsafe or unavailable storage directory") from None
    try:
        yield fd
    finally:
        os.close(fd)


def _regular(info: os.stat_result, limit: int) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PathPolicyError("Only regular files with a single link are allowed")
    if info.st_size > limit:
        raise PathPolicyError("File size limit exceeded")


def _read_at(directory: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        _regular(os.fstat(stream.fileno()), limit)
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise PathPolicyError("File size limit exceeded")
        _regular(os.fstat(stream.fileno()), limit)
        return data


def _write_at(directory: int, name: str, data: bytes, *, overwrite: bool,
              limit: int) -> None:
    """Install complete bytes atomically; never truncate an existing inode."""
    if len(data) > limit:
        raise PathPolicyError("File size limit exceeded")
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not overwrite:
            raise FileExistsError("Destination already exists")
        _regular(info, limit)
    temporary = ".evo-write-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            # link is an atomic no-clobber install even with concurrent writers.
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass  # os.replace already consumed the temporary name.


class WorkspaceFilesystem:
    """One agent's UTF-8 files. Only trusted application code constructs this.

    Mount only ``workspace`` into agent containers, never ``state_root`` or
    ``agents/<agent>``. No method accepts a second agent or a host path.
    """

    def __init__(self, state_root: Path | str, agent: str, *,
                 max_file_bytes: int = MAX_FILE_BYTES):
        if sys.platform != "linux":
            raise RuntimeError("Workspace tools require Linux")
        if type(max_file_bytes) is not int or not 0 < max_file_bytes <= MAX_FILE_BYTES:
            raise ValueError("Invalid file size limit")
        self.agent = validate_agent(agent)
        self.max_file_bytes = max_file_bytes
        self._state_root = Path(os.path.abspath(state_root))
        self.workspace = self._state_root / "agents" / self.agent / "workspace"
        with _directory(self.workspace, create=True):
            pass

    @contextmanager
    def _parent(self, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
        # Resolve first for containment, then walk with no-follow descriptors to
        # close the usual resolve/check/open symlink substitution window.
        try:
            candidate = self.workspace.joinpath(*parts).resolve()
            candidate.relative_to(self.workspace)
        except (ValueError, OSError, RuntimeError):
            raise PathPolicyError("Path escapes the workspace") from None
        with _directory(self.workspace) as root:
            fd = os.dup(root)
            try:
                for part in parts[:-1]:
                    if create:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fd)
                    os.close(fd)
                    fd = child
                yield fd
            finally:
                os.close(fd)

    def _read_bytes(self, path: str, *, purpose: str) -> bytes:
        parts = validate_file_path(path, purpose)
        with self._parent(parts) as directory:
            return _read_at(directory, parts[-1], self.max_file_bytes)

    def _write_bytes(self, path: str, data: bytes, *, purpose: str,
                     overwrite: bool) -> None:
        parts = validate_file_path(path, purpose)
        if type(overwrite) is not bool:
            raise ValueError("overwrite must be boolean")
        if len(data) > self.max_file_bytes:
            raise PathPolicyError("File size limit exceeded")
        with self._parent(parts, create=True) as directory:
            _write_at(directory, parts[-1], data, overwrite=overwrite,
                      limit=self.max_file_bytes)

    def read_file(self, path: str, *, purpose: str = "document") -> str:
        try:
            return self._read_bytes(path, purpose=purpose).decode("utf-8")
        except UnicodeError:
            raise PathPolicyError("Files must contain UTF-8 text") from None

    def write_file(self, path: str, content: str, *, purpose: str = "document",
                   overwrite: bool = True) -> None:
        if not isinstance(content, str):
            raise ValueError("File content must be text")
        try:
            data = content.encode("utf-8")
        except UnicodeError:
            raise PathPolicyError("Files must contain UTF-8 text") from None
        self._write_bytes(path, data, purpose=purpose, overwrite=overwrite)

    def list_files(self, path: str = ".") -> tuple[str, ...]:
        """List one directory, hiding unsafe files and reserved broker names."""
        parts = _parts(path, directory=True)
        with self._parent(parts + ("_listing",)) as directory:
            visible = []
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORY_ENTRIES:
                        raise PathPolicyError("Directory entry limit exceeded")
                    try:
                        _parts(entry.name)
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            visible.append(entry.name + "/")
                        else:
                            _regular(info, self.max_file_bytes)
                            if Path(entry.name).suffix.lower() in _ALLOWED_SUFFIXES:
                                visible.append(entry.name)
                    except (ValueError, OSError):
                        continue
            return tuple(sorted(visible))

    def delete_file(self, path: str, *, purpose: str = "document",
                    approved: bool = False) -> None:
        """C10 must supply approval from trusted state, never model arguments."""
        if approved is not True:
            raise PermissionError("File deletion requires human approval")
        parts = validate_file_path(path, purpose)
        with self._parent(parts) as directory:
            _regular(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False),
                     self.max_file_bytes)
            os.unlink(parts[-1], dir_fd=directory)

"""Explicit, recipient-scoped snapshot exchange outside agent workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from evo.ports.tools import Artifact

from .filesystem import (MAX_DIRECTORY_ENTRIES, MAX_FILE_BYTES, PathPolicyError,
                         WorkspaceFilesystem, _directory, _read_at, _write_at,
                         validate_agent, validate_file_path)


_ARTIFACT_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_LIMIT = 16 * 1024


def _recipients(recipients: Sequence[str]) -> tuple[str, ...]:
    if (not isinstance(recipients, (tuple, list))
            or not 1 <= len(recipients) <= 64):
        raise PathPolicyError("Provide 1 to 64 explicit artifact recipients")
    return tuple(sorted({validate_agent(agent) for agent in recipients}))


class ArtifactExchange:
    """Trusted broker for one workspace, with no arbitrary source-agent access.

    The artifacts directory is private application state, never sandbox-mounted.
    Publish snapshots bytes, and import verifies both permission and digest.
    """

    def __init__(self, workspace: WorkspaceFilesystem):
        self._workspace = workspace
        self._root = workspace._state_root / "artifacts"
        with _directory(self._root, create=True):
            pass

    def publish(self, path: str, recipients: Sequence[str], *,
                purpose: str = "document") -> Artifact:
        return self._publish(path, recipients, purpose, uuid.uuid4().hex)

    def publish_verified(self, path, recipient, *, purpose, artifact_id, digest) -> Artifact:
        """Share the verified snapshot under a stable delegation ID."""
        if not _ARTIFACT_ID.fullmatch(artifact_id) or not _SHA256.fullmatch(digest):
            raise PathPolicyError("Invalid verified artifact identity")
        with _directory(self._root) as root:
            try:
                os.stat(artifact_id, dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                return self._publish(path, [recipient], purpose, artifact_id, digest=digest)
        artifact = self._load(artifact_id)
        if (artifact.owner, artifact.name, artifact.purpose, artifact.sha256, artifact.recipients) != (
                self._workspace.agent, Path(path).name, purpose, digest, (recipient,)):
            raise PathPolicyError("Verified artifact identity changed")
        return artifact

    def _publish(self, path, recipients, purpose, artifact_id, *, digest=None):
        recipients = _recipients(recipients)
        data = self._workspace._read_bytes(path, purpose=purpose)
        try:
            data.decode("utf-8")
        except UnicodeError:
            raise PathPolicyError("Artifacts must contain UTF-8 text") from None
        checksum = hashlib.sha256(data).hexdigest()
        if digest is not None and checksum != digest:
            raise PathPolicyError("Artifact changed after verification")
        artifact = Artifact(artifact_id, self._workspace.agent,
                            Path(path).name, purpose, len(data),
                            checksum, recipients)
        manifest = json.dumps(asdict(artifact), ensure_ascii=False).encode("utf-8")
        staging = ".evo-publish-" + uuid.uuid4().hex
        with _directory(self._root) as root:
            os.mkdir(staging, mode=0o700, dir_fd=root)
            try:
                with _directory(self._root / staging) as directory:
                    _write_at(directory, "content", data, overwrite=False,
                              limit=MAX_FILE_BYTES)
                    _write_at(directory, "manifest.json", manifest, overwrite=False,
                              limit=_MANIFEST_LIMIT)
                os.rename(staging, artifact.id, src_dir_fd=root, dst_dir_fd=root)
            except BaseException:
                # Only broker-created names are removed on failure.
                with _directory(self._root / staging) as directory:
                    for name in ("content", "manifest.json"):
                        try:
                            os.unlink(name, dir_fd=directory)
                        except FileNotFoundError:
                            pass
                os.rmdir(staging, dir_fd=root)
                raise
        return artifact

    def _load(self, artifact_id: str) -> Artifact:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise PathPolicyError("Invalid artifact identifier")
        with _directory(self._root / artifact_id) as directory:
            data = _read_at(directory, "manifest.json", _MANIFEST_LIMIT)
        try:
            record = json.loads(data.decode("utf-8"))
            if not isinstance(record, dict) or set(record) != set(Artifact.__dataclass_fields__):
                raise ValueError
            artifact = Artifact(**{**record, "recipients": _recipients(record["recipients"])})
            validate_agent(artifact.owner)
            if len(validate_file_path(artifact.name, artifact.purpose)) != 1:
                raise ValueError
            if (artifact.id != artifact_id or type(artifact.size) is not int
                    or not 0 <= artifact.size <= MAX_FILE_BYTES
                    or not isinstance(artifact.sha256, str)
                    or not _SHA256.fullmatch(artifact.sha256)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise PathPolicyError("Invalid artifact manifest") from None
        return artifact

    def _authorize(self, artifact: Artifact) -> None:
        agent = self._workspace.agent
        if agent != artifact.owner and agent not in artifact.recipients:
            raise PermissionError("Artifact is not shared with this agent")

    def list_available(self) -> tuple[Artifact, ...]:
        visible = []
        with _directory(self._root) as root:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_DIRECTORY_ENTRIES:
                        raise PathPolicyError("Artifact listing limit exceeded")
                    if not _ARTIFACT_ID.fullmatch(entry.name):
                        continue
                    artifact = self._load(entry.name)
                    if (self._workspace.agent == artifact.owner
                            or self._workspace.agent in artifact.recipients):
                        visible.append(artifact)
        return tuple(sorted(visible, key=lambda artifact: artifact.id))

    def import_artifact(self, artifact_id: str, destination: str) -> Artifact:
        artifact = self._load(artifact_id)
        self._authorize(artifact)
        validate_file_path(destination, artifact.purpose)
        if Path(destination).suffix.lower() != Path(artifact.name).suffix.lower():
            raise PathPolicyError("Imported files must preserve the artifact suffix")
        data = self._content(artifact)
        try:
            data.decode("utf-8")
        except UnicodeError:
            raise PathPolicyError("Artifacts must contain UTF-8 text") from None
        self._workspace._write_bytes(destination, data, purpose=artifact.purpose,
                                     overwrite=False)
        return artifact

    def preview(self, artifact_id: str, digest: str) -> dict:
        artifact, data = self.read_snapshot(artifact_id, digest)
        return {"name": artifact.name, "content": data[:65536].decode("utf-8", errors="replace"),
                "truncated": len(data) > 65536}

    def read_snapshot(self, artifact_id: str, digest: str):
        artifact = self._load(artifact_id)
        self._authorize(artifact)
        if artifact.sha256 != digest:
            raise PathPolicyError("Artifact does not match the verified snapshot")
        return artifact, self._content(artifact)

    def _content(self, artifact):
        with _directory(self._root / artifact.id) as directory:
            data = _read_at(directory, "content", MAX_FILE_BYTES)
        if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise PathPolicyError("Artifact content failed integrity verification")
        return data

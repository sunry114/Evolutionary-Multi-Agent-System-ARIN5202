"""Trusted Linux Docker command construction; no model-supplied flags."""
import os
import re
import sys
from pathlib import Path

from evo.ports.tools import ToolError
from evo.adapters.tools.filesystem import _directory

DEFAULT_IMAGE = (
    "python:3.11.11-slim-bookworm@sha256:"
    "081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5"
)


def validate_image(image: str) -> str:
    if not isinstance(image, str) or not re.fullmatch(
        r"python:(?:3\.11\.\d+-slim-bookworm)(?:@sha256:[0-9a-f]{64})?", image
    ):
        raise ToolError("Use a fixed Python 3.11 patch image, optionally pinned by digest")
    return image


def validate_workspace(workspace: Path) -> Path:
    if sys.platform != "linux":
        raise ToolError("Unsupported platform: Docker execution requires Linux")
    if os.getuid() == 0 or os.getgid() == 0:
        raise ToolError("Docker execution requires a non-root host UID/GID")
    workspace = Path(workspace)
    if not workspace.is_absolute() or any(c in str(workspace) for c in ',\n\r\x00'):
        raise ToolError("Invalid absolute workspace mount path")
    if workspace.name != "workspace" or workspace.parent.parent.name != "agents":
        raise ToolError("Only a bound agent workspace may be mounted")
    try:
        with _directory(workspace):
            pass
    except (OSError, ValueError):
        raise ToolError("Unsafe or unavailable workspace") from None
    return workspace


def create_arguments(workspace: Path, name: str, image_id: str, script: str) -> list[str]:
    workspace = validate_workspace(workspace)
    return [
        "create", "--name", name, "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--cpus", "1", "--memory", "256m", "--memory-swap", "256m",
        "--pids-limit", "64", "--ulimit", "nofile=256:256",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--mount", f"type=bind,src={workspace},dst=/workspace",
        "--workdir", "/workspace", "--log-driver", "none",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python",
        image_id, "-I", "-B", "-u", "/workspace/" + script,
    ]

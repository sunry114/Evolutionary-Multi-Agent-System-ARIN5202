"""Trusted observations; a failed technical run can never pass a check."""

from __future__ import annotations

import hashlib
from pathlib import Path

from evo.evaluation.dataset import EvaluationCase


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _fingerprint(workspace, path: str, purpose: str):
    try:
        content = workspace.read_file(path, purpose=purpose)
        info = (workspace.workspace / path).stat()
        return (hashlib.sha256(content.encode()).hexdigest(), info.st_mtime_ns)
    except (OSError, ValueError):
        return None


def snapshot_files(case: EvaluationCase, orchestrator, state_dir: Path) -> dict:
    """Capture targets before execution, including files a factory preseeded."""
    baseline = {}
    root = state_dir.resolve()
    for check in case.checks:
        if not check["kind"].startswith("file_"):
            continue
        agent = check["agent"]
        workspace = orchestrator.workspace_factory(agent)
        if not _within(Path(workspace.workspace).resolve(), root):
            raise ValueError("Evaluation workspace escapes isolated case state")
        key = (agent, check["path"], check["purpose"])
        baseline[key] = _fingerprint(workspace, check["path"], check["purpose"])
    return baseline


def evaluate_checks(case: EvaluationCase, snapshot: dict, orchestrator,
                    state_dir: Path, baseline: dict, trusted_tests: dict) -> list[dict]:
    root = snapshot["task"]
    details = []
    result_text = "\n".join([str(root.get("result", ""))] +
                            [str(child.get("result", "")) for child in snapshot.get("subtasks", ())])
    for check in case.checks:
        kind = check["kind"]
        passed = False
        if root["status"] == "success":
            if kind == "result_contains":
                passed = check["text"] in result_text
            elif kind == "test":
                passed = trusted_tests[check["name"]](case, snapshot, state_dir) is True
            else:
                workspace = orchestrator.workspace_factory(check["agent"])
                if not _within(Path(workspace.workspace).resolve(), state_dir.resolve()):
                    raise ValueError("Evaluation workspace escapes isolated case state")
                key = (check["agent"], check["path"], check["purpose"])
                current = _fingerprint(workspace, check["path"], check["purpose"])
                fresh = current is not None and current != baseline[key]
                if fresh:
                    try:
                        content = workspace.read_file(check["path"], purpose=check["purpose"])
                    except (OSError, ValueError):
                        content = None
                    passed = content is not None and (
                        kind == "file_exists" or
                        kind == "file_contains" and check["text"] in content or
                        kind == "file_exact" and check["text"] == content)
        details.append({"kind": kind, "passed": bool(passed)})
    return details

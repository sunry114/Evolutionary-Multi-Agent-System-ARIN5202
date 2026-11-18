"""Strict JSONL case and expected-result schema."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from evo.adapters.tools.filesystem import validate_agent, validate_file_path
from evo.application.orchestrator import ArtifactCheck


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_KINDS = {"result_contains", "file_exists", "file_contains", "file_exact", "test"}


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    prompt: str
    checks: tuple[dict, ...]
    subtask_checks: dict[str, ArtifactCheck]

    @property
    def basis(self) -> str:
        return "objective" if self.checks else "self_reported"


@dataclass(frozen=True)
class Dataset:
    cases: tuple[EvaluationCase, ...]
    sha256: str


def _check(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
        raise ValueError("Invalid expected check kind")
    kind = raw["kind"]
    fields = {"result_contains": {"kind", "text"},
              "file_exists": {"kind", "agent", "path", "purpose"},
              "file_contains": {"kind", "agent", "path", "purpose", "text"},
              "file_exact": {"kind", "agent", "path", "purpose", "text"},
              "test": {"kind", "name"}}[kind]
    required = {"result_contains": {"kind", "text"},
                "file_exists": {"kind", "agent", "path"},
                "file_contains": {"kind", "agent", "path", "text"},
                "file_exact": {"kind", "agent", "path", "text"},
                "test": {"kind", "name"}}[kind]
    if set(raw) - fields or required - set(raw):
        raise ValueError("Invalid expected check fields")
    check = dict(raw)
    if kind.startswith("file_"):
        validate_agent(check["agent"])
        if not isinstance(check["path"], str):
            raise ValueError("Expected a file path")
        check.setdefault("purpose", "slides" if check["path"].lower().endswith(".html") else "document")
        validate_file_path(check["path"], check["purpose"])
    elif kind == "test":
        if not isinstance(check["name"], str) or not _IDENTIFIER.fullmatch(check["name"]):
            raise ValueError("Invalid trusted test name")
    if "text" in check and (not isinstance(check["text"], str) or not check["text"]
                             or len(check["text"].encode()) > 8192):
        raise ValueError("Expected text must be nonempty and bounded")
    return check


def _case(raw: object) -> EvaluationCase:
    if not isinstance(raw, dict) or set(raw) - {"id", "prompt", "expected", "subtask_checks"}:
        raise ValueError("Invalid dataset case")
    ident, prompt = raw.get("id"), raw.get("prompt")
    if not isinstance(ident, str) or not _IDENTIFIER.fullmatch(ident):
        raise ValueError("Invalid case ID")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 32768:
        raise ValueError("Invalid case prompt")
    expected = raw.get("expected")
    if expected is None:
        checks = ()
    elif not isinstance(expected, list) or not expected or len(expected) > 32:
        raise ValueError("Expected checks must be a nonempty list")
    else:
        checks = tuple(_check(item) for item in expected)
    subtask_raw = raw.get("subtask_checks", {})
    if not isinstance(subtask_raw, dict) or len(subtask_raw) > 5:
        raise ValueError("Invalid subtask checks")
    subtask_checks = {}
    for key, value in subtask_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"s[1-5]", key) or not isinstance(value, dict):
            raise ValueError("Invalid subtask check")
        if set(value) - {"path", "purpose", "contains", "exact"}:
            raise ValueError("Invalid subtask check fields")
        check = ArtifactCheck(**value)
        validate_file_path(check.path, check.purpose)
        if (len(check.contains.encode()) > 8192 or
                (check.exact is not None and len(check.exact.encode()) > 8192)):
            raise ValueError("Subtask expectation is oversized")
        subtask_checks[key] = check
    return EvaluationCase(ident, prompt.strip(), checks, subtask_checks)


def load_dataset(path: str | Path) -> Dataset:
    data = Path(path).read_bytes()
    if not data or len(data) > 4 * 1024 * 1024:
        raise ValueError("Dataset is empty or oversized")
    try:
        lines = data.decode("utf-8").splitlines()
        if not 1 <= len(lines) <= 10000:
            raise ValueError("Invalid dataset size")
        cases = tuple(_case(json.loads(line)) for line in lines if line.strip())
    except (UnicodeError, json.JSONDecodeError, TypeError, KeyError) as error:
        raise ValueError("Invalid JSONL dataset") from error
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Empty dataset or duplicate case ID")
    return Dataset(cases, hashlib.sha256(data).hexdigest())

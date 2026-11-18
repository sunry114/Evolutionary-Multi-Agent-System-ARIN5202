"""Machine-readable metrics with explicit objective denominators."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from statistics import mean

from evo.evaluation.dataset import Dataset


def summarize(results: list[dict], *, dataset_sha256: str,
              memory_enabled: bool, approval_policy: str) -> dict:
    if not results:
        raise ValueError("Cannot report an empty evaluation")
    objectives = [item for item in results if item["basis"] == "objective"]
    total_subtasks = sum(item["total_subtasks"] for item in results)
    completed_subtasks = sum(item["completed_subtasks"] for item in results)
    elapsed = [item["elapsed_seconds"] for item in results]
    return {
        "schema_version": 1, "dataset_sha256": dataset_sha256,
        "memory_enabled": memory_enabled, "approval_policy": approval_policy,
        "case_count": len(results), "objective_count": len(objectives),
        "goal_completion_rate": sum(item["status"] == "success" for item in results) / len(results),
        "subtask_completion_rate": completed_subtasks / total_subtasks if total_subtasks else 0.0,
        "objective_completion_rate": (
            sum(item["passed"] for item in objectives) / len(objectives) if objectives else None),
        "total_elapsed_seconds": round(sum(elapsed), 6),
        "mean_elapsed_seconds": round(mean(elapsed), 6),
        "cases": results,
    }


def verify_report(report: dict, dataset: Dataset) -> bool:
    """Recalculate aggregates and dataset identity from the recorded case facts."""
    try:
        if (report["schema_version"] != 1 or report["dataset_sha256"] != dataset.sha256
                or type(report["memory_enabled"]) is not bool
                or not isinstance(report["approval_policy"], str)
                or [item["id"] for item in report["cases"]] != [case.id for case in dataset.cases]):
            return False
        for item, case in zip(report["cases"], dataset.cases):
            if (item["basis"] != case.basis or item["status"] not in
                    ("success", "failed", "blocked", "waiting_human", "error")
                    or type(item["passed"]) is not bool
                    or not isinstance(item["elapsed_seconds"], (int, float))
                    or not math.isfinite(item["elapsed_seconds"])
                    or item["elapsed_seconds"] < 0
                    or not 0 <= item["completed_subtasks"] <= item["total_subtasks"]
                    or len(item["checks"]) != len(case.checks)
                    or any(value["kind"] != check["kind"] or type(value["passed"]) is not bool
                           for value, check in zip(item["checks"], case.checks))
                    or item["passed"] != (item["status"] == "success" and
                        (all(value["passed"] for value in item["checks"]) if case.checks else True))):
                return False
        expected = summarize(report["cases"], dataset_sha256=dataset.sha256,
                             memory_enabled=report["memory_enabled"],
                             approval_policy=report["approval_policy"])
        return report == expected
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def write_report(report: dict, destination: str | Path, dataset: Dataset) -> None:
    if not verify_report(report, dataset):
        raise ValueError("Invalid evaluation report")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".eval-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()

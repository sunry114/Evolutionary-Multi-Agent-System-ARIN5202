"""Machine-readable metrics with explicit objective denominators."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path
from statistics import mean

from evo.evaluation.dataset import Dataset

APPROVAL_POLICIES = ("manual", "deny_critical")
MEMORY_METRICS = ("negotiation_recalled", "negotiation_cross_issue_recalled",
                  "execution_recalled", "execution_cross_issue_recalled")


def summarize(results: list[dict], *, dataset_sha256: str, model: str,
              memory_enabled: bool, approval_policy: str) -> dict:
    if not results:
        raise ValueError("Cannot report an empty evaluation")
    objectives = [item for item in results if item["basis"] == "objective"]
    total_subtasks = sum(item["total_subtasks"] for item in results)
    completed_subtasks = sum(item["completed_subtasks"] for item in results)
    elapsed = [item["elapsed_seconds"] for item in results]
    return {
        "schema_version": 2, "dataset_sha256": dataset_sha256, "model": model,
        "routing_mode": "squad", "initial_memory": "empty",
        "memory_enabled": memory_enabled, "approval_policy": approval_policy,
        "case_count": len(results), "objective_count": len(objectives),
        "goal_completion_rate": sum(item["status"] == "success" for item in results) / len(results),
        "subtask_completion_rate": completed_subtasks / total_subtasks if total_subtasks else 0.0,
        "objective_completion_rate": (
            sum(item["passed"] for item in objectives) / len(objectives) if objectives else None),
        "total_elapsed_seconds": round(sum(elapsed), 6),
        "mean_elapsed_seconds": round(mean(elapsed), 6),
        "total_routing_calls": sum(item["routing_calls"] for item in results),
        **{key: sum(item[key] for item in results) for key in MEMORY_METRICS},
        "failure_categories": dict(Counter(item["failure_category"] for item in results if not item["passed"])),
        "cases": results,
    }


def comparison(memory_on, memory_off):
    return {"schema_version": 2, "comparison": "squad_memory",
            "memory_on": memory_on, "memory_off": memory_off}


def verify_report(report: dict, dataset: Dataset) -> bool:
    """Recalculate aggregates and dataset identity from the recorded case facts."""
    try:
        if "comparison" in report:
            on, off = report["memory_on"], report["memory_off"]
            return (report == comparison(on, off) and _verify_arm(on, dataset) and _verify_arm(off, dataset)
                    and on["memory_enabled"] and not off["memory_enabled"] and on["model"] == off["model"]
                    and on["approval_policy"] == off["approval_policy"] == "deny_critical")
        return _verify_arm(report, dataset)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def _verify_arm(report, dataset):
    if (report["schema_version"] != 2 or report["dataset_sha256"] != dataset.sha256
            or not isinstance(report["model"], str) or not report["model"].strip()
            or type(report["memory_enabled"]) is not bool
            or report["approval_policy"] not in APPROVAL_POLICIES
            or [item["id"] for item in report["cases"]] != [case.id for case in dataset.cases]):
        return False
    for item, case in zip(report["cases"], dataset.cases):
        if (item["basis"] != case.basis or item["status"] not in
                ("success", "failed", "blocked", "waiting_human", "error")
                or type(item["passed"]) is not bool
                or not isinstance(item["elapsed_seconds"], (int, float))
                or not math.isfinite(item["elapsed_seconds"])
                or item["elapsed_seconds"] < 0
                or any(type(item[key]) is not int or item[key] < 0
                       for key in ("routing_calls", "completed_subtasks", "total_subtasks", *MEMORY_METRICS))
                or not 0 <= item["completed_subtasks"] <= item["total_subtasks"]
                or item["negotiation_cross_issue_recalled"] > item["negotiation_recalled"]
                or item["execution_cross_issue_recalled"] > item["execution_recalled"]
                or (not report["memory_enabled"] and any(item[key] for key in MEMORY_METRICS))
                or (item["failure_category"] is not None if item["passed"] else
                    not isinstance(item["failure_category"], str) or not item["failure_category"])
                or len(item["checks"]) != len(case.checks)
                or any(value["kind"] != check["kind"] or type(value["passed"]) is not bool
                       for value, check in zip(item["checks"], case.checks))
                or item["passed"] != (item["status"] == "success" and
                    (all(value["passed"] for value in item["checks"]) if case.checks else True))):
            return False
    expected = summarize(report["cases"], dataset_sha256=dataset.sha256,
                         model=report["model"],
                         memory_enabled=report["memory_enabled"],
                         approval_policy=report["approval_policy"])
    return report == expected


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

"""One fresh application state and workspace tree per dataset case."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from evo.evaluation.checks import evaluate_checks, snapshot_files
from evo.evaluation.dataset import Dataset, load_dataset
from evo.evaluation.report import summarize, write_report


def _failure(snapshot: dict, passed: bool) -> str | None:
    if passed:
        return None
    if snapshot["task"]["status"] == "waiting_human":
        return "approval_pending"
    children = snapshot.get("subtasks", ())
    results = [str(item.get("result", "")) for item in children]
    if any("Objective evidence missing" in value for value in results):
        return "verification"
    if any("Dependency did not succeed" in value for value in results):
        return "dependency"
    if any("Assignment failed" in value for value in results):
        return "assignment"
    if any("Execution blocked" in value for value in results):
        return "approval_or_policy"
    return "objective" if snapshot["task"]["status"] == "success" else "execution"


class EvaluationRunner:
    def __init__(self, application_factory, *, trusted_tests=None, clock=None):
        self.application_factory = application_factory
        self.trusted_tests = dict(trusted_tests or {})
        self.clock = clock or time.perf_counter

    def run(self, dataset: Dataset, *, memory_enabled=True,
            approval_policy="manual") -> dict:
        if not isinstance(dataset, Dataset) or not dataset.cases:
            raise ValueError("Evaluation requires a nonempty dataset")
        if type(memory_enabled) is not bool or not isinstance(approval_policy, str) or not approval_policy:
            raise ValueError("Invalid evaluation policy")
        unknown = {check["name"] for case in dataset.cases for check in case.checks
                   if check["kind"] == "test" and check["name"] not in self.trusted_tests}
        if unknown:
            raise ValueError("Unknown trusted test check")
        results = []
        for case in dataset.cases:
            with tempfile.TemporaryDirectory(prefix="evo-eval-") as temp:
                state = Path(temp)
                start = None
                snapshot = {"task": {"status": "error"}, "subtasks": []}
                checks = []
                error = None
                try:
                    orchestrator = self.application_factory(state)
                    baseline = snapshot_files(case, orchestrator, state)
                    start = self.clock()
                    snapshot = orchestrator.run(case.prompt, checks=case.subtask_checks,
                                                memory_enabled=memory_enabled)
                    checks = evaluate_checks(case, snapshot, orchestrator, state,
                                             baseline, self.trusted_tests)
                except Exception:
                    # Provider/tool errors can contain keys or user data. Only a
                    # fixed category is emitted in the portable JSON report.
                    error = "evaluation_error"
                    checks = [{"kind": check["kind"], "passed": False} for check in case.checks]
                elapsed = round(max(0.0, self.clock() - start), 6) if start is not None else 0.0
                children = snapshot.get("subtasks", ())
                status = snapshot["task"]["status"]
                if error:
                    status = "error"
                passed = status == "success" and (all(item["passed"] for item in checks)
                                                   if case.checks else True)
                # An exception before or during checks is never a self-reported pass.
                if error:
                    passed = False
                results.append({"id": case.id, "basis": case.basis, "status": status,
                                "passed": passed, "checks": checks,
                                "completed_subtasks": sum(item["status"] == "success" for item in children),
                                "total_subtasks": len(children),
                                "elapsed_seconds": elapsed,
                                "failure_category": error or _failure(snapshot, passed)})
        return summarize(results, dataset_sha256=dataset.sha256,
                         memory_enabled=memory_enabled, approval_policy=approval_policy)


def evaluate_jsonl(dataset_path, output_path, application_factory, *,
                   memory_enabled=True, approval_policy="manual", trusted_tests=None) -> dict:
    """Validate JSONL, execute fresh cases and write a verified JSON report."""
    dataset = load_dataset(dataset_path)
    report = EvaluationRunner(application_factory, trusted_tests=trusted_tests).run(
        dataset, memory_enabled=memory_enabled, approval_policy=approval_policy)
    write_report(report, output_path, dataset)
    return report

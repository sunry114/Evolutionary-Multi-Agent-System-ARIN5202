"""Evaluate ordered cases with isolated workspaces and a private memory state per arm."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from uuid import uuid4

from evo.adapters.llm.client import ProviderError
from evo.adapters.llm.credentials import CredentialError
from evo.application.approvals import ApprovalService
from evo.domain.models import Task
from evo.evaluation.checks import evaluate_checks, snapshot_files
from evo.evaluation.dataset import Dataset, load_dataset
from evo.evaluation.report import APPROVAL_POLICIES, comparison, summarize, write_report


def _failure(snapshot, passed, events, requests):
    if passed:
        return None
    status = snapshot["task"]["status"]
    if status == "waiting_human":
        return ("clarification_pending" if any(row["status"] == "pending" and row["kind"] == "clarification"
                                               for row in requests) else "approval_pending")
    if status == "success":
        return "objective"
    if any(row["status"] == "denied" for row in requests):
        return "approval_denied"
    for event in events:
        if event["event_type"] == "assignment_blocked":
            return event["payload"]["code"]
        if event["event_type"] == "orchestration_error":
            return event["payload"]["code"]
    results = [item["result"] for item in snapshot["subtasks"]]
    if "Objective evidence missing" in results:
        return "verification"
    if any(value.startswith("Dependency did not succeed") for value in results):
        return "dependency"
    if "Execution blocked" in results:
        return "execution_blocked"
    return "execution"


def _resume_after_denials(coordinator, issue_id, snapshot):
    approval = ApprovalService(coordinator.store)
    while snapshot["task"]["status"] == "waiting_human":
        pending = [row for row in coordinator.store.issue_records(issue_id)[4] if row["status"] == "pending"]
        decisions = [row for row in pending if row["kind"] == "approval"]
        if not decisions:
            break
        for row in decisions:
            approval.answer(row["id"], {"approved": False})
        if len(decisions) != len(pending):
            break
        snapshot = coordinator.resume(issue_id)
    return snapshot


class EvaluationRunner:
    def __init__(self, application_factory, *, model, trusted_tests=None, clock=None):
        self.application_factory = application_factory
        self.model = model
        self.trusted_tests = dict(trusted_tests or {})
        self.clock = clock or time.perf_counter

    def run(self, dataset: Dataset, *, memory_enabled=True, approval_policy="manual") -> dict:
        if not isinstance(dataset, Dataset) or not dataset.cases:
            raise ValueError("Evaluation requires a nonempty dataset")
        if type(memory_enabled) is not bool or approval_policy not in APPROVAL_POLICIES:
            raise ValueError("Invalid evaluation policy")
        unknown = {check["name"] for case in dataset.cases for check in case.checks
                   if check["kind"] == "test" and check["name"] not in self.trusted_tests}
        if unknown:
            raise ValueError("Unknown trusted test check")
        with tempfile.TemporaryDirectory(prefix="evo-eval-memory-") as memory:
            results = [self._case(case, Path(memory), memory_enabled, approval_policy) for case in dataset.cases]
        return summarize(results, dataset_sha256=dataset.sha256, model=self.model,
                         memory_enabled=memory_enabled, approval_policy=approval_policy)

    def _case(self, case, memory_state, memory_enabled, approval_policy):
        with tempfile.TemporaryDirectory(prefix="evo-eval-case-") as temp:
            state = Path(temp)
            start = self.clock()
            task_id = uuid4().hex
            store = None
            snapshot = {"task": {"status": "error"}, "subtasks": []}
            checks = [{"kind": check["kind"], "passed": False} for check in case.checks]
            error = None
            try:
                coordinator = self.application_factory(state, memory_state=memory_state)
                baseline = snapshot_files(case, coordinator, state)
                coordinator.store.save_task(Task(task_id, case.prompt, assignee_type="squad",
                                                  assignee_id=coordinator.squad["name"], routing_mode="squad"))
                store = coordinator.store
                snapshot = coordinator.start(task_id, checks=case.subtask_checks, memory_enabled=memory_enabled)
                if approval_policy == "deny_critical":
                    snapshot = _resume_after_denials(coordinator, task_id, snapshot)
            except CredentialError:
                error = "credential_unavailable"
            except ProviderError:
                error = "planning_provider_error"
            except Exception:
                error = "evaluation_error"

            children, events, requests = [], [], []
            if store is not None:
                _, tasks, events, _, requests = store.issue_records(task_id)
                children = [row for row in tasks if row["parent_id"] is not None]
            snapshot["subtasks"] = children
            if error is None:
                try:
                    checks = evaluate_checks(case, snapshot, coordinator, state, baseline, self.trusted_tests)
                except Exception:
                    error = "objective_check_error"
            elapsed = round(max(0.0, self.clock() - start), 6)
            status = "error" if error else snapshot["task"]["status"]
            passed = status == "success" and all(item["passed"] for item in checks)
            routing = [row["payload"] for row in events if row["event_type"] == "routing_call"]
            execution = [row["payload"] for row in events if row["event_type"] == "memory_context"]
            return {
                "id": case.id, "basis": case.basis, "status": status, "passed": passed, "checks": checks,
                "completed_subtasks": sum(item["status"] == "success" for item in children),
                "total_subtasks": len(children), "elapsed_seconds": elapsed,
                "routing_calls": len(routing),
                "negotiation_recalled": sum(row.get("memory_count", 0) for row in routing),
                "negotiation_cross_issue_recalled": sum(row.get("cross_task", 0) for row in routing),
                "execution_recalled": sum(row["count"] for row in execution),
                "execution_cross_issue_recalled": sum(row["cross_task"] for row in execution),
                "failure_category": error or _failure(snapshot, passed, events, requests),
            }


def evaluate_jsonl(dataset_path, output_path, application_factory, *, model,
                   memory_enabled=True, compare_memory=False, trusted_tests=None) -> dict:
    """Run one memory policy or compare two independently initialized memory policies."""
    dataset = load_dataset(dataset_path)
    runner = EvaluationRunner(application_factory, model=model, trusted_tests=trusted_tests)
    if compare_memory:
        report = comparison(runner.run(dataset, memory_enabled=True, approval_policy="deny_critical"),
                            runner.run(dataset, memory_enabled=False, approval_policy="deny_critical"))
    else:
        report = runner.run(dataset, memory_enabled=memory_enabled)
    write_report(report, output_path, dataset)
    return report

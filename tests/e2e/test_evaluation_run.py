import tempfile
import unittest
import json
from pathlib import Path

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.assignment import AssignmentService
from evo.application.orchestrator import Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent
from evo.evaluation.runner import evaluate_jsonl
from tests.e2e.test_collective_task import ScriptedHarness, Workspace


class PlannerProvider:
    def complete(self, messages, json_object=False):
        return {"subtasks": [{"id": "s1", "description": "Write report"}]}


class BidProvider:
    def complete(self, messages, json_object=False):
        return {"score": 5, "reason": "can write"}


class EvaluationE2ETests(unittest.TestCase):
    def test_scripted_plan_bid_write_verify_and_report(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "smoke.jsonl"
            path.write_text(
                '{"id":"report","prompt":"Write report.md containing done",'
                '"subtask_checks":{"s1":{"path":"report.md","exact":"done"}},'
                '"expected":[{"kind":"file_contains","agent":"builder","path":"report.md","text":"done"}]}\n'
                '{"id":"report_again","prompt":"Write report.md containing done",'
                '"subtask_checks":{"s1":{"path":"report.md","exact":"done"}},'
                '"expected":[{"kind":"file_contains","agent":"builder","path":"report.md","text":"done"}]}\n')
            states = []
            agent = Agent("builder", "documents")

            def factory(state):
                states.append(state)
                store = SQLiteStore(state / "events.sqlite3")
                return Orchestrator(
                    store, Planner(PlannerProvider(), (agent,), ApprovalService(store)),
                    AssignmentService(BidProvider(), (agent,), store),
                    lambda task_id, owner, workspace: ScriptedHarness(
                        workspace, lambda request: ("success", "report.md", "done")),
                    lambda owner: Workspace(state, owner), agents=(agent,))

            output = Path(temp) / "result.json"
            report = evaluate_jsonl(path, output, factory, memory_enabled=False,
                                    approval_policy="deny_critical")
            self.assertEqual(report["case_count"], 2)
            self.assertEqual(report["objective_count"], 2)
            self.assertEqual(report["objective_completion_rate"], 1)
            self.assertEqual(report["goal_completion_rate"], 1)
            self.assertEqual(report["subtask_completion_rate"], 1)
            self.assertGreaterEqual(report["total_elapsed_seconds"], 0)
            self.assertEqual(json.loads(output.read_text()), report)
            self.assertEqual(len(set(states)), 2)
            self.assertTrue(all(not state.exists() for state in states))


if __name__ == "__main__":
    unittest.main()

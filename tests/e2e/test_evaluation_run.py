import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.host import HostPythonRunner
from evo.application.approvals import ApprovalService
from evo.application.orchestrator import Orchestrator
from evo.application.planner import Planner
from evo.config import HarnessConfig
from evo.domain.models import Agent
from evo.evaluation.dataset import load_dataset
from evo.evaluation.report import verify_report
from evo.evaluation.runner import evaluate_jsonl
from evo.interfaces.execution_factory import create_orchestrator
from evo.ports.harness import ToolCall
from tests.e2e.test_collective_task import NegotiationProvider, ScriptedHarness, Workspace


class PlannerProvider(NegotiationProvider):
    def complete(self, messages, json_object=False):
        if "Decompose the goal" in messages[0]["content"]:
            goal = json.loads(messages[1]["content"])["goal"]
            return {"subtasks": [{"id": "s1", "description": goal}]}
        return super().complete(messages, json_object)


class EvaluationE2ETests(unittest.TestCase):
    def test_comparison_denies_critical_actions_and_reports_blocked_result_in_both_arms(self):
        agent = Agent("builder", "documents")
        squad = {"name": "triage", "lead": "builder", "members": ["builder"]}

        def factory(state, *, memory_state):
            coordinator = create_orchestrator(
                state, agents=(agent,), squad=squad, provider=PlannerProvider(),
                harness_config=HarnessConfig(adapter="baseline"), memory_state=memory_state)
            start = coordinator.start

            def start_with_actions(issue_id, **options):
                return start(issue_id, actions={"s1": (
                    ToolCall("write_file", {"path": "check.py", "content": "print(1)", "purpose": "code"}),
                    ToolCall("run_python", {"path": "check.py"}),
                )}, **options)
            coordinator.start = start_with_actions
            return coordinator

        with tempfile.TemporaryDirectory() as temp, patch.object(HostPythonRunner, "run_python") as execute:
            path = Path(temp) / "critical.jsonl"
            path.write_text('{"id":"critical","prompt":"Check the script"}\n')
            report = evaluate_jsonl(path, Path(temp) / "result.json", factory, model="scripted", compare_memory=True)
        execute.assert_not_called()
        for arm in report["memory_on"], report["memory_off"]:
            self.assertEqual(arm["cases"][0]["status"], "blocked")
            self.assertEqual(arm["failure_categories"], {"approval_denied": 1})
            self.assertEqual(arm["goal_completion_rate"], 0)
            self.assertEqual(arm["subtask_completion_rate"], 0)

    def test_comparison_reuses_only_private_memory_across_related_issues(self):
        path = Path(__file__).resolve().parents[2] / "examples/memory_sequence.jsonl"
        states = []
        initial_counts = []
        agent = Agent("builder", "documents")
        squad = {"name": "triage", "lead": "builder", "members": ["builder"]}

        def factory(state, *, memory_state):
            states.append((state, memory_state))
            store = SQLiteStore(state / "events.sqlite3")
            provider = PlannerProvider()
            initial_counts.append(len(list(memory_state.iterdir())))

            def script(request):
                content = "DISK TRIAGE FOLLOWUP" if "FOLLOWUP" in request.prompt else "DISK TRIAGE FIRST"
                return "success", "handoff.md", content

            return Orchestrator(
                store, Planner(provider, (agent,), ApprovalService(store), actor=agent),
                lambda task_id, owner, workspace: ScriptedHarness(workspace, script),
                lambda owner: Workspace(state, owner), agents=(agent,), negotiation_provider=provider,
                squad=squad, memory_factory=lambda owner: SQLiteMemoryStore(memory_state, owner))

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "result.json"
            report = evaluate_jsonl(path, output, factory, model="scripted", compare_memory=True)
            self.assertEqual(json.loads(output.read_text()), report)

        self.assertTrue(verify_report(report, load_dataset(path)))
        on, off = report["memory_on"], report["memory_off"]
        for arm in (on, off):
            self.assertEqual(arm["case_count"], 2)
            self.assertEqual(arm["objective_completion_rate"], 1)
            self.assertEqual(arm["goal_completion_rate"], 1)
            self.assertEqual(arm["subtask_completion_rate"], 1)
            self.assertEqual(arm["total_routing_calls"], 6)
            self.assertGreaterEqual(arm["total_elapsed_seconds"], 0)
            self.assertEqual(arm["failure_categories"], {})
        self.assertEqual(on["cases"][0]["negotiation_cross_issue_recalled"], 0)
        self.assertEqual(on["cases"][1]["negotiation_cross_issue_recalled"], 2)
        self.assertEqual(on["cases"][1]["execution_cross_issue_recalled"], 1)
        self.assertEqual(off["negotiation_recalled"], 0)
        self.assertEqual(off["execution_recalled"], 0)
        self.assertEqual(initial_counts, [0, 1, 0, 0])
        self.assertEqual(len({state for state, _ in states}), 4)
        self.assertEqual(states[0][1], states[1][1])
        self.assertEqual(states[2][1], states[3][1])
        self.assertNotEqual(states[0][1], states[2][1])
        self.assertTrue(all(not state.exists() and not memory.exists() for state, memory in states))


if __name__ == "__main__":
    unittest.main()

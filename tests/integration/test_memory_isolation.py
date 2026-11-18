import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.assignment import AssignmentService
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent, Experience
from evo.domain.statuses import TaskStatus
from tests.e2e.test_collective_task import ScriptedHarness, Workspace


class PlanProvider:
    def complete(self, messages, json_object=False):
        return {"subtasks": [{"id": "s1", "description": "Python report"}]}


class BidProvider:
    def complete(self, messages, json_object=False):
        return {"score": 5 if "builder" in messages[0]["content"] else 4,
                "reason": "available"}


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SQLiteStore(self.root / "events.sqlite3")
        self.agents = (Agent("builder", "Python"), Agent("reviewer", "review"))
        self.seen = []
        self.memories = {agent.name: SQLiteMemoryStore(self.root, agent.name) for agent in self.agents}
        self.script = lambda request: ("success", "report.md", "done")

    def make(self):
        def harness(task_id, agent, workspace):
            instance = ScriptedHarness(workspace, lambda request: (
                self.seen.append(request.memory), self.script(request))[1])
            return instance
        return Orchestrator(
            self.store, Planner(PlanProvider(), self.agents, ApprovalService(self.store)),
            AssignmentService(BidProvider(), self.agents, self.store), harness,
            lambda agent: Workspace(self.root, agent), agents=self.agents,
            memory_factory=self.memories.__getitem__)

    def test_private_recall_bid_and_verified_experience(self):
        self.memories["builder"].add(Experience(
            "builder", "Python report", TaskStatus.FAILED, "Check failed Python report", failure_category="verification"))
        self.memories["reviewer"].add(Experience(
            "reviewer", "Unrelated forecast", TaskStatus.SUCCESS, "Private reviewer lesson"))
        result = self.make().run("Python report", checks={"s1": ArtifactCheck("report.md", exact="done")})
        self.assertEqual(result["task"]["status"], "success")
        self.assertIn("Caution", self.seen[0][0])
        self.assertNotIn("Private reviewer lesson", str(self.seen))
        bids = [event["payload"] for event in self.store.list_events(result["task"]["id"])
                if event["event_type"] == "agent_bid"]
        self.assertLess(next(b["adjustment"] for b in bids if b["agent"] == "builder"), 0)
        self.assertEqual(next(b["adjustment"] for b in bids if b["agent"] == "reviewer"), 0)
        self.assertEqual([x.outcome for x in self.memories["builder"].recall("Python report")],
                         [TaskStatus.SUCCESS, TaskStatus.FAILED])
        self.assertEqual(self.memories["reviewer"].recall("Python report"), ())

    def test_no_memory_skips_read_adjustment_and_write(self):
        class ForbiddenMemory:
            def recall(self, *args):
                raise AssertionError("memory read")
            def adjustment(self, *args):
                raise AssertionError("memory adjustment")
            def add(self, *args):
                raise AssertionError("memory write")
        coordinator = self.make()
        coordinator.memory_factory = lambda agent: ForbiddenMemory()
        coordinator.assignment.memory_adjustment = lambda agent, query: ForbiddenMemory().adjustment(query)
        result = coordinator.run("Python report", checks={"s1": ArtifactCheck("report.md", exact="done")},
                                 memory_enabled=False)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(self.seen, [()])
        self.assertEqual(self.memories["builder"].recall("Python report"), ())
        self.assertTrue(all(event["payload"]["adjustment"] == 0
                            for event in self.store.list_events(result["task"]["id"])
                            if event["event_type"] == "agent_bid"))

    def test_failed_artifact_is_saved_only_as_caution(self):
        coordinator = self.make()
        result = coordinator.run("Python report", checks={"s1": ArtifactCheck("report.md", exact="expected")})
        self.assertEqual(result["task"]["status"], "failed")
        found = self.memories["builder"].recall("Python report")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].outcome, TaskStatus.FAILED)
        self.assertEqual(found[0].failure_category, "verification")
        self.assertEqual(found[0].evidence, "")
        self.assertIn("Caution", found[0].lesson)

    def test_wrong_agent_factory_cannot_supply_another_agents_memory(self):
        coordinator = self.make()
        coordinator.memory_factory = lambda agent: self.memories["reviewer"]
        result = coordinator.run("Python report", checks={"s1": ArtifactCheck("report.md", exact="done")})
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(self.memories["reviewer"].recall("Python report"), ())

    def test_resume_uses_original_memory_context(self):
        self.memories["builder"].add(Experience(
            "builder", "Python report", TaskStatus.SUCCESS, "First verified method"))
        calls = []
        def script(request):
            calls.append(request.task_id)
            return ("waiting_human", "", "") if len(calls) == 1 else ("success", "report.md", "done")
        self.script = script
        coordinator = self.make()
        first = coordinator.run("Python report", checks={"s1": ArtifactCheck("report.md", exact="done")})
        self.assertEqual(first["task"]["status"], "waiting_human")
        self.memories["builder"].add(Experience(
            "builder", "Python report", TaskStatus.SUCCESS, "New method after pause"))
        final = coordinator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertEqual(self.seen[0], self.seen[1])
        self.assertNotIn("New method", str(self.seen[1]))

    def test_no_memory_mode_survives_resume(self):
        calls = []
        def script(request):
            calls.append(request.task_id)
            return ("waiting_human", "", "") if len(calls) == 1 else ("success", "report.md", "done")
        self.script = script
        coordinator = self.make()
        first = coordinator.run("Python report", checks={"s1": ArtifactCheck("report.md", exact="done")},
                                memory_enabled=False)
        self.assertEqual(first["task"]["status"], "waiting_human")
        final = coordinator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertEqual(self.seen, [(), ()])
        self.assertEqual(self.memories["builder"].recall("Python report"), ())


if __name__ == "__main__":
    unittest.main()

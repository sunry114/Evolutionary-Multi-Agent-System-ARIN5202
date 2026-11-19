import json
import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.approvals import ApprovalService
from evo.application.orchestrator import ArtifactCheck, Orchestrator
from evo.application.planner import Planner
from evo.domain.models import Agent, Experience, Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import SQLiteApplication
from tests.e2e.test_collective_task import NegotiationProvider, ScriptedHarness, Workspace


class PlanProvider:
    def complete(self, messages, json_object=False):
        return {"subtasks": [{"id": "s1", "description": "Python report"}]}


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = SQLiteStore(self.root / "events.sqlite3")
        self.agents = (Agent("builder", "Python"), Agent("reviewer", "review"))
        self.seen = []
        self.routing = []
        self.memories = {agent.name: SQLiteMemoryStore(self.root, agent.name) for agent in self.agents}
        self.script = lambda request: ("success", "report.md", "done")

    def make(self):
        def harness(task_id, agent, workspace):
            instance = ScriptedHarness(workspace, lambda request: (
                self.seen.append(request.memory), self.script(request))[1])
            return instance
        class RecordingProvider(NegotiationProvider):
            def complete(inner, messages, json_object=False):
                self.routing.append((messages[0]["content"], json.loads(messages[1]["content"])))
                return super().complete(messages, json_object)

        return Orchestrator(
            self.store, Planner(PlanProvider(), self.agents, ApprovalService(self.store)),
            harness,
            lambda agent: Workspace(self.root, agent), agents=self.agents,
            squad={"name": "default", "lead": "reviewer", "members": ["builder", "reviewer"]},
            negotiation_provider=RecordingProvider(), memory_factory=self.memories.__getitem__)

    def test_captain_member_and_harness_get_only_their_own_memory(self):
        self.memories["builder"].add(Experience(
            "builder", "Python report", TaskStatus.FAILED, "Builder private caution",
            failure_category="verification", source_issue_id="prior-builder"))
        self.memories["reviewer"].add(Experience(
            "reviewer", "Python report", TaskStatus.SUCCESS, "Reviewer private lesson",
            source_issue_id="prior-reviewer"))
        coordinator = self.make()
        coordinator.store.save_task(Task("root", "Python report"))
        result = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="done")})
        self.assertEqual(result["task"]["status"], "success")
        (nomination_system, nomination), (reply_system, reply) = self.routing
        self.assertTrue(nomination_system.startswith("You are reviewer"))
        self.assertEqual(nomination["private_memory"], ["Prior verified success: Reviewer private lesson"])
        self.assertTrue(reply_system.startswith("You are builder"))
        self.assertEqual(reply["private_memory"], ["Caution from prior failed: Builder private caution"])
        self.assertNotIn("Reviewer private lesson", str(reply))
        self.assertNotIn("Builder private caution", str(nomination))
        self.assertEqual(self.seen, [("Caution from prior failed: Builder private caution",)])
        app = SQLiteApplication(self.root)
        for task_id in ("root", result["subtasks"][0]["id"]):
            trace = app.dispatch("trace", {"task_id": task_id})
            self.assertNotIn("private caution", str(trace))
            self.assertNotIn("private lesson", str(trace))
        recorded = self.memories["builder"].recall("Python report")
        self.assertEqual([item.outcome for item in recorded], [TaskStatus.SUCCESS, TaskStatus.FAILED])
        self.assertEqual(recorded[0].source_issue_id, "root")

    def test_no_memory_skips_opening_repositories_and_supplying_private_context(self):
        coordinator = self.make()
        def forbidden_memory(agent):
            raise AssertionError("memory store was opened")
        coordinator.memory_factory = forbidden_memory
        coordinator.store.save_task(Task("root", "Python report"))
        result = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="done")},
                                   memory_enabled=False)
        self.assertEqual(result["task"]["status"], "success")
        self.assertEqual(self.seen, [()])
        self.assertTrue(all("private_memory" not in context for _, context in self.routing))
        self.assertEqual(self.memories["builder"].recall("Python report"), ())

    def test_failed_artifact_is_saved_only_as_caution(self):
        coordinator = self.make()
        coordinator.store.save_task(Task("root", "Python report"))
        result = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="expected")})
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
        coordinator.store.save_task(Task("root", "Python report"))
        result = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="done")})
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
        coordinator.store.save_task(Task("root", "Python report"))
        first = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="done")})
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
        coordinator.store.save_task(Task("root", "Python report"))
        first = coordinator.start("root", checks={"s1": ArtifactCheck("report.md", exact="done")},
                                memory_enabled=False)
        self.assertEqual(first["task"]["status"], "waiting_human")
        final = coordinator.resume(first["task"]["id"])
        self.assertEqual(final["task"]["status"], "success")
        self.assertEqual(self.seen, [(), ()])
        self.assertEqual(self.memories["builder"].recall("Python report"), ())


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.application.orchestrator import ArtifactCheck
from evo.application.approvals import ApprovalService
from evo.adapters.tools.docker import DockerPythonRunner
from evo.config import HarnessConfig
from evo.domain.models import Agent, Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.execution_factory import create_orchestrator
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.harness import ToolCall
from evo.ports.tools import ExecutionResult


class Provider:
    def complete(self, messages, json_object=False):
        if "Decompose the goal" in messages[0]["content"]:
            return {"subtasks": [{"id": "s1", "description": "Write report.md"}]}
        return {"score": 5, "reason": "Can write the report"}


class ExecutionFactoryTests(unittest.TestCase):
    def test_real_store_workspace_broker_and_baseline_harness_are_wired(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent("builder", "documents")
            coordinator = create_orchestrator(
                temp, agents=(agent,), provider=Provider(),
                harness_config=HarnessConfig(adapter="baseline"))
            result = coordinator.run("Write report.md", checks={
                "s1": ArtifactCheck("report.md", exact="done")},
                actions={"s1": (ToolCall("write_file", {"path": "report.md", "content": "done"}),)})
            self.assertEqual(result["task"]["status"], "success")
            self.assertEqual(result["subtasks"][0]["status"], "success")
            self.assertEqual((Path(temp) / "agents" / "builder" / "workspace" / "report.md").read_text(), "done")
            events = coordinator.store.list_events(result["subtasks"][0]["id"])
            self.assertTrue(any(item["event_type"] == "objective_verified" for item in events))

    def test_recreated_factory_resumes_one_approved_action(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent("builder", "documents and Python")
            def build():
                return create_orchestrator(
                    temp, agents=(agent,), provider=Provider(),
                    harness_config=HarnessConfig(adapter="baseline"))
            with patch.object(DockerPythonRunner, "run_python",
                              return_value=ExecutionResult(0, "ok", "")) as python:
                coordinator = build()
                first = coordinator.run("Write report.md", checks={
                    "s1": ArtifactCheck("report.md", exact="done")}, actions={"s1": (
                    ToolCall("write_file", {"path": "report.md", "content": "done"}),
                    ToolCall("write_file", {"path": "check.py", "content": "print('ok')", "purpose": "code"}),
                    ToolCall("run_python", {"path": "check.py"}),
                )})
                self.assertEqual(first["task"]["status"], "waiting_human")
                request = coordinator.store.list_pending_requests()[0]
                ApprovalService(coordinator.store).answer(request["id"], {"approved": True})
                final = build().resume(first["task"]["id"])
                self.assertEqual(final["task"]["status"], "success")
                python.assert_called_once_with("check.py", approved=True)

    def test_default_gateway_run_and_resume_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider(), agents=(Agent("builder", "documents"),))
            called = []
            class Coordinator:
                def run(self, prompt, *, checks, task_id, memory_enabled, group_context=None):
                    called.append((prompt, checks["s1"].path, memory_enabled))
                    app.store.save_task(Task(task_id, prompt, status=TaskStatus.WAITING_HUMAN))
                    return {"task": {"id": task_id, "status": "waiting_human"}, "subtasks": []}
                def resume(self, task_id):
                    called.append(("resume", task_id))
                    return {"task": {"id": task_id, "status": "success"}, "subtasks": []}
            with patch.object(app, "_coordinator", return_value=Coordinator()):
                first = app.dispatch("run", {"prompt": "Write report.md", "no_memory": True,
                                             "checks": {"s1": {"path": "report.md", "exact": "done"}}})
                final = app.dispatch("resume", {"task_id": first["task"]["id"]})
            self.assertEqual(first["status"], "waiting_human")
            self.assertEqual(final["status"], "success")
            self.assertEqual(called[0], ("Write report.md", "report.md", False))
            self.assertEqual(called[1], ("resume", first["task"]["id"]))

    def test_four_configured_roles_chat_and_execute_with_objective_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            roles = [{"name": name, "specialty": specialty} for name, specialty in (
                ("researcher", "research"), ("architect", "design"),
                ("builder", "documents"), ("reviewer", "testing"))]
            class TeamProvider(Provider):
                def complete(self, messages, json_object=False):
                    if "multi-agent group chat" in messages[0]["content"]:
                        return {"reply": "Ready to collaborate"}
                    if "Bid honestly" in messages[0]["content"]:
                        return {"score": 5, "reason": "Can contribute"}
                    return super().complete(messages, json_object)
            app = SQLiteApplication(temp, provider=TeamProvider())
            self.assertEqual(app.dispatch("agents", {"agents": roles})["agents"],
                             [item["name"] for item in roles])
            restored = SQLiteApplication(temp, provider=TeamProvider())
            self.assertEqual(restored.snapshot()["agent_details"], roles)
            self.assertEqual(set(restored.dispatch("group", {"message": "Plan report.md"})["replies"]),
                             {item["name"] for item in roles})
            with patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}):
                coordinator = restored._coordinator()
            result = coordinator.run("Write report.md", checks={
                "s1": ArtifactCheck("report.md", exact="done")},
                actions={"s1": (ToolCall("write_file", {"path": "report.md", "content": "done"}),)})
            self.assertEqual(result["task"]["status"], "success")
            self.assertEqual(len([event for event in coordinator.store.list_events(result["task"]["id"])
                                  if event["event_type"] == "agent_bid"]), 4)
            config = next(event["payload"] for event in coordinator.store.list_events(result["task"]["id"])
                          if event["event_type"] == "run_config")
            self.assertEqual(config["agents"], roles)

    def test_group_execute_dispatches_task_after_four_agent_discussion(self):
        with tempfile.TemporaryDirectory() as temp:
            class TeamProvider(Provider):
                def complete(self, messages, json_object=False):
                    return {"reply": "Agreed"}
            app = SQLiteApplication(temp, provider=TeamProvider())
            roles = [{"name": name, "specialty": name} for name in ("a", "b", "c", "d")]
            app.dispatch("agents", {"agents": roles})
            class Coordinator:
                def run(self, prompt, *, checks, task_id, memory_enabled, group_context):
                    self.context = group_context
                    self.prompt = prompt
                    return {"task": {"id": task_id, "status": "waiting_human"}, "subtasks": []}
            coordinator = Coordinator()
            with patch.object(app, "_coordinator", return_value=coordinator):
                result = app.dispatch("group", {"message": "Create report.md", "execute": True,
                    "checks": {"s1": {"path": "report.md", "exact": "done"}}})
            self.assertEqual(result["status"], "waiting_human")
            self.assertEqual(len(result["replies"]), 4)
            self.assertEqual([row["sender"] for row in coordinator.context], ["human", "a", "b", "c", "d"])
            self.assertEqual(coordinator.prompt, "Create report.md")

    def test_group_clarification_restores_confirmed_requirements_and_builds_slide_plan(self):
        class DiscussionProvider(Provider):
            def complete(self, messages, json_object=False):
                if "Decompose the goal" in messages[0]["content"]:
                    self.inputs = json.loads(messages[1]["content"])
                    if "slides.html" not in self.inputs["goal"]:
                        return {"question": "Which file and title should we use?"}
                    return {"subtasks": [{"id": "s1", "description": "Present the confirmed risk analysis"}],
                            "checks": {"s1": {"path": "slides.html", "contains": "Risk analysis"}}}
                if "Bid honestly" in messages[0]["content"]:
                    return super().complete(messages, json_object)
                return {"reply": "Ready"}

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}):
            provider = DiscussionProvider()
            app = SQLiteApplication(temp, provider=provider)
            app.dispatch("group", {"message": "Confirmed: explain investment risks."})
            app.dispatch("dm", {"agent": "builder", "message": "private note"})
            result = app.dispatch("group", {"message": "Prepare the presentation", "execute": True})
            self.assertEqual(result["status"], "waiting_human")
            context = provider.inputs["group_history"]
            self.assertIn({"sender": "human", "content": "Confirmed: explain investment risks."}, context)
            self.assertNotIn("private note", str(context))
            root = result["task"]["id"]
            app.dispatch("group", {"message": "Unrelated later discussion"})
            app = SQLiteApplication(temp, provider=provider)
            request = app.store.list_pending_requests()[0]
            app.dispatch("answer", {"request_id": request["id"], "answer": "slides.html, titled Risk analysis"})
            app.dispatch("resume", {"task_id": root})
            self.assertEqual(provider.inputs["group_history"], context)
            plan = next(e["payload"] for e in app.store.list_events(root) if e["event_type"] == "plan_created")
            self.assertEqual(plan["checks"]["s1"]["purpose"], "slides")
            self.assertEqual(plan["checks"]["s1"]["contains"], "Risk analysis")

    def test_slide_artifact_generation_and_objective_verification(self):
        first = '<section id="intro"><h1>Risk analysis</h1><a href="#risk">Next</a></section>'
        second = '<section id="risk"><h1>Risk factors</h1></section>'
        for body, expected in ((first + second, "success"), (first, "failed"),
                               (first.replace("#risk", "#missing") + second, "failed"),
                               ("Risk analysis", "failed")):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temp:
                coordinator = create_orchestrator(temp, agents=(Agent("builder", "slides"),),
                                                  provider=Provider(), harness_config=HarnessConfig(adapter="baseline"))
                result = coordinator.run("Prepare slides", checks={"s1": ArtifactCheck("slides.html", contains="Risk analysis")},
                                         actions={"s1": (ToolCall("write_file", {"path": "slides.html", "purpose": "slides",
                                                                                "content": body}),)})
                self.assertEqual(result["task"]["status"], expected)
                self.assertEqual((Path(temp) / "agents/builder/workspace/slides.html").read_text(), body)

    def test_resume_uses_task_roles_after_workspace_roles_change(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            original = [{"name": name, "specialty": name} for name in ("a", "b", "c", "d")]
            app.store.save_task(Task("saved", "Goal", status=TaskStatus.WAITING_HUMAN))
            app.store.append_event(task_id="saved", event_type="run_config", actor="orchestrator",
                                   payload={"agents": original})
            app.dispatch("agents", {"agents": [{"name": "replacement", "specialty": "new role"}]})
            class Coordinator:
                def resume(self, task_id):
                    return {"task": {"id": task_id, "status": "success"}, "subtasks": []}
            with patch.object(app, "_coordinator", return_value=Coordinator()) as factory:
                self.assertEqual(app.dispatch("resume", {"task_id": "saved"})["status"], "success")
            self.assertEqual([agent.name for agent in factory.call_args.args[0]], ["a", "b", "c", "d"])

    def test_invalid_role_registry_does_not_replace_existing_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            for roles in ([], [{"name": "a/../b", "specialty": "bad"}],
                          [{"name": "human", "specialty": "reserved"}],
                          [{"name": "same", "specialty": "one"},
                           {"name": "same", "specialty": "two"}]):
                with self.subTest(roles=roles), self.assertRaises(ValueError):
                    app.dispatch("agents", {"agents": roles})
            self.assertEqual(SQLiteApplication(temp).snapshot()["agents"],
                             [agent.name for agent in app.agents])

    def test_evaluation_gateway_uses_scenario_roles_and_restricted_report_path(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            roles = [{"name": name, "specialty": name} for name in ("a", "b", "c", "d")]
            app.dispatch("agents", {"agents": roles})
            output = str(Path(temp) / "reports" / "mid.json")
            with self.assertRaises(ValueError):
                app.dispatch("eval", {"dataset": "mid.jsonl", "output": str(Path(temp) / "agents.json")})
            def evaluate(dataset, destination, factory, *, memory_enabled):
                self.assertEqual(factory(Path(temp) / "case"), "coordinator")
                self.assertEqual([agent.name for agent in create.call_args.kwargs["agents"]],
                                 [item["name"] for item in roles])
                self.assertEqual((dataset, destination, memory_enabled), ("mid.jsonl", Path(output), False))
                return {"objective_count": 1}
            with patch("evo.interfaces.execution_factory.create_orchestrator",
                       return_value="coordinator") as create, patch(
                       "evo.evaluation.runner.evaluate_jsonl", side_effect=evaluate):
                report = app.dispatch("eval", {"dataset": "mid.jsonl", "output": output,
                                                "no_memory": True})
            self.assertEqual(report, {"objective_count": 1})

    def test_invalid_checks_rejected_before_provider_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            for checks in ({}, {"s1": {"path": "../report.md", "exact": "done"}},
                           {"s1": {"path": "report.md"}}):
                with self.subTest(checks=checks), self.assertRaises(ValueError):
                    app.dispatch("run", {"prompt": "Write report.md", "checks": checks})
            self.assertEqual(app.store.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()

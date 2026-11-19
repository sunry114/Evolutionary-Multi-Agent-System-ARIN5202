import contextlib
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from threading import Barrier
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
from evo.interfaces.cli import main
from evo.interfaces.execution_factory import create_orchestrator
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.ports.harness import ToolCall
from evo.ports.tools import ExecutionResult
from tests.e2e.test_collective_task import NegotiationProvider, ScriptedHarness


class Provider(NegotiationProvider):
    def complete(self, messages, json_object=False):
        if "Decompose the goal" in messages[0]["content"]:
            return {"subtasks": [{"id": "s1", "description": "Write report.md"}]}
        return super().complete(messages, json_object)


def registry_for(roles):
    names = [item["name"] for item in roles]
    return {"agents": roles, "squad": {"name": "default", "lead": names[0] if names else "", "members": names}}


class ExecutionFactoryTests(unittest.TestCase):
    def test_real_store_workspace_broker_and_baseline_harness_are_wired(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = Agent("builder", "documents")
            coordinator = create_orchestrator(
                temp, agents=(agent,), direct_assignee="builder", provider=Provider(),
                harness_config=HarnessConfig(adapter="baseline"))
            coordinator.store.save_task(Task("root", "Write report.md"))
            result = coordinator.start("root", checks={
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
                    temp, agents=(agent,), direct_assignee="builder", provider=Provider(),
                    harness_config=HarnessConfig(adapter="baseline"))
            with patch.object(DockerPythonRunner, "run_python",
                              return_value=ExecutionResult(0, "ok", "")) as python:
                coordinator = build()
                coordinator.store.save_task(Task("root", "Write report.md"))
                first = coordinator.start("root", checks={
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

    def test_cli_configure_create_start_and_query_same_squad_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = {"agents": [{"name": "builder", "specialty": "documents"},
                                   {"name": "external", "specialty": "other"}],
                        "squad": {"name": "reports", "lead": "builder", "members": ["builder"]}}
            config_path = root / "squad.json"
            config_path.write_text(json.dumps(registry))

            def command(*args, application=None):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(["--state", temp, *args], application), 0)
                return json.loads(output.getvalue())

            with patch("evo.interfaces.execution_factory.load_key", side_effect=AssertionError) as load_key:
                self.assertEqual(command("configure", "--file", str(config_path))["squad"], registry["squad"])
                issue = command("create", "Write report.md", "--squad", "reports", "--no-memory",
                                "--expect-file", "report.md", "--expect-text", "done")["task"]
                load_key.assert_not_called()
            issue_id = issue["id"]
            self.assertEqual((issue["status"], issue["assignee_id"], issue["owner"]), ("created", "reports", None))
            listed, = command("tasks")["tasks"]
            self.assertEqual((listed["id"], listed["lane"]), (issue_id, "draft"))

            app = SQLiteApplication(temp, provider=Provider())
            def harness(config, state, broker, **kwargs):
                return ScriptedHarness(broker.executor.workspace, lambda request: ("success", "report.md", "done"))
            with patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}), patch(
                    "evo.interfaces.execution_factory.create_harness", side_effect=harness) as runtime:
                started = command("start", issue_id, application=app)
                self.assertEqual(started["task"]["id"], issue_id)
                self.assertEqual(started["status"], "success")
                self.assertEqual(command("start", issue_id, application=app), started)
                runtime.assert_called_once()
            detail = command("task", issue_id)
            self.assertEqual(detail["task"]["status"], "success")
            self.assertEqual(detail["children"][0]["owner"], "builder")
            self.assertEqual([row["id"] for row in command("tasks")["tasks"]], [issue_id])
            config = app.store.latest_event_payload(issue_id, "run_config")
            self.assertEqual(config["squad"], registry["squad"])
            self.assertEqual(config["agents"], registry["agents"][:1])
            self.assertEqual(config["routing_mode"], "squad")
            self.assertFalse(config["memory_enabled"])

    def test_concurrent_start_claims_once(self):
        class Clarifier:
            def complete(self, messages, json_object=False):
                return {"question": "Which file?"}

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}):
            apps = [SQLiteApplication(temp, provider=Clarifier()) for _ in range(2)]
            task_id = apps[0].dispatch("create", {"prompt": "Write a report", "agent": "builder"})["task"]["id"]
            gate = Barrier(2)

            def build(*args, **kwargs):
                coordinator = create_orchestrator(*args, **kwargs)
                gate.wait(timeout=5)
                return coordinator

            with patch("evo.interfaces.execution_factory.create_orchestrator", side_effect=build):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda app: app.dispatch("start", {"task_id": task_id}), apps))
            store = apps[0].store
            self.assertEqual({result["task"]["id"] for result in results}, {task_id})
            self.assertEqual(store.get_task(task_id)["status"], "waiting_human")
            self.assertEqual(len(store.list_pending_requests()), 1)
            self.assertEqual(sum(event["event_type"] == "run_config" for event in store.list_events(task_id)), 1)
            config = store.latest_event_payload(task_id, "run_config")
            self.assertEqual(config["direct_assignee"], "builder")
            self.assertEqual(config["routing_mode"], "direct")
            self.assertIsNone(config["squad"])

    def test_group_discussion_is_claimed_by_one_issue(self):
        class TeamProvider:
            def complete(self, messages, json_object=False):
                return {"reply": "Agreed"}

        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=TeamProvider())
            roles = [{"name": name, "specialty": name} for name in ("a", "b", "c", "d")]
            app.dispatch("configure", registry_for(roles))
            replies = app.dispatch("group", {"message": "Plan report.md"})["replies"]
            self.assertEqual(set(replies), {item["name"] for item in roles})
            issue = app.dispatch("create", {"prompt": "Write report.md", "squad": "default", "from_group": True})
            issue_id = issue["task"]["id"]
            context = app.store.list_group_messages(task_id=issue_id)
            self.assertEqual([row["sender"] for row in context], ["human", "a", "b", "c", "d"])
            self.assertTrue(all(row["task_id"] == issue_id for row in context))
            self.assertEqual(app.store.list_group_messages(), [])
            with self.assertRaisesRegex(ValueError, "No unassigned"):
                app.dispatch("create", {"prompt": "Another task", "squad": "default", "from_group": True})
            self.assertEqual(len(app.store.list_tasks()), 1)

    def test_group_clarification_restores_confirmed_requirements_and_builds_slide_plan(self):
        class DiscussionProvider(Provider):
            def complete(self, messages, json_object=False):
                if "Decompose the goal" in messages[0]["content"]:
                    self.inputs = json.loads(messages[1]["content"])
                    if "slides.html" not in self.inputs["goal"]:
                        return {"question": "Which file and title should we use?"}
                    return {"subtasks": [{"id": "s1", "description": "Present the confirmed risk analysis"}],
                            "checks": {"s1": {"path": "slides.html", "contains": "Risk analysis"}}}
                if any(phrase in messages[0]["content"] for phrase in
                       ("Nominate one listed member", "Respond to the proposed assignment")):
                    return super().complete(messages, json_object)
                return {"reply": "Ready"}

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}):
            provider = DiscussionProvider()
            app = SQLiteApplication(temp, provider=provider)
            app.dispatch("group", {"message": "Confirmed: explain investment risks."})
            app.dispatch("dm", {"agent": "builder", "message": "private note"})
            issue = app.dispatch("create", {"prompt": "Prepare the presentation", "squad": "default", "from_group": True})
            result = app.dispatch("start", {"task_id": issue["task"]["id"]})
            self.assertEqual(result["status"], "waiting_human")
            context = provider.inputs["group_history"]
            self.assertIn({"sender": "human", "content": "Confirmed: explain investment risks."}, context)
            self.assertNotIn("private note", str(context))
            root = result["task"]["id"]
            app.dispatch("group", {"message": "Unrelated later discussion"})
            app = SQLiteApplication(temp, provider=provider)
            request = app.store.list_pending_requests()[0]
            app.dispatch("answer", {"task_id": root, "request_id": request["id"], "answer": "slides.html, titled Risk analysis"})
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
                                                  provider=Provider(), direct_assignee="builder",
                                                  harness_config=HarnessConfig(adapter="baseline"))
                coordinator.store.save_task(Task("root", "Prepare slides"))
                result = coordinator.start("root", checks={"s1": ArtifactCheck("slides.html", contains="Risk analysis")},
                                         actions={"s1": (ToolCall("write_file", {"path": "slides.html", "purpose": "slides",
                                                                                "content": body}),)})
                self.assertEqual(result["task"]["status"], expected)
                self.assertEqual((Path(temp) / "agents/builder/workspace/slides.html").read_text(), body)

    def test_upgrade_v1_config_and_resume_with_saved_roles(self):
        class HistoricalProvider(Provider):
            def complete(self, messages, json_object=False):
                if "Decompose the goal" in messages[0]["content"]:
                    self.inputs = json.loads(messages[1]["content"])
                return super().complete(messages, json_object)

        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"}):
            root = Path(temp)
            initial = files("evo.adapters.persistence").joinpath("migrations", "0001_initial.sql")
            original = [{"name": "builder", "specialty": "original role"}]
            (root / "agents.json").write_text(json.dumps(original))
            config = {"agents": original, "actions": {}, "memory_enabled": False, "group_context": None,
                      "checks": {"s1": {"path": "report.md", "exact": "done"}}}
            now = "2025-11-01T00:00:00+00:00"
            with contextlib.closing(sqlite3.connect(root / "events.sqlite3")) as conn:
                conn.executescript(initial.read_text())
                conn.execute("INSERT INTO tasks (id, description, status, created_at, updated_at) "
                             "VALUES ('saved', 'Write the report', 'waiting_human', ?, ?)", (now, now))
                conn.execute("INSERT INTO approval_requests (id, task_id, agent, kind, status, "
                             "created_at, answer_json) VALUES ('question', 'saved', 'planner', "
                             "'clarification', 'answered', ?, ?)", (now, json.dumps({"answer": "Use report.md"})))
                for kind, payload in (("run_config", config), ("clarification_requested", {
                        "request_id": "question", "question": "Which file?"})):
                    conn.execute("INSERT INTO events (task_id, event_type, payload_json, created_at) "
                                 "VALUES ('saved', ?, ?, ?)", (kind, json.dumps(payload), now))
                conn.commit()

            provider = HistoricalProvider()
            app = SQLiteApplication(temp, provider=provider)
            self.assertEqual(app.store.schema_version(), 9)
            self.assertEqual(json.loads((root / "agents.json").read_text()), registry_for(original))
            self.assertEqual(app.dispatch("task", {"task_id": "saved"})["task"]["id"], "saved")
            self.assertEqual(app.store.latest_event_payload("saved", "run_config"),
                             {**config, "squad": registry_for(original)["squad"], "routing_mode": "squad"})
            app.dispatch("configure", registry_for([{"name": "replacement", "specialty": "new role"}]))
            resumed = SQLiteApplication(temp, provider=provider).dispatch("resume", {"task_id": "saved"})
            self.assertEqual(resumed["task"]["id"], "saved")
            self.assertEqual(provider.inputs["agents"], {"builder": "original role"})
            self.assertIn("Use report.md", provider.inputs["goal"])

    def test_invalid_role_registry_does_not_replace_existing_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            saved = registry_for([{"name": "builder", "specialty": "documents"},
                                  {"name": "reviewer", "specialty": "verification"}])
            app.dispatch("configure", saved)
            for roles in ([], [{"name": "a/../b", "specialty": "bad"}],
                          [{"name": "human", "specialty": "reserved"}],
                          [{"name": "same", "specialty": "one"},
                           {"name": "same", "specialty": "two"}]):
                with self.subTest(roles=roles), self.assertRaises(ValueError):
                    app.dispatch("configure", registry_for(roles))
            for changes in ({"lead": "reviewer", "members": ["builder"]}, {"members": ["builder", "unknown"]},
                            {"members": ["builder", "builder"]}, {"name": "../reports"}):
                with self.subTest(squad=changes), self.assertRaises(ValueError):
                    app.dispatch("configure", {**saved, "squad": {**saved["squad"], **changes}})
            snapshot = SQLiteApplication(temp).snapshot()
            self.assertEqual(snapshot["agent_details"], saved["agents"])
            self.assertEqual(snapshot["squad"], saved["squad"])

    def test_evaluation_gateway_uses_scenario_roles_and_restricted_report_path(self):
        with tempfile.TemporaryDirectory() as temp:
            app = SQLiteApplication(temp, provider=Provider())
            roles = [{"name": name, "specialty": name} for name in ("a", "b", "c", "d")]
            app.dispatch("configure", registry_for(roles))
            output = str(Path(temp) / "reports" / "mid.json")
            with self.assertRaises(ValueError):
                app.dispatch("eval", {"dataset": "mid.jsonl", "output": str(Path(temp) / "agents.json")})
            def evaluate(dataset, destination, factory, *, model, memory_enabled, compare_memory):
                memory_state = Path(temp) / "cohort"
                self.assertEqual(factory(Path(temp) / "case", memory_state=memory_state), "coordinator")
                self.assertEqual([agent.name for agent in create.call_args.kwargs["agents"]],
                                 [item["name"] for item in roles])
                self.assertEqual(create.call_args.kwargs["squad"], registry_for(roles)["squad"])
                self.assertEqual(create.call_args.kwargs["memory_state"], memory_state)
                self.assertEqual(model, create.call_args.kwargs["provider_config"].model)
                self.assertFalse(compare_memory)
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
                    app.dispatch("create", {"prompt": "Write report.md", "agent": "builder", "checks": checks})
            self.assertEqual(app.store.list_tasks(), [])


if __name__ == "__main__":
    unittest.main()

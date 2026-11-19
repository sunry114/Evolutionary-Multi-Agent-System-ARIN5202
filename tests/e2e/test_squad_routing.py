"""CLI, SQLite, negotiation, and artifact verification with scripted model replies."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.interfaces.cli import main
from evo.interfaces.sqlite_application import SQLiteApplication
from tests.e2e.test_collective_task import ScriptedHarness


ROSTER = {
    "agents": [{"name": name, "specialty": skill} for name, skill in (
        ("captain", "coordinate reports"), ("analyst", "analyze logs"), ("reviewer", "verify evidence"))],
    "squad": {"name": "incident", "lead": "captain", "members": ["captain", "analyst", "reviewer"]},
}


def nominate(member, reason, scope=""):
    return {"decision": "nominate", "member": member, "reason": reason, "scope": scope}


class RoutingScript:
    def __init__(self, descriptions, captain, members):
        self.descriptions = descriptions
        self.captain = captain
        self.members = members
        self.calls = []

    def complete(self, messages, *, json_object):
        system = messages[0]["content"]
        context = json.loads(messages[1]["content"])
        self.calls.append((system, context))
        if "Decompose the goal" in system:
            return {"subtasks": [{"id": key, "description": description}
                                 for key, description in self.descriptions.items()]}
        key = (context["subtask"]["id"], context["round"])
        return self.captain[key] if "Act as Squad captain" in system else self.members[key]


class SquadRoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        config = self.state / "team.json"
        config.write_text(json.dumps(ROSTER), encoding="utf-8")
        self.command("configure", "--file", str(config))
        self.requests = []
        self.harness_options = patch.dict("os.environ", {"EVO_HARNESS_ADAPTER": "baseline"})
        self.harness_options.start()
        self.addCleanup(self.harness_options.stop)
        harness = patch("evo.interfaces.execution_factory.create_harness", side_effect=self.harness)
        harness.start()
        self.addCleanup(harness.stop)

    def command(self, *args, provider=None, code=0):
        app = SQLiteApplication(self.state, provider=provider)
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            result = main(args, app)
        self.assertEqual(result, code, errors.getvalue())
        return json.loads(output.getvalue()) if output.getvalue() else None

    def create(self, checks):
        path = self.state / "checks.json"
        path.write_text(json.dumps({key: {"path": f"{key}.md", "exact": "verified"} for key in checks}))
        return self.command("create", "Analyze incident logs and verify the findings.", "--squad", "incident",
                            "--checks-file", str(path), "--no-memory")["task"]["id"]

    def harness(self, config, state, broker, **kwargs):
        def finish(request):
            self.requests.append(request)
            subtask = request.task_id.rsplit("-", 1)[1]
            return "success", f"{subtask}.md", "verified"
        return ScriptedHarness(broker.workspace, finish)

    def test_accept_reassign_and_exhaust_in_one_issue(self):
        scope = "Verify the timestamps independently against the supplied log."
        provider = RoutingScript(
            {"s1": "Write the incident analysis.", "s2": "Verify the evidence.",
             "s3": "Verify the archived logs."},
            {("s1", 1): nominate("analyst", "Experienced with logs."),
             ("s2", 1): nominate("analyst", "Knows the incident."),
             ("s2", 2): nominate("reviewer", "Independent verification is needed.", scope),
             ("s3", 1): nominate("analyst", "May have the archive."),
             ("s3", 2): nominate("reviewer", "Recommended by the analyst."),
             ("s3", 3): nominate("captain", "Check whether the archive can be found.")},
            {("s1", 1): {"decision": "accept", "reason": "I can write the analysis."},
             ("s2", 1): {"decision": "revise", "reason": "We need an independent review.", "scope": scope},
             ("s2", 2): {"decision": "accept", "reason": "I can check the timestamps."},
             ("s3", 1): {"decision": "recommend", "reason": "Ask the reviewer for the archive.", "member": "reviewer"},
             ("s3", 2): {"decision": "blocked", "reason": "I do not have the archive."},
             ("s3", 3): {"decision": "blocked", "reason": "The archive was never supplied."}},
        )
        issue_id = self.create(provider.descriptions)
        self.assertEqual(self.command("task", issue_id)["task"]["routing_mode"], "squad")
        result = self.command("start", issue_id, provider=provider, code=1)
        self.assertEqual((result["task"]["id"], result["status"]), (issue_id, "blocked"))
        first, second, blocked = self.command("task", issue_id)["children"]
        self.assertEqual((first["status"], first["owner"]), ("success", "analyst"))
        self.assertEqual((second["status"], second["owner"]), ("success", "reviewer"))
        self.assertTrue(second["description"].startswith("Verify the evidence."))
        self.assertIn(scope, second["description"])
        self.assertIsNone(blocked["owner"])
        self.assertIn("Negotiation exhausted after 3 rounds", result["task"]["result"])
        self.assertIn("The archive was never supplied.", blocked["result"])
        self.assertEqual([request.agent for request in self.requests], ["analyst", "reviewer"])
        self.assertIn(scope, self.requests[1].prompt)
        self.assertIn('"path": "s2.md"', self.requests[1].prompt)
        self.assertIn('"exact": "verified"', self.requests[1].prompt)

        events = self.command("trace", issue_id)["events"]
        plan = next(event for event in events if event["event_type"] == "plan_created")
        self.assertEqual(plan["actor"], "captain")
        self.assertIn("You are captain", provider.calls[0][0])
        self.assertEqual(provider.calls[0][1]["explicit_checks"]["s2"]["exact"], "verified")
        second_events = [event for event in events if event["payload"].get("subtask_id") == "s2"]
        self.assertEqual([event["event_type"] for event in second_events],
                         ["assignment_proposed", "assignment_reply", "assignment_proposed",
                          "assignment_reply", "assignment_confirmed"])
        self.assertEqual([(event["payload"]["from"], event["payload"]["to"]) for event in second_events[:4]],
                         [("captain", "analyst"), ("analyst", "captain"),
                          ("captain", "reviewer"), ("reviewer", "captain")])
        self.assertEqual(second_events[-1]["payload"]["description"], second["description"])
        blockers = [event["payload"] for event in events if event["event_type"] == "assignment_blocked"]
        self.assertEqual([(entry["subtask_id"], entry["code"], entry["round"]) for entry in blockers],
                         [("s3", "negotiation_exhausted", 3)])

    def test_scope_question_pauses_and_uses_saved_team_on_resume(self):
        proposed = "Corroborate the logs with the external archive."
        approved = "Use only the local logs."
        provider = RoutingScript(
            {"s1": "Review the incident logs."},
            {("s1", 1): nominate("analyst", "Understands the logs."),
             ("s1", 2): {"decision": "clarify", "reason": "The scope may add a new data source.",
                         "question": "Should the review use an external archive?"},
             ("s1", 3): nominate("reviewer", "Can complete the review with the original local logs.")},
            {("s1", 1): {"decision": "revise", "reason": "I propose an additional source.", "scope": proposed},
             ("s1", 3): {"decision": "accept", "reason": "The local logs are enough."}},
        )
        issue_id = self.create(provider.descriptions)
        self.assertEqual(self.command("start", issue_id, provider=provider)["status"], "waiting_human")
        self.assertEqual(self.requests, [])
        trace = self.command("trace", issue_id)["events"]
        question = next(event["payload"] for event in trace if event["event_type"] == "assignment_clarification_requested")
        self.command("resume", issue_id, provider=provider, code=2)
        self.command("answer", issue_id, question["request_id"], "--text", approved)
        SQLiteApplication(self.state).dispatch("configure", {
            "agents": [{"name": "substitute", "specialty": "different"}],
            "squad": {"name": "replacement", "lead": "substitute", "members": ["substitute"]}})
        result = self.command("resume", issue_id, provider=provider)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["subtasks"][0]["owner"], "reviewer")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn(proposed, self.requests[0].prompt)
        system, context = provider.calls[-2]
        self.assertIn("You are captain", system)
        self.assertEqual(context["round"], 3)
        self.assertEqual(context["human_answers"], [{"question": question["question"], "answer": approved}])

    def test_confirmed_scope_is_reused_after_harness_pause(self):
        scope = "Compare against the timestamp column."
        provider = RoutingScript(
            {"s1": "Verify the logs."},
            {("s1", 1): nominate("analyst", "Familiar with the logs."),
             ("s1", 2): nominate("reviewer", "Can verify independently.", scope)},
            {("s1", 1): {"decision": "revise", "scope": scope, "reason": "Use the exact timestamps."},
             ("s1", 2): {"decision": "accept", "reason": "I will verify the timestamps."}},
        )
        issue_id = self.create(provider.descriptions)

        def harness(config, state, broker, **kwargs):
            def finish(request):
                self.requests.append(request)
                if len(self.requests) == 1:
                    return "waiting_human", "", ""
                return "success", "s1.md", "verified"
            return ScriptedHarness(broker.workspace, finish)

        with patch("evo.interfaces.execution_factory.create_harness", side_effect=harness):
            paused = self.command("start", issue_id, provider=provider)
            self.assertEqual(paused["status"], "waiting_human")
            calls_before_resume = len(provider.calls)
            resumed = self.command("resume", issue_id, provider=provider)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(len(provider.calls), calls_before_resume)
        self.assertEqual([request.agent for request in self.requests], ["reviewer", "reviewer"])
        self.assertEqual(self.requests[0].prompt, self.requests[1].prompt)
        self.assertIn(scope, self.requests[1].prompt)

    def test_direct_issue_is_accepted_by_the_named_member(self):
        provider = RoutingScript({"s1": "Write the analysis."}, {},
                                 {("s1", 1): {"decision": "accept", "reason": "I will do the review."}})
        task = self.command("create", "Review the incident", "--agent", "reviewer",
                            "--expect-file", "s1.md", "--expect-text", "verified")["task"]
        self.assertEqual(task["routing_mode"], "direct")
        result = self.command("start", task["id"], provider=provider)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["subtasks"][0]["owner"], "reviewer")
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(all(system.startswith("You are reviewer") for system, _ in provider.calls))
        self.assertIn("analyst", provider.calls[-1][1]["members"])

    def test_squad_issues_route_by_assignee_and_removed_flag_is_rejected(self):
        draft = self.command("create", "Review the incident")["task"]
        assigned = self.command("assign", draft["id"], "--squad", "incident")
        self.assertEqual(assigned["routing_mode"], "squad")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(("create", "Review the incident", "--squad", "incident", "--routing", "squad"),
                 SQLiteApplication(self.state))
        self.assertEqual(error.exception.code, 2)
        self.assertEqual(len(self.command("tasks")["tasks"]), 1)

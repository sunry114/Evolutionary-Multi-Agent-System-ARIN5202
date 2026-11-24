"""Exercise the first Squad demo with scripted planning and Claude CLI responses."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.harness.claude_code import ClaudeCodeAdapter
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.config import HarnessConfig, HarnessModelConfig
from evo.interfaces.sqlite_application import SQLiteApplication
from tests.fixtures.harness import FakeCLITransport


SCENARIO = Path(__file__).resolve().parents[2] / "examples" / "squad_incident"
AGREED_SCOPE = ("The analyst will diagnose the root cause and assemble report.md. The reviewer will independently "
                "check the source log under a delegation initiated by the analyst.")


class IncidentProvider:
    def complete(self, messages, *, json_object):
        system = messages[0]["content"]
        context = json.loads(messages[1]["content"])
        if "Decompose the goal" in system:
            return {"subtasks": [{"id": "s1", "description": context["goal"]}]}
        if "Act as Squad captain" in system:
            return {"decision": "nominate", "member": "analyst", "scope": context["offered_scopes"][-1],
                    "reason": "The analyst owns the report; independent verification can be delegated."}
        if context["proposal"]["to"] == "analyst" and context["round"] == 1:
            return {"decision": "revise", "scope": AGREED_SCOPE,
                    "reason": "A peer should check the log independently of my diagnosis."}
        return {"decision": "accept", "reason": "I can deliver the proposed work."}


def scripted_harness(config, state, broker, **options):
    config = HarnessConfig(adapter="claude_code", timeout=8)
    model = HarnessModelConfig(provider="hkgai", endpoint="https://api1.hkgai.net/v1", model="scripted")
    adapter = ClaudeCodeAdapter(state, broker, config=config, model=model,
                                credential_loader=lambda: "fixture-credential")
    adapter.transport = FakeCLITransport(config)
    return adapter


def prepare_issue(app):
    app.dispatch("configure", json.loads((SCENARIO / "roles.json").read_text(encoding="utf-8")))
    log = (SCENARIO / "test-log.txt").read_text(encoding="utf-8")
    for agent in ("analyst", "reviewer"):
        WorkspaceFilesystem(app.state, agent).write_file("test-log.txt", log, overwrite=False)
    issue = app.dispatch("create", {"prompt": (SCENARIO / "goal.txt").read_text(encoding="utf-8"),
                                   "checks": json.loads((SCENARIO / "checks.json").read_text(encoding="utf-8")),
                                   "squad": "incident", "no_memory": True})
    return issue["task"]["id"]


class SquadIncidentDemoTests(unittest.TestCase):
    def test_counterproposal_delegation_evidence_and_acceptance_share_one_issue(self):
        with tempfile.TemporaryDirectory() as state:
            app = SQLiteApplication(state, provider=IncidentProvider())
            issue = prepare_issue(app)
            with patch("evo.interfaces.execution_factory.create_harness", side_effect=scripted_harness):
                start = app.dispatch("start", {"task_id": issue})
            self.assertEqual(start["status"], "success", start)

            detail = app.dispatch("task", {"task_id": issue})
            self.assertEqual(detail["next_action"], "review")
            self.assertEqual(len(detail["tree"]), 3)
            analyst, = [task for task in detail["tree"] if task["owner"] == "analyst"]
            reviewer, = [task for task in detail["tree"] if task["owner"] == "reviewer"]
            self.assertEqual((analyst["owner"], analyst["parent_id"]), ("analyst", issue))
            self.assertEqual((reviewer["owner"], reviewer["parent_id"]), ("reviewer", analyst["id"]))
            self.assertEqual([event["event_type"] for event in detail["negotiations"]], [
                "assignment_proposed", "assignment_reply", "assignment_proposed", "assignment_reply",
                "assignment_confirmed", "assignment_proposed", "assignment_reply", "assignment_confirmed"])
            initial, reply, proposal, _, confirmed, _, review_reply, _ = detail["negotiations"]
            self.assertEqual((initial["actor"], initial["payload"]["to"], initial["payload"]["scope"]),
                             ("captain", "analyst", ""))
            self.assertEqual((reply["actor"], reply["payload"]["decision"], reply["payload"]["scope"]),
                             ("analyst", "revise", AGREED_SCOPE))
            self.assertEqual((proposal["actor"], proposal["payload"]["scope"]), ("captain", AGREED_SCOPE))
            self.assertEqual(confirmed["payload"]["description"], analyst["description"])
            self.assertIn(AGREED_SCOPE, analyst["description"])
            self.assertEqual((review_reply["actor"], review_reply["payload"]["decision"]), ("reviewer", "accept"))

            requested, completed = [event for event in detail["logs"] if event["event_type"] in
                                    ("delegation_requested", "delegation_completed")]
            self.assertEqual((requested["actor"], requested["payload"]["delegation_id"]), ("analyst", reviewer["id"]))
            self.assertEqual((completed["actor"], completed["payload"]["to"]), ("reviewer", "analyst"))
            self.assertLess(confirmed["id"], requested["id"])
            artifacts = {item["path"]: item for item in detail["artifacts"]}
            self.assertEqual(completed["payload"]["artifact"], artifacts["review.md"]["archive"])
            report, review = [app.dispatch("artifact", {"task_id": issue, "event_id": artifacts[path]["event_id"]})["content"]
                              for path in ("report.md", "review.md")]
            conclusion = next(line for line in review.splitlines() if line.startswith("Independent review conclusion:"))
            self.assertIn(conclusion, report.split("## Independent verification", 1)[1])
            self.assertEqual(WorkspaceFilesystem(state, "analyst").read_file("review.md"), review)

            app.dispatch("review", {"task_id": issue, "decision": "accepted", "feedback": "Verified against the fixture log."})
            reopened = SQLiteApplication(state).dispatch("task", {"task_id": issue})
            self.assertEqual((reopened["lane"], reopened["current"]["review_status"]), ("done", "accepted"))
            decision = next(event for event in reopened["logs"] if event["event_type"] == "review_decided")
            self.assertEqual((decision["task_id"], decision["actor"], decision["payload"]["decision"]),
                             (issue, "human", "accepted"))

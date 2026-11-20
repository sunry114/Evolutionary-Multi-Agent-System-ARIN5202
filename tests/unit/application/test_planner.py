import unittest
from unittest.mock import Mock

from evo.application.planner import Planner
from evo.adapters.llm.client import ProviderError
from evo.domain.models import Agent
from evo.domain.policies import MAX_SUBTASK_DESCRIPTION, PlanValidationError


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete(self, messages, json_object=False):
        self.calls += 1
        return self.result


class Approval:
    def __init__(self):
        self.calls = []

    def request_clarification(self, task_id, question):
        self.calls.append((task_id, question))
        return "request-1"


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.agents = (Agent("builder", "Python"),)

    def test_valid_dag(self):
        result = Planner(Provider({"question": None, "subtasks": [
            {"id": "s1", "description": "Build", "skills": ["Python"], "depends_on": []},
            {"id": "s2", "description": "Check", "depends_on": ["s1"]}]}),
            self.agents).plan("Build and check")
        self.assertEqual([s.id for s in result.subtasks], ["s1", "s2"])
        self.assertEqual(result.subtasks[1].depends_on, ("s1",))

    def test_clarification_is_persisted_when_port_available(self):
        approval = Approval()
        result = Planner(Provider({"question": "Which output file?", "subtasks": []}),
                         self.agents, approval).plan("Create a file", task_id="root")
        self.assertEqual(result.request_id, "request-1")
        self.assertEqual(result.subtasks, ())
        self.assertEqual(approval.calls, [("root", "Which output file?")])

    def test_missing_required_input_creates_request_before_model_call(self):
        provider = Provider({"subtasks": [{"id": "s1", "description": "Guess"}]})
        approval = Approval()
        result = Planner(provider, self.agents, approval).plan(
            "Summarize the attached report", task_id="root",
            required_inputs={"report": None, "audience": "  "})
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result.request_id, "request-1")
        self.assertEqual(result.subtasks, ())
        self.assertEqual(approval.calls, [("root",
            "Please provide the missing required input(s): report, audience.")])

    def test_clarification_requires_persistence_context(self):
        provider = Provider({"question": "Which report?"})
        with self.assertRaisesRegex(ValueError, "request store and task_id"):
            Planner(provider, self.agents).plan("Summarize report")
        with self.assertRaisesRegex(ValueError, "request store and task_id"):
            Planner(provider, self.agents, Approval()).plan("Summarize report")
        with self.assertRaisesRegex(ValueError, "request store and task_id"):
            Planner(provider, self.agents).plan(
                "Summarize report", task_id="root", required_inputs={"report": None})

    def test_clarification_rejects_missing_request_id(self):
        approval = Approval()
        approval.request_clarification = lambda task_id, question: None
        with self.assertRaisesRegex(ValueError, "request ID"):
            Planner(Provider({"question": "Which report?"}),
                    self.agents, approval).plan("Summarize report", task_id="root")

    def test_invalid_dags_rejected(self):
        cases = [[], [{"id": "s2", "description": "Wrong"}],
                 [{"id": "s1", "description": " ", "depends_on": []}],
                 [{"id": "s1", "description": "Cycle", "depends_on": ["s1"]}],
                 [{"id": "s1", "description": "Forward", "depends_on": ["s2"]}],
                 [{"id": "s%d" % i, "description": "Many"} for i in range(1, 7)]]
        for subtasks in cases:
            with self.subTest(subtasks=subtasks), self.assertRaises(ValueError):
                Planner(Provider({"subtasks": subtasks}), self.agents).plan("Goal")

    def test_invalid_completion_and_plan_recover_with_bounded_feedback(self):
        provider, events = Mock(), Mock()
        provider.complete.side_effect = [
            ProviderError("private output", code="invalid_completion"),
            {"subtasks": [{"id": "s2", "description": "Wrong ID"}]},
            {"subtasks": [{"id": "s1", "description": "Build"}]}]
        plan = Planner(provider, self.agents, events=events).plan("Goal", task_id="root")
        self.assertEqual(plan.subtasks[0].id, "s1")
        self.assertEqual(provider.complete.call_count, 3)
        self.assertEqual([c.kwargs["payload"] for c in events.append_event.call_args_list], [
            {"attempt": 1, "code": "invalid_completion", "retrying": True},
            {"attempt": 2, "code": "invalid_plan", "retrying": True,
             "reason": "subtask_id", "field": "subtasks[0].id"}])
        messages = provider.complete.call_args.args[0]
        self.assertEqual(len(messages), 3)
        self.assertIn('"goal": "Goal"', messages[1]["content"])
        self.assertIn("previous response was invalid", messages[2]["content"])
        self.assertNotIn("private output", str(events.mock_calls) + str(messages))

    def test_detailed_software_plan_preserves_descriptions_over_old_limit(self):
        descriptions = ["a" * 2212, "b" * 829, "c" * 1007]
        provider = Provider({"question": None, "subtasks": [
            {"id": "s%d" % (i + 1), "description": description,
             "skills": ["builder"], "depends_on": ["s%d" % n for n in range(1, i + 1)]}
            for i, description in enumerate(descriptions)]})
        result = Planner(provider, self.agents).plan("Build and verify a software application")
        self.assertEqual([s.description for s in result.subtasks], descriptions)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.subtasks[2].depends_on, ("s1", "s2"))

    def test_description_limit_feedback_names_field_and_counts_without_content(self):
        provider, events = Mock(), Mock()
        description = "sensitive details " * 500
        provider.complete.return_value = {"subtasks": [{"id": "s1", "description": description}]}
        with self.assertRaises(PlanValidationError) as raised:
            Planner(provider, self.agents, events=events).plan("Goal", task_id="root")
        self.assertEqual(raised.exception.details, {
            "reason": "description_too_long", "field": "subtasks[0].description",
            "actual": len(description), "limit": MAX_SUBTASK_DESCRIPTION})
        feedback = provider.complete.call_args.args[0][-1]["content"]
        self.assertIn("subtasks[0].description", feedback)
        self.assertIn(str(len(description)), feedback)
        self.assertIn(str(MAX_SUBTASK_DESCRIPTION), feedback)
        self.assertNotIn("sensitive details", feedback + str(events.mock_calls) + str(raised.exception))
        self.assertEqual(events.append_event.call_args.kwargs["payload"]["reason"], "description_too_long")

    def test_description_boundary_and_dependency_rules_remain_enforced(self):
        valid = {"subtasks": [{"id": "s1", "description": "x" * MAX_SUBTASK_DESCRIPTION}]}
        self.assertEqual(len(Planner._parse_result(valid).subtasks[0].description), MAX_SUBTASK_DESCRIPTION)
        for dependencies, reason in ((["s1"], "dependency_order"), (["s1", "s1"], "duplicate_dependency")):
            with self.subTest(dependencies=dependencies), self.assertRaises(PlanValidationError) as raised:
                Planner._parse_result({"subtasks": [{"id": "s1", "description": "Build", "depends_on": dependencies}]})
            self.assertEqual(raised.exception.details["reason"], reason)

    def test_persistent_invalid_completion_stops_after_three_attempts(self):
        provider, events = Mock(), Mock()
        provider.complete.side_effect = ProviderError("private", code="invalid_completion")
        with self.assertRaises(ProviderError):
            Planner(provider, self.agents, events=events).plan("Goal", task_id="root")
        self.assertEqual(provider.complete.call_count, 3)
        self.assertFalse(events.append_event.call_args.kwargs["payload"]["retrying"])

    def test_authentication_and_clarification_store_failures_are_not_retried(self):
        provider = Mock()
        provider.complete.side_effect = ProviderError("private", code="authentication")
        with self.assertRaises(ProviderError):
            Planner(provider, self.agents).plan("Goal")
        self.assertEqual(provider.complete.call_count, 1)
        provider.reset_mock(side_effect=True)
        provider.complete.return_value = {"question": "Which file?"}
        approval = Mock()
        approval.request_clarification.side_effect = ValueError("Store unavailable")
        with self.assertRaisesRegex(ValueError, "Store unavailable"):
            Planner(provider, self.agents, approval).plan("Goal", task_id="root")
        self.assertEqual(provider.complete.call_count, 1)
        self.assertEqual(approval.request_clarification.call_count, 1)


if __name__ == "__main__":
    unittest.main()

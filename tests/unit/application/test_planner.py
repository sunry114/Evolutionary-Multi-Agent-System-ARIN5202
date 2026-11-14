import unittest

from evo.application.planner import Planner
from evo.domain.models import Agent


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


if __name__ == "__main__":
    unittest.main()

import unittest

from evo.application.assignment import AssignmentError, AssignmentService
from evo.adapters.llm.client import ProviderError
from evo.domain.models import Agent, Subtask


class Provider:
    def __init__(self, *results):
        self.results = list(results)

    def complete(self, messages, json_object=False):
        return self.results.pop(0)


class Events:
    def __init__(self):
        self.events = []

    def append_event(self, **values):
        self.events.append(values)


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.agents = (Agent("builder", "Python implementation"),
                       Agent("reviewer", "testing and review"))

    def test_all_bid_and_specialty_breaks_tie(self):
        events = Events()
        service = AssignmentService(Provider(
            {"score": 4, "reason": "Can build", "proposed_approach": "Code", "risks": "Time"},
            {"score": 4, "reason": "Can test"}), self.agents, events)
        result = service.assign(Subtask("s1", "Implement", skills=("Python",)), "task-1")
        self.assertEqual(result.owner, "builder")
        self.assertEqual([event["event_type"] for event in events.events],
                         ["agent_bid", "agent_bid", "agent_assigned"])
        self.assertEqual(events.events[0]["payload"]["raw_score"], 4)
        self.assertEqual(events.events[0]["payload"]["subtask_id"], "s1")

    def test_bids_for_sibling_subtasks_remain_attributable(self):
        events = Events()
        service = AssignmentService(Provider(*([
            {"score": 3, "reason": "Available"}
        ] * 4)), self.agents, events)
        service.assign(Subtask("s1", "First"), "task-1")
        service.assign(Subtask("s2", "Second"), "task-1")
        bids = [event for event in events.events if event["event_type"] == "agent_bid"]
        self.assertEqual([event["payload"]["subtask_id"] for event in bids],
                         ["s1", "s1", "s2", "s2"])

    def test_name_breaks_remaining_tie_and_adjustment_is_bounded(self):
        service = AssignmentService(Provider(
            {"score": 3, "reason": "One"}, {"score": 3, "reason": "Two"}),
            self.agents, memory_adjustment=lambda agent, task: 50)
        result = service.assign(Subtask("s1", "Task"))
        self.assertEqual(result.owner, "builder")
        self.assertTrue(all(b.adjustment == 1 for b in result.bids))

    def test_invalid_bid_rejected(self):
        for score in (-1, 6, True, "nan", float("nan")):
            with self.subTest(score=score), self.assertRaises(ValueError):
                AssignmentService(Provider(*([{"score": score, "reason": "x"}] * 2)),
                                  self.agents).assign(Subtask("s1", "Task"))

    def test_invalid_bid_retried_and_numeric_string_accepted(self):
        events = Events()
        provider = Provider({"reply": "I can help"},
                            {"score": "4.5", "reason": "Can implement"},
                            {"score": 3, "reason": "Can review"})
        result = AssignmentService(provider, self.agents, events).assign(
            Subtask("s1", "Implement"), "task-1")
        self.assertEqual(result.owner, "builder")
        self.assertEqual(result.bids[0].raw_score, 4.5)
        self.assertEqual([event["event_type"] for event in events.events],
                         ["agent_bid_retry", "agent_bid", "agent_bid", "agent_assigned"])

    def test_provider_failure_has_safe_category(self):
        class FailingProvider:
            def complete(self, messages, json_object=False):
                raise ProviderError("provider-secret", code="rate_limit")
        with self.assertRaises(AssignmentError) as error:
            AssignmentService(FailingProvider(), self.agents).assign(Subtask("s1", "Task"))
        self.assertEqual((error.exception.code, error.exception.agent), ("rate_limit", "builder"))
        self.assertNotIn("provider-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()

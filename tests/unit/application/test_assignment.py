import unittest

from evo.application.assignment import AssignmentService
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

    def test_name_breaks_remaining_tie_and_adjustment_is_bounded(self):
        service = AssignmentService(Provider(
            {"score": 3, "reason": "One"}, {"score": 3, "reason": "Two"}),
            self.agents, memory_adjustment=lambda agent, task: 50)
        result = service.assign(Subtask("s1", "Task"))
        self.assertEqual(result.owner, "builder")
        self.assertTrue(all(b.adjustment == 1 for b in result.bids))

    def test_invalid_bid_rejected(self):
        for score in (-1, 6, True, "5", float("nan")):
            with self.subTest(score=score), self.assertRaises(ValueError):
                AssignmentService(Provider({"score": score, "reason": "x"}),
                                  self.agents).assign(Subtask("s1", "Task"))


if __name__ == "__main__":
    unittest.main()

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

    def test_invalid_bid_uses_neutral_fallback(self):
        for score in (-1, 6, True, "nan", float("nan")):
            with self.subTest(score=score):
                result = AssignmentService(Provider(*([{"score": score, "reason": "x"}] * 4)),
                                           self.agents).assign(Subtask("s1", "Task"))
                self.assertEqual([bid.raw_score for bid in result.bids], [0, 0])

    def test_invalid_first_agent_does_not_prevent_other_bids(self):
        events = Events()
        result = AssignmentService(Provider(
            {"reply": "invalid"}, {"reply": "still invalid"},
            {"score": 4, "reason": "Can complete"}), self.agents, events).assign(
                Subtask("s1", "Review"), "task-1")
        self.assertEqual(result.owner, "reviewer")
        self.assertEqual([event["event_type"] for event in events.events],
                         ["agent_bid_retry", "agent_bid_retry", "agent_bid_fallback",
                          "agent_bid", "agent_bid", "agent_assigned"])

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

    def test_invalid_completion_retries_then_other_agent_can_win(self):
        class IntermittentProvider:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, json_object=False):
                self.calls += 1
                if self.calls <= 2:
                    raise ProviderError("private model output", code="invalid_completion")
                return {"score": 4, "reason": "Can implement"}

        events = Events()
        provider = IntermittentProvider()
        result = AssignmentService(provider, self.agents, events).assign(
            Subtask("s1", "Implement"), "task-1")
        self.assertEqual(provider.calls, 3)
        self.assertEqual(result.owner, "reviewer")
        self.assertEqual([bid.raw_score for bid in result.bids], [0, 4])
        self.assertEqual([event["event_type"] for event in events.events],
                         ["agent_bid_retry", "agent_bid_retry", "agent_bid_fallback",
                          "agent_bid", "agent_bid", "agent_assigned"])
        self.assertEqual(events.events[2]["payload"]["reason"], "invalid_completion")
        self.assertNotIn("private model output", str(events.events))

    def test_invalid_completion_recovers_on_second_attempt(self):
        class OneBadCompletion:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, json_object=False):
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("private model output", code="invalid_completion")
                return {"score": 5 if self.calls == 2 else 2, "reason": "Can help"}

        events = Events()
        provider = OneBadCompletion()
        result = AssignmentService(provider, self.agents, events).assign(
            Subtask("s1", "Implement"), "task-1")
        self.assertEqual(result.owner, "builder")
        self.assertEqual([bid.raw_score for bid in result.bids], [5, 2])
        self.assertEqual(events.events[0]["payload"]["reason"], "invalid_completion")

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

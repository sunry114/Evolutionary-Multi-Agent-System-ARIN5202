import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.chat import ChatService, human_question
from evo.domain.models import Agent, Message, Task


class ScriptedProvider:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def complete(self, messages, json_object=False):
        self.calls.append(messages)
        return self.results.pop(0)


class ChatTests(unittest.TestCase):
    def test_workers_escalate_to_lead_and_only_lead_asks_the_human(self):
        agents = (Agent("dev", "Implementation"), Agent("qa", "Testing"), Agent("lead", "Coordinate"))
        provider = ScriptedProvider(
            {"reply": "@human Which rooms?", "waiting_for_human": True},
            {"reply": "@human Which timezone?", "waiting_for_human": True},
            {"reply": "I will coordinate"},
            {"reply": "@human Which rooms and timezone?", "waiting_for_human": True,
             "human_question": "Which rooms and timezone?"})
        questions = []
        service = ChatService(provider, self.store, agents, question_writer=lambda *args: questions.append(args))
        service.group_message("Build")
        self.assertEqual(questions, [("lead", "Which rooms and timezone?", None)])
        self.assertEqual(service.question_sources, ["dev", "qa"])
        worker_messages = [m for m in self.store.list_group_messages() if m["sender"] in ("dev", "qa")]
        self.assertTrue(all("@lead" in m["content"] and "@human" not in m["content"] for m in worker_messages))
        self.assertIn("Which timezone?", provider.calls[-1][1]["content"])

    def test_lead_can_resolve_worker_question_without_interrupting_the_human(self):
        provider = ScriptedProvider({"reply": "Ready"},
                                    {"reply": "@human What is the scope?", "waiting_for_human": True},
                                    {"reply": "The existing task specifies a small trusted team; use that scope."})
        questions = []
        service = ChatService(provider, self.store, self.agents, question_writer=lambda *args: questions.append(args))
        service.group_message("Build for a small trusted team")
        self.assertEqual(questions, [])
        self.assertFalse(service.waiting_for_human)

    def test_only_human_sections_are_extracted_from_mixed_agent_reply(self):
        mixed = "@dev Build the API.\n\n@human Which rooms and timezone?\n\n@qa Test conflicts."
        self.assertEqual(human_question(mixed), "Which rooms and timezone?")
        self.assertEqual(human_question("@dev Implement it. @qa Check it."), "")
        self.assertEqual(human_question("@human Which room? @dev Plan. @human Which date?"),
                         "Which room?\n\nWhich date?")

    def test_structured_human_question_is_separate_from_team_reply(self):
        provider = ScriptedProvider({"reply": "@reviewer Plan checks. @human Please confirm scope.",
                                     "waiting_for_human": True, "human_question": "Which rooms should be supported?"},
                                    {"reply": "I will prepare checks"})
        questions = []
        service = ChatService(provider, self.store, self.agents,
                              question_writer=lambda *args: questions.append(args))
        service.group_message("Discuss")
        self.assertEqual(questions, [("builder", "Which rooms should be supported?", None)])
        self.assertIn("@reviewer Plan checks", self.store.list_group_messages()[1]["content"])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "events.sqlite3")
        self.agents = (Agent("builder", "Python"), Agent("reviewer", "testing"))

    def test_dm_is_private_and_persisted(self):
        self.store.save_message(Message("other", "dm", "human", "secret for reviewer", "reviewer"))
        provider = ScriptedProvider({"reply": "Hello builder"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.direct_message("builder", "Hello"), "Hello builder")
        history = self.store.list_direct_messages("human", "builder")
        self.assertEqual([record["content"] for record in history], ["Hello", "Hello builder"])
        self.assertNotIn("secret for reviewer", provider.calls[0][1]["content"])

    def test_long_group_reply_is_saved_without_silent_truncation(self):
        long_reply = "Detail " * 700 + "FULL-END"
        provider = ScriptedProvider({"reply": long_reply}, {"reply": "Reviewed"})
        service = ChatService(provider, self.store, self.agents)
        replies = service.group_message("Discuss")
        self.assertEqual(replies["builder"], long_reply)
        self.assertEqual(self.store.list_group_messages()[1]["content"], long_reply)

    def test_oversized_reply_is_rejected_instead_of_cut(self):
        service = ChatService(ScriptedProvider({"reply": "x" * 32769}), self.store,
                              (self.agents[0],))
        with self.assertRaisesRegex(ValueError, "size limit"):
            service.group_message("Discuss")
        self.assertEqual(len(self.store.list_group_messages()), 1)

    def test_group_only_uses_chat_history(self):
        self.store.save_task(Task("task-1", "Build"))
        self.store.save_message(Message("task-log", "group", "planner", "private bid", task_id="task-1"))
        provider = ScriptedProvider({"reply": "builder reply"}, {"reply": "reviewer reply"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.group_message("Discuss"),
                         {"builder": "builder reply", "reviewer": "reviewer reply"})
        self.assertEqual(len(self.store.list_group_messages()), 4)
        self.assertNotIn("private bid", provider.calls[0][1]["content"])

    def test_agent_mention_routes_a_bounded_reply_in_the_same_task(self):
        self.store.save_task(Task("task-mention", "Plan"))
        provider = ScriptedProvider({"reply": "@reviewer Please check this"},
                                    {"reply": "I will check it"},
                                    {"reply": "Checked"})
        service = ChatService(provider, self.store, self.agents)
        replies = service.group_message("Plan", task_id="task-mention")
        self.assertEqual(set(replies), {"builder", "reviewer"})
        self.assertEqual(service.directed_replies,
                         [{"from": "reviewer", "to": "builder", "reply": "Checked"}])
        history = [item for item in self.store.list_group_messages()
                   if item["task_id"] == "task-mention"]
        self.assertEqual([item["sender"] for item in history],
                         ["human", "builder", "reviewer", "reviewer"])
        self.assertIn("addressed by @builder", provider.calls[-1][0]["content"])

    def test_unknown_and_self_mentions_do_not_trigger_more_calls(self):
        provider = ScriptedProvider({"reply": "@unknown @builder no routing"},
                                    {"reply": "done"})
        service = ChatService(provider, self.store, self.agents)
        service.group_message("Discuss")
        self.assertEqual(len(provider.calls), 2)

    def test_human_question_waits_privately_while_other_agents_keep_speaking(self):
        provider = ScriptedProvider({"reply": "Which rooms?", "waiting_for_human": True},
                                    {"reply": "I can plan validation meanwhile"},
                                    {"reply": "Using Room A and Room B"})
        questions = []
        service = ChatService(provider, self.store, self.agents,
                              question_writer=lambda *args: questions.append(args))
        self.assertEqual(set(service.group_message("Build a booking system")),
                         {"builder", "reviewer"})
        self.assertTrue(service.waiting_for_human)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(questions, [("builder", "Which rooms?", None)])
        self.store.save_message(Message("answer", "dm", "human", "Room A and Room B", "builder"))
        service.resume_question("builder", None)
        self.assertFalse(service.waiting_for_human)
        self.assertEqual(len(provider.calls), 3)
        self.assertIn("Room A and Room B", provider.calls[2][1]["content"])

    def test_human_mention_stops_queued_agent_followups(self):
        provider = ScriptedProvider({"reply": "@reviewer check"},
                                    {"reply": "@human What capacity is needed?"},
                                    {"reply": "@human Please confirm room capacity."})
        service = ChatService(provider, self.store, self.agents)
        service.group_message("Discuss")
        self.assertTrue(service.waiting_for_human)
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(service.directed_replies, [])

    def test_human_question_in_directed_followup_stops_the_round(self):
        provider = ScriptedProvider({"reply": "@reviewer check"}, {"reply": "OK"},
                                    {"reply": "@human Please confirm @builder"},
                                    {"reply": "@human Please confirm the requirement."})
        service = ChatService(provider, self.store, self.agents)
        service.group_message("Discuss")
        self.assertTrue(service.waiting_for_human)
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(len(service.directed_replies), 1)

    def test_mutual_mentions_stop_after_each_pair_is_answered(self):
        provider = ScriptedProvider(*({"reply": "@reviewer check"} if index % 2 == 0
                                      else {"reply": "@builder revise"} for index in range(4)))
        service = ChatService(provider, self.store, self.agents)
        service.group_message("Discuss")
        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(len(service.directed_replies), 2)

    def test_invalid_input_and_reply(self):
        service = ChatService(ScriptedProvider({"reply": " "}), self.store, self.agents)
        with self.assertRaises(ValueError):
            service.direct_message("unknown", "Hello")
        with self.assertRaises(ValueError):
            service.group_message(" ")
        with self.assertRaises(ValueError):
            service.direct_message("builder", "Hello")


if __name__ == "__main__":
    unittest.main()

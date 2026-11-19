import json
import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.chat import ChatService
from evo.domain.models import Agent, Message, Task


class ScriptedProvider:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def complete(self, messages, json_object=False):
        self.calls.append(messages)
        return self.results.pop(0)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = SQLiteStore(Path(self.directory.name) / "events.sqlite3")
        self.agents = (Agent("builder", "Python"), Agent("reviewer", "testing"))

    def test_dm_is_private_and_persisted(self):
        self.store.save_message(Message("other", "dm", "human", "secret for reviewer", "reviewer"))
        provider = ScriptedProvider({"reply": "I can build it"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.direct_message("builder", "Can you build it?"), "I can build it")
        history = self.store.list_direct_messages("human", "builder")
        self.assertEqual([record["content"] for record in history], ["Can you build it?", "I can build it"])
        self.assertNotIn("secret for reviewer", provider.calls[0][1]["content"])

    def test_group_only_uses_chat_history(self):
        self.store.save_task(Task("task-1", "Build"))
        self.store.save_message(Message("task-log", "group", "planner", "private bid", task_id="task-1"))
        self.store.save_message(Message("earlier", "group", "human", "讨论构建报告"))
        provider = ScriptedProvider({"reply": "builder reply"}, {"reply": "reviewer reply"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.group_message("你好，帮我看看刚才的讨论"),
                         {"builder": "builder reply", "reviewer": "reviewer reply"})
        self.assertEqual(len(self.store.list_group_messages()), 4)
        builder = json.loads(provider.calls[0][1]["content"])
        reviewer = json.loads(provider.calls[1][1]["content"])
        self.assertEqual(builder["latest_message"], {"sender": "human", "content": "你好，帮我看看刚才的讨论"})
        self.assertEqual(builder["history"], [{"sender": "human", "content": "讨论构建报告"}])
        self.assertEqual(reviewer["history"], builder["history"] + [{"sender": "builder", "content": "builder reply"}])

    def test_standalone_greetings_do_not_include_history_or_issue_instructions(self):
        agents = (Agent("captain", "Nominate analyst immediately"), Agent("analyst", "Write report.md now"))
        self.store.save_message(Message("old-group", "group", "captain", "Resume the old nomination"))
        self.store.save_message(Message("old-dm", "dm", "human", "Private review instructions", "captain"))
        provider = ScriptedProvider({"reply": "你好！"}, {"reply": "见到你很高兴！"}, {"reply": "Hello there!"})
        service = ChatService(provider, self.store, agents)

        self.assertEqual(service.group_message("你们好！"), {"captain": "你好！", "analyst": "见到你很高兴！"})
        self.assertEqual(service.direct_message("captain", "HELLO?"), "Hello there!")
        for call, agent, message in zip(provider.calls, ("captain", "analyst", "captain"), ("你们好！", "你们好！", "HELLO?")):
            self.assertTrue(call[0]["content"].startswith(f"You are {agent},"))
            self.assertIn("standalone greeting", call[0]["content"])
            self.assertNotIn("Nominate analyst immediately", call[0]["content"])
            self.assertNotIn("Write report.md now", call[0]["content"])
            self.assertEqual(json.loads(call[1]["content"]), {"latest_message": {"sender": "human", "content": message}})
        self.assertEqual([row["sender"] for row in self.store.list_group_messages()], ["captain", "human", "captain", "analyst"])

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

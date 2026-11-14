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
        provider = ScriptedProvider({"reply": "Hello builder"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.direct_message("builder", "Hello"), "Hello builder")
        history = self.store.list_direct_messages("human", "builder")
        self.assertEqual([record["content"] for record in history], ["Hello", "Hello builder"])
        self.assertNotIn("secret for reviewer", provider.calls[0][1]["content"])

    def test_group_only_uses_chat_history(self):
        self.store.save_task(Task("task-1", "Build"))
        self.store.save_message(Message("task-log", "group", "planner", "private bid", task_id="task-1"))
        provider = ScriptedProvider({"reply": "builder reply"}, {"reply": "reviewer reply"})
        service = ChatService(provider, self.store, self.agents)
        self.assertEqual(service.group_message("Discuss"),
                         {"builder": "builder reply", "reviewer": "reviewer reply"})
        self.assertEqual(len(self.store.list_group_messages()), 4)
        self.assertNotIn("private bid", provider.calls[0][1]["content"])

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

"""C06 planner questions use the real SQLite request and event tables."""

import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.application.planner import Planner, StoredClarificationRequests
from evo.domain.models import Agent, Task


class MissingInputProvider:
    def complete(self, messages, json_object=False):
        return {"question": "Which report should I summarize?", "subtasks": []}


class ClarificationPersistenceTests(unittest.TestCase):
    def test_question_survives_reopening_store_and_is_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = SQLiteStore(path)
            store.save_task(Task(id="root", description="Summarize the report"))
            result = Planner(MissingInputProvider(), (Agent("writer", "summaries"),),
                             StoredClarificationRequests(store)).plan(
                                 "Summarize the report", task_id="root")

            self.assertEqual(result.subtasks, ())
            self.assertIsNotNone(result.request_id)
            reopened = SQLiteStore(path)
            request = reopened.get_approval_request(result.request_id)
            self.assertEqual(request["task_id"], "root")
            self.assertEqual(request["kind"], "clarification")
            self.assertEqual(request["status"], "pending")
            self.assertEqual(request["payload"], {"question": result.question})
            self.assertEqual(
                [entry["id"] for entry in reopened.list_pending_requests()],
                [result.request_id])
            self.assertTrue(any(event["event_type"] == "request_created"
                                for event in reopened.list_events("root")))


if __name__ == "__main__":
    unittest.main()

"""C11 task lifecycle transitions are atomic and keep review separate."""

import tempfile
import unittest
from pathlib import Path

from evo.adapters.persistence.sqlite_store import ConflictError, SQLiteStore
from evo.domain.models import Task


class TaskTransitionTests(unittest.TestCase):
    def test_success_creates_pending_review_and_rejects_stale_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(Path(temp) / "state.sqlite3")
            store.save_task(Task("t1", "Build"))
            store.transition_task("t1", "ready", expected="created")
            store.transition_task("t1", "assigned", expected="ready", owner="builder")
            store.transition_task("t1", "running", expected="assigned")
            with self.assertRaises(ConflictError):
                store.transition_task("t1", "failed", expected="assigned")
            task = store.transition_task("t1", "success", expected="running", result="verified")
            self.assertEqual(task["status"], "success")
            self.assertEqual(task["review_status"], "pending")
            self.assertEqual(task["result"], "verified")


if __name__ == "__main__":
    unittest.main()

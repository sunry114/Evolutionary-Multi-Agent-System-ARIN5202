import gc
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import (
    ConflictError,
    SQLiteStore,
)
from evo.application.reviews import ReviewService
from evo.domain.models import Message, Task
from evo.domain.statuses import TaskStatus


class SQLiteMigrationTests(unittest.TestCase):
    def test_empty_directory_is_migrated_and_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "nested"
                / "events.sqlite3"
            )

            first = SQLiteStore(database)

            self.assertTrue(database.is_file())
            self.assertEqual(first.schema_version(), 1)

            second = SQLiteStore(database)

            self.assertEqual(second.schema_version(), 1)

            with closing(sqlite3.connect(database)) as connection:
                 rows = connection.execute(
                    """
                    SELECT version, name
                    FROM schema_migrations
                    ORDER BY version
                    """
                ).fetchall()

            self.assertEqual(
                rows,
                [(1, "0001_initial")],
            )

    def test_foreign_keys_busy_timeout_and_schema_version(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(
                Path(temp) / "events.sqlite3",
                busy_timeout_ms=4321,
            )

            settings = store.sqlite_settings()

            self.assertEqual(settings["foreign_keys"], 1)
            self.assertEqual(settings["busy_timeout"], 4321)
            self.assertEqual(settings["user_version"], 1)

    def test_connection_is_released_and_directory_can_be_removed(self):
        temp = tempfile.mkdtemp()
        state = Path(temp)
        database = state / "events.sqlite3"

        store = SQLiteStore(database)
        store.schema_version()
        del store
        gc.collect()

        database.unlink()
        state.rmdir()

        self.assertFalse(state.exists())


class SQLiteTaskAndTraceTests(unittest.TestCase):
    def test_task_message_and_trace_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "events.sqlite3"
            first = SQLiteStore(database)

            first.save_task(
                Task(
                    id="t1",
                    description="Create report",
                    owner="builder",
                    status=TaskStatus.RUNNING,
                )
            )

            first.save_message(
                Message(
                    id="m1",
                    kind="dm",
                    sender="human",
                    recipient="builder",
                    task_id="t1",
                    content="Please continue",
                )
            )

            first.append_event(
                task_id="t1",
                event_type="custom_event",
                actor="builder",
                payload={"safe": True},
            )

            del first

            second = SQLiteStore(database)
            task = second.get_task("t1")
            messages = second.list_direct_messages(
                "human",
                "builder",
            )
            events = second.list_events("t1")

            self.assertEqual(task["status"], "running")
            self.assertEqual(task["owner"], "builder")

            self.assertEqual(len(messages), 1)
            self.assertEqual(
                messages[0]["content"],
                "Please continue",
            )

            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "task_created",
                    "message_created",
                    "custom_event",
                ],
            )
            self.assertEqual(
                events[-1]["payload"],
                {"safe": True},
            )

    def test_foreign_key_violation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(
                Path(temp) / "events.sqlite3"
            )

            with self.assertRaises(ConflictError):
                store.save_message(
                    Message(
                        id="m1",
                        kind="dm",
                        sender="human",
                        recipient="builder",
                        task_id="missing-task",
                        content="Hello",
                    )
                )

    def test_duplicate_task_does_not_create_dirty_event(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SQLiteStore(
                Path(temp) / "events.sqlite3"
            )

            task = Task(
                id="t1",
                description="Create report",
                owner="builder",
            )

            store.save_task(task)

            with self.assertRaises(ConflictError):
                store.save_task(task)

            events = store.list_events("t1")

            self.assertEqual(
                [
                    event["event_type"]
                    for event in events
                ],
                ["task_created"],
            )


class SQLiteReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = (
            tempfile.TemporaryDirectory()
        )
        self.addCleanup(
            self.temporary_directory.cleanup
        )

        self.database = (
            Path(self.temporary_directory.name)
            / "events.sqlite3"
        )
        self.store = SQLiteStore(self.database)
        self.store.save_task(
            Task(
                id="t1",
                description="Create report",
                owner="builder",
                status=TaskStatus.SUCCESS,
                result="report.md",
            )
        )

    def test_review_survives_restart(self):
        service = ReviewService(self.store)

        result = service.decide(
            "t1",
            "accepted",
        )

        self.assertEqual(
            result["review_status"],
            "accepted",
        )
        self.assertEqual(
            result["status"],
            "success",
        )

        restarted = SQLiteStore(self.database)
        restored = restarted.get_task("t1")

        self.assertEqual(
            restored["review_status"],
            "accepted",
        )
        self.assertEqual(
            restored["status"],
            "success",
        )

    def test_duplicate_review_decision_has_no_dirty_event(self):
        service = ReviewService(self.store)

        service.decide(
            "t1",
            "changes_requested",
            "Fix the output",
        )

        with self.assertRaises(ValueError):
            service.decide(
                "t1",
                "accepted",
            )

        events = self.store.list_events("t1")
        review_events = [
            event
            for event in events
            if event["event_type"]
            == "review_decided"
        ]

        self.assertEqual(len(review_events), 1)

        restored = self.store.get_task("t1")
        self.assertEqual(
            restored["review_status"],
            "changes_requested",
        )
        self.assertEqual(
            restored["review_feedback"],
            "Fix the output",
        )

    def test_revision_is_created_and_linked_atomically(self):
        service = ReviewService(self.store)

        service.decide(
            "t1",
            "changes_requested",
            "Fix the output",
        )

        child = service.revise(
            "t1",
            "Apply the review feedback",
        )

        parent = self.store.get_task("t1")
        children = self.store.list_children("t1")

        self.assertEqual(
            parent["status"],
            "success",
        )
        self.assertEqual(
            parent["review_status"],
            "revising",
        )
        self.assertEqual(
            parent["revision_task_id"],
            child["id"],
        )

        self.assertEqual(child["parent_id"], "t1")
        self.assertEqual(child["status"], "created")
        self.assertEqual(len(children), 1)

        with self.assertRaises(ValueError):
            service.revise(
                "t1",
                "Create a duplicate revision",
            )

        self.assertEqual(
            len(self.store.list_children("t1")),
            1,
        )

        events = self.store.list_events("t1")
        revision_events = [
            event
            for event in events
            if event["event_type"]
            == "revision_linked"
        ]

        self.assertEqual(len(revision_events), 1)

    def test_changes_requested_requires_feedback(self):
        service = ReviewService(self.store)

        with self.assertRaises(ValueError):
            service.decide(
                "t1",
                "changes_requested",
                " ",
            )

        task = self.store.get_task("t1")

        self.assertEqual(
            task["review_status"],
            "pending",
        )

        review_events = [
            event
            for event in self.store.list_events("t1")
            if event["event_type"]
            == "review_decided"
        ]

        self.assertEqual(review_events, [])


class SQLiteApprovalTests(unittest.TestCase):
    def test_approval_request_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "events.sqlite3"
            first = SQLiteStore(database)

            first.save_task(
                Task(
                    id="t1",
                    description="Run code",
                    owner="builder",
                    status=TaskStatus.WAITING_HUMAN,
                )
            )

            first.save_approval_request(
                request_id="r1",
                task_id="t1",
                agent="builder",
                kind="approval",
                action="run_python",
                payload={
                    "path": "main.py",
                },
            )

            del first

            second = SQLiteStore(database)
            request = second.get_approval_request(
                "r1"
            )

            self.assertEqual(
                request["status"],
                "pending",
            )
            self.assertEqual(
                request["payload"],
                {"path": "main.py"},
            )


class SQLiteMemoryIsolationTests(unittest.TestCase):
    def test_memory_database_is_physically_separate_per_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = SQLiteMemoryStore(
                temp,
                "builder",
            )
            researcher = SQLiteMemoryStore(
                temp,
                "researcher",
            )

            self.assertNotEqual(
                builder.path,
                researcher.path,
            )
            self.assertTrue(builder.path.is_file())
            self.assertTrue(researcher.path.is_file())
            self.assertEqual(
                builder.schema_version(),
                1,
            )
            self.assertEqual(
                researcher.schema_version(),
                1,
            )

    def test_invalid_agent_identifier_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for agent in (
                "../builder",
                "builder/researcher",
                "",
                ".",
            ):
                with self.subTest(agent=agent):
                    with self.assertRaises(ValueError):
                        SQLiteMemoryStore(
                            temp,
                            agent,
                        )


if __name__ == "__main__":
    unittest.main()

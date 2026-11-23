import gc
import sqlite3
import tempfile
import unittest
from contextlib import closing
from importlib.resources import files
from pathlib import Path

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import (
    ConflictError,
    MIGRATIONS,
    SQLiteStore,
)
from evo.application.reviews import ReviewService
from evo.domain.models import Message, Task
from evo.domain.statuses import TaskStatus


class SQLiteMigrationTests(unittest.TestCase):
    def test_v7_private_messages_become_one_default_chat_per_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "events.sqlite3"
            migrations = files("evo.adapters.persistence").joinpath("migrations")
            with closing(sqlite3.connect(database)) as connection:
                for name in MIGRATIONS[:7]:
                    connection.executescript(migrations.joinpath(name + ".sql").read_text())
                connection.execute(
                    "INSERT INTO tasks(id, description, status, created_at, updated_at) "
                    "VALUES ('issue', 'Report', 'created', 'now', 'now')")
                connection.executemany(
                    "INSERT INTO messages(id, kind, sender, recipient, task_id, content, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)", (
                        ("1", "dm", "human", "builder", None, "Build report", "2025-01-01T08:00:00+00:00"),
                        ("2", "dm", "builder", "human", None, "Built report", "2025-01-01T08:01:00+00:00"),
                        ("3", "dm", "human", "reviewer", None, "Review report", "2025-01-01T08:02:00+00:00"),
                        ("4", "group", "human", None, None, "Workspace message", "2025-01-01T08:03:00+00:00"),
                        ("5", "dm", "human", "builder", "issue", "Issue message", "2025-01-01T08:04:00+00:00"),
                    ))
                connection.commit()

            store = SQLiteStore(database)
            reviewer, builder = store.list_chats()
            self.assertEqual((reviewer["agent"], builder["agent"]), ("reviewer", "builder"))
            self.assertEqual(builder["title"], "Build report")
            self.assertEqual(builder["updated_at"], "2025-01-01T08:01:00+00:00")
            self.assertEqual(store.create_chat("builder", default=True)["id"], builder["id"])
            self.assertEqual([row["id"] for row in store.list_direct_messages("human", "builder", chat_id=builder["id"])],
                             ["1", "2"])
            self.assertEqual([row["id"] for row in store.list_group_messages()], ["4"])
            with closing(sqlite3.connect(database)) as connection:
                self.assertIsNone(connection.execute("SELECT chat_id FROM messages WHERE id = '5'").fetchone()[0])

    def test_v4_revision_becomes_a_runnable_root_in_the_same_issue(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "events.sqlite3"
            migrations = files("evo.adapters.persistence").joinpath("migrations")
            with closing(sqlite3.connect(database)) as connection:
                for filename in ("0001_initial.sql", "0002_issues.sql", "0003_routing.sql", "0004_delegations.sql"):
                    connection.executescript(migrations.joinpath(filename).read_text())
                connection.execute(
                    "INSERT INTO tasks(id, description, status, created_at, updated_at, assignee_type, assignee_id, routing_mode) "
                    "VALUES ('original', 'Report', 'success', 'now', 'now', 'agent', 'builder', 'direct')")
                connection.execute(
                    "INSERT INTO tasks(id, description, parent_id, status, created_at, updated_at) "
                    "VALUES ('revision', 'Improve report', 'original', 'created', 'now', 'now')")
                connection.execute(
                    "INSERT INTO reviews(task_id, status, revision_task_id, created_at, updated_at) "
                    "VALUES ('original', 'revising', 'revision', 'now', 'now')")
                connection.execute(
                    """INSERT INTO events(task_id, event_type, payload_json, created_at)
                    VALUES ('original', 'issue_config',
                    '{"checks":{"s1":{"path":"report.md","exact":"done"}},"memory_enabled":false,"group_context":[]}', 'now')""")
                connection.commit()

            store = SQLiteStore(database)
            self.assertEqual(store.schema_version(), len(MIGRATIONS))
            revision = store.get_task("revision")
            self.assertIsNone(revision["parent_id"])
            self.assertEqual((revision["assignee_id"], revision["routing_mode"]), ("builder", "direct"))
            self.assertEqual(store.issue_id("revision"), "original")
            issue, = store.list_issues()
            self.assertEqual((issue["id"], issue["current_task_id"]), ("original", "revision"))
            self.assertEqual((issue["title"], issue["prompt"]), (None, "Report"))
            settings = {"checks": {"s1": {"path": "report.md", "exact": "done"}}, "memory_enabled": False}
            self.assertEqual(store.latest_event_payload("revision", "issue_config"), settings)
            self.assertEqual(store.latest_event_payload("original", "issue_config"), settings)

    def test_v2_drafts_and_existing_run_migrate_to_current_routes(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "events.sqlite3"
            migrations = files("evo.adapters.persistence").joinpath("migrations")
            with closing(sqlite3.connect(database)) as connection:
                for filename in ("0001_initial.sql", "0002_issues.sql"):
                    connection.executescript(migrations.joinpath(filename).read_text())
                connection.executemany(
                    "INSERT INTO tasks(id, description, status, created_at, updated_at, assignee_type, assignee_id) "
                    "VALUES (?, 'Test issue', ?, 'now', 'now', ?, ?)", (
                        ("squad-draft", "created", "squad", "reports"),
                        ("agent-draft", "created", "agent", "builder"),
                        ("agent-run", "waiting_human", "agent", "builder")))
                connection.execute(
                    "INSERT INTO events(task_id, event_type, payload_json, created_at) "
                    "VALUES ('agent-run', 'run_config', '{\"routing_mode\":\"bidding\"}', 'now')")
                connection.execute(
                    "INSERT INTO events(task_id, event_type, payload_json, created_at) "
                    "VALUES ('agent-run', 'plan_created', '{\"subtasks\":[]}', 'now')")
                connection.commit()

            store = SQLiteStore(database)
            self.assertEqual(store.get_task("squad-draft")["routing_mode"], "squad")
            self.assertEqual(store.get_task("agent-draft")["routing_mode"], "direct")
            self.assertEqual(store.get_task("agent-run")["routing_mode"], "direct")
            self.assertEqual(store.latest_event_payload("agent-run", "run_config"),
                             {"routing_mode": "direct", "direct_assignee": "builder", "squad": None})
            self.assertEqual(store.latest_event_payload("agent-run", "plan_created"),
                             {"goal": "Test issue", "subtasks": []})
            with closing(sqlite3.connect(database)) as connection, self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE tasks SET routing_mode = 'bidding' WHERE id = 'squad-draft'")

    def test_empty_directory_is_migrated_and_reopen_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            database = (
                Path(temp)
                / "nested"
                / "events.sqlite3"
            )

            first = SQLiteStore(database)

            self.assertTrue(database.is_file())
            self.assertEqual(first.schema_version(), len(MIGRATIONS))

            second = SQLiteStore(database)

            self.assertEqual(second.schema_version(), len(MIGRATIONS))

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
                list(enumerate(MIGRATIONS, start=1)),
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
            self.assertEqual(settings["user_version"], len(MIGRATIONS))

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

        self.assertIsNone(child["parent_id"])
        self.assertEqual(self.store.issue_id(child["id"]), "t1")
        self.assertEqual(child["status"], "created")
        self.assertEqual(len(children), 0)

        with self.assertRaises(ValueError):
            service.revise(
                "t1",
                "Create a duplicate revision",
            )

        self.assertEqual(
            len(self.store.list_children("t1")),
            0,
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
                2,
            )
            self.assertEqual(
                researcher.schema_version(),
                2,
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

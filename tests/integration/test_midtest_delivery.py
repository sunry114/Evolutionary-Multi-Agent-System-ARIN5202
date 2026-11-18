import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from evo.interfaces.midtest import prepare_midtest, audit_midtest
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.domain.models import Task, Experience
from evo.domain.statuses import TaskStatus
from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore


def scenario_archive(path, *, extra=None, omit=False):
    with zipfile.ZipFile(path, "w") as z:
        roles = "\n\n".join("## Role (" + name + ")\n\n" + "`" * 3 + "text\nRole prompt " + name + "\n" + "`" * 3
                            for name in ("lead", "dev", "qa", "release"))
        z.writestr("scenario_1_fixture/01_role_initialization.md", roles)
        for i in (1, 2):
            if omit and i == 2:
                continue
            z.writestr("scenario_1_fixture/" + ("02_task_1.md" if i == 1 else "03_task_2.md"),
                       "# Task\n\n" + "`" * 3 + "text\nVerbatim task " + str(i) + "\n" + "`" * 3)
        z.writestr("__MACOSX/._junk", "ignore")
        if extra:
            z.writestr(extra, "unsafe")


class MidtestAndDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / ".evo"
        self.private.mkdir()

    def test_prepare_preserves_roles_tasks_and_shared_state_without_execution(self):
        archive = self.root / "input.zip"
        scenario_archive(archive)
        out = self.private / "prepared"
        report = prepare_midtest(archive, out)
        self.assertEqual(report["status"], "prepared_not_executed")
        state = out / "scenario_1"
        self.assertEqual([r["name"] for r in json.loads((state / "agents.json").read_text())],
                         ["lead", "dev", "qa", "release"])
        self.assertEqual((state / "task_2.txt").read_text().strip(), "Verbatim task 2")
        self.assertFalse((state / "events.sqlite3").exists())
        self.assertEqual(report["scenarios"][0]["status"], "not_run")

    def test_prepare_rejects_overwrite_public_path_and_traversal(self):
        archive = self.root / "input.zip"
        scenario_archive(archive)
        for path in (self.root / "public", self.private):
            with self.assertRaises(ValueError):
                prepare_midtest(archive, path)
        scenario_archive(archive, extra="../escape.md")
        with self.assertRaises(ValueError):
            prepare_midtest(archive, self.private / "bad")
        self.assertFalse((self.private / "bad").exists())

    def test_prepare_rejects_incomplete_scenario_before_writing(self):
        archive = self.root / "input.zip"
        scenario_archive(archive, omit=True)
        with self.assertRaises(ValueError):
            prepare_midtest(archive, self.private / "bad")
        self.assertFalse((self.private / "bad").exists())

    def test_chat_only_export_never_becomes_task_score(self):
        state = self.private / "source"
        app = SQLiteApplication(state)
        from evo.domain.models import Message
        app.store.save_message(Message("human", "group", "human", "Input"))
        app.store.save_message(Message("reply", "group", "builder", "Reply"))
        archive = self.root / "evidence.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.write(state / "events.sqlite3", "export/scenario-1/events.sqlite3")
        report = audit_midtest(archive, self.private / "audit.json")
        entry = report["scenarios"][0]
        self.assertEqual(entry["counts"]["messages"], 2)
        self.assertEqual(entry["counts"]["tasks"], 0)
        self.assertIsNone(entry["task_completion_rate"])
        self.assertIsNone(entry["task_execution_seconds"])
        self.assertEqual(len(entry["group_response_span_seconds"]), 1)

    def test_memory_query_is_scoped_and_empty_query_does_not_create_databases(self):
        app = SQLiteApplication(self.private / "runtime")
        result = app.dispatch("memory", {"agent": "builder"})
        self.assertEqual(result["memory"], [])
        self.assertFalse((app.state / "agents").exists())
        memory = SQLiteMemoryStore(app.state, "builder")
        memory.add(Experience("builder", "meeting report", TaskStatus.SUCCESS, "Verify booking conflicts"))
        memory.add(Experience("builder", "other topic", TaskStatus.FAILED, "Retry carefully"))
        self.assertEqual(len(app.dispatch("memory", {"agent": "builder"})["memory"]), 2)
        result = app.dispatch("memory", {"agent": "builder", "query": "meeting"})
        self.assertEqual(len(result["memory"]), 1)
        self.assertEqual(app.dispatch("memory", {"agent": "reviewer"})["memory"], [])
        for agent in ("../builder", "unknown"):
            with self.assertRaises(ValueError):
                app.dispatch("memory", {"agent": agent})

    def test_memory_limits_and_task_metrics_are_not_official_scores(self):
        app = SQLiteApplication(self.private / "runtime")
        app.store.save_task(Task("root", "Goal", status=TaskStatus.SUCCESS))
        app.store.save_task(Task("child", "Part", parent_id="root", status=TaskStatus.SUCCESS))
        result = app.dispatch("metrics", {"task_id": "root"})
        self.assertEqual(result["completed_subtasks"], 1)
        self.assertEqual(result["subtask_completion_rate"], 1)
        self.assertEqual(result["basis"], "stored_task_state")
        self.assertTrue(result["timing_final"])
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertNotIn("official_score", result)
        with self.assertRaises(KeyError):
            app.dispatch("metrics", {"task_id": "unknown"})

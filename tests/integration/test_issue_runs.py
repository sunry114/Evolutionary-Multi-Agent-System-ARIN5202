import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.workbench import Workbench
from evo.ports.harness import HarnessError, RunResult


class IssueRunsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = directory.name
        self.app = Workbench(SQLiteApplication(self.state))

    def create(self, **fields):
        return self.app.dispatch("create", {"prompt": "Check the log", **fields})

    def status(self, issue_id, status):
        return self.app.dispatch("issue-status", {"task_id": issue_id, "status": status})

    def test_backlog_parks_assignment_and_moving_to_todo_starts_it_once(self):
        issue_id = self.create()["task"]["id"]
        self.status(issue_id, "backlog")
        result = self.app.dispatch("assign", {"task_id": issue_id, "squad": "default"})
        self.assertIsNone(result["run_id"])
        self.assertEqual(self.app.queue.list(issue_id), [])
        queued = self.status(issue_id, "todo")["run_id"]
        self.assertIsNone(self.status(issue_id, "todo")["run_id"])
        run, = self.app.queue.list(issue_id)
        self.assertEqual((run["id"], run["agent"], run["status"]), (queued, "researcher", "queued"))

    def test_renamed_squad_keeps_existing_assignments_and_runs(self):
        created = self.create(squad="default")
        issue_id = created["task"]["id"]
        registry = self.app.application.registry.payload()
        registry["squad"]["name"] = "小队1"
        self.app.dispatch("configure", registry)

        app = Workbench(SQLiteApplication(self.state))
        detail = app.dispatch("task", {"task_id": issue_id})
        self.assertEqual(detail["current"]["assignee_id"], "小队1")
        self.assertEqual(app.snapshot()["tasks"][0]["assignee_id"], "小队1")
        matching = app.dispatch("tasks", {"owner": "小队1", "owner_type": "squad"})["tasks"]
        self.assertEqual([issue["id"] for issue in matching], [issue_id])
        payload = {"task_id": issue_id}
        self.assertIsNone(app.dispatch("assign", {**payload, "squad": "小队1"})["run_id"])
        self.assertEqual(app.dispatch("start", payload)["run_id"], created["run_id"])
        run = app.queue.claim()
        self.assertEqual(run["id"], created["run_id"])
        assignee = app._assignee(app.store.get_task(issue_id))
        self.assertEqual(assignee.name, "researcher")
        app.queue.finish(run, "completed", "Done", assignee=assignee)
        self.assertEqual(app.dispatch("task", payload)["lane"], "in_review")
        new_issue = app.dispatch("create", {"prompt": "New issue", "squad": "小队1"})
        self.assertEqual(new_issue["task"]["assignee_id"], "小队1")
        self.assertTrue(new_issue["run_id"])
        with self.assertRaises(ValueError):
            app.dispatch("create", {"prompt": "Wrong squad", "squad": "unknown"})

    def test_restart_keeps_queued_run_and_does_not_replay_unconfirmed_execution(self):
        first = self.create(agent="builder")
        second = self.create(agent="reviewer")
        self.assertEqual(self.app.queue.claim()["id"], first["run_id"])
        reopened = Workbench(SQLiteApplication(self.state))
        reopened.queue.recover_interrupted()
        interrupted, = reopened.queue.list(first["task"]["id"])
        queued, = reopened.queue.list(second["task"]["id"])
        self.assertEqual((interrupted["status"], queued["status"]), ("interrupted", "queued"))
        self.assertEqual(reopened.dispatch("task", {"task_id": first["task"]["id"]})["lane"], "blocked")

    def test_review_request_waits_for_successful_run_completion(self):
        created = self.create(squad="default")
        task_id = created["task"]["id"]
        payload = {"task_id": task_id}
        assignee = self.app._assignee(self.app.store.get_task(task_id))
        first = self.app.queue.claim()
        expected = {"issue_status": "in_progress", "requested_status": "in_review", "deferred": True}
        self.assertEqual(self.app.queue.set_run_status(first, "in_review"), expected)
        self.assertEqual(self.app.dispatch("task", payload)["lane"], "in_progress")
        self.app.queue.finish(first, "failed", "Artifact export failed", assignee=assignee)
        failed = self.app.dispatch("task", payload)
        self.assertEqual(failed["lane"], "blocked")
        self.assertFalse(any(event["event_type"] == "issue_status_changed" and event["payload"]["to"] == "in_review"
                             for event in failed["logs"]))
        retry_id = self.app.dispatch("retry-run", {**payload, "run_id": first["id"]})["run_id"]
        retry = self.app.queue.claim()
        self.assertEqual(retry["id"], retry_id)
        self.assertEqual(self.app.queue.set_run_status(retry, "in_review"), expected)
        self.app.queue.finish(retry, "completed", "Artifact archived", assignee=assignee)
        self.assertEqual(self.app.dispatch("task", payload)["lane"], "in_review")

    def test_progress_monitor_error_cancels_harness_before_blocking_issue(self):
        created = self.create(agent="builder")
        task_id = created["task"]["id"]
        run = self.app.queue.claim()
        harness = Mock()
        harness.wait.return_value = None
        harness.events.side_effect = HarnessError("Harness checkpoint unavailable")

        def cancel(handle):
            self.assertEqual(self.app.queue.list(task_id)[0]["status"], "running")
            return RunResult("cancelled")

        harness.cancel.side_effect = cancel
        workspace = Mock(workspace=Path(self.state))
        with patch("evo.interfaces.execution_factory.create_issue_harness", return_value=(workspace, harness)):
            self.app._execute(run)
        harness.cancel.assert_called_once_with(harness.start.return_value)
        detail = self.app.dispatch("task", {"task_id": task_id})
        self.assertEqual(detail["lane"], "blocked")
        self.assertEqual(detail["executions"][0]["result"], "Execution failed: Harness checkpoint unavailable")

    def test_comment_filters_unknown_agents_and_email_and_retries_a_stopped_run_once(self):
        issue_id = self.create()["task"]["id"]
        result = self.app.dispatch("comment", {"task_id": issue_id, "message":
                                  "通知 x@reviewer，另外请@builder 核对，@builder @unknown。"})
        run_id, = result["run_ids"]
        self.assertEqual(self.app.queue.stop(issue_id, run_id), "stopped")
        retry_id = self.app.queue.retry(issue_id, run_id)
        self.assertEqual(self.app.queue.retry(issue_id, run_id), retry_id)
        retry, original = self.app.queue.list(issue_id)
        self.assertEqual((retry["agent"], retry["comment_id"], retry["retry_of"], original["status"]),
                         ("builder", result["message_id"], run_id, "stopped"))

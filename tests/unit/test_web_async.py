"""Board execution returns promptly and disconnected clients do not crash handlers."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.gateway import ApplicationFailure
from evo.interfaces.web.app import make_server


class WebAsyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = SQLiteApplication(Path(temp.name))

    def test_start_returns_while_task_runs_and_rejects_duplicate(self):
        self.app.store.save_task(Task("draft", "Build", status=TaskStatus.CREATED))
        entered, release = threading.Event(), threading.Event()

        def run(_payload):
            entered.set()
            release.wait(3)
            return {"status": "waiting_human"}

        payload = {"task_id": "draft", "prompt": "Build", "final_check": {"path": "result.md"}}
        with patch.object(self.app, "_coordinator", return_value=object()), \
                patch.object(self.app, "run_task", side_effect=run):
            result = self.app.dispatch("start", payload)
            self.assertEqual(result, {"status": "started", "task_id": "draft"})
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.app.dispatch("start", payload)
            release.set()

    def test_continue_returns_while_resume_runs(self):
        self.app.store.save_task(Task("paused", "Build", owner="builder",
                                      status=TaskStatus.WAITING_HUMAN))
        entered, release = threading.Event(), threading.Event()

        def resume(_task_id):
            entered.set()
            release.wait(3)
            return {"status": "success"}

        with patch.object(self.app, "resume_task", side_effect=resume):
            self.assertEqual(self.app.dispatch("continue", {"task_id": "paused"})["status"], "started")
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ValueError):
                self.app.dispatch("continue", {"task_id": "paused"})
            release.set()

    def test_start_does_not_wait_for_inflight_group_model(self):
        self.app.store.save_task(Task("draft", "Build"))
        entered, release = threading.Event(), threading.Event()

        def slow_reply(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release provider")
            return {"reply": "Ready"}

        self.app._provider = Mock(complete=slow_reply)
        payload = {"task_id": "draft", "prompt": "Build", "final_check": {"path": "report.md"}}
        with patch.object(self.app, "_coordinator", return_value=object()), \
                patch.object(self.app, "run_task", return_value={"status": "waiting_human"}), \
                ThreadPoolExecutor(max_workers=2) as pool:
            discussion = pool.submit(self.app.dispatch, "group", {"message": "Discuss", "task_id": "draft"})
            try:
                self.assertTrue(entered.wait(1))
                start = pool.submit(self.app.dispatch, "start", payload)
                self.assertEqual(start.result(timeout=1), {"status": "started", "task_id": "draft"})
                self.assertFalse(discussion.done())
            finally:
                release.set()
            self.assertEqual(discussion.result(timeout=2)["status"], "success")

    def test_termination_does_not_wait_or_allow_late_chat_messages(self):
        self.app.store.save_task(Task("draft", "Build"))
        entered, release = threading.Event(), threading.Event()

        def slow_reply(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release provider")
            return {"reply": "Late reply @builder"}

        provider = Mock()
        provider.complete.side_effect = slow_reply
        self.app._provider = provider
        with ThreadPoolExecutor(max_workers=2) as pool:
            discussion = pool.submit(self.app.dispatch, "group", {"message": "Discuss", "task_id": "draft"})
            try:
                self.assertTrue(entered.wait(1))
                termination = pool.submit(self.app.dispatch, "terminate", {"task_id": "draft"})
                self.assertEqual(termination.result(timeout=1)["status"], "blocked")
                self.assertFalse(discussion.done())
                self.assertEqual(self.app.messages(task_id="draft"), [])
            finally:
                release.set()
            with self.assertRaises(ApplicationFailure):
                discussion.result(timeout=2)
        self.assertEqual(self.app.messages(task_id="draft"), [])
        self.assertEqual(provider.complete.call_count, 1)

    def test_continue_does_not_wait_for_inflight_chat(self):
        self.app.store.save_task(Task("paused", "Build", status=TaskStatus.WAITING_HUMAN))
        entered, release = threading.Event(), threading.Event()

        def slow_reply(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test did not release provider")
            return {"reply": "Ready"}

        self.app._provider = Mock(complete=slow_reply)
        with patch.object(self.app, "resume_task", return_value={"status": "waiting_human"}), \
                ThreadPoolExecutor(max_workers=2) as pool:
            discussion = pool.submit(self.app.dispatch, "dm", {
                "message": "Discuss", "agent": "builder", "task_id": "paused"})
            try:
                self.assertTrue(entered.wait(1))
                resume = pool.submit(self.app.dispatch, "continue", {"task_id": "paused"})
                self.assertEqual(resume.result(timeout=1)["status"], "started")
                self.assertFalse(discussion.done())
            finally:
                release.set()
            self.assertEqual(discussion.result(timeout=2)["status"], "success")

    def test_reply_handles_disconnect_during_headers_and_body(self):
        class FakeServer:
            def __init__(self, address, handler):
                self.RequestHandlerClass = handler
                self.server_port = address[1]

        with patch("evo.interfaces.web.app.ThreadingHTTPServer", FakeServer):
            server = make_server(self.app, 8768)
        handler = object.__new__(server.RequestHandlerClass)
        handler.send_response = lambda _status: None
        handler.send_header = lambda _name, _value: None
        handler.end_headers = Mock(side_effect=BrokenPipeError())
        handler.wfile = Mock()
        handler.reply(200, {"status": "started"})
        handler.end_headers = Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler.reply(200, {"status": "started"})

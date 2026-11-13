import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.cli import main
from evo.interfaces.gateway import ApplicationUnavailable, load_application
from evo.interfaces.sqlite_application import SQLiteApplication


class SQLiteInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.app = SQLiteApplication(self.state)
        for name in ("t1", "t2"):
            self.app.store.save_task(Task(name, "Stored result", owner="builder",
                                         status=TaskStatus.SUCCESS, result="report.md"))

    def command(self, *args):
        env = dict(os.environ, EVO_APPLICATION_FACTORY="", PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, "-m", "evo", "--state", str(self.state), *args],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=15)

    def test_default_loader_and_factory_override(self):
        with patch.dict(os.environ, {"EVO_APPLICATION_FACTORY": ""}):
            self.assertIsInstance(load_application(self.state), SQLiteApplication)
            self.assertEqual(load_application(self.state, demo=True).snapshot()["tasks"], [])
        with patch.dict(os.environ, {"EVO_APPLICATION_FACTORY": "evo.interfaces.sqlite_application:create_application"}):
            self.assertEqual(len(load_application(self.state).snapshot()["tasks"]), 2)

    def test_cli_review_trace_and_revision_across_processes(self):
        accepted = self.command("review", "t1", "accepted")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["review_status"], "accepted")
        requested = self.command("review", "t2", "changes_requested", "--feedback", "fix output")
        self.assertEqual(requested.returncode, 0, requested.stderr)
        revised = self.command("revision", "t2", "Apply feedback")
        self.assertEqual(revised.returncode, 0, revised.stderr)
        child = json.loads(revised.stdout)
        self.assertEqual(child["status"], "created")
        query = self.command("task", "t2")
        self.assertEqual(query.returncode, 0, query.stderr)
        restored = json.loads(query.stdout)
        self.assertEqual(restored["task"]["status"], "success")
        self.assertEqual(restored["task"]["revision_task_id"], child["id"])
        self.assertEqual(restored["children"][0]["id"], child["id"])
        trace = self.command("trace", "t2")
        self.assertEqual(trace.returncode, 0, trace.stderr)
        self.assertEqual([e["event_type"] for e in json.loads(trace.stdout)["events"]],
                         ["task_created", "review_decided", "revision_linked"])

    def test_duplicate_operations_leave_no_extra_events_or_children(self):
        self.app.dispatch("review", {"task_id": "t1", "decision": "changes_requested", "feedback": "fix"})
        with self.assertRaises(ValueError):
            self.app.dispatch("review", {"task_id": "t1", "decision": "accepted"})
        self.app.dispatch("revision", {"task_id": "t1", "prompt": "fix"})
        with self.assertRaises(ValueError):
            SQLiteApplication(self.state).dispatch("revision", {"task_id": "t1", "prompt": "duplicate"})
        self.assertEqual(len(self.app.store.list_children("t1")), 1)
        self.assertEqual(len(self.app.store.list_events("t1")), 3)

    def test_concurrent_revisions_create_only_one_child(self):
        self.app.dispatch("review", {"task_id": "t1", "decision": "changes_requested", "feedback": "fix"})
        def revise(_):
            try:
                return SQLiteApplication(self.state).dispatch("revision", {"task_id": "t1", "prompt": "fix"})["id"]
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.app.store.list_children("t1")), 1)

    def test_invalid_input_and_unknown_task_cannot_write(self):
        for op, payload in (
            ("review", {"task_id": "t1", "decision": "changes_requested", "feedback": " "}),
            ("review", {"task_id": "t1", "decision": "accepted", "feedback": {}}),
            ("review", {"task_id": [], "decision": "accepted"}),
            ("revision", {"task_id": "t1", "prompt": "fix"}),
            ("tasks", {"limit": True}), ("tasks", {"limit": 501}),
            ("tasks", {"limit": 0}), ("task", {"task_id": "unknown"}),
            ("trace", {"task_id": "unknown"}),
        ):
            with self.subTest(op=op, payload=payload), self.assertRaises((ValueError, KeyError)):
                self.app.dispatch(op, payload)
        self.assertEqual(self.app.store.get_task("t1")["review_status"], "pending")
        self.assertEqual(len(self.app.store.list_events("t1")), 1)

    def test_unavailable_backends_have_no_side_effects(self):
        before = self.app.snapshot()
        for op in ("run", "dm", "group", "answer", "eval", "metrics", "memory"):
            with self.subTest(op=op), self.assertRaises(ApplicationUnavailable):
                self.app.dispatch(op, {"prompt": "x"})
        self.assertEqual(self.app.snapshot(), before)

    def test_snapshot_real_records_and_readonly_requests(self):
        self.app.store.save_approval_request(request_id="r1", task_id="t1", agent="builder",
                                             kind="approval", payload={"tool": "run_python"})
        snapshot = SQLiteApplication(self.state).snapshot()
        self.assertEqual(len(snapshot["tasks"]), 2)
        self.assertEqual(snapshot["requests"][0]["id"], "r1")
        self.assertEqual(snapshot["requests"][0]["payload"], {"tool": "run_python"})
        self.assertFalse(snapshot["capabilities"]["answer"])
        self.assertFalse(snapshot["capabilities"]["run"])

    def test_task_limit_and_failed_task_query_exit(self):
        self.app.store.save_task(Task("failed", "Failed task", status=TaskStatus.FAILED))
        limited = self.app.dispatch("tasks", {"limit": 1})["tasks"]
        self.assertEqual(len(limited), 1)
        with patch.dict(os.environ, {"EVO_APPLICATION_FACTORY": ""}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--state", str(self.state), "task", "failed"]), 0)
        self.assertEqual(len(self.app.store.list_tasks()), 3)

    def start_web(self):
        env = dict(os.environ, EVO_APPLICATION_FACTORY="", PYTHONIOENCODING="utf-8")
        child = subprocess.Popen(
            [sys.executable, "-m", "evo", "--state", str(self.state), "web", "--port", "0"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(self.stop_web, child)
        line = child.stdout.readline().strip()
        self.assertTrue(line.startswith("Open http://127.0.0.1:"), line)
        return child, urlsplit(line.removeprefix("Open "))

    @staticmethod
    def stop_web(child):
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()

    def http(self, address, path, payload=None, token=None):
        connection = HTTPConnection(address.hostname, address.port, timeout=5)
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers = {"Origin": "http://" + address.netloc, "X-CSRF-Token": token or "",
                       "Content-Type": "application/json"}
        try:
            connection.request("POST" if payload is not None else "GET", path, body, headers)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def token(self, address):
        status, html = self.http(address, "/")
        self.assertEqual(status, 200)
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def test_http_review_revision_and_trace_survive_server_process_restart(self):
        child, address = self.start_web()
        token = self.token(address)
        self.assertEqual(self.http(address, "/api/review",
            {"task_id": "t1", "decision": "changes_requested", "feedback": "fix"}, token)[0], 200)
        status, result = self.http(address, "/api/revision", {"task_id": "t1", "prompt": "fix"}, token)
        self.assertEqual(status, 200)
        child_id = json.loads(result)["id"]
        self.stop_web(child)
        _, new_address = self.start_web()
        new_token = self.token(new_address)
        self.assertNotEqual(token, new_token)
        status, state = self.http(new_address, "/api/state")
        self.assertEqual(status, 200)
        parent = next(t for t in json.loads(state)["tasks"] if t["id"] == "t1")
        self.assertEqual(parent["revision_task_id"], child_id)
        self.assertEqual(parent["status"], "success")
        status, trace = self.http(new_address, "/api/trace", {"task_id": "t1"}, new_token)
        self.assertEqual(status, 200)
        self.assertIn("revision_linked", trace)
        self.assertEqual(self.http(new_address, "/api/review",
            {"task_id": "t2", "decision": "accepted"}, token)[0], 403)
        self.assertEqual(self.http(new_address, "/api/revision",
            {"task_id": "t1", "prompt": "duplicate"}, new_token)[0], 409)
        self.assertEqual(len(self.app.store.list_children("t1")), 1)

    def test_http_validation_and_unavailable_operations(self):
        _, address = self.start_web()
        token = self.token(address)
        for op, payload, expected in (
            ("review", {"task_id": "missing", "decision": "accepted"}, 409),
            ("review", {"task_id": "t1", "decision": "accepted", "feedback": []}, 409),
            ("revision", {"task_id": "t1", "prompt": " "}, 409),
            ("run", {"prompt": "x"}, 503), ("answer", {"request_id": "r1", "approved": True}, 503),
        ):
            with self.subTest(op=op):
                self.assertEqual(self.http(address, "/api/" + op, payload, token)[0], expected)
        self.assertEqual(self.app.store.get_task("t1")["review_status"], "pending")
        self.assertEqual(len(self.app.store.list_tasks()), 2)

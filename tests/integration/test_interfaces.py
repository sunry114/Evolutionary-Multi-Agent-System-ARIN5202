import contextlib
import io
import json
import os
import threading
import tempfile
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from evo.interfaces.cli import main
from evo.interfaces.demo import DemoApplication
from evo.interfaces.web.app import make_server
from evo.application.reviews import ReviewService


class CliTests(unittest.TestCase):
    def test_demo_cannot_claim_task_success(self):
        app = DemoApplication()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["create", "write hello.md", "--squad", "default"], app), 0)
        issue_id = json.loads(output.getvalue())["task"]["id"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["start", issue_id], app), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")

    def test_cli_can_queue_before_the_worker_is_running(self):
        with tempfile.TemporaryDirectory() as state, \
                patch.dict(os.environ, {"EVO_APPLICATION_FACTORY": ""}), \
                patch("evo.interfaces.execution_factory.load_key", side_effect=ValueError("missing credential")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--state", state, "create", "hello", "--agent", "builder"]), 0)
            issue = json.loads(output.getvalue())
            issue_id = issue["task"]["id"]
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--state", state, "start", issue_id]), 0)
            self.assertEqual(json.loads(output.getvalue())["run_id"], issue["run_id"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["--state", state, "task", issue_id]), 0)
            detail = json.loads(output.getvalue())
            self.assertEqual(detail["task"]["status"], "created")
            self.assertEqual(detail["executions"][0]["status"], "queued")

    def test_commands_map_to_backend(self):
        class App:
            def dispatch(self, operation, payload):
                self.call = operation, payload
                return {"status": "success"}
        app = App()
        commands = [
            (["create", "Python report", "--agent", "builder", "--no-memory"], "create",
             {"prompt": "Python report", "agent": "builder", "no_memory": True, "from_group": False}),
            (["start", "t1"], "start", {"task_id": "t1"}),
            (["dm", "builder", "hello"], "dm", {"agent": "builder", "message": "hello"}),
            (["answer", "t1", "r1", "--deny"], "answer", {"task_id": "t1", "request_id": "r1", "approved": False}),
            (["revision", "t1", "fix"], "revision", {"task_id": "t1", "prompt": "fix"}),
            (["eval", "input.jsonl", "--output", "report.json", "--no-memory"], "eval",
             {"dataset": "input.jsonl", "output": "report.json", "compare_memory": False, "no_memory": True}),
        ]
        for argv, operation, payload in commands:
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv, app), 0)
                self.assertEqual(app.call, (operation, payload))

    def test_provider_failure_does_not_leak(self):
        class App:
            def dispatch(self, *args):
                raise RuntimeError("private-key")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["start", "t1"], App()), 2)
        self.assertNotIn("private-key", output.getvalue())


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = DemoApplication()
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        status, html = self.request("GET", "/")
        import re
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, body=None, extra=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": self.server.authority}
        headers.update(extra or {})
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            conn.close()

    def write_headers(self):
        return {"Origin": "http://" + self.server.authority,
                "X-CSRF-Token": self.token, "Content-Type": "application/json"}

    def test_csrf_origin_host_rejected_without_side_effect(self):
        for headers in ({}, {"Host": "evil.test"}, {"Origin": "https://evil.test"},
                        {"X-CSRF-Token": "wrong"}):
            valid = self.write_headers()
            valid.update(headers)
            if not headers:
                valid.pop("X-CSRF-Token")
            status, _ = self.request("POST", "/api/create", '{"prompt":"x","squad":"default"}', valid)
            self.assertEqual(status, 403)
        self.assertEqual(self.app.snapshot()["tasks"], [])

    def test_json_and_routes(self):
        headers = self.write_headers()
        status, result = self.request("POST", "/api/create", '{"prompt":"hello","squad":"default"}', headers)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.app.snapshot()["tasks"]), 1)
        issue_id = json.loads(result)["task"]["id"]
        status, result = self.request("POST", "/api/start", json.dumps({"task_id": issue_id}), headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(result)["task"]["id"], issue_id)
        for body in ("[]", "{broken"):
            self.assertEqual(self.request("POST", "/api/create", body, headers)[0], 400)
        headers["Content-Type"] = "text/plain"
        self.assertEqual(self.request("POST", "/api/create", "x", headers)[0], 415)
        self.assertEqual(self.request("GET", "/missing")[0], 404)

    def test_dns_rebinding_host_and_secret_redaction(self):
        self.assertEqual(self.request("GET", "/", extra={"Host": "evil.test"})[0], 403)
        self.app.data["api_key"] = "hidden"
        self.app.data["messages"] = ["example-secret"]
        with patch.dict(os.environ, {"HKGAI_API_KEY": "example-secret"}):
            status, content = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn("example-secret", content)
        self.assertNotIn("hidden", content)

    def test_page_escapes_untrusted_content_at_render_time(self):
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("协作工作台", page)


class AtomicReviewRepository:
    """Contract fixture only; production implementation belongs to B."""
    def __init__(self):
        self.task = {"id": "t1", "status": "success", "review_status": "pending"}
        self.revisions = []

    def get_task(self, task_id):
        return dict(self.task)

    def decide_review(self, task_id, *, expected, decision, feedback):
        if self.task["review_status"] != expected:
            raise ValueError("Conflict")
        self.task.update(review_status=decision, feedback=feedback)
        return dict(self.task)

    def link_revision(self, task_id, *, expected, prompt):
        if self.task["review_status"] != expected:
            raise ValueError("Conflict")
        child = {"id": "t2", "parent_id": None, "prompt": prompt, "status": "created"}
        self.revisions.append(child)
        self.task.update(review_status="revising", revision_task_id="t2")
        return child


class ReviewTests(unittest.TestCase):
    def test_review_revision_and_service_restart_contract(self):
        repo = AtomicReviewRepository()
        ReviewService(repo).decide("t1", "changes_requested", "fix output")
        service = ReviewService(repo)
        child = service.revise("t1", "fix output")
        self.assertIsNone(child["parent_id"])
        self.assertEqual(repo.task["status"], "success")
        with self.assertRaises(ValueError):
            service.revise("t1", "duplicate")
        self.assertEqual(len(repo.revisions), 1)

    def test_invalid_and_duplicate_decisions(self):
        repo = AtomicReviewRepository()
        service = ReviewService(repo)
        with self.assertRaises(ValueError):
            service.decide("t1", "changes_requested")
        service.decide("t1", "accepted")
        with self.assertRaises(ValueError):
            service.decide("t1", "accepted")
        repo.task.update(status="failed", review_status="pending")
        with self.assertRaises(ValueError):
            service.decide("t1", "accepted")

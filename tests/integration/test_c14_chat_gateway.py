import contextlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import contextmanager
from http.client import HTTPConnection
import threading
import re
from unittest.mock import patch

from evo.domain.models import Message, Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.cli import main
from evo.interfaces.gateway import ApplicationFailure, ApplicationUnavailable
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.web.app import make_server


class ScriptedProvider:
    def __init__(self):
        self.calls = []
        self.fail = False

    def complete(self, messages, json_object=False):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("private-provider-secret")
        return {"reply": "Persisted agent reply"}


class C14GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.provider = ScriptedProvider()
        self.app = SQLiteApplication(self.state, provider=self.provider)

    @contextmanager
    def http_service(self):
        server = make_server(self.app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, payload=None, csrf=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                headers = {"Origin": "http://" + server.authority, "Content-Type": "application/json",
                           "X-CSRF-Token": csrf or ""}
                conn.request("POST" if payload is not None else "GET", path,
                             json.dumps(payload) if payload is not None else None, headers)
                result = conn.getresponse()
                return result.status, result.read().decode("utf-8")
            finally:
                conn.close()
        try:
            token = re.search(r'name="csrf-token" content="([^"]+)"', request("/")[1]).group(1)
            yield request, token
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_http_chat_csrf_validation_history_and_safe_provider_failure(self):
        with self.http_service() as (request, token):
            payload = {"agent": "builder", "message": "HTTP private input"}
            self.assertEqual(request("/api/dm", payload)[0], 403)
            self.assertEqual(self.app.messages(), [])
            self.assertEqual(request("/api/dm", {**payload, "agent": "unknown"}, token)[0], 409)
            self.assertEqual(request("/api/dm", payload, token)[0], 200)
            status, history = request("/api/messages", {"agent": "builder"}, token)
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(history)["messages"]), 2)
            self.provider.fail = True
            status, result = request("/api/group", {"message": "Group request"}, token)
            self.assertEqual(status, 502)
            self.assertNotIn("private-provider-secret", result)

    def test_http_missing_key_returns_503_without_saving_message(self):
        self.app._provider = None
        with self.http_service() as (request, token), patch(
                "evo.adapters.llm.credentials.load_key", side_effect=ValueError("secret")):
            status, result = request("/api/group", {"message": "hello"}, token)
            self.assertEqual(status, 503)
            self.assertIn("setup-key", result)
            self.assertNotIn("secret", result)
        self.assertEqual(self.app.messages(), [])

    def test_dm_cli_and_messages_are_persisted_and_private(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["dm", "builder", "Private build request"], self.app), 0)
        self.assertEqual(json.loads(output.getvalue())["reply"], "Persisted agent reply")
        self.app.dispatch("dm", {"agent": "reviewer", "message": "Private review request"})
        history = SQLiteApplication(self.state).dispatch("messages", {"agent": "builder"})["messages"]
        self.assertEqual([m["content"] for m in history], ["Private build request", "Persisted agent reply"])
        self.assertNotIn("Private build request", self.provider.calls[-1][1]["content"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["messages", "--agent", "reviewer"], self.app), 0)
        self.assertNotIn("Private build request", output.getvalue())

    def test_group_has_three_real_service_replies_and_excludes_dm_and_task_logs(self):
        self.app.dispatch("dm", {"agent": "builder", "message": "Private input"})
        self.app.store.save_task(Task("task", "Fixture"))
        self.app.store.save_message(Message("bid", "group", "planner", "private task bid", task_id="task"))
        response = self.app.dispatch("group", {"message": "Discuss the plan"})
        self.assertEqual(set(response["replies"]), {"researcher", "builder", "reviewer"})
        group_calls = self.provider.calls[-3:]
        for call in group_calls:
            self.assertNotIn("Private input", call[1]["content"])
            self.assertNotIn("private task bid", call[1]["content"])
        snapshot = SQLiteApplication(self.state).snapshot()
        self.assertEqual(sum(m["kind"] == "group" for m in snapshot["messages"]), 4)
        self.assertNotIn("private task bid", json.dumps(snapshot))

    def test_invalid_chat_never_calls_provider_or_saves(self):
        for op, payload in (
            ("dm", {"agent": "unknown", "message": "hello"}),
            ("dm", {"agent": "builder", "message": " "}),
            ("group", {"message": []}), ("group", {"message": "x" * 8001}),
            ("messages", {"agent": "unknown"}),
        ):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.app.dispatch(op, payload)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.app.snapshot()["messages"], [])

    def test_created_task_and_imported_roles_feed_the_group_chat(self):
        roles = [{"name": "analyst", "specialty": "Analyze the task"},
                 {"name": "writer", "specialty": "Write the result"}]
        created = self.app.dispatch("create", {"prompt": "Prepare an outline", "agents": roles})
        task_id = created["task_id"]
        self.assertEqual(self.app.store.get_task(task_id)["status"], "created")
        self.assertEqual([item["name"] for item in SQLiteApplication(self.state).snapshot()["agent_details"]],
                         ["analyst", "writer"])
        discussion = self.app.dispatch("group", {"message": "Prepare an outline"})
        self.assertEqual(set(discussion["replies"]), {"analyst", "writer"})
        self.assertEqual([m["sender"] for m in self.app.snapshot()["messages"]],
                         ["human", "analyst", "writer"])
        for payload in ({"prompt": " "}, {"prompt": "x" * 8001},
                        {"prompt": "Another task", "agents": [{"name": "bad name", "specialty": "x"}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.app.dispatch("create", payload)
        self.assertEqual(len(self.app.snapshot()["tasks"]), 1)

    def test_termination_clears_only_its_messages_and_pending_questions(self):
        first = self.app.dispatch("create", {"prompt": "First"})["task_id"]
        second = self.app.dispatch("create", {"prompt": "Second"})["task_id"]
        self.app.dispatch("group", {"message": "First", "task_id": first})
        self.app.dispatch("group", {"message": "Second", "task_id": second})
        self.app.dispatch("dm", {"agent": "builder", "message": "First private", "task_id": first})
        self.app.dispatch("dm", {"agent": "builder", "message": "Second private", "task_id": second})
        self.assertNotIn("First private", self.provider.calls[-1][1]["content"])
        self.app.dispatch("dm", {"agent": "builder", "message": "Independent private"})
        self.assertNotIn("Second private", self.provider.calls[-1][1]["content"])
        self.app.approvals.request_clarification(first, "Which file?")
        self.assertEqual(len(self.app.snapshot()["messages"]), 14)
        result = self.app.dispatch("terminate", {"task_id": first})
        self.assertEqual(result["cleared_messages"], 6)
        self.assertEqual(self.app.store.get_task(first)["status"], "blocked")
        remaining = self.app.snapshot()["messages"]
        self.assertEqual(len(remaining), 8)
        self.assertTrue(all(message["task_id"] != first for message in remaining))
        self.assertTrue(all(message["created_at"] for message in remaining))
        self.assertIn("Second private", [message["content"] for message in remaining])
        self.assertIn("Independent private", [message["content"] for message in remaining])
        self.assertEqual(self.app.snapshot()["requests"], [])
        with self.assertRaises(ValueError):
            self.app.dispatch("terminate", {"task_id": first})
        with self.assertRaises(ValueError):
            self.app.dispatch("group", {"message": "Again", "task_id": first})

    def test_clear_finished_board_preserves_trace_and_future_tasks(self):
        finished = self.app.dispatch("create", {"prompt": "Finished"})["task_id"]
        active = self.app.dispatch("create", {"prompt": "Active"})["task_id"]
        self.app.dispatch("terminate", {"task_id": finished})
        self.assertEqual(self.app.dispatch("clear_finished", {})["cleared_tasks"], 1)
        self.assertEqual([task["id"] for task in SQLiteApplication(self.state).snapshot()["tasks"]],
                         [active])
        self.assertEqual(self.app.store.get_task(finished)["status"], "blocked")
        self.assertTrue(self.app.store.list_events(finished))
        self.assertEqual(self.app.dispatch("clear_finished", {})["cleared_tasks"], 0)
        future = self.app.dispatch("create", {"prompt": "Future"})["task_id"]
        self.assertEqual({task["id"] for task in self.app.snapshot()["tasks"]}, {active, future})
        with self.assertRaises(ValueError):
            self.app.dispatch("clear_finished", {"unexpected": True})

    def test_missing_key_is_actionable_and_does_not_save_incoming_message(self):
        app = SQLiteApplication(self.state)
        with patch("evo.adapters.llm.credentials.load_key", side_effect=ValueError("secret")):
            with self.assertRaises(ApplicationUnavailable) as error:
                app.dispatch("dm", {"agent": "builder", "message": "hello"})
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(app.snapshot()["messages"], [])

    def test_provider_failure_safe_and_saved_human_message_visible_after_restart(self):
        self.provider.fail = True
        with self.assertRaises(ApplicationFailure) as error:
            self.app.dispatch("dm", {"agent": "builder", "message": "Keep this input"})
        self.assertNotIn("private-provider-secret", str(error.exception))
        history = SQLiteApplication(self.state).messages(agent="builder")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["sender"], "human")
        with contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(["dm", "builder", "Try another"], self.app), 1)
        self.assertNotIn("private-provider-secret", output.getvalue())

    def test_concurrent_chat_sequence_has_complete_pairs(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n: self.app.dispatch("dm", {"agent": "builder", "message": str(n)}), range(2)))
        history = self.app.messages(agent="builder")
        self.assertEqual([m["sender"] for m in history], ["human", "builder", "human", "builder"])

    def test_clarification_duplicate_denial_and_expiration(self):
        store = self.app.store
        for name in ("clarify", "deny", "approve", "stale"):
            store.save_task(Task(name, name, owner="builder", status=TaskStatus.RUNNING))
        request = self.app.approvals.request_clarification("clarify", "Which file?")
        with self.assertRaises(ValueError):
            self.app.dispatch("answer", {"request_id": request, "answer": ""})
        answer = self.app.dispatch("answer", {"request_id": request, "answer": "report.md"})
        self.assertEqual(answer["status"], "answered")
        with self.assertRaises(ValueError):
            self.app.dispatch("answer", {"request_id": request, "answer": "duplicate"})
        for task, approved in (("deny", False), ("approve", True), ("stale", True)):
            req = self.app.approvals.request_approval(task, "builder", "delete_file", {"fixture": True})
            if task == "stale":
                with store._transaction() as conn:
                    conn.execute("UPDATE approval_requests SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (req,))
            result = self.app.dispatch("answer", {"request_id": req, "approved": approved})
            self.assertEqual(result["status"], {"deny": "denied", "approve": "approved", "stale": "expired"}[task])
            self.assertEqual(store.get_action(req)["status"], "requested" if task == "approve" else "blocked")
        restored = SQLiteApplication(self.state)
        self.assertEqual(restored.store.get_approval_request(request)["answer"], {"answer": "report.md"})
        self.assertEqual(restored.store.get_task("approve")["status"], "waiting_human")

    def test_capabilities_do_not_claim_unimplemented_execution_or_evaluation(self):
        snapshot = self.app.snapshot()
        for operation in ("dm", "group", "messages", "answer", "review"):
            self.assertTrue(snapshot["capabilities"][operation])
        self.assertTrue(snapshot["capabilities"]["run"])
        self.assertTrue(snapshot["capabilities"]["eval"])
        for operation in ("memory", "metrics"):
            self.assertTrue(snapshot["capabilities"][operation])
            with self.assertRaises(ValueError):
                self.app.dispatch(operation, {})

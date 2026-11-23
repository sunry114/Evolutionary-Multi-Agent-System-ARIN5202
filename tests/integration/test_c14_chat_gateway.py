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
from evo.interfaces.projects import Projects
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
        directory = Path(self.temp.name)
        self.catalog = SQLiteApplication(directory / "state")
        project = Projects(self.catalog).create({"name": "Chat", "directory": str(directory)})["project"]
        self.project_path = "/p/" + project["id"]
        self.state = self.catalog.state / "projects" / project["id"]
        self.provider = ScriptedProvider()
        self.app = SQLiteApplication(self.state, provider=self.provider)

    @contextmanager
    def http_service(self):
        server = make_server(self.catalog, 0, application_factory=lambda state: self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path, payload=None, csrf=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                headers = {"Origin": "http://" + server.authority, "Content-Type": "application/json",
                           "X-CSRF-Token": csrf or ""}
                conn.request("POST" if payload is not None else "GET", self.project_path + path,
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

    def test_same_agent_chats_keep_independent_histories_and_default_cli_chat(self):
        first = self.app.dispatch("new-chat", {"agent": "builder"})["chat"]["id"]
        second = self.app.dispatch("new-chat", {"agent": "builder"})["chat"]["id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.app.dispatch("messages", {"chat_id": second})["messages"], [])
        for chat_id, message in ((first, "First thread"), (second, "Second thread"), (first, "Follow-up")):
            self.app.dispatch("dm", {"chat_id": chat_id, "message": message})
        self.app.dispatch("dm", {"agent": "builder", "message": "Default thread"})
        for call, expected in zip(self.provider.calls, (
                ["First thread"], ["Second thread"], ["First thread", "Follow-up"], ["Default thread"])):
            context = json.loads(call[1]["content"])
            earlier = [row["content"] for row in context["history"] if row["sender"] == "human"]
            self.assertEqual(earlier, expected[:-1])
            self.assertEqual(context["latest_message"], {"sender": "human", "content": expected[-1]})

        restored = SQLiteApplication(self.state)
        default, latest, other = restored.dispatch("chat-threads", {})["chats"]
        self.assertEqual((default["title"], latest["id"], other["id"]), ("Default thread", first, second))
        self.assertEqual([row["content"] for row in restored.dispatch("messages", {"chat_id": second})["messages"]],
                         ["Second thread", "Persisted agent reply"])
        self.assertEqual([row["content"] for row in restored.messages(agent="builder") if row["sender"] == "human"],
                         ["Default thread"])

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
        history = SQLiteApplication(self.state).dispatch("messages", {})["messages"]
        self.assertEqual(sum(m["kind"] == "group" for m in history), 4)
        self.assertNotIn("private task bid", json.dumps(history))
        scoped = self.app.dispatch("messages", {"scope": "group"})["messages"]
        self.assertEqual([m for m in history if m["kind"] == "group"], scoped)

    def test_named_groups_keep_members_and_history_separate_from_other_chats(self):
        first = self.app.dispatch("new-group", {"name": "设计讨论", "members": ["reviewer", "builder"]})["group"]
        second = self.app.dispatch("new-group", {"name": "交付讨论", "members": ["builder"]})["group"]
        default = self.app.dispatch("group", {"message": "Default group context"})
        self.assertEqual(set(default["replies"]), {"researcher", "builder", "reviewer"})
        for room, message in ((first, "First group context"), (second, "Second group context"),
                              (first, "First group follow-up")):
            reply = self.app.dispatch("group", {"group_id": room["id"], "message": message})
            self.assertEqual(list(reply["replies"]), room["members"])
        contexts = [json.loads(call[1]["content"]) for call in self.provider.calls[3:]]
        self.assertEqual(
            [[row["content"] for row in context["history"] if row["sender"] == "human"] for context in contexts],
            [[], [], [], ["First group context"], ["First group context"]])

        issue = self.app.dispatch("create", {"prompt": "Work from the workspace group", "from_group": True})
        archived = self.app.store.list_group_messages(task_id=issue["task"]["id"])
        self.assertEqual([row["content"] for row in archived if row["sender"] == "human"], ["Default group context"])
        restored = SQLiteApplication(self.state)
        self.assertEqual(restored.dispatch("messages", {"scope": "group"})["messages"], [])
        groups = restored.dispatch("chat-threads", {})["groups"]
        self.assertEqual([(room["id"], room["name"], room["members"]) for room in groups],
                         [(first["id"], "设计讨论", ["reviewer", "builder"]), (second["id"], "交付讨论", ["builder"])])
        history = restored.dispatch("messages", {"group_id": second["id"]})["messages"]
        self.assertEqual([(row["sender"], row["content"]) for row in history],
                         [("human", "Second group context"), ("builder", "Persisted agent reply")])

    def test_invalid_chat_never_calls_provider_or_saves(self):
        for op, payload in (
            ("dm", {"agent": "unknown", "message": "hello"}),
            ("dm", {"agent": "builder", "message": " "}),
            ("group", {"message": []}), ("group", {"message": "x" * 8001}),
            ("group", {"message": "hello", "task_id": "other", "group_id": "unknown"}),
            ("new-group", {"name": "讨论", "members": []}),
            ("new-group", {"name": "讨论", "members": ["builder", "builder"]}),
            ("new-group", {"name": "讨论", "members": ["unknown"]}),
            ("new-group", {"name": " ", "members": ["builder"]}),
            ("messages", {"agent": "unknown"}),
        ):
            with self.subTest(op=op), self.assertRaises(ValueError):
                self.app.dispatch(op, payload)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.app.dispatch("messages", {})["messages"], [])

    def test_named_group_issue_import_copies_messages_and_keeps_source_history(self):
        group = self.app.dispatch("new-group", {"name": "RealSE", "members": ["builder"]})["group"]
        other = SQLiteApplication(self.state / "other")
        foreign = other.dispatch("new-group", {"name": "RealSE", "members": ["builder"]})["group"]
        self.app.dispatch("group", {"group_id": group["id"], "message": "Keep the @builder exchange"})
        original = self.app.store.list_group_messages(group_id=group["id"])
        with self.http_service() as (request, token):
            invalid = {"prompt": "Work from a group", "group_id": foreign["id"], "agent": "builder"}
            self.assertEqual(request("/api/create", invalid, token)[0], 409)
            self.assertEqual(self.app.store.list_tasks(), [])
            payload = {"prompt": "Work from RealSE", "group_id": group["id"]}
            status, draft = request("/api/create", payload, token)
            self.assertEqual(status, 200)
            issue_id = json.loads(draft)["task"]["id"]
            self.app.dispatch("group", {"group_id": group["id"], "message": "Later discussion"})
            status, detail = request("/api/task", {"task_id": issue_id}, token)
            self.assertEqual(status, 200)
        detail = json.loads(detail)
        self.assertEqual(detail["source_group"], {"id": group["id"], "name": "RealSE"})
        keys = ("kind", "sender", "content", "created_at")
        self.assertEqual([tuple(row[key] for key in keys) for row in detail["messages"]],
                         [tuple(row[key] for key in keys) for row in original])
        self.assertEqual(detail["executions"], [])
        history = self.app.store.list_group_messages(group_id=group["id"])
        self.assertEqual(history[:len(original)], original)
        self.assertEqual(len(history), len(original) + 2)

    def test_http_links_existing_issue_once_and_keeps_its_discussion(self):
        first = self.app.dispatch("new-group", {"name": "RealSE", "members": ["builder"]})["group"]
        second = self.app.dispatch("new-group", {"name": "Other", "members": ["builder"]})["group"]
        self.app.dispatch("group", {"group_id": first["id"], "message": "Group requirement"})
        with self.http_service() as (request, token):
            _, created = request("/api/create", {"prompt": "Already created"}, token)
            issue_id = json.loads(created)["task"]["id"]
            status, _ = request("/api/comment", {"task_id": issue_id, "message": "Existing comment"}, token)
            self.assertEqual(status, 200)
            payload = {"task_id": issue_id, "group_id": first["id"]}
            self.assertEqual(request("/api/link-group", payload)[0], 403)
            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(lambda _: request("/api/link-group", payload, token)[0], range(2)))
            self.assertEqual(statuses, [200, 200])
            self.assertEqual(request("/api/link-group", {**payload, "group_id": second["id"]}, token)[0], 409)
            _, detail = request("/api/task", {"task_id": issue_id}, token)
        detail = json.loads(detail)
        self.assertEqual(detail["source_group"], {"id": first["id"], "name": "RealSE"})
        self.assertEqual([row["content"] for row in detail["messages"]],
                         ["Group requirement", "Persisted agent reply", "Existing comment"])
        self.assertEqual(detail["executions"], [])

    def test_missing_key_is_actionable_and_does_not_save_incoming_message(self):
        app = SQLiteApplication(self.state)
        with patch("evo.adapters.llm.credentials.load_key", side_effect=ValueError("secret")):
            with self.assertRaises(ApplicationUnavailable) as error:
                app.dispatch("dm", {"agent": "builder", "message": "hello"})
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(app.dispatch("messages", {})["messages"], [])

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
            self.app.dispatch("answer", {"task_id": "clarify", "request_id": request, "answer": ""})
        answer = self.app.dispatch("answer", {"task_id": "clarify", "request_id": request, "answer": "report.md"})
        self.assertEqual(answer["status"], "answered")
        with self.assertRaises(ValueError):
            self.app.dispatch("answer", {"task_id": "clarify", "request_id": request, "answer": "duplicate"})
        for task, approved in (("deny", False), ("approve", True), ("stale", True)):
            req = self.app.approvals.request_approval(task, "builder", "delete_file", {"fixture": True})
            if task == "stale":
                with store._transaction() as conn:
                    conn.execute("UPDATE approval_requests SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (req,))
            result = self.app.dispatch("answer", {"task_id": task, "request_id": req, "approved": approved})
            self.assertEqual(result["status"], {"deny": "denied", "approve": "approved", "stale": "expired"}[task])
            self.assertEqual(store.get_action(req)["status"], "requested" if task == "approve" else "blocked")
        restored = SQLiteApplication(self.state)
        self.assertEqual(restored.store.get_approval_request(request)["answer"], {"answer": "report.md"})
        self.assertEqual(restored.store.get_task("approve")["status"], "waiting_human")

    def test_capabilities_do_not_claim_unimplemented_execution_or_evaluation(self):
        snapshot = self.app.snapshot()
        for operation in ("dm", "group", "messages", "answer", "review"):
            self.assertTrue(snapshot["capabilities"][operation])
        self.assertTrue(snapshot["capabilities"]["start"])
        self.assertTrue(snapshot["capabilities"]["eval"])
        for operation in ("memory", "metrics"):
            self.assertTrue(snapshot["capabilities"][operation])
            with self.assertRaises(ValueError):
                self.app.dispatch(operation, {})

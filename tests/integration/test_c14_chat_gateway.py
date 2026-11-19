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
    def test_board_and_chat_share_discussion_state_and_child_approval_counts(self):
        task_id = self.app.dispatch("create", {"prompt": "Build"})["task_id"]
        child = task_id + "-s1"
        self.app.store.save_task(Task(child, "Run", owner="builder", parent_id=task_id,
                                      status=TaskStatus.RUNNING))
        approval_id = self.app.approvals.request_approval(child, "builder", "run_python", {"arguments": {"path": "check.py"}})
        self.app.store.save_chat_question(task_id, "researcher", "Which input?", [
            {"name": "researcher", "specialty": "Research"}])
        self.app.store.append_event(task_id=task_id, event_type="chat_started", actor="chat", payload={})
        self.app._active_discussions.add((task_id, None))
        task = next(t for t in self.app.snapshot()["tasks"] if t["id"] == task_id)
        live = self.app.dispatch("messages", {"task_id": task_id})
        self.assertEqual(task["discussion"], live["discussion"])
        self.assertTrue(task["discussion"]["active"])
        self.assertEqual(task["pending_approvals"], 1)
        self.assertEqual(task["pending_questions"], 1)
        self.app.approvals.answer(approval_id, {"approved": True})
        self.app._active_discussions.clear()
        self.app.store.append_event(task_id=task_id, event_type="chat_completed", actor="chat", payload={})
        updated = next(t for t in self.app.snapshot()["tasks"] if t["id"] == task_id)
        self.assertEqual(updated["pending_approvals"], 0)
        self.assertEqual(updated["pending_questions"], 1)
        self.assertFalse(updated["discussion"]["active"])
        self.assertEqual(updated["discussion"], self.app.dispatch("messages", {"task_id": task_id})["discussion"])

    def test_worker_questions_are_consolidated_by_lead_and_answers_relay_to_workers(self):
        from unittest.mock import Mock
        provider = Mock()
        provider.complete.side_effect = [
            {"reply": "@human Which rooms?", "waiting_for_human": True},
            {"reply": "@human Which timezone?", "waiting_for_human": True},
            {"reply": "I will coordinate"},
            {"reply": "@human Please confirm rooms and timezone.", "waiting_for_human": True,
             "human_question": "Which rooms and timezone?"},
            {"reply": "Use Room A in UTC+8."},
            {"reply": "I will implement Room A in UTC+8."},
            {"reply": "I will verify Room A in UTC+8."},
        ]
        self.app._provider = provider
        task_id = self.app.dispatch("create", {"prompt": "Build", "agents": [
            {"name": name, "specialty": name} for name in ("dev", "qa", "lead")]})["task_id"]
        self.app.dispatch("group", {"task_id": task_id, "message": "Discuss"})
        requests = self.app.snapshot()["requests"]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["agent"], "lead")
        self.assertEqual(request["payload"]["sources"], ["dev", "qa"])
        history = self.app.messages(task_id=task_id)
        self.assertEqual({m["sender"] for m in history if m["kind"] == "dm"}, {"lead"})
        with patch.object(self.app, "start_discussion"):
            self.app.dispatch("answer", {"request_id": request["id"],
                                         "answer": "Room A UTC+8; PRIVATE-BUDGET"})
        self.app.chat("group", {"task_id": task_id, "message": "Resume"}, resume_request=request["id"])
        contexts = [call.args[0][1]["content"] for call in provider.complete.call_args_list]
        self.assertIn("PRIVATE-BUDGET", contexts[4])
        self.assertNotIn("PRIVATE-BUDGET", contexts[5])
        self.assertNotIn("PRIVATE-BUDGET", contexts[6])
        self.assertIn("Use Room A in UTC+8", contexts[5])
        self.assertEqual(provider.complete.call_count, 7)
        self.assertEqual(self.app.snapshot()["requests"], [])

    def test_worker_action_approvals_keep_their_owners_and_accept_only_buttons(self):
        task_id = self.app.dispatch("create", {"prompt": "Build", "agents": [
            {"name": name, "specialty": name} for name in ("lead", "dev", "qa")]})["task_id"]
        requests = {}
        for name in ("dev", "qa"):
            child = task_id + "-" + name
            self.app.store.save_task(Task(child, "Run checks", owner=name, parent_id=task_id,
                                          status=TaskStatus.RUNNING))
            requests[name] = self.app.approvals.request_approval(
                child, name, "run_python", {"arguments": {"path": "check.py"}, "sha256": "f" * 64})
        restored = SQLiteApplication(self.state, provider=self.provider)
        pending = restored.snapshot()["requests"]
        self.assertEqual({r["agent"] for r in pending if r["kind"] == "approval"}, {"dev", "qa"})
        with self.assertRaises(ValueError):
            restored.dispatch("answer", {"request_id": requests["dev"], "answer": "approve"})
        restored.dispatch("answer", {"request_id": requests["dev"], "approved": True})
        restored.dispatch("answer", {"request_id": requests["qa"], "approved": False})
        self.assertEqual(restored.store.get_approval_request(requests["dev"])["status"], "approved")
        self.assertEqual(restored.store.get_approval_request(requests["qa"])["status"], "denied")

    def test_restart_routes_legacy_worker_questions_to_lead_and_keeps_approvals(self):
        task_id = self.app.dispatch("create", {"prompt": "Build", "agents": [
            {"name": name, "specialty": name} for name in ("dev", "lead")]})["task_id"]
        question_id = self.app.store.save_chat_question(task_id, "dev", "Which input?", [
            {"name": name, "specialty": name} for name in ("dev", "lead")])
        child = task_id + "-s1"
        self.app.store.save_task(Task(child, "Run", owner="dev", parent_id=task_id, status=TaskStatus.RUNNING))
        approval_id = self.app.approvals.request_approval(child, "dev", "run_python", {"arguments": {"path": "check.py"}})
        restored = SQLiteApplication(self.state, provider=self.provider)
        question = restored.store.get_approval_request(question_id)
        self.assertEqual(question["agent"], "lead")
        self.assertEqual(question["payload"]["sources"], ["dev"])
        self.assertEqual(restored.store.get_approval_request(approval_id)["agent"], "dev")
        private = [m for m in restored.messages(task_id=task_id) if m["kind"] == "dm"]
        self.assertEqual(private[0]["sender"], "lead")

    def test_lead_pending_question_updates_keep_worker_sources_and_one_inbox_card(self):
        roles = [{"name": name, "specialty": name} for name in ("lead", "dev", "qa")]
        task_id = self.app.dispatch("create", {"prompt": "Build", "agents": roles})["task_id"]
        request_id = self.app.store.save_chat_question(task_id, "lead", "Which rooms?", roles, sources=["dev"])
        updated_id = self.app.store.save_chat_question(task_id, "lead", "Which timezone?", roles, sources=["qa"])
        self.assertEqual(updated_id, request_id)
        pending = self.app.dispatch("messages", {"task_id": task_id})["requests"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["sources"], ["dev", "qa"])
        self.assertIn("Which rooms?", pending[0]["payload"]["question"])
        self.assertIn("Which timezone?", pending[0]["payload"]["question"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.provider = ScriptedProvider()
        self.app = SQLiteApplication(self.state, provider=self.provider)

    def test_group_question_goes_to_private_inbox_without_stopping_other_agents(self):
        from unittest.mock import Mock
        provider = Mock()
        provider.complete.side_effect = [
            {"reply": "@human Which rooms should be bookable?", "waiting_for_human": True},
            {"reply": "I will plan implementation"},
            {"reply": "I will plan validation"},
            {"reply": "Thanks, using Room A"},
        ]
        self.app._provider = provider
        task = self.app.dispatch("create", {"prompt": "Build booking system"})
        payload = {"task_id": task["task_id"], "message": "Discuss rooms"}
        result = self.app.dispatch("group", payload)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["waiting_for_human"])
        self.assertEqual(set(result["replies"]), {"researcher", "builder", "reviewer"})
        self.assertEqual(provider.complete.call_count, 3)
        request = self.app.snapshot()["requests"][0]
        self.assertEqual(request["action"], "chat_question")
        self.assertEqual(request["agent"], "researcher")
        history = self.app.messages(task_id=task["task_id"])
        self.assertTrue(any(m["kind"] == "dm" and m["sender"] == "researcher" for m in history))
        with patch.object(self.app, "start_discussion", return_value={"status": "started"}) as wake:
            self.app.dispatch("answer", {"request_id": request["id"], "answer": "Room A"})
        wake.assert_called_once_with({"task_id": task["task_id"], "request_id": request["id"]})
        self.app.chat("group", payload, resume_request=request["id"])
        self.assertEqual(provider.complete.call_count, 4)
        self.assertIn("Room A", provider.complete.call_args[0][0][1]["content"])
        self.assertFalse(any(m["kind"] == "group" and m["content"] == "Room A"
                             for m in self.app.messages(task_id=task["task_id"])))

    def test_provider_failure_reports_safe_cause_instead_of_human_timeout(self):
        from evo.adapters.llm.client import ProviderError
        from unittest.mock import Mock
        self.app._provider = Mock()
        self.app._provider.complete.side_effect = ProviderError("private-secret", code="connection")
        with self.assertRaisesRegex(ApplicationFailure, "连接失败或超时") as result:
            self.app.dispatch("group", {"message": "Discuss"})
        self.assertNotIn("private-secret", str(result.exception))

    def test_chat_question_survives_restart_and_does_not_expire_while_typing(self):
        task = self.app.dispatch("create", {"prompt": "Build"})
        request_id = self.app.store.save_chat_question(
            task["task_id"], "builder", "Which room?",
            [{"name": "builder", "specialty": "Build software"}])
        with self.app.store._transaction() as connection:
            connection.execute("UPDATE approval_requests SET created_at=? WHERE id=?",
                               ("2000-01-01T00:00:00+00:00", request_id))
        restored = SQLiteApplication(self.state, provider=self.provider)
        self.assertIsNone(restored.approvals.get_answer(request_id))
        self.assertEqual(restored.store.get_task(task["task_id"])["status"], "created")
        with self.assertRaisesRegex(ApplicationUnavailable, "私聊"):
            restored.start_task({"task_id": task["task_id"], "prompt": "Build",
                                 "final_check": {"path": "report.md"}})
        with patch.object(restored, "start_discussion"):
            restored.dispatch("answer", {"request_id": request_id, "answer": "Room A"})
            with self.assertRaises(ValueError):
                restored.dispatch("answer", {"request_id": request_id, "answer": "Room B"})
        answers = [m for m in restored.messages(task_id=task["task_id"])
                   if m["kind"] == "dm" and m["sender"] == "human"]
        self.assertEqual([m["content"] for m in answers], ["Room A"])

    def test_task_keeps_its_roles_when_a_later_task_changes_the_registry(self):
        first = self.app.dispatch("create", {"prompt": "First", "agents": [
            {"name": "lead", "specialty": "Coordinate"}, {"name": "dev", "specialty": "Develop"}]})
        self.app.dispatch("create", {"prompt": "Second", "agents": [
            {"name": "writer", "specialty": "Write"}]})
        result = self.app.dispatch("group", {"task_id": first["task_id"], "message": "Discuss"})
        self.assertEqual(set(result["replies"]), {"lead", "dev"})
        self.assertEqual([a.name for a in self.app._task_agents(first["task_id"])], ["lead", "dev"])

    def test_live_inbox_includes_child_questions_without_other_task_questions(self):
        self.app.store.save_task(Task("root", "Build"))
        self.app.store.save_task(Task("child", "Check", parent_id="root"))
        self.app.store.save_task(Task("other", "Other"))
        for scope in ("child", "other"):
            self.app.store.save_approval_request(request_id=scope + "-question", task_id=scope,
                                                 agent="builder", kind="clarification",
                                                 payload={"question": "Please clarify"})
        live = self.app.dispatch("messages", {"task_id": "root"})
        self.assertEqual([r["id"] for r in live["requests"]], ["child-question"])

    def test_legacy_mixed_questions_are_filtered_only_in_private_views(self):
        task_id = self.app.dispatch("create", {"prompt": "Discuss"})["task_id"]
        mixed = "@builder Implement the API.\n@human Which timezone?\n@reviewer Test overlaps."
        self.app.store.save_chat_question(task_id, "researcher", mixed,
                                          [{"name": "researcher", "specialty": "Research"}])
        self.app.store.save_message(Message("team", "group", "researcher", mixed, "chat", task_id))
        history = self.app.dispatch("messages", {"task_id": task_id})
        private = [m for m in history["messages"] if m["kind"] == "dm"]
        self.assertEqual([m["content"] for m in private], ["Which timezone?"])
        self.assertEqual(history["requests"][0]["payload"]["question"], "Which timezone?")
        self.assertEqual(self.app.snapshot()["requests"][0]["payload"]["question"], "Which timezone?")
        public = [m for m in history["messages"] if m["kind"] == "group"]
        self.assertEqual(public[0]["content"], mixed)


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

    def test_task_scoped_messages_keep_older_history_beyond_board_snapshot_limit(self):
        self.app.store.save_task(Task("long-task", "Long discussion"))
        self.app.store.save_task(Task("other-task", "Other discussion"))
        self.app.store.save_message(Message("first", "group", "builder", "FULL-END",
                                            "chat", "long-task"))
        for index in range(401):
            self.app.store.save_message(Message("later-" + str(index), "group", "builder",
                                                "Other message", "chat", "other-task"))
        self.assertNotIn("first", {item["id"] for item in self.app.snapshot()["messages"]})
        scoped = self.app.dispatch("messages", {"task_id": "long-task"})["messages"]
        self.assertEqual([item["content"] for item in scoped], ["FULL-END"])
        with self.http_service() as (request, token):
            status, body = request("/api/messages", {"task_id": "long-task"}, token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["messages"][0]["content"], "FULL-END")

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

"""Transactional issue statuses and the local execution queue."""

import re
from uuid import uuid4

from evo.adapters.persistence.sqlite_store import ConflictError, _utc_now
from evo.domain.models import Agent, Message, Task


ISSUE_STATUSES = {"backlog", "todo", "in_progress", "in_review", "blocked", "done", "cancelled"}
ACTIVE_RUNS = ("queued", "running", "stopping")
RETRYABLE_RUNS = ("failed", "stopped")
MENTION = re.compile(r"(?<![a-zA-Z0-9_.+@/-])@([a-zA-Z0-9_-]{1,64})(?![a-zA-Z0-9_-])")


def issue_status(task):
    if task.get("issue_status"):
        return task["issue_status"]
    if task["status"] == "success":
        return "done" if task.get("review_status") == "accepted" else "in_review"
    if task["status"] in ("failed", "blocked", "waiting_human"):
        return "blocked"
    return "todo" if task["status"] == "created" else "in_progress"


class IssueRunStore:
    def __init__(self, store):
        self.store = store

    def _issue(self, connection, issue_id):
        return connection.execute("""
            SELECT t.*, r.status AS review_status FROM tasks t LEFT JOIN reviews r ON r.task_id = t.id
            WHERE t.id = ? AND t.parent_id IS NULL
        """, (self.store._issue_id(connection, issue_id),)).fetchone()

    def _event(self, connection, issue_id, kind, actor, payload):
        self.store._insert_event(connection, task_id=issue_id, event_type=kind, actor=actor, payload=payload)

    def _status(self, connection, issue, status, actor):
        previous = issue_status(dict(issue))
        if previous != status or issue["issue_status"] is None:
            connection.execute("UPDATE tasks SET issue_status=?, updated_at=? WHERE id=?", (status, _utc_now(), issue["id"]))
            self._event(connection, issue["id"], "issue_status_changed", actor, {"from": previous, "to": status})

    def _enqueue(self, connection, issue, agent, kind, *, comment_id=None, parent_run_id=None, retry_of=None):
        run_id = uuid4().hex
        self.store._insert_task(connection, Task(run_id, "Agent run", owner=agent.name, parent_id=issue["id"]))
        connection.execute("""
            INSERT INTO issue_runs (id,issue_id,agent,agent_role,kind,status,comment_id,parent_run_id,retry_of,created_at)
            VALUES (?,?,?,?,?,'queued',?,?,?,?)
        """, (run_id, issue["id"], agent.name, agent.specialty, kind, comment_id, parent_run_id, retry_of, _utc_now()))
        if comment_id:
            connection.execute("INSERT INTO issue_run_triggers VALUES (?,?,?)", (run_id, comment_id, parent_run_id))
        self._event(connection, issue["id"], "issue_run_queued", agent.name, {"run_id": run_id, "kind": kind})
        return run_id

    def _route_comment(self, connection, issue, agent, kind, message, parent_run_id):
        existing = connection.execute("""
            SELECT r.id, r.status FROM issue_runs r
            WHERE r.issue_id=? AND r.agent=? AND (
                r.status='queued' OR (r.status='running' AND EXISTS (
                    SELECT 1 FROM issue_run_triggers t JOIN messages m ON m.id=t.comment_id
                    WHERE t.run_id=r.id AND m.sender=? AND m.content=?
                      AND (t.parent_run_id IS NULL)=(? IS NULL)
                ))
            )
            ORDER BY r.status='running' DESC, r.rowid LIMIT 1
        """, (issue["id"], agent.name, message.sender, message.content, parent_run_id)).fetchone()
        if not existing:
            return self._enqueue(connection, issue, agent, kind, comment_id=message.id, parent_run_id=parent_run_id)
        connection.execute("INSERT INTO issue_run_triggers VALUES (?,?,?)",
                           (existing["id"], message.id, parent_run_id))
        self._event(connection, issue["id"], "issue_run_trigger_merged", agent.name,
                    {"run_id": existing["id"], "comment_id": message.id, "status": existing["status"]})
        return existing["id"]

    def triggers(self, run_id):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT m.id, m.sender, m.content, t.parent_run_id
                FROM issue_run_triggers t JOIN messages m ON m.id=t.comment_id
                WHERE t.run_id=? ORDER BY t.rowid
            """, (run_id,))]

    def create(self, connection, issue_id, agents, squad):
        issue = self._issue(connection, issue_id)
        self._status(connection, issue, "todo", "human")
        name = squad.lead if issue["assignee_type"] == "squad" else issue["assignee_id"]
        return self._enqueue(connection, issue, agents[name], "assignment") if name else None

    def assign(self, issue_id, assignee_type, assignee_id, routing_mode, agent):
        with self.store._transaction() as connection:
            issue = self._issue(connection, issue_id)
            if issue["assignee_type"] == assignee_type and (assignee_type == "squad" or issue["assignee_id"] == assignee_id):
                return None
            status = issue_status(dict(issue))
            connection.execute("""UPDATE tasks SET assignee_type=?, assignee_id=?, routing_mode=?,
                issue_status=?, updated_at=? WHERE id=?""", (assignee_type, assignee_id, routing_mode, status,
                                                            _utc_now(), issue["id"]))
            self._event(connection, issue["id"], "issue_assigned", "human",
                        {"assignee_type": assignee_type, "assignee_id": assignee_id})
            self._stop_queued_assignments(connection, issue["id"])
            if status not in ("backlog", "done", "cancelled"):
                self._status(connection, issue, "todo", "human")
                return self._enqueue(connection, issue, agent, "assignment")
        return None

    def change_status(self, issue_id, status, *, agent=None):
        if status not in ISSUE_STATUSES:
            raise ValueError("Unknown issue status")
        with self.store._transaction() as connection:
            issue = self._issue(connection, issue_id)
            previous = issue_status(dict(issue))
            self._status(connection, issue, status, "human")
            if status in ("backlog", "done", "cancelled"):
                self._stop_queued_assignments(connection, issue["id"])
            if status == "todo" and previous != "todo" and agent and not connection.execute(
                "SELECT 1 FROM issue_runs WHERE issue_id=? AND kind='assignment' AND status IN ('queued','running','stopping')",
                (issue["id"],),
            ).fetchone():
                return self._enqueue(connection, issue, agent, "assignment")
        return None

    def _stop_queued_assignments(self, connection, issue_id):
        rows = connection.execute("SELECT * FROM issue_runs WHERE issue_id=? AND status='queued' AND kind='assignment'",
                                  (issue_id,)).fetchall()
        for run in rows:
            self._finish(connection, run, "stopped", "Issue status or assignee changed")

    def start(self, issue_id, agent):
        with self.store._transaction() as connection:
            issue = self._issue(connection, issue_id)
            if not issue["assignee_id"] or issue_status(dict(issue)) in ("backlog", "done", "cancelled"):
                raise ConflictError("The issue is not ready to start")
            active = connection.execute("""SELECT id FROM issue_runs WHERE issue_id=? AND kind='assignment'
                AND status IN ('queued','running','stopping') ORDER BY rowid DESC LIMIT 1""", (issue["id"],)).fetchone()
            return active["id"] if active else self._enqueue(connection, issue, agent, "assignment")

    def comment(self, issue_id, content, agents, *, sender="human", parent_run_id=None, targets=None, assignee=None):
        if targets is None:
            targets = MENTION.findall(content) if sender == "human" else ()
        targets = tuple(target for target in dict.fromkeys(targets) if target in agents and target != sender)
        with self.store._transaction() as connection:
            issue = self._issue(connection, issue_id)
            if parent_run_id is not None:
                run = self._run(connection, issue["id"], parent_run_id)
                if run["agent"] != sender or run["status"] != "running":
                    raise ConflictError("Only the active run may post comments")
                if targets and issue_status(dict(issue)) not in ("todo", "in_progress"):
                    raise ConflictError("The issue is no longer open for delegation")
            message = Message(uuid4().hex, "comment" if sender == "human" else "agent", sender,
                              content, task_id=issue["id"])
            self.store._insert_message(connection, message, issue_run_id=parent_run_id)
            lane = issue_status(dict(issue))
            if sender == "human" and assignee in targets and lane in ("in_review", "blocked"):
                self._status(connection, issue, "in_progress", sender)
                if lane == "in_review":
                    self._event(connection, issue["id"], "issue_follow_up_requested", sender,
                                {"comment_id": message.id})
            runs = [self._route_comment(connection, issue, agents[target], "mention", message, parent_run_id)
                    for target in targets]
        return {"issue_id": message.task_id, "message_id": message.id, "run_ids": runs}

    def _run(self, connection, issue_id, run_id):
        run = connection.execute("SELECT * FROM issue_runs WHERE issue_id=? AND id=?", (issue_id, run_id)).fetchone()
        if run is None:
            raise ConflictError("Run does not belong to this issue")
        return run

    def list(self, issue_id):
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT r.id,r.issue_id,r.agent,r.kind,r.status,r.result,r.comment_id,r.retry_of,r.parent_run_id,
                       r.created_at,r.started_at,r.finished_at,
                       NOT EXISTS (SELECT 1 FROM issue_runs newer WHERE newer.retry_of=r.id) AS latest_attempt
                FROM issue_runs r WHERE r.issue_id=? ORDER BY r.rowid DESC
            """, (issue_id,))]

    def claim(self):
        with self.store._transaction() as connection:
            run = connection.execute("SELECT * FROM issue_runs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if run is None:
                return None
            issue = self._issue(connection, run["issue_id"])
            connection.execute("UPDATE issue_runs SET status='running',started_at=? WHERE id=?", (_utc_now(), run["id"]))
            connection.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (_utc_now(), run["id"]))
            self._event(connection, run["issue_id"], "issue_run_started", run["agent"], {"run_id": run["id"]})
            if run["kind"] == "assignment" and issue_status(dict(issue)) == "todo":
                self._status(connection, issue, "in_progress", run["agent"])
            return dict(run)

    def recover_interrupted(self):
        with self.store._transaction() as connection:
            for run in connection.execute("SELECT * FROM issue_runs WHERE status IN ('running','stopping')").fetchall():
                self._finish(connection, run, "interrupted", "Worker exited before runtime cleanup was confirmed")
                issue = self._issue(connection, run["issue_id"])
                if run["kind"] != "mention" and issue_status(dict(issue)) == "in_progress":
                    self._status(connection, issue, "blocked", run["agent"])

    def stopping(self, run_id):
        with self.store._connect() as connection:
            row = connection.execute("SELECT status FROM issue_runs WHERE id=?", (run_id,)).fetchone()
            return row["status"] == "stopping"

    def stop(self, issue_id, run_id):
        with self.store._transaction() as connection:
            run = self._run(connection, issue_id, run_id)
            if run["status"] == "queued":
                self._finish(connection, run, "stopped", "Stopped before execution")
                return "stopped"
            if run["status"] == "running":
                connection.execute("UPDATE issue_runs SET status='stopping' WHERE id=?", (run_id,))
                self._event(connection, issue_id, "issue_run_stop_requested", "human", {"run_id": run_id})
                return "stopping"
            return run["status"]

    def retry(self, issue_id, run_id):
        with self.store._transaction() as connection:
            run = self._run(connection, issue_id, run_id)
            if run["status"] not in RETRYABLE_RUNS:
                raise ConflictError("Only failed or stopped runs can be retried")
            previous = connection.execute("SELECT id FROM issue_runs WHERE retry_of=?", (run_id,)).fetchone()
            if previous:
                return previous["id"]
            issue = self._issue(connection, issue_id)
            if run["kind"] == "assignment" and issue_status(dict(issue)) == "blocked":
                self._status(connection, issue, "todo", "human")
            retry_id = self._enqueue(connection, issue, Agent(run["agent"], run["agent_role"]), run["kind"],
                                     comment_id=run["comment_id"], parent_run_id=run["parent_run_id"], retry_of=run_id)
            connection.execute("""INSERT OR IGNORE INTO issue_run_triggers
                SELECT ?,comment_id,parent_run_id FROM issue_run_triggers WHERE run_id=?""", (retry_id, run_id))
            return retry_id

    def set_run_status(self, run, status):
        if status not in ("in_progress", "in_review", "blocked"):
            raise ValueError("The run cannot set that issue status")
        with self.store._transaction() as connection:
            current = self._run(connection, run["issue_id"], run["id"])
            issue = self._issue(connection, run["issue_id"])
            lane = issue_status(dict(issue))
            if current["status"] != "running" or lane in ("backlog", "done", "cancelled"):
                raise ConflictError("Issue is no longer active")
            if status == "in_review" and lane != status:
                if lane != "in_progress":
                    raise ConflictError("Move the issue to in_progress before requesting review")
                if self._pending_children(connection, run["id"]):
                    raise ConflictError("Finish this run so queued teammate runs can start. "
                                        "Request review on the follow-up after checking their work.")
                self._event(connection, run["id"], "issue_review_requested", run["agent"], {"run_id": run["id"]})
                return {"issue_status": lane, "requested_status": status, "deferred": True}
            self._status(connection, issue, status, run["agent"])
            return {"issue_status": status, "deferred": False}

    @staticmethod
    def _pending_children(connection, run_id):
        return connection.execute(
            """SELECT 1 FROM issue_run_triggers t JOIN issue_runs r ON r.id=t.run_id
               WHERE t.parent_run_id=? AND r.status IN ('queued','running','stopping') LIMIT 1""",
            (run_id,)).fetchone() is not None

    def finish(self, run, status, result, *, reply=None, assignee=None):
        with self.store._transaction() as connection:
            current = self._run(connection, run["issue_id"], run["id"])
            if current["status"] not in ("running", "stopping"):
                return
            if current["status"] == "stopping" and status != "interrupted":
                status, result = "stopped", "Stopped by user"
            if reply is None:
                reply = result if status == "completed" else ""
            if status != "completed" and reply:
                result += "\n\n" + reply
            issue = self._issue(connection, run["issue_id"])
            self._finish(connection, current, status, result)
            message = None
            if reply.strip():
                kind = "agent" if status == "completed" else "run_error"
                message = Message(uuid4().hex, kind, run["agent"], reply, task_id=issue["id"])
                self.store._insert_message(connection, message, issue_run_id=run["id"])
            if not assignee or issue_status(dict(issue)) != "in_progress":
                return
            if assignee.name != run["agent"]:
                if issue["assignee_type"] == "squad" and status in ("completed", "failed", "interrupted"):
                    self._return_to_lead(connection, issue, current, assignee, message, result)
                return
            requested = connection.execute("""SELECT 1 FROM events WHERE task_id=?
                AND event_type='issue_review_requested' LIMIT 1""", (run["id"],)).fetchone()
            direct_delivery = issue["assignee_type"] != "squad" and run["kind"] == "assignment"
            if status == "completed" and (requested or direct_delivery) and not self._pending_children(connection, run["id"]):
                self._finish_review(connection, issue, run["agent"])
            elif status in ("failed", "stopped", "interrupted") and (run["kind"] != "mention" or requested):
                self._status(connection, issue, "blocked", run["agent"])

    def _return_to_lead(self, connection, issue, run, assignee, message, result):
        delegated = connection.execute("""SELECT 1 FROM issue_run_triggers t JOIN issue_runs p ON p.id=t.parent_run_id
            WHERE t.run_id=? AND p.agent=? LIMIT 1""", (run["id"], assignee.name)).fetchone()
        if not delegated or (message is None and result == "NO_ACTION"):
            return
        if message is None:
            message = Message(uuid4().hex, "run_error", run["agent"], result, task_id=issue["id"])
            self.store._insert_message(connection, message, issue_run_id=run["id"])
        self._route_comment(connection, issue, assignee, "handoff", message, run["id"])

    def _finish_review(self, connection, issue, agent):
        queued = connection.execute("""
            SELECT r.*, EXISTS (
                SELECT 1 FROM issue_run_triggers t JOIN messages m ON m.id=t.comment_id
                WHERE t.run_id=r.id AND t.parent_run_id IS NULL AND m.sender='human'
            ) AS human_requested
            FROM issue_runs r WHERE r.issue_id=? AND r.status='queued' AND r.kind IN ('mention','handoff')
        """, (issue["id"],)).fetchall()
        for run in queued:
            if not run["human_requested"] and run["retry_of"] is None:
                self._finish(connection, run, "stopped", "Superseded by issue sign-off")
        if not any(run["human_requested"] and run["agent"] == agent for run in queued):
            self._status(connection, issue, "in_review", agent)

    def _finish(self, connection, run, status, result):
        now = _utc_now()
        connection.execute("UPDATE issue_runs SET status=?,result=?,finished_at=? WHERE id=?",
                           (status, result, now, run["id"]))
        task_status = {"completed": "success", "stopped": "blocked", "interrupted": "blocked"}.get(status, "failed")
        connection.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?",
                           (task_status, result[:4000], now, run["id"]))
        self._event(connection, run["issue_id"], "issue_run_finished", run["agent"],
                    {"run_id": run["id"], "status": status, "result": result[:500]})

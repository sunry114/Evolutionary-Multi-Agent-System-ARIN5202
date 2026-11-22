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
        self._event(connection, issue["id"], "issue_run_queued", agent.name, {"run_id": run_id, "kind": kind})
        return run_id

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

    def comment(self, issue_id, content, agents, *, sender="human", parent_run_id=None):
        targets = tuple(dict.fromkeys(MENTION.findall(content)))
        with self.store._transaction() as connection:
            issue = self._issue(connection, issue_id)
            if parent_run_id:
                run = self._run(connection, issue["id"], parent_run_id)
                if run["agent"] != sender or run["status"] != "running":
                    raise ConflictError("Only the active run may post comments")
                depth = connection.execute("""WITH RECURSIVE parents(id,parent_run_id) AS (
                    SELECT id,parent_run_id FROM issue_runs WHERE id=? UNION ALL
                    SELECT r.id,r.parent_run_id FROM issue_runs r JOIN parents p ON p.parent_run_id=r.id
                ) SELECT count(*) FROM parents""", (parent_run_id,)).fetchone()[0]
                if depth >= 8:
                    targets = ()
            message = Message(uuid4().hex, "comment" if sender == "human" else "agent", sender,
                              content, task_id=issue["id"])
            self.store._insert_message(connection, message)
            runs = [self._enqueue(connection, issue, agents[target], "mention", comment_id=message.id,
                                  parent_run_id=parent_run_id)
                    for target in targets if target in agents and target != sender]
        result = {"issue_id": message.task_id, "message_id": message.id, "run_ids": runs}
        if parent_run_id and runs:
            result.update(handoff_queued=True, finish_current_run=True,
                          instruction="Finish this run successfully now so its outputs can be archived. "
                                      "Delegated runs start after archiving; do not wait or poll for them.")
        return result

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
            # A queued mention is not runnable until its producer has archived
            # its outputs and committed completed. Also reconcile legacy queues.
            orphaned = connection.execute("""SELECT child.* FROM issue_runs child
                JOIN issue_runs parent ON parent.id=child.parent_run_id
                WHERE child.status='queued' AND parent.status IN ('failed','stopped','interrupted')""").fetchall()
            for child in orphaned:
                self._finish(connection, child, "stopped", "Producer failed before artifact handoff")
            candidates = connection.execute("""SELECT child.* FROM issue_runs child
                LEFT JOIN issue_runs parent ON parent.id=child.parent_run_id
                WHERE child.status='queued' AND
                (child.parent_run_id IS NULL OR parent.status='completed')
                ORDER BY child.rowid""").fetchall()
            run = next((item for item in candidates if item["kind"] != "handoff" or not
                        self._pending_descendants(connection, item["parent_run_id"], item["id"])), None)
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
            return self._enqueue(connection, issue, Agent(run["agent"], run["agent_role"]), run["kind"],
                                 comment_id=run["comment_id"], parent_run_id=run["parent_run_id"], retry_of=run_id)

    def set_run_status(self, run, status):
        if status not in ("in_progress", "in_review", "blocked") or run["kind"] == "mention":
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
                self._event(connection, issue["id"], "issue_review_requested", run["agent"], {"run_id": run["id"]})
                result = {"issue_status": lane, "requested_status": status, "deferred": True}
                if self._pending_children(connection, run["id"]):
                    result.update(waiting_for_members=True, finish_current_run=True,
                                  instruction="Review is deferred. Finish this run successfully now to archive outputs "
                                              "and let members execute. Do not wait for them in this run.")
                return result
            self._status(connection, issue, status, run["agent"])
            return {"issue_status": status, "deferred": False}

    @staticmethod
    def _pending_descendants(connection, parent_id, exclude_id):
        return connection.execute("""WITH RECURSIVE descendants(id,status) AS (
            SELECT id,status FROM issue_runs WHERE parent_run_id=?
            UNION ALL SELECT r.id,r.status FROM issue_runs r JOIN descendants d ON r.parent_run_id=d.id
        ) SELECT 1 FROM descendants WHERE id!=? AND status IN ('queued','running','stopping') LIMIT 1""",
                                  (parent_id, exclude_id)).fetchone() is not None

    @staticmethod
    def _pending_children(connection, run_id):
        return connection.execute(
            "SELECT 1 FROM issue_runs WHERE parent_run_id=? AND status IN ('queued','running','stopping') LIMIT 1",
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
            if reply.strip() or run["kind"] == "mention":
                kind = "agent" if status == "completed" else "run_error"
                message = Message(uuid4().hex, kind, run["agent"], reply or result[:4000], task_id=issue["id"])
                self.store._insert_message(connection, message)
            if run["kind"] == "mention":
                parent = connection.execute("SELECT agent,kind,status FROM issue_runs WHERE id=?",
                                            (run["parent_run_id"],)).fetchone()
                if (parent and parent["status"] == "completed" and issue["assignee_type"] == "squad" and assignee
                        and parent["agent"] == assignee.name and parent["kind"] in ("assignment", "handoff")
                        and assignee.name != run["agent"]):
                    pending = connection.execute("""SELECT 1 FROM issue_runs WHERE parent_run_id=? AND kind='mention'
                        AND status IN ('queued','running','stopping') LIMIT 1""", (run["parent_run_id"],)).fetchone()
                    if not pending and issue_status(dict(issue)) in ("todo", "in_progress"):
                        self._enqueue(connection, issue, assignee, "handoff", comment_id=message.id,
                                      parent_run_id=run["parent_run_id"])
            if not assignee or assignee.name != run["agent"] or run["kind"] == "mention":
                return
            if issue_status(dict(issue)) != "in_progress":
                return
            if status == "completed" and not self._pending_children(connection, run["id"]):
                self._status(connection, issue, "in_review", run["agent"])
            elif status in ("failed", "stopped", "interrupted"):
                self._status(connection, issue, "blocked", run["agent"])

    def _finish(self, connection, run, status, result):
        now = _utc_now()
        connection.execute("UPDATE issue_runs SET status=?,result=?,finished_at=? WHERE id=?",
                           (status, result, now, run["id"]))
        task_status = {"completed": "success", "stopped": "blocked", "interrupted": "blocked"}.get(status, "failed")
        connection.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?",
                           (task_status, result[:4000], now, run["id"]))
        self._event(connection, run["issue_id"], "issue_run_finished", run["agent"],
                    {"run_id": run["id"], "status": status, "result": result[:500]})
        if status != "completed":
            children = connection.execute("SELECT * FROM issue_runs WHERE parent_run_id=? AND status='queued'",
                                          (run["id"],)).fetchall()
            for child in children:
                self._finish(connection, child, "stopped", "Producer failed before artifact handoff")

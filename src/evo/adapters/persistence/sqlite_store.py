"""SQLite persistence adapter with explicit connection lifetimes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from typing import Iterator, Mapping, Optional, Sequence

from evo.domain.models import Message, Task
from evo.domain.statuses import ReviewStatus, TaskStatus
from evo.domain.statuses import validate_task_transition


class StorageError(RuntimeError):
    """Base class for safe persistence errors."""


class RecordNotFoundError(StorageError, KeyError):
    """Requested persistence record does not exist."""


class ConflictError(StorageError, ValueError):
    """The stored state no longer matches the expected state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: Optional[Mapping[str, object]]) -> str:
    return json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _decode_json(value: Optional[str]) -> object:
    if value is None:
        return None
    return json.loads(value)


class SQLiteStore:
    """Durable task, message, event, approval and review storage.

    Every public operation opens and explicitly closes its own SQLite
    connection. Transactional operations use BEGIN IMMEDIATE so the
    expected state is rechecked while holding the write transaction.
    """

    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms

        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000,
        )

        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "PRAGMA busy_timeout = %d" % self.busy_timeout_ms
            )

            foreign_keys = connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0]

            if foreign_keys != 1:
                raise StorageError(
                    "SQLite foreign key enforcement is unavailable"
                )

            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _migrate(self) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'schema_migrations'
                """
            ).fetchone()

            if exists:
                row = connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()
                version = int(row[0] or 0)
            else:
                version = 0

            if version > 1:
                raise StorageError(
                    "Database schema is newer than this application"
                )

            if version == 0:
                migration = (
                    files("evo.adapters.persistence")
                    .joinpath("migrations")
                    .joinpath("0001_initial.sql")
                    .read_text(encoding="utf-8")
                )

                try:
                    connection.executescript(migration)
                except Exception:
                    try:
                        connection.rollback()
                    finally:
                        raise

            final_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            if final_version != 1:
                raise StorageError(
                    "Unexpected SQLite schema version"
                )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()
            return int(row[0] or 0)

    def sqlite_settings(self) -> Mapping[str, int]:
        with self._connect() as connection:
            return {
                "foreign_keys": int(
                    connection.execute(
                        "PRAGMA foreign_keys"
                    ).fetchone()[0]
                ),
                "busy_timeout": int(
                    connection.execute(
                        "PRAGMA busy_timeout"
                    ).fetchone()[0]
                ),
                "user_version": int(
                    connection.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                ),
            }

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def save_task(self, task: Task) -> None:
        now = _utc_now()
        status = TaskStatus(task.status).value

        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO tasks(
                        id,
                        description,
                        owner,
                        parent_id,
                        status,
                        result,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.id,
                        task.description,
                        task.owner,
                        task.parent_id,
                        status,
                        task.result,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "Task could not be created"
                ) from exc

            if status == TaskStatus.SUCCESS.value:
                connection.execute(
                    """
                    INSERT INTO reviews(
                        task_id,
                        status,
                        feedback,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, '', ?, ?)
                    """,
                    (
                        task.id,
                        ReviewStatus.PENDING.value,
                        now,
                        now,
                    ),
                )

            self._insert_event(
                connection,
                task_id=task.id,
                event_type="task_created",
                actor=task.owner or "",
                payload={
                    "status": status,
                    "parent_id": task.parent_id,
                },
            )

    def get_task(self, task_id: str) -> Mapping[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    t.id,
                    t.description,
                    t.description AS prompt,
                    t.owner,
                    t.parent_id,
                    t.status,
                    t.result,
                    t.created_at,
                    t.updated_at,
                    r.status AS review_status,
                    r.feedback AS review_feedback,
                    r.revision_task_id
                FROM tasks AS t
                LEFT JOIN reviews AS r
                  ON r.task_id = t.id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()

        if row is None:
            raise RecordNotFoundError("Task not found")

        return dict(row)

    def transition_task(self, task_id: str, status: str, *, expected: str,
                        owner: Optional[str] = None, result: str = "") -> Mapping[str, object]:
        """Atomically advance a technical task state and audit the transition."""
        target = TaskStatus(status).value
        if not isinstance(result, str) or len(result) > 4000:
            raise ValueError("Invalid task result")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("Invalid task owner")
        with self._transaction() as connection:
            row = connection.execute("SELECT status, owner FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise RecordNotFoundError("Task not found")
            if row["status"] != expected:
                raise ConflictError("Task status changed")
            validate_task_transition(expected, target)
            if owner is not None and expected != TaskStatus.READY.value:
                raise ValueError("Owner may only be assigned to a ready task")
            if target == TaskStatus.ASSIGNED.value and not (owner or row["owner"]):
                raise ValueError("Assigned task requires an owner")
            connection.execute(
                "UPDATE tasks SET status = ?, owner = ?, result = ?, updated_at = ? WHERE id = ?",
                (target, owner or row["owner"], result, _utc_now(), task_id))
            if target == TaskStatus.SUCCESS.value:
                connection.execute(
                    "INSERT INTO reviews(task_id, status, feedback, created_at, updated_at) "
                    "VALUES (?, 'pending', '', ?, ?)", (task_id, _utc_now(), _utc_now()))
            self._insert_event(connection, task_id=task_id, event_type="task_transition",
                               actor=owner or row["owner"] or "",
                               payload={"from": expected, "to": target, "result": result[:500]})
        return self.get_task(task_id)

    def list_children(
        self,
        parent_id: str,
    ) -> Sequence[Mapping[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    t.id,
                    t.description,
                    t.owner,
                    t.parent_id,
                    t.status,
                    t.result,
                    r.status AS review_status,
                    r.revision_task_id
                FROM tasks AS t
                LEFT JOIN reviews AS r
                  ON r.task_id = t.id
                WHERE t.parent_id = ?
                ORDER BY t.created_at, t.id
                """,
                (parent_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def list_tasks(self, limit: int = 100) -> Sequence[Mapping[str, object]]:
        """Bounded, deterministic task/review view for the local interface."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Task limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, t.description AS prompt,
                       r.status AS review_status, r.feedback AS review_feedback,
                       r.revision_task_id
                FROM tasks AS t LEFT JOIN reviews AS r ON r.task_id = t.id
                ORDER BY t.created_at DESC, t.id DESC LIMIT ?
                """, (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_board_tasks(self, limit: int = 100) -> Sequence[Mapping[str, object]]:
        """Tasks still visible on the board; clearing is recorded in the event log."""
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Task limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.*, t.description AS prompt,
                          r.status AS review_status, r.feedback AS review_feedback,
                          r.revision_task_id
                   FROM tasks AS t LEFT JOIN reviews AS r ON r.task_id = t.id
                   WHERE NOT EXISTS (SELECT 1 FROM events AS e
                                     WHERE e.task_id = t.id AND e.event_type = 'board_cleared')
                   ORDER BY t.created_at DESC, t.id DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_finished_board(self) -> Mapping[str, int]:
        """Remove terminal task cards from the board without touching task records."""
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT t.id FROM tasks AS t
                   WHERE t.status IN ('success', 'failed', 'blocked')
                     AND NOT EXISTS (SELECT 1 FROM events AS e
                                     WHERE e.task_id = t.id AND e.event_type = 'board_cleared')"""
            ).fetchall()
            for row in rows:
                self._insert_event(connection, task_id=row["id"],
                                   event_type="board_cleared", actor="human")
        return {"cleared_tasks": len(rows)}

    def terminate_task(self, task_id: str) -> Mapping[str, object]:
        """Block an active root and its children, and remove its scoped chat history."""
        with self._transaction() as connection:
            root = connection.execute("SELECT id, status, parent_id FROM tasks WHERE id = ?",
                                      (task_id,)).fetchone()
            if root is None:
                raise RecordNotFoundError("Task not found")
            if root["parent_id"] or root["status"] in ("success", "failed", "blocked"):
                raise ConflictError("Only an active root task can be terminated")
            rows = connection.execute(
                "WITH RECURSIVE family(id) AS (SELECT id FROM tasks WHERE id = ? "
                "UNION ALL SELECT t.id FROM tasks t JOIN family f ON t.parent_id = f.id) "
                "SELECT id FROM family", (task_id,)).fetchall()
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            now = _utc_now()
            connection.execute(
                f"UPDATE tasks SET status = 'blocked', result = 'Terminated by human', updated_at = ? "
                f"WHERE id IN ({placeholders}) AND status NOT IN ('success', 'failed', 'blocked')",
                (now, *ids))
            connection.execute(
                f"UPDATE approval_requests SET status = 'denied', answered_at = ? "
                f"WHERE task_id IN ({placeholders}) AND status = 'pending'", (now, *ids))
            connection.execute(
                f"UPDATE actions SET status = 'blocked', completed_at = ? "
                f"WHERE task_id IN ({placeholders}) AND status IN ('requested', 'running')",
                (now, *ids))
            removed = connection.execute(
                f"DELETE FROM messages WHERE task_id IN ({placeholders})", ids).rowcount
            self._insert_event(connection, task_id=task_id, event_type="task_terminated",
                               actor="human", payload={"cleared_messages": removed})
        return {"task_id": task_id, "status": "blocked", "cleared_messages": removed}

    def list_pending_requests(self) -> Sequence[Mapping[str, object]]:
        """Read pending requests; answering/resuming remains C's responsibility."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, task_id, agent, kind, action, payload_json, status, created_at
                   FROM approval_requests WHERE status = 'pending'
                   ORDER BY created_at, id LIMIT 100"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = _decode_json(item.pop("payload_json"))
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def save_message(self, message: Message) -> None:
        now = _utc_now()

        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO messages(
                        id,
                        kind,
                        sender,
                        recipient,
                        task_id,
                        content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        message.kind,
                        message.sender,
                        message.recipient,
                        message.task_id,
                        message.content,
                        now,
                    ),
                )

                if message.task_id:
                    self._insert_event(
                        connection,
                        task_id=message.task_id,
                        event_type="message_created",
                        actor=message.sender,
                        payload={
                            "message_id": message.id,
                            "kind": message.kind,
                            "recipient": message.recipient,
                        },
                    )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "Message could not be created"
            ) from exc

    def list_direct_messages(
        self,
        human: str,
        agent: str,
    ) -> Sequence[Mapping[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM messages
                WHERE kind = 'dm'
                  AND (
                      (sender = ? AND recipient = ?)
                      OR
                      (sender = ? AND recipient = ?)
                  )
                ORDER BY created_at, id
                """,
                (human, agent, agent, human),
            ).fetchall()

        return [dict(row) for row in rows]

    def list_group_messages(
        self,
    ) -> Sequence[Mapping[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM messages
                WHERE kind = 'group'
                ORDER BY created_at, id
                """
            ).fetchall()

        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Events / trace
    # ------------------------------------------------------------------

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: Optional[str],
        event_type: str,
        actor: str = "",
        payload: Optional[Mapping[str, object]] = None,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO events(
                task_id,
                event_type,
                actor,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                task_id,
                event_type,
                actor,
                _json_object(payload),
                _utc_now(),
            ),
        )
        return int(cursor.lastrowid)

    def append_event(
        self,
        *,
        task_id: str,
        event_type: str,
        actor: str = "",
        payload: Optional[Mapping[str, object]] = None,
    ) -> int:
        with self._transaction() as connection:
            try:
                return self._insert_event(
                    connection,
                    task_id=task_id,
                    event_type=event_type,
                    actor=actor,
                    payload=payload,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "Event could not be created"
                ) from exc

    def list_events(
        self,
        task_id: str,
    ) -> Sequence[Mapping[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    task_id,
                    event_type,
                    actor,
                    payload_json,
                    created_at
                FROM events
                WHERE task_id = ?
                ORDER BY id
                """,
                (task_id,),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)
            item["payload"] = _decode_json(
                item.pop("payload_json")
            )
            result.append(item)

        return result

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def save_action(
        self,
        *,
        action_id: str,
        task_id: str,
        agent: str,
        tool_name: str,
        arguments: Mapping[str, object],
        status: str = "requested",
    ) -> None:
        now = _utc_now()

        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO actions(
                        id,
                        task_id,
                        agent,
                        tool_name,
                        arguments_json,
                        status,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        task_id,
                        agent,
                        tool_name,
                        _json_object(arguments),
                        status,
                        now,
                    ),
                )

                self._insert_event(
                    connection,
                    task_id=task_id,
                    event_type="action_created",
                    actor=agent,
                    payload={
                        "action_id": action_id,
                        "tool_name": tool_name,
                        "status": status,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "Action could not be created"
            ) from exc

    # ------------------------------------------------------------------
    # Approval and clarification persistence
    # ------------------------------------------------------------------

    def save_approval_request(
        self,
        *,
        request_id: str,
        task_id: str,
        agent: str,
        kind: str,
        action: str = "",
        payload: Optional[Mapping[str, object]] = None,
    ) -> None:
        if kind not in ("approval", "clarification"):
            raise ValueError("Invalid request kind")

        now = _utc_now()

        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO approval_requests(
                        id,
                        task_id,
                        agent,
                        kind,
                        action,
                        payload_json,
                        status,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        request_id,
                        task_id,
                        agent,
                        kind,
                        action or None,
                        _json_object(payload),
                        now,
                    ),
                )

                self._insert_event(
                    connection,
                    task_id=task_id,
                    event_type="request_created",
                    actor=agent,
                    payload={
                        "request_id": request_id,
                        "kind": kind,
                        "action": action,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "Request could not be created"
            ) from exc

    def get_approval_request(
        self,
        request_id: str,
    ) -> Mapping[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM approval_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()

        if row is None:
            raise RecordNotFoundError(
                "Approval request not found"
            )

        item = dict(row)
        item["payload"] = _decode_json(
            item.pop("payload_json")
        )
        item["answer"] = _decode_json(
            item.pop("answer_json")
        )
        return item

    # ------------------------------------------------------------------
    # Atomic critical actions and human decisions
    # ------------------------------------------------------------------

    def _active_task(self, connection, task_id, agent=None):
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Task not found")
        if row["status"] in ("success", "failed", "blocked"):
            raise ConflictError("Task is terminal")
        if agent is not None and row["owner"] != agent:
            raise ConflictError("Action owner does not match task")
        return row

    def prepare_critical_action(self, task_id, agent, tool_name, payload):
        """Deduplicate and persist the exact pending action and approval atomically."""
        encoded = _json_object(payload)
        with self._transaction() as connection:
            task = self._active_task(connection, task_id, agent)
            row = connection.execute(
                """SELECT id FROM approval_requests WHERE task_id=? AND agent=?
                   AND kind='approval' AND action=? AND payload_json=? ORDER BY created_at LIMIT 1""",
                (task_id, agent, tool_name, encoded)).fetchone()
            if row:
                request_id = row["id"]
            else:
                if task["status"] != "running":
                    raise ConflictError("Only a running task may request a new critical action")
                request_id = str(uuid.uuid4())
                now = _utc_now()
                connection.execute(
                    """INSERT INTO actions(id,task_id,agent,tool_name,arguments_json,status,created_at)
                       VALUES(?,?,?,?,?,'requested',?)""",
                    (request_id, task_id, agent, tool_name, encoded, now))
                connection.execute(
                    """INSERT INTO approval_requests
                       (id,task_id,agent,kind,action,payload_json,status,created_at)
                       VALUES(?,?,?,'approval',?,?,'pending',?)""",
                    (request_id, task_id, agent, tool_name, encoded, now))
                connection.execute("UPDATE tasks SET status='waiting_human',updated_at=? WHERE id=?",
                                   (now, task_id))
                self._insert_event(connection, task_id=task_id, event_type="request_created", actor=agent,
                                   payload={"request_id": request_id, "action": tool_name,
                                            "arguments": payload, "status": "pending"})
        return request_id

    def _expire_action_request(self, connection, row, max_age_seconds):
        if (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds() < max_age_seconds:
            return False
        now = _utc_now()
        connection.execute("UPDATE approval_requests SET status='expired',answered_at=? WHERE id=?",
                           (now, row["id"]))
        connection.execute("UPDATE actions SET status='blocked',completed_at=? WHERE id=? AND status='requested'",
                           (now, row["id"]))
        connection.execute("UPDATE tasks SET status='blocked',updated_at=? WHERE id=?", (now, row["task_id"]))
        self._insert_event(connection, task_id=row["task_id"], event_type="request_expired", actor="system",
                           payload={"request_id": row["id"]})
        return True

    def answer_request(self, request_id, response, *, max_age_seconds=3600):
        """One human answer and audit event; stale/duplicate decisions never write."""
        if not isinstance(response, Mapping):
            raise ValueError("Invalid human response")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise RecordNotFoundError("Approval request not found")
            self._active_task(connection, row["task_id"])
            if row["status"] != "pending":
                raise ConflictError("Request already answered")
            if row["kind"] == "approval":
                if set(response) != {"approved"} or type(response["approved"]) is not bool:
                    raise ValueError("Approval requires an explicit boolean")
                status = "approved" if response["approved"] else "denied"
            else:
                if (set(response) != {"answer"} or not isinstance(response["answer"], str)
                        or not response["answer"].strip() or len(response["answer"]) > 4000):
                    raise ValueError("Clarification requires bounded text")
                status = "answered"
            if not self._expire_action_request(connection, row, max_age_seconds):
                now = _utc_now()
                connection.execute(
                    "UPDATE approval_requests SET status=?,answered_at=?,answer_json=? WHERE id=?",
                    (status, now, _json_object(response), request_id))
                if status == "denied":
                    connection.execute("UPDATE actions SET status='blocked',completed_at=? WHERE id=? AND status='requested'",
                                       (now, request_id))
                    connection.execute("UPDATE tasks SET status='blocked',updated_at=? WHERE id=?", (now, row["task_id"]))
                self._insert_event(connection, task_id=row["task_id"], event_type="request_answered", actor="human",
                                   payload={"request_id": request_id, "status": status})
        return self.get_approval_request(request_id)

    def expire_request_if_due(self, request_id, *, max_age_seconds=3600):
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise RecordNotFoundError("Approval request not found")
            action = connection.execute("SELECT status FROM actions WHERE id=?", (request_id,)).fetchone()
            task = connection.execute("SELECT status FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if (row["status"] in ("pending", "approved") and (action is None or action["status"] == "requested")
                    and task["status"] not in ("success", "failed", "blocked")):
                self._expire_action_request(connection, row, max_age_seconds)
        return self.get_approval_request(request_id)

    def claim_approved_action(self, request_id, task_id, agent, tool_name, payload, *, max_age_seconds=3600):
        """Atomically consume approval once; never execute a changed or replayed action."""
        claimed = False
        with self._transaction() as connection:
            self._active_task(connection, task_id, agent)
            row = connection.execute("SELECT * FROM approval_requests WHERE id=?", (request_id,)).fetchone()
            if (row is None or row["kind"] != "approval" or row["task_id"] != task_id
                    or row["agent"] != agent or row["action"] != tool_name
                    or row["payload_json"] != _json_object(payload) or row["status"] != "approved"):
                raise ConflictError("Approval does not authorize this action")
            action = connection.execute("SELECT * FROM actions WHERE id=?", (request_id,)).fetchone()
            if (action is None or action["status"] != "requested"
                    or action["arguments_json"] != row["payload_json"]):
                raise ConflictError("Approval already consumed or action unavailable")
            if not self._expire_action_request(connection, row, max_age_seconds):
                now = _utc_now()
                connection.execute("UPDATE actions SET status='running' WHERE id=?", (request_id,))
                connection.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (now, task_id))
                self._insert_event(connection, task_id=task_id, event_type="action_started", actor=agent,
                                   payload={"action_id": request_id, "tool_name": tool_name})
                claimed = True
        return claimed

    def start_action(self, task_id, agent, tool_name, arguments):
        with self._transaction() as connection:
            task = self._active_task(connection, task_id, agent)
            if task["status"] != "running":
                raise ConflictError("Task is not running")
            action_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO actions(id,task_id,agent,tool_name,arguments_json,status,created_at)
                   VALUES(?,?,?,?,?,'running',?)""",
                (action_id, task_id, agent, tool_name, _json_object(arguments), _utc_now()))
            self._insert_event(connection, task_id=task_id, event_type="action_started", actor=agent,
                               payload={"action_id": action_id, "tool_name": tool_name, "arguments": arguments})
        return action_id

    def finish_action(self, action_id, *, succeeded, result):
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise ConflictError("Action is not running")
            status = "success" if succeeded else "failed"
            now = _utc_now()
            connection.execute("UPDATE actions SET status=?,result_json=?,completed_at=? WHERE id=?",
                               (status, _json_object(result), now, action_id))
            if not succeeded:
                connection.execute("UPDATE tasks SET status='failed',updated_at=? WHERE id=?", (now, row["task_id"]))
            self._insert_event(connection, task_id=row["task_id"], event_type="action_completed", actor=row["agent"],
                               payload={"action_id": action_id, "status": status, "result": result})

    def get_action(self, action_id):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Action not found")
        item = dict(row)
        item["arguments"] = _decode_json(item.pop("arguments_json"))
        item["result"] = _decode_json(item.pop("result_json"))
        return item

    # ------------------------------------------------------------------
    # Atomic human review and revision
    # ------------------------------------------------------------------

    def decide_review(
        self,
        task_id: str,
        *,
        expected: str,
        decision: str,
        feedback: str,
    ) -> Mapping[str, object]:
        if decision not in (
            ReviewStatus.ACCEPTED.value,
            ReviewStatus.CHANGES_REQUESTED.value,
        ):
            raise ValueError("Invalid review decision")

        feedback = feedback.strip()

        if (
            decision == ReviewStatus.CHANGES_REQUESTED.value
            and not feedback
        ):
            raise ValueError(
                "Changes requested requires feedback"
            )

        now = _utc_now()

        with self._transaction() as connection:
            task = connection.execute(
                """
                SELECT status
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()

            if task is None:
                raise RecordNotFoundError("Task not found")

            if task["status"] != TaskStatus.SUCCESS.value:
                raise ConflictError(
                    "Only successful tasks may be reviewed"
                )

            cursor = connection.execute(
                """
                UPDATE reviews
                SET
                    status = ?,
                    feedback = ?,
                    decided_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                  AND status = ?
                """,
                (
                    decision,
                    feedback,
                    now,
                    now,
                    task_id,
                    expected,
                ),
            )

            if cursor.rowcount != 1:
                raise ConflictError(
                    "Review state changed"
                )

            self._insert_event(
                connection,
                task_id=task_id,
                event_type="review_decided",
                actor="human",
                payload={
                    "previous_status": expected,
                    "decision": decision,
                    "feedback": feedback,
                },
            )

        return self.get_task(task_id)

    def link_revision(
        self,
        task_id: str,
        *,
        expected: str,
        prompt: str,
    ) -> Mapping[str, object]:
        prompt = prompt.strip()

        if not prompt:
            raise ValueError("Revision prompt is required")

        revision_task_id = "revision-" + uuid.uuid4().hex
        now = _utc_now()

        with self._transaction() as connection:
            original = connection.execute(
                """
                SELECT
                    t.owner,
                    t.status,
                    r.status AS review_status,
                    r.revision_task_id
                FROM tasks AS t
                JOIN reviews AS r
                  ON r.task_id = t.id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()

            if original is None:
                raise RecordNotFoundError("Task not found")

            if original["status"] != TaskStatus.SUCCESS.value:
                raise ConflictError(
                    "Only successful tasks may be revised"
                )

            if original["review_status"] != expected:
                raise ConflictError(
                    "Review state changed"
                )

            if original["revision_task_id"] is not None:
                raise ConflictError(
                    "Revision already exists"
                )

            connection.execute(
                """
                INSERT INTO tasks(
                    id,
                    description,
                    owner,
                    parent_id,
                    status,
                    result,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 'created', '', ?, ?)
                """,
                (
                    revision_task_id,
                    prompt,
                    original["owner"],
                    task_id,
                    now,
                    now,
                ),
            )

            cursor = connection.execute(
                """
                UPDATE reviews
                SET
                    status = 'revising',
                    revision_task_id = ?,
                    updated_at = ?
                WHERE task_id = ?
                  AND status = ?
                  AND revision_task_id IS NULL
                """,
                (
                    revision_task_id,
                    now,
                    task_id,
                    expected,
                ),
            )

            if cursor.rowcount != 1:
                raise ConflictError(
                    "Revision link conflict"
                )

            self._insert_event(
                connection,
                task_id=revision_task_id,
                event_type="task_created",
                actor=original["owner"] or "",
                payload={
                    "status": TaskStatus.CREATED.value,
                    "parent_id": task_id,
                },
            )

            self._insert_event(
                connection,
                task_id=task_id,
                event_type="revision_linked",
                actor="human",
                payload={
                    "revision_task_id": revision_task_id,
                },
            )

        return self.get_task(revision_task_id)

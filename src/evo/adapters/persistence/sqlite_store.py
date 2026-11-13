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

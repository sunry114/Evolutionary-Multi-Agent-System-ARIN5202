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


MIGRATIONS = ("0001_initial", "0002_issues", "0003_routing", "0004_delegations", "0005_workbench",
              "0006_squad_routing")

ANCESTORS = """
    WITH RECURSIVE chain AS (
        SELECT id, parent_id, owner, status FROM tasks WHERE id = ?
        UNION ALL
        SELECT t.id, t.parent_id, t.owner, t.status FROM tasks t JOIN chain c ON t.id = c.parent_id
    )
    SELECT * FROM chain
"""

DESCENDANTS = """
    WITH RECURSIVE tree(id) AS (
        SELECT id FROM tasks WHERE id = ?
        UNION ALL
        SELECT t.id FROM tasks t JOIN tree ON t.parent_id = tree.id
    )
"""

ISSUE_TREE = """
    WITH RECURSIVE edges(parent_id, child_id) AS (
        SELECT parent_id, id FROM tasks WHERE parent_id IS NOT NULL
        UNION ALL
        SELECT task_id, revision_task_id FROM reviews WHERE revision_task_id IS NOT NULL
    ),
    tree(id) AS (
        SELECT id FROM tasks WHERE id = ?
        UNION
        SELECT e.child_id FROM edges e JOIN tree ON e.parent_id = tree.id
    )
"""


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
        with self._transaction() as connection:
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

            if version > len(MIGRATIONS):
                raise StorageError(
                    "Database schema is newer than this application"
                )

            migrations = files("evo.adapters.persistence").joinpath("migrations")
            for name in MIGRATIONS[version:]:
                script = migrations.joinpath(name + ".sql").read_text(encoding="utf-8")
                statement = ""
                for line in script.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise StorageError("Incomplete SQLite migration")

            final_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]

            if final_version != len(MIGRATIONS):
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

    def save_task(self, task: Task, *, issue_config: Optional[Mapping[str, object]] = None,
                  from_group: bool = False) -> None:
        with self._transaction() as connection:
            self._insert_task(connection, task)
            if issue_config is not None:
                if from_group:
                    updated = connection.execute(
                        "UPDATE messages SET task_id = ? WHERE kind = 'group' AND task_id IS NULL",
                        (task.id,))
                    if updated.rowcount == 0:
                        raise ValueError("No unassigned group discussion")
                self._insert_event(connection, task_id=task.id, event_type="issue_config",
                                   actor="human", payload=issue_config)

    def _insert_task(self, connection, task):
        now = _utc_now()
        status = TaskStatus(task.status).value

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
                    updated_at,
                    assignee_type,
                    assignee_id,
                    routing_mode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    task.assignee_type,
                    task.assignee_id,
                    task.routing_mode,
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
                    t.assignee_type,
                    t.assignee_id,
                    t.routing_mode,
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

    def claim_start(self, task_id: str, run_config: Mapping[str, object], *, expected_updated_at: str) -> bool:
        """Only the caller that records the run configuration may begin planning."""
        try:
            self.transition_task(task_id, "planning", expected="created", run_config=run_config,
                                 expected_updated_at=expected_updated_at)
        except ConflictError:
            return False
        return True

    def assign_issue(self, task_id, assignee_type, assignee_id, routing_mode):
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT assignee_type, assignee_id, routing_mode FROM tasks "
                "WHERE id = ? AND parent_id IS NULL AND status = 'created'", (task_id,)).fetchone()
            if current is None:
                raise ConflictError("Only a draft issue or revision may be assigned")
            connection.execute(
                "UPDATE tasks SET assignee_type = ?, assignee_id = ?, routing_mode = ?, updated_at = ? WHERE id = ?",
                (assignee_type, assignee_id, routing_mode, _utc_now(), task_id))
            self._insert_event(connection, task_id=task_id, event_type="issue_assigned", actor="human",
                               payload={"previous": dict(current), "assignee_type": assignee_type,
                                        "assignee_id": assignee_id, "routing_mode": routing_mode})
        return self.get_task(task_id)

    def transition_task(self, task_id: str, status: str, *, expected: str,
                        owner: Optional[str] = None, result: str = "",
                        run_config: Optional[Mapping[str, object]] = None,
                        assignment: Optional[Mapping[str, object]] = None,
                        expected_updated_at: Optional[str] = None,
                        resume_id: Optional[str] = None) -> Mapping[str, object]:
        """Atomically advance a technical task state and audit the transition."""
        target = TaskStatus(status).value
        if not isinstance(result, str) or len(result) > 4000:
            raise ValueError("Invalid task result")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("Invalid task owner")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status, owner, parent_id, description, updated_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise RecordNotFoundError("Task not found")
            if row["status"] != expected or (expected_updated_at is not None and row["updated_at"] != expected_updated_at):
                raise ConflictError("Task status changed")
            validate_task_transition(expected, target)
            if owner is not None and expected != TaskStatus.READY.value:
                raise ValueError("Owner may only be assigned to a ready task")
            if target == TaskStatus.ASSIGNED.value and not (owner or row["owner"]):
                raise ValueError("Assigned task requires an owner")
            if run_config is not None:
                if (expected, target) != ("created", "planning"):
                    raise ValueError("Run configuration is only recorded at start")
                self._insert_event(connection, task_id=task_id, event_type="run_config",
                                   actor="orchestrator", payload=run_config)
            description = row["description"]
            if assignment is not None:
                if target != TaskStatus.ASSIGNED.value or not owner or not row["parent_id"]:
                    raise ValueError("A negotiated assignment requires a ready subtask and owner")
                description = assignment["description"]
                self._insert_event(connection, task_id=row["parent_id"], event_type="assignment_confirmed",
                                   actor=owner, payload={**assignment, "task_id": task_id, "owner": owner})
            connection.execute(
                "UPDATE tasks SET status = ?, owner = ?, description = ?, result = ?, updated_at = ? WHERE id = ?",
                (target, owner or row["owner"], description, result, _utc_now(), task_id))
            if target == TaskStatus.SUCCESS.value:
                connection.execute(
                    "INSERT INTO reviews(task_id, status, feedback, created_at, updated_at) "
                    "VALUES (?, 'pending', '', ?, ?)", (task_id, _utc_now(), _utc_now()))
            self._insert_event(connection, task_id=task_id, event_type="task_transition",
                               actor=owner or row["owner"] or "",
                               payload={"from": expected, "to": target, "result": result[:500]})
            if resume_id is not None:
                self._insert_event(connection, task_id=task_id, event_type="resume_claimed",
                                   actor="orchestrator", payload={"resume_id": resume_id})
        return self.get_task(task_id)

    def task_chain(self, task_id: str):
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(ANCESTORS, (task_id,))]

    def describe_wait(self, task_id: str, reason: str):
        with self._transaction() as connection:
            connection.execute("UPDATE tasks SET result = ?, updated_at = ? WHERE id = ? AND status = 'waiting_human'",
                               (reason[:4000], _utc_now(), task_id))

    def pending_in_tree(self, root_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                DESCENDANTS + "SELECT 1 FROM approval_requests a JOIN tree ON a.task_id = tree.id "
                "JOIN tasks t ON t.id = a.task_id WHERE a.status = 'pending' "
                "AND t.status IN ('running', 'waiting_human', 'planning', 'ready') LIMIT 1", (root_id,)
            ).fetchone() is not None

    def register_delegation(self, delegation_id, parent_id, sender, recipient, description, check,
                            *, max_depth, max_tasks, replay):
        with self._transaction() as connection:
            chain = connection.execute(ANCESTORS, (parent_id,)).fetchall()
            if not chain or chain[0]["owner"] != sender:
                raise ConflictError("Only the task owner can delegate")
            existing = connection.execute(
                "SELECT outcome_json FROM delegations WHERE id = ?", (delegation_id,)).fetchone()
            if existing is not None:
                return _decode_json(existing["outcome_json"])
            if replay or chain[0]["status"] != "running":
                raise ConflictError("No matching delegation for this task state")
            if recipient in {row["owner"] for row in chain}:
                raise ValueError("Cannot delegate to an active ancestor or yourself")
            root_id = chain[-1]["id"]
            # One edge belongs to the issue's planned subtask, the rest are delegations.
            total = connection.execute(DESCENDANTS + "SELECT count(*) FROM tree", (root_id,)).fetchone()[0]
            if len(chain) - 1 > max_depth or total >= max_tasks:
                raise ValueError("Delegation budget exceeded")

            self._insert_task(connection, Task(delegation_id, description, parent_id=parent_id))
            connection.execute(
                "INSERT INTO delegations(id, issue_id, sender, recipient, check_json) VALUES (?, ?, ?, ?, ?)",
                (delegation_id, root_id, sender, recipient, _json_object(check)))
            payload = {"delegation_id": delegation_id, "parent_id": parent_id, "from": sender, "to": recipient,
                       "description": description}
            self._insert_event(connection, task_id=root_id, event_type="delegation_requested",
                               actor=sender, payload=payload)
            self._insert_message(connection, Message(delegation_id + "-request", "agent", sender,
                                                     description, recipient, delegation_id))
        return None

    def get_delegation(self, delegation_id: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT d.*, t.parent_id FROM delegations d JOIN tasks t ON d.id = t.id WHERE d.id = ?",
                (delegation_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Delegation not found")
        return {**dict(row), "check": _decode_json(row["check_json"]),
                "outcome": _decode_json(row["outcome_json"])}

    def complete_delegation(self, delegation_id: str, artifact):
        """Publish one durable result to the original requester after the child terminates."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT d.*, t.owner, t.status, t.result, t.parent_id FROM delegations d "
                "JOIN tasks t ON d.id = t.id WHERE d.id = ?", (delegation_id,)).fetchone()
            if row is None or row["status"] not in ("success", "failed", "blocked"):
                raise ConflictError("Delegation has not completed")
            if row["outcome_json"] is not None:
                return _decode_json(row["outcome_json"])
            if row["status"] == "success" and artifact is None:
                raise ValueError("Successful delegation requires shared evidence")
            speaker = row["owner"] or "orchestrator"
            outcome = {"id": delegation_id, "owner": row["owner"], "status": row["status"],
                       "result": row["result"], "artifact": artifact}
            connection.execute("UPDATE delegations SET outcome_json = ? WHERE id = ?",
                               (_json_object(outcome), delegation_id))
            self._insert_event(connection, task_id=row["issue_id"], event_type="delegation_completed",
                               actor=speaker, payload={**outcome, "parent_id": row["parent_id"], "to": row["sender"]})
            self._insert_message(connection, Message(delegation_id + "-result", "agent", speaker,
                                                     _json_object(outcome), row["sender"], delegation_id))
        return outcome

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
        """Bounded, deterministic task and review records."""
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

    def list_issues(self, limit=100, *, owner=None, owner_type=None):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Issue limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute("""
                WITH RECURSIVE runs(issue_id, id) AS (
                    SELECT t.id, t.id FROM tasks t
                    LEFT JOIN reviews preceding ON preceding.revision_task_id = t.id
                    WHERE t.parent_id IS NULL AND preceding.task_id IS NULL
                    UNION ALL
                    SELECT runs.issue_id, r.revision_task_id FROM runs
                    JOIN reviews r ON r.task_id = runs.id WHERE r.revision_task_id IS NOT NULL
                ), current(issue_id, id) AS (
                    SELECT runs.issue_id, runs.id FROM runs
                    LEFT JOIN reviews upcoming ON upcoming.task_id = runs.id
                    WHERE upcoming.revision_task_id IS NULL
                ), workers(issue_id, id) AS (
                    SELECT issue_id, id FROM current
                    UNION ALL
                    SELECT workers.issue_id, t.id FROM workers JOIN tasks t ON t.parent_id = workers.id
                )
                SELECT original.id, original.description AS prompt, latest.id AS current_task_id,
                       latest.status, latest.result, latest.assignee_id, latest.assignee_type,
                       latest.routing_mode, latest.updated_at, r.status AS review_status
                FROM current JOIN tasks original ON original.id = current.issue_id
                JOIN tasks latest ON latest.id = current.id LEFT JOIN reviews r ON r.task_id = latest.id
                WHERE :owner IS NULL
                    OR (latest.assignee_id = :owner AND (:owner_type IS NULL OR latest.assignee_type = :owner_type))
                    OR (COALESCE(:owner_type, 'agent') = 'agent' AND EXISTS (
                        SELECT 1 FROM workers JOIN tasks worker ON worker.id = workers.id
                        WHERE workers.issue_id = original.id AND worker.owner = :owner
                    ))
                ORDER BY latest.updated_at DESC, original.id DESC LIMIT :limit
                """, {"owner": owner, "owner_type": owner_type, "limit": limit}).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _issue_id(connection, task_id):
        row = connection.execute("""
            WITH RECURSIVE chain(id, upstream_id) AS (
                SELECT t.id, COALESCE(t.parent_id, r.task_id)
                FROM tasks t LEFT JOIN reviews r ON r.revision_task_id = t.id WHERE t.id = ?
                UNION ALL
                SELECT t.id, COALESCE(t.parent_id, r.task_id) FROM chain
                JOIN tasks t ON t.id = chain.upstream_id LEFT JOIN reviews r ON r.revision_task_id = t.id
            )
            SELECT id FROM chain WHERE upstream_id IS NULL
            """, (task_id,)).fetchone()
        if row is None:
            raise RecordNotFoundError("Issue not found")
        return row["id"]

    def issue_id(self, task_id):
        with self._connect() as connection:
            return self._issue_id(connection, task_id)

    def issue_records(self, task_id):
        """Read one issue family, including revisions and nested tasks, in one snapshot."""
        with self._connect() as connection:
            connection.execute("BEGIN")
            issue_id = self._issue_id(connection, task_id)
            def rows(query):
                return [dict(row) for row in connection.execute(ISSUE_TREE + query, (issue_id,))]

            tasks = rows("""
                SELECT t.*, t.description AS prompt, r.status AS review_status, r.feedback AS review_feedback,
                       r.revision_task_id
                FROM tree JOIN tasks t ON t.id = tree.id LEFT JOIN reviews r ON r.task_id = t.id
                ORDER BY t.created_at, t.id
                """)
            events = rows("SELECT e.* FROM tree JOIN events e ON e.task_id = tree.id ORDER BY e.id")
            messages = rows("SELECT m.* FROM tree JOIN messages m ON m.task_id = tree.id ORDER BY m.created_at, m.id")
            requests = rows("""
                SELECT a.* FROM tree JOIN approval_requests a ON a.task_id = tree.id ORDER BY a.created_at, a.id
                """)
        for event in events:
            event["payload"] = _decode_json(event.pop("payload_json"))
        for request in requests:
            request["payload"] = _decode_json(request.pop("payload_json"))
            request["answer"] = _decode_json(request.pop("answer_json"))
        return issue_id, tasks, events, messages, requests

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
        try:
            with self._transaction() as connection:
                self._insert_message(connection, message)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Message could not be created") from exc

    def _insert_message(self, connection, message):
        connection.execute(
            "INSERT INTO messages(id, kind, sender, recipient, task_id, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.kind, message.sender, message.recipient,
             message.task_id, message.content, _utc_now()))
        if message.task_id:
            self._insert_event(connection, task_id=message.task_id, event_type="message_created",
                               actor=message.sender, payload={"message_id": message.id, "kind": message.kind,
                                                              "recipient": message.recipient})

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
        self, *, task_id=None,
    ) -> Sequence[Mapping[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM messages
                WHERE kind = 'group' AND task_id IS ?
                ORDER BY created_at, id
                """, (task_id,)
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

    def latest_event_payload(self, task_id: str, event_type: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM events WHERE task_id = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT 1", (task_id, event_type)).fetchone()
        return _decode_json(row[0]) if row else None

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

        try:
            with self._transaction() as connection:
                self._insert_approval_request(connection, request_id, task_id, agent, kind, action, payload)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Request could not be created") from exc

    def _insert_approval_request(self, connection, request_id, task_id, agent, kind, action, payload):
        connection.execute(
            "INSERT INTO approval_requests(id, task_id, agent, kind, action, payload_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (request_id, task_id, agent, kind, action or None, _json_object(payload), _utc_now()))
        self._insert_event(connection, task_id=task_id, event_type="request_created", actor=agent,
                           payload={"request_id": request_id, "kind": kind, "action": action})

    def pause_assignment(self, root_id: str, child_id: str, *, actor: str, question: str, round_number: int) -> None:
        """Publish a clarification; a delegated task's callers park before its issue does."""
        request_id = str(uuid.uuid4())
        with self._transaction() as connection:
            chain = connection.execute(ANCESTORS, (child_id,)).fetchall()
            if len(chain) < 2 or chain[-1]["id"] != root_id or chain[0]["status"] != "ready":
                raise ConflictError("Subtask is no longer ready for assignment")
            if chain[-1]["status"] != "running":
                raise ConflictError("Issue is no longer running")
            result = "Assignment clarification: " + question
            self._insert_approval_request(connection, request_id, root_id, actor, "clarification", "", {"question": question})
            self._insert_event(connection, task_id=root_id, event_type="assignment_clarification_requested", actor=actor,
                               payload={"request_id": request_id, "task_id": child_id, "round": round_number,
                                        "question": question, "from": actor, "to": "human"})
            if len(chain) == 2:
                connection.execute("UPDATE tasks SET status = 'waiting_human', result = ?, updated_at = ? WHERE id = ?",
                                   (result, _utc_now(), root_id))
                self._insert_event(connection, task_id=root_id, event_type="task_transition", actor=actor,
                                   payload={"from": "running", "to": "waiting_human", "result": result[:500]})

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
        reason = "Approval expired: " + row["action"] if row["action"] else "Clarification expired"
        connection.execute("UPDATE tasks SET status='blocked', result=?, updated_at=? WHERE id=?",
                           (reason, now, row["task_id"]))
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
                    connection.execute("UPDATE tasks SET status='blocked', result=?, updated_at=? WHERE id=?",
                                       ("Approval denied: " + row["action"], now, row["task_id"]))
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

    def finish_action(self, action_id, *, succeeded, result, fail_task=True):
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None or row["status"] != "running":
                raise ConflictError("Action is not running")
            status = "success" if succeeded else "failed"
            now = _utc_now()
            connection.execute("UPDATE actions SET status=?,result_json=?,completed_at=? WHERE id=?",
                               (status, _json_object(result), now, action_id))
            if not succeeded and fail_task:
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
                    t.*,
                    r.feedback,
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

            if original["parent_id"] is not None or original["status"] != TaskStatus.SUCCESS.value:
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

            description = (original["description"] + "\n\nReview feedback: " + original["feedback"]
                           + "\nRevision request: " + prompt)
            self._insert_task(connection, Task(revision_task_id, description,
                                               assignee_type=original["assignee_type"],
                                               assignee_id=original["assignee_id"],
                                               routing_mode=original["routing_mode"] if original["assignee_type"] else None))
            settings = {}
            for row in connection.execute(
                    "SELECT payload_json FROM events WHERE task_id = ? "
                    "AND event_type IN ('issue_config', 'run_config', 'plan_created') ORDER BY id", (task_id,)):
                settings.update(_decode_json(row["payload_json"]))
            self._insert_event(connection, task_id=revision_task_id, event_type="issue_config", actor="human",
                               payload={"checks": settings.get("checks", {}),
                                        "memory_enabled": settings.get("memory_enabled", True)})

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
                task_id=task_id,
                event_type="revision_linked",
                actor="human",
                payload={
                    "revision_task_id": revision_task_id,
                },
            )

        return self.get_task(revision_task_id)

"""Physically separate, bounded experience memory for one agent."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from evo.domain.models import Experience
from evo.domain.statuses import TaskStatus


_AGENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_WORDS = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
_STOP = frozenset({"a", "an", "and", "as", "for", "in", "of", "on", "the", "to", "with", "write", "make", "create", "task"})


def _terms(value: str) -> set[str]:
    words = _WORDS.findall(value.lower())
    terms = {word for word in words if word not in _STOP and len(word) > 1}
    for word in words:
        if "\u4e00" <= word[0] <= "\u9fff" and len(word) > 2:
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
    return terms


class SQLiteMemoryStore:
    """One agent's experiences; callers never select another agent in a query."""

    def __init__(self, state_root: Path | str, agent: str):
        if not _AGENT_ID.fullmatch(agent):
            raise ValueError("Invalid agent identifier")

        self.agent = agent
        self.path = (
            Path(state_root)
            / "agents"
            / agent
            / "memory.sqlite3"
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path))

        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_schema (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )

                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS experiences (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task TEXT NOT NULL,
                        outcome TEXT NOT NULL CHECK (
                            outcome IN (
                                'success',
                                'failed',
                                'blocked'
                            )
                        ),
                        lesson TEXT NOT NULL,
                        evidence TEXT NOT NULL DEFAULT '',
                        failure_category TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )

                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_schema(
                        version,
                        applied_at
                    )
                    VALUES (
                        1,
                        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    )
                    """
                )

                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) FROM memory_schema"
            ).fetchone()
            return int(row[0] or 0)

    def add(self, experience: Experience) -> None:
        if (not isinstance(experience, Experience) or experience.agent != self.agent
                or not isinstance(experience.outcome, TaskStatus)
                or experience.outcome not in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.BLOCKED)
                or not isinstance(experience.task, str) or not isinstance(experience.lesson, str)
                or not isinstance(experience.evidence, str)
                or not experience.task.strip() or not experience.lesson.strip()
                or len(experience.task) > 1000 or len(experience.lesson) > 2000
                or len(experience.evidence) > 2000
                or (experience.failure_category is not None and (
                    not isinstance(experience.failure_category, str) or len(experience.failure_category) > 100))):
            raise ValueError("Invalid or cross-agent experience")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO experiences(task,outcome,lesson,evidence,failure_category,created_at) "
                    "VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (experience.task, experience.outcome.value, experience.lesson,
                     experience.evidence, experience.failure_category),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def recall(self, query: str, limit: int = 3) -> tuple[Experience, ...]:
        if not isinstance(query, str) or not isinstance(limit, int) or not 0 <= limit <= 3:
            raise ValueError("Invalid memory query")
        terms = _terms(query)
        if not terms or limit == 0:
            return ()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task,outcome,lesson,evidence,failure_category,created_at,id "
                "FROM experiences ORDER BY id DESC LIMIT 500"
            ).fetchall()
        ranked = []
        seen = set()
        for row in rows:
            key = " ".join(row["lesson"].lower().split())
            overlap = len(terms & _terms(row["task"]))
            if overlap and key not in seen:
                seen.add(key)
                ranked.append((overlap / len(terms), row["id"], row))
        ranked.sort(key=lambda value: (-value[0], -value[1]))
        return tuple(Experience(self.agent, row["task"], TaskStatus(row["outcome"]),
                                row["lesson"], row["evidence"], row["failure_category"])
                     for _, _, row in ranked[:limit])

    def adjustment(self, query: str) -> float:
        weights = {TaskStatus.SUCCESS: 0.25, TaskStatus.FAILED: -0.25,
                   TaskStatus.BLOCKED: -0.1}
        return max(-0.75, min(0.75, sum(weights[item.outcome] for item in self.recall(query))))

    def list_recent(self, limit=20):
        """Bounded human-facing view; does not recall or adjust agent behavior."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid memory list limit")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task,outcome,lesson,evidence,failure_category,created_at "
                "FROM experiences ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row, agent=self.agent) for row in rows]

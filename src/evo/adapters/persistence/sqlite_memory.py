"""Per-agent SQLite memory database reservation for C12."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_AGENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class SQLiteMemoryStore:
    """Creates one physically separate SQLite database per agent.

    C05 only establishes isolation and schema. Relevance ranking, recall,
    bid adjustment and no-memory behavior are implemented by C12.
    """

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

"""SQLite connection helpers."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[sqlite3.Connection]:
    """Run a write unit atomically with an immediate lock."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()

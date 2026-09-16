"""Numbered SQL migration runner."""

import re
import sqlite3
from importlib.resources import files

_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def migrate(connection: sqlite3.Connection) -> int:
    """Apply pending migrations in order and return the resulting version."""
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    directory = files("tgbridge.db.migrations")
    migrations: list[tuple[int, str]] = []
    for item in directory.iterdir():
        match = _MIGRATION_RE.match(item.name)
        if match:
            migrations.append((int(match.group(1)), item.read_text(encoding="utf-8")))

    expected = 1
    for version, sql in sorted(migrations):
        if version != expected:
            raise RuntimeError(f"migration sequence has a gap at version {expected}")
        expected += 1
        if version <= current:
            continue
        script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version={version};\nCOMMIT;"
        connection.executescript(script)
        current = version
    return current

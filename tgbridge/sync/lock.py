"""Exclusive lock so overlapping sync processes skip instead of sharing a session."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def sync_lock_path(session: str | Path) -> Path:
    """Place sync.lock next to the Telethon session file."""
    session_path = Path(session)
    if not str(session_path).endswith(".session"):
        session_path = Path(f"{session_path}.session")
    return session_path.parent / "sync.lock"


@contextmanager
def try_acquire_sync_lock(path: Path) -> Generator[bool]:
    """Non-blocking exclusive flock. Yields False when another sync holds the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

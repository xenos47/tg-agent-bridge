"""Shared Telethon client and secure session construction."""
# pyright: reportArgumentType=false, reportMissingTypeStubs=false

import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import SQLiteSession

_UMASK_LOCK = threading.Lock()


@contextmanager
def _private_umask() -> Generator[None]:
    """Serialize process-global umask changes while SQLite creates a session."""
    with _UMASK_LOCK:
        previous = os.umask(0o177)
        try:
            yield
        finally:
            os.umask(previous)


def _session_filename(session_id: str | Path) -> Path:
    path = Path(session_id)
    return path if str(path).endswith(".session") else Path(f"{path}.session")


def _protect(path: Path) -> None:
    if path.exists():
        path.chmod(0o600)


class ForcedPortSQLiteSession(SQLiteSession):
    """SQLite session that keeps a configured port across DC migrations."""

    def __init__(self, session_id: str | Path, port: int) -> None:
        self._forced_port = port
        filename = _session_filename(session_id)
        try:
            with _private_umask():
                super().__init__(str(session_id))
        finally:
            _protect(filename)

    def set_dc(self, dc_id: int, server_address: str, port: int) -> None:
        del port
        super().set_dc(dc_id, server_address, self._forced_port)
        _protect(Path(self.filename))


def create_client(
    session_id: str | Path,
    api_id: int,
    api_hash: str,
    *,
    port: int = 443,
) -> TelegramClient:
    """Create a client whose session is private before any network operation."""
    session = ForcedPortSQLiteSession(session_id, port)
    client = TelegramClient(session, api_id, api_hash)
    session.set_dc(session.dc_id, session.server_address, port)
    _protect(Path(session.filename))
    return client

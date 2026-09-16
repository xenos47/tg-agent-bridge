"""Shared Telethon client and secure session construction."""
# pyright: reportArgumentType=false, reportGeneralTypeIssues=false, reportMissingTypeStubs=false

import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import SQLiteSession

from tgbridge.logging import classify_connection_error, event, get_logger

_UMASK_LOCK = threading.Lock()
_LOG = get_logger("sync.client")


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
    role: str = "sync",
) -> TelegramClient:
    """Create a client whose session is private before any network operation."""
    session = ForcedPortSQLiteSession(session_id, port)
    client = TelegramClient(session, api_id, api_hash)
    session.set_dc(session.dc_id, session.server_address, port)
    _protect(Path(session.filename))
    event(
        _LOG,
        20,
        "telegram_client_configured",
        role=role,
        transport="tcp_full",
        dc=session.dc_id,
        address=session.server_address,
        port=session.port,
    )
    return client


async def start_client(client: TelegramClient, *, role: str) -> None:
    started = time.perf_counter()
    event(_LOG, 20, "telegram_client_starting", role=role)
    try:
        await client.start()
    except Exception as error:
        error_type, hint = classify_connection_error(error)
        event(
            _LOG,
            40,
            "telegram_client_failed",
            role=role,
            error_type=error_type,
            hint=hint,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise RuntimeError(hint) from error
    event(
        _LOG,
        20,
        "telegram_client_started",
        role=role,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


async def disconnect_client(client: TelegramClient, *, role: str) -> None:
    started = time.perf_counter()
    await client.disconnect()
    event(
        _LOG,
        20,
        "telegram_client_disconnected",
        role=role,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )

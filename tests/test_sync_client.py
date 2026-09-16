import stat
from pathlib import Path
from typing import Any

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from tgbridge.config import Config, TelegramSettings
from tgbridge.outbox import Outbox
from tgbridge.sync.client import ForcedPortSQLiteSession, create_client
from tgbridge.sync.runtime import run_sender, run_sync


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_session_is_private_before_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_path = tmp_path / "new.session"

    class FakeClient:
        def __init__(self, session: ForcedPortSQLiteSession, *_: Any) -> None:
            assert mode(Path(session.filename)) == 0o600
            self.session = session

    monkeypatch.setattr("tgbridge.sync.client.TelegramClient", FakeClient)
    create_client(session_path, 1, "hash", port=5222)
    assert mode(session_path) == 0o600


def test_existing_session_is_repaired_before_client_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_path = tmp_path / "existing.session"
    session = SQLiteSession(str(session_path))
    session.close()
    session_path.chmod(0o644)

    class FakeClient:
        def __init__(self, session: ForcedPortSQLiteSession, *_: Any) -> None:
            assert mode(Path(session.filename)) == 0o600
            self.session = session

    monkeypatch.setattr("tgbridge.sync.client.TelegramClient", FakeClient)
    create_client(session_path, 1, "hash", port=5222)
    assert mode(session_path) == 0o600


def test_port_override_survives_dc_change_and_preserves_auth_key(tmp_path: Path) -> None:
    session_path = tmp_path / "authorized.session"
    session = ForcedPortSQLiteSession(session_path, 5222)
    key = AuthKey(data=b"x" * 256)
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = key
    session.set_dc(4, "149.154.167.91", 80)
    assert session.port == 5222
    assert session.dc_id == 4
    assert session.auth_key is not None
    assert session.auth_key.key == key.key
    session.close()

    reopened = ForcedPortSQLiteSession(session_path, 5222)
    assert reopened.port == 5222
    assert reopened.auth_key is not None
    assert reopened.auth_key.key == key.key
    reopened.close()


def test_client_constructor_failure_leaves_private_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_path = tmp_path / "failed.session"

    def fail(*_: Any, **__: Any) -> None:
        raise RuntimeError("synthetic client failure")

    monkeypatch.setattr("tgbridge.sync.client.TelegramClient", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        create_client(session_path, 1, "hash", port=5222)
    assert mode(session_path) == 0o600


@pytest.mark.asyncio
async def test_sync_and_sender_share_configured_client_factory(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ports: list[int] = []

    class FakeClient:
        async def start(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    def fake_factory(*_: Any, port: int, **__: Any) -> FakeClient:
        ports.append(port)
        return FakeClient()

    monkeypatch.setattr("tgbridge.sync.runtime._credentials", lambda: (1, "hash"))
    monkeypatch.setattr("tgbridge.sync.runtime.create_client", fake_factory)
    config = Config(
        peers=(),
        telegram=TelegramSettings(port=5222),
    )
    await run_sync(db, config, session="unused.session")
    await run_sender(Outbox(db, config), session="unused.session")
    assert ports == [5222, 5222]

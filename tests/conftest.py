import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tgbridge.db import connect, migrate


@pytest.fixture(autouse=True)
def isolate_tgq_path_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in (
        "TGQ_SETTINGS",
        "TGQ_DB",
        "TGQ_CONFIG",
        "TGQ_SESSION",
        "TGQ_TELEGRAM_PORT",
        "TGQ_SYNC_INTERVAL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def reset_project_logger() -> Iterator[None]:
    """configure_logging() binds to the current stderr; drop it so later tests
    never write to a capsys stream that pytest has already closed."""
    yield
    logging.getLogger("tgbridge").handlers.clear()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite")
    migrate(connection)
    yield connection
    connection.close()

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
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite")
    migrate(connection)
    yield connection
    connection.close()

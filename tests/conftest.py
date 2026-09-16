import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tgbridge.db import connect, migrate


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.sqlite")
    migrate(connection)
    yield connection
    connection.close()

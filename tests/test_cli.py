import sqlite3
from pathlib import Path

import pytest

from tests.factories import message, peer
from tgbridge.cli.formatting import render
from tgbridge.cli.main import main
from tgbridge.cli.query import search_messages
from tgbridge.db import connect, migrate


def test_jsonl_matches_golden(db: sqlite3.Connection) -> None:
    peer(db)
    message(db, msg_id=1)
    message(db, msg_id=2, text="release ready", reply_to=1)
    db.execute(
        """
        INSERT INTO tags(peer_id,msg_id,tag,source,created_at)
        VALUES (1,2,'urgent','rule',1700000002)
        """
    )
    rows = search_messages(db)
    actual = render(rows, output_format="jsonl")
    golden = Path(__file__).with_name("golden").joinpath("search_v1.jsonl").read_text()
    assert actual == golden


def test_empty_result_has_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = tmp_path / "empty.sqlite"
    connection = connect(database)
    migrate(connection)
    connection.close()
    assert main(["--db", str(database), "search"]) == 1
    assert capsys.readouterr().out == ""


def test_budget_has_explicit_metadata() -> None:
    rows = [
        {
            "id": f"peer#{index}",
            "peer": "peer",
            "ts": index,
            "from": None,
            "text": "x" * 400,
            "tags": [],
            "reply_to": None,
            "media": None,
        }
        for index in range(5)
    ]
    output = render(rows, output_format="jsonl", max_tokens=120)
    assert '"_meta":{"truncated":' in output
    assert '"format_version":1' in output


def test_output_flags_work_after_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "cli.sqlite"
    connection = connect(database)
    migrate(connection)
    peer(connection)
    message(connection)
    connection.close()
    assert main(["--db", str(database), "search", "--format", "count"]) == 0
    assert capsys.readouterr().out == "1\n"


def test_sync_dry_run_does_not_create_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "dry.sqlite"
    config = tmp_path / "watchlist.yaml"
    config.write_text("peers: []\n")
    monkeypatch.delenv("TGQ_API_ID", raising=False)
    monkeypatch.delenv("TGQ_API_HASH", raising=False)
    result = main(
        [
            "--db",
            str(database),
            "--config",
            str(config),
            "sync",
            "--dry-run",
        ]
    )
    assert result == 4
    assert not database.exists()
    assert "required" in capsys.readouterr().err


def test_cli_package_does_not_import_telethon() -> None:
    cli_root = Path(__file__).parents[1] / "tgbridge" / "cli"
    assert "telethon" not in "".join(path.read_text() for path in cli_root.glob("*.py")).lower()

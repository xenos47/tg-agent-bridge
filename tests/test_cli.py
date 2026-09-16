import sqlite3
from pathlib import Path

import pytest

from tests.factories import message, peer
from tgbridge.cli.formatting import render
from tgbridge.cli.main import main
from tgbridge.cli.query import search_messages
from tgbridge.db import connect, migrate
from tgbridge.sync.models import Peer


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


def test_diagnostic_modes_do_not_change_stdout_or_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "diagnostics.sqlite"
    connection = connect(database)
    migrate(connection)
    peer(connection)
    message(connection)
    connection.close()
    outputs: list[str] = []
    for flag in (None, "--verbose", "--quiet"):
        argv = ["--db", str(database)]
        if flag:
            argv.append(flag)
        argv.append("search")
        assert main(argv) == 0
        captured = capsys.readouterr()
        outputs.append(captured.out)
    assert outputs[0] == outputs[1] == outputs[2]


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


def test_watchlist_resolve_needs_no_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    config.write_text("peers: []\n")

    async def fake_resolve(*_: object, **__: object) -> list[Peer]:
        return [Peer(-100123, "work-chat", "group", "Work Chat")]

    monkeypatch.setattr("tgbridge.sync.runtime.run_resolve", fake_resolve)
    assert main(["--config", str(config), "watchlist", "resolve", "Work Chat"]) == 0
    assert capsys.readouterr().out.startswith("- slug: work-chat\n")
    assert not (tmp_path / "messages.sqlite").exists()


def test_watchlist_resolve_ambiguous_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    config.write_text("peers: []\n")
    original = config.read_text()

    async def fake_resolve(*_: object, **__: object) -> list[Peer]:
        return [
            Peer(-1001, "work-chat", "group", "Work Chat"),
            Peer(-1002, "work-news", "channel", "Work News"),
        ]

    monkeypatch.setattr("tgbridge.sync.runtime.run_resolve", fake_resolve)
    result = main(
        [
            "--config",
            str(config),
            "watchlist",
            "resolve",
            "Work",
            "--write",
        ]
    )
    assert result == 1
    assert "id: -1001" in capsys.readouterr().out
    assert config.read_text() == original


def test_watchlist_resolve_write_dry_run_prints_fragment_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    config.write_text("peers: []\n")
    original = config.read_text()

    async def fake_resolve(*_: object, **__: object) -> list[Peer]:
        return [Peer(-100123, "work-chat", "group", "Work Chat")]

    monkeypatch.setattr("tgbridge.sync.runtime.run_resolve", fake_resolve)
    result = main(
        [
            "--config",
            str(config),
            "watchlist",
            "resolve",
            "Work Chat",
            "--write",
            "--dry-run",
        ]
    )
    assert result == 0
    assert capsys.readouterr().out.startswith("- slug: work-chat\n")
    assert config.read_text() == original


def test_watchlist_resolve_duplicate_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "watchlist.yaml"
    config.write_text(
        "peers:\n"
        "  - slug: existing\n"
        "    id: -100123\n"
        "    kind: group\n"
        "    title: Existing\n"
    )
    original = config.read_text()

    async def fake_resolve(*_: object, **__: object) -> list[Peer]:
        return [Peer(-100123, "resolved", "group", "Resolved")]

    monkeypatch.setattr("tgbridge.sync.runtime.run_resolve", fake_resolve)
    argv = [
        "--config",
        str(config),
        "watchlist",
        "resolve",
        "-100123",
        "--write",
        "--dry-run",
    ]
    assert main(argv) == 2
    assert config.read_text() == original
    assert "already exists" in capsys.readouterr().err

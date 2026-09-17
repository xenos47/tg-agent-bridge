import sqlite3
from pathlib import Path

import pytest

from tests.factories import message, peer
from tgbridge.cli.formatting import render
from tgbridge.cli.main import _apply_user_settings, _parser, main
from tgbridge.cli.query import search_messages
from tgbridge.config import Config
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


def test_read_command_discovers_database_from_default_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "messages.sqlite"
    connection = connect(database)
    migrate(connection)
    connection.close()
    settings = tmp_path / "xdg" / "tgq" / "config.yaml"
    settings.parent.mkdir(parents=True)
    settings.write_text(f"paths:\n  db: {database}\n")

    assert main(["search"]) == 1
    assert capsys.readouterr().out == ""


def test_outbox_command_uses_database_and_watchlist_from_settings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "messages.sqlite"
    connection = connect(database)
    migrate(connection)
    connection.close()
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers: []\n")
    settings = tmp_path / "config.yaml"
    settings.write_text(
        f"paths:\n  db: {database}\n  watchlist: {watchlist}\n"
    )

    assert main(["--settings", str(settings), "outbox", "list"]) == 1
    assert capsys.readouterr().out == ""


def test_explicit_missing_settings_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.yaml"
    assert main(["--settings", str(missing), "search"]) == 2
    assert "settings file does not exist" in capsys.readouterr().err


def test_watchlist_resolve_uses_watchlist_session_and_port_from_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = tmp_path / "config.yaml"
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers: []\n")
    settings.write_text(
        "paths:\n"
        "  watchlist: watchlist.yaml\n"
        "  session: sessions/tgq.session\n"
        "telegram:\n"
        "  port: 5222\n"
    )
    observed: dict[str, object] = {}

    async def fake_resolve(config: Config, query: str, *, session: str) -> list[Peer]:
        observed["port"] = config.telegram.port
        observed["query"] = query
        observed["session"] = session
        return [Peer(-100123, "work-chat", "group", "Work Chat")]

    monkeypatch.setattr("tgbridge.sync.runtime.run_resolve", fake_resolve)
    assert (
        main(
            [
                "--settings",
                str(settings),
                "watchlist",
                "resolve",
                "Work Chat",
            ]
        )
        == 0
    )
    assert observed == {
        "port": 5222,
        "query": "Work Chat",
        "session": str(tmp_path / "sessions" / "tgq.session"),
    }
    assert capsys.readouterr().out.startswith("- slug: work-chat\n")


def test_sync_and_sender_receive_configured_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers: []\n")
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "paths:\n"
        "  db: messages.sqlite\n"
        "  watchlist: watchlist.yaml\n"
        "  session: sessions/tgq.session\n"
    )
    sessions: list[str] = []

    async def fake_sync(
        connection: sqlite3.Connection,
        config: Config,
        *,
        session: str,
        dry_run: bool,
    ) -> int:
        del connection, config, dry_run
        sessions.append(session)
        return 0

    async def fake_sender(
        outbox: object, *, session: str, dry_run: bool
    ) -> int:
        del outbox, dry_run
        sessions.append(session)
        return 0

    monkeypatch.setattr("tgbridge.sync.runtime.run_sync", fake_sync)
    monkeypatch.setattr("tgbridge.sync.runtime.run_sender", fake_sender)
    prefix = ["--settings", str(settings)]
    assert main([*prefix, "sync", "--dry-run"]) == 0
    capsys.readouterr()
    assert main([*prefix, "outbox", "send", "--dry-run"]) == 0
    capsys.readouterr()
    assert sessions == [
        str(tmp_path / "sessions" / "tgq.session"),
        str(tmp_path / "sessions" / "tgq.session"),
    ]


def test_cli_and_environment_override_user_settings(tmp_path: Path) -> None:
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "paths:\n"
        "  db: settings.db\n"
        "  watchlist: settings.yaml\n"
        "  session: settings.session\n"
        "telegram:\n"
        "  port: 443\n"
    )
    environment = {
        "TGQ_DB": "env.db",
        "TGQ_CONFIG": "env.yaml",
        "TGQ_SESSION": "env.session",
        "TGQ_TELEGRAM_PORT": "5222",
    }

    env_args = _parser().parse_args(
        ["--settings", str(settings), "watchlist", "resolve", "query"]
    )
    _apply_user_settings(env_args, environ=environment)
    assert (env_args.db, env_args.config, env_args.session, env_args.telegram_port) == (
        "env.db",
        "env.yaml",
        "env.session",
        "5222",
    )

    cli_args = _parser().parse_args(
        [
            "--settings",
            str(settings),
            "--db",
            "cli.db",
            "--config",
            "cli.yaml",
            "watchlist",
            "resolve",
            "query",
            "--session",
            "cli.session",
        ]
    )
    _apply_user_settings(cli_args, environ=environment)
    assert (cli_args.db, cli_args.config, cli_args.session) == (
        "cli.db",
        "cli.yaml",
        "cli.session",
    )


@pytest.mark.parametrize(
    "command",
    [
        ["sync"],
        ["outbox", "send"],
        ["watchlist", "resolve", "query"],
    ],
)
def test_session_setting_applies_to_network_commands(
    tmp_path: Path, command: list[str]
) -> None:
    settings = tmp_path / "config.yaml"
    settings.write_text("paths:\n  session: configured.session\n")
    args = _parser().parse_args(["--settings", str(settings), *command])
    _apply_user_settings(args, environ={})
    assert args.session == str(tmp_path / "configured.session")


def test_no_settings_preserves_existing_defaults(tmp_path: Path) -> None:
    args = _parser().parse_args(["search"])
    settings = _apply_user_settings(
        args,
        environ={
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "missing"),
        },
    )
    assert settings.db is None
    assert args.db is None
    assert args.config == "watchlist.yaml"


def test_sync_interval_precedence(tmp_path: Path) -> None:
    settings = tmp_path / "config.yaml"
    settings.write_text("sync:\n  interval: 180\n")

    from_settings = _parser().parse_args(["--settings", str(settings), "sync"])
    _apply_user_settings(from_settings, environ={})
    assert from_settings.interval == 180

    from_env = _parser().parse_args(["--settings", str(settings), "sync"])
    _apply_user_settings(from_env, environ={"TGQ_SYNC_INTERVAL": "240"})
    assert from_env.interval == 240

    from_cli = _parser().parse_args(
        ["--settings", str(settings), "sync", "--interval", "300"]
    )
    _apply_user_settings(from_cli, environ={"TGQ_SYNC_INTERVAL": "240"})
    assert from_cli.interval == 300

    defaulted = _parser().parse_args(["sync"])
    _apply_user_settings(
        defaulted,
        environ={
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "missing"),
        },
    )
    assert defaulted.interval == 60


def test_overlapping_sync_skips_without_starting_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers: []\n")
    session = tmp_path / "tgq.session"
    session.write_text("")
    called = False

    async def boom(*_: object, **__: object) -> int:
        nonlocal called
        called = True
        raise AssertionError("run_sync must not start while lock is held")

    monkeypatch.setattr("tgbridge.sync.runtime.run_sync", boom)
    from tgbridge.sync.lock import try_acquire_sync_lock

    with try_acquire_sync_lock(tmp_path / "sync.lock") as held:
        assert held is True
        assert (
            main(
                [
                    "--db",
                    str(tmp_path / "messages.sqlite"),
                    "--config",
                    str(watchlist),
                    "sync",
                    "--session",
                    str(session),
                    "--dry-run",
                ]
            )
            == 0
        )
    assert called is False


def test_sync_loop_uses_configured_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers: []\n")
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "paths:\n"
        f"  db: {tmp_path / 'messages.sqlite'}\n"
        "  watchlist: watchlist.yaml\n"
        "  session: tgq.session\n"
        "sync:\n"
        "  interval: 180\n"
    )
    observed: dict[str, object] = {}

    async def fake_loop(
        connection: object,
        config: Config,
        *,
        session: str,
        interval: int,
    ) -> None:
        del connection, config
        observed["session"] = session
        observed["interval"] = interval

    monkeypatch.setattr("tgbridge.sync.runtime.run_sync_loop", fake_loop)
    assert main(["--settings", str(settings), "sync", "--loop"]) == 0
    assert observed == {
        "session": str(tmp_path / "tgq.session"),
        "interval": 180,
    }

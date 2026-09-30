"""Named accounts: selection, `--account all` fan-out, and handle routing (#29)."""

import json
import sqlite3
from pathlib import Path

import pytest

from tests.factories import message, peer
from tgbridge.cli.main import _apply_user_settings, _parser, main
from tgbridge.cli.query import split_handle
from tgbridge.config import Config
from tgbridge.db import connect, migrate


def _mirror(path: Path, rows: list[tuple[int, str, int]]) -> None:
    """A mirror whose one peer `work-chat` (peer_id 1) holds (msg_id, text, ts) rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    migrate(connection)
    peer(connection, sendable=True)
    for msg_id, text, ts in rows:
        message(connection, msg_id=msg_id, text=text, ts=ts)
    connection.close()


@pytest.fixture
def accounts(tmp_path: Path) -> Path:
    """Two accounts whose mirrors reuse the same peer_id, slug and msg_ids."""
    for name, rows in (
        ("personal", [(1, "personal one", 100), (2, "personal two", 300)]),
        ("work", [(1, "work one", 200), (2, "work two", 400)]),
    ):
        _mirror(tmp_path / name / "messages.sqlite", rows)
        (tmp_path / name / "watchlist.yaml").write_text(
            "allow_send: true\n"
            "peers:\n"
            "  - slug: work-chat\n"
            "    id: 1\n"
            "    kind: group\n"
            "    sendable: true\n"
        )
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "default_account: personal\n"
        "accounts:\n"
        + "".join(
            f"  {name}:\n"
            f"    db: {name}/messages.sqlite\n"
            f"    watchlist: {name}/watchlist.yaml\n"
            f"    session: {name}/tgq.session\n"
            f"    secrets: {name}/secrets.env\n"
            for name in ("personal", "work")
        )
    )
    return settings


def _jsonl(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines()]


def _ids(output: str) -> list[object]:
    return [row["id"] for row in _jsonl(output)]


def test_default_account_output_matches_plain_single_database(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--settings", str(accounts), "search"]) == 0
    named = capsys.readouterr().out
    assert main(["--db", str(tmp_path / "personal" / "messages.sqlite"), "search"]) == 0
    assert named == capsys.readouterr().out
    assert _ids(named) == ["work-chat#2", "work-chat#1"]
    assert "account" not in named


def test_account_option_and_environment_select_one_account(
    accounts: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--settings", str(accounts), "--account", "work", "search"]) == 0
    assert [row["text"] for row in _jsonl(capsys.readouterr().out)] == ["work two", "work one"]
    monkeypatch.setenv("TGQ_ACCOUNT", "work")
    assert main(["--settings", str(accounts), "search", "--limit", "1"]) == 0
    assert [row["text"] for row in _jsonl(capsys.readouterr().out)] == ["work two"]
    # The CLI option wins over the environment.
    assert main(["--settings", str(accounts), "--account", "personal", "search"]) == 0
    assert _jsonl(capsys.readouterr().out)[0]["text"] == "personal two"


def test_all_merges_reads_by_time_and_limits_after_merge(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts), "--account", "all"]
    assert main([*prefix, "search", "--limit", "3"]) == 0
    rows = _jsonl(capsys.readouterr().out)
    assert [row["id"] for row in rows] == [
        "work:work-chat#2",
        "personal:work-chat#2",
        "work:work-chat#1",
    ]
    assert list(rows[0])[:3] == ["id", "account", "peer"]
    assert rows[0]["account"] == "work"
    assert rows[0]["peer"] == "work-chat"

    assert main([*prefix, "search", "--order", "asc"]) == 0
    assert _ids(capsys.readouterr().out) == [
        "personal:work-chat#1",
        "work:work-chat#1",
        "personal:work-chat#2",
        "work:work-chat#2",
    ]
    assert main([*prefix, "tail", "--peer", "work-chat", "--limit", "1"]) == 0
    assert _ids(capsys.readouterr().out) == ["work:work-chat#2"]
    assert main([*prefix, "digest", "--since", "0", "--format", "count"]) == 0
    assert capsys.readouterr().out == "4\n"
    assert main([*prefix, "search", "--limit", "-1", "--format", "count"]) == 0
    assert capsys.readouterr().out == "4\n"


def test_all_markdown_groups_by_account_and_peer(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--settings", str(accounts), "--account", "all", "search", "--format", "md"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert output.startswith("## work:work-chat\n- `work:work-chat#2` Lena — work two\n")
    assert "## personal:work-chat\n" in output


def test_all_with_empty_results_exits_one(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--settings", str(accounts), "--account", "all", "search", "--q", "absent"]
    assert main(argv) == 1
    assert capsys.readouterr().out == ""


def test_all_peers_and_outbox_list_carry_account(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts)]
    assert main([*prefix, "--account", "all", "peers"]) == 0
    assert [(row["account"], row["slug"]) for row in _jsonl(capsys.readouterr().out)] == [
        ("personal", "work-chat"),
        ("work", "work-chat"),
    ]
    assert main([*prefix, "--account", "work", "send", "--peer", "work-chat", "--body", "hi"]) == 0
    capsys.readouterr()
    assert main([*prefix, "--account", "all", "outbox", "list"]) == 0
    [row] = _jsonl(capsys.readouterr().out)
    assert (row["account"], row["peer"], row["body"]) == ("work", "work-chat", "hi")


def test_thread_under_all_requires_qualified_handle(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts), "--account", "all"]
    assert main([*prefix, "thread", "work-chat#2"]) == 2
    assert "account-qualified handle" in capsys.readouterr().err
    assert main([*prefix, "thread", "work:work-chat#2"]) == 0
    [row] = _jsonl(capsys.readouterr().out)
    assert (row["id"], row["account"], row["text"]) == ("work:work-chat#2", "work", "work two")
    assert main([*prefix, "thread", "home:work-chat#2"]) == 2
    assert "unknown account 'home'" in capsys.readouterr().err


def test_single_account_accepts_matching_qualified_handle_only(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts), "--account", "work"]
    assert main([*prefix, "thread", "work:work-chat#2"]) == 0
    [row] = _jsonl(capsys.readouterr().out)
    assert (row["id"], row["text"]) == ("work-chat#2", "work two")
    assert "account" not in row
    assert main([*prefix, "thread", "personal:work-chat#2"]) == 2
    assert "belongs to account 'personal'" in capsys.readouterr().err


def test_tag_routes_qualified_handle_and_audits_account(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts), "--account", "all"]
    assert main([*prefix, "tag", "work-chat#1", "todo"]) == 2
    assert main([*prefix, "tag", "work:work-chat#1", "todo"]) == 0
    assert _jsonl(capsys.readouterr().out)[0]["id"] == "work:work-chat#1"

    work = sqlite3.connect(tmp_path / "work" / "messages.sqlite")
    assert work.execute("SELECT peer_id, msg_id, tag FROM tags").fetchall() == [(1, 1, "todo")]
    target, detail = work.execute("SELECT target, detail FROM audit").fetchone()
    assert target == "work-chat#1"
    assert json.loads(detail) == {"tag": "todo", "account": "work"}
    work.close()
    personal = sqlite3.connect(tmp_path / "personal" / "messages.sqlite")
    assert personal.execute("SELECT count(*) FROM tags").fetchone() == (0,)
    personal.close()


@pytest.mark.parametrize(
    "command",
    [
        ["send", "--peer", "work-chat", "--body", "hi"],
        ["outbox", "approve", "1"],
        ["outbox", "reject", "1"],
        ["outbox", "send"],
        ["retag"],
        ["watchlist", "resolve", "query"],
    ],
)
def test_single_account_writes_reject_all(
    accounts: Path, command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--settings", str(accounts), "--account", "all", *command]) == 2
    assert "acts on one account; pass --account NAME, not all" in capsys.readouterr().err


def test_several_accounts_without_default_need_account(
    accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts.write_text(accounts.read_text().replace("default_account: personal\n", ""))
    assert main(["--settings", str(accounts), "search"]) == 2
    assert "pass --account with one of: personal, work or all" in capsys.readouterr().err
    assert main(["--settings", str(accounts), "--account", "work", "search"]) == 0


def test_empty_account_environment_means_unset(
    accounts: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TGQ_ACCOUNT", "")
    assert main(["--settings", str(accounts), "search", "--limit", "1"]) == 0
    assert _jsonl(capsys.readouterr().out)[0]["text"] == "personal two"
    assert main(["--settings", str(accounts), "--account", "", "search"]) == 2
    assert "account name must not be empty" in capsys.readouterr().err


def test_all_reads_survive_one_broken_mirror(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "work" / "messages.sqlite").unlink()
    prefix = ["--settings", str(accounts), "--account", "all"]
    assert main([*prefix, "search"]) == 3
    captured = capsys.readouterr()
    assert _ids(captured.out) == ["personal:work-chat#2", "personal:work-chat#1"]
    [error] = _jsonl(captured.err)
    assert (error["event"], error["account"], error["exit_code"]) == ("cli_error", "work", 3)
    assert "database does not exist" in str(error["error"])
    assert main([*prefix, "doctor"]) == 3
    captured = capsys.readouterr()
    assert {row["account"] for row in _jsonl(captured.out)} == {"personal"}
    assert _jsonl(captured.err)[0]["account"] == "work"


def test_transport_environment_beats_account_settings(tmp_path: Path) -> None:
    settings = tmp_path / "config.yaml"
    settings.write_text(
        "accounts:\n"
        "  personal:\n"
        "    db: personal/messages.sqlite\n"
        "    watchlist: personal/watchlist.yaml\n"
        "    session: personal/tgq.session\n"
        "  work:\n"
        "    db: work/messages.sqlite\n"
        "    watchlist: work/watchlist.yaml\n"
        "    session: work/tgq.session\n"
        "    telegram:\n"
        "      port: 443\n"
        "    sync:\n"
        "      interval: 900\n"
    )
    def selected(environment: dict[str, str]) -> dict[str, tuple[object, int]]:
        values = {}
        for name in ("personal", "work"):
            argv = ["--settings", str(settings), "--account", name, "sync"]
            args = _parser().parse_args(argv)
            _apply_user_settings(args, environ=environment)
            values[name] = (args.telegram_port, args.interval)
        return values

    # Same rule as flat settings: the export beats the file, the file the default.
    exported = {"TGQ_TELEGRAM_PORT": "5222", "TGQ_SYNC_INTERVAL": "120"}
    assert selected(exported) == {"personal": ("5222", 120), "work": ("5222", 120)}
    assert selected({}) == {"personal": (None, 60), "work": (443, 900)}


def test_all_outbox_list_does_not_create_missing_mirror(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "work" / "messages.sqlite"
    missing.unlink()
    assert main(["--settings", str(accounts), "--account", "all", "outbox", "list"]) == 3
    assert not missing.exists()
    [error] = _jsonl(capsys.readouterr().err)
    assert (error["account"], error["exit_code"]) == ("work", 3)


def test_all_reads_survive_malformed_watchlist(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "personal" / "watchlist.yaml").write_text("peers:\n  - {id: 1, kind: group}\n")
    assert main(["--settings", str(accounts), "--account", "all", "outbox", "list"]) == 2
    captured = capsys.readouterr()
    [error] = _jsonl(captured.err)
    assert (error["account"], error["error_type"], error["exit_code"]) == (
        "personal",
        "ValueError",
        2,
    )
    assert "malformed peer or rule entry" in str(error["error"])


def test_all_reraises_unexpected_error_after_other_accounts(
    accounts: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tgbridge.cli import main as cli

    real_peers = cli.peers
    seen: list[str] = []

    def flaky_peers(connection: sqlite3.Connection) -> list[dict[str, object]]:
        seen.append("call")
        if len(seen) == 1:
            raise AttributeError("bug")
        return real_peers(connection)

    monkeypatch.setattr(cli, "peers", flaky_peers)
    with pytest.raises(AttributeError, match="bug"):
        main(["--settings", str(accounts), "--account", "all", "peers"])
    # The second account still ran and printed before the bug surfaced.
    assert [row["account"] for row in _jsonl(capsys.readouterr().out)] == ["work"]


@pytest.mark.parametrize(
    ("option", "exit_code"), [(["--q", "foo("], 3), (["--since", "yesterday"], 2)]
)
def test_all_reports_query_errors_once_without_account(
    accounts: Path, option: list[str], exit_code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--settings", str(accounts), "--account", "all", "search", *option]
    assert main(argv) == exit_code
    [error] = _jsonl(capsys.readouterr().err)
    assert error["event"] == "cli_error"
    assert "account" not in error


def test_unknown_account_exits_two(accounts: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--settings", str(accounts), "--account", "home", "search"]) == 2
    assert "configured: personal, work" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("variable", "command"),
    [("TGQ_DB", ["search"]), ("TGQ_CONFIG", ["search"]), ("TGQ_SESSION", ["sync"])],
)
def test_path_environment_overrides_are_rejected_with_named_accounts(
    accounts: Path,
    variable: str,
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(variable, "/elsewhere")
    assert main(["--settings", str(accounts), *command]) == 2
    assert f"{variable} cannot be used with an accounts section" in capsys.readouterr().err
    assert main(["--settings", str(accounts), "--account", "all", *command]) == 2
    assert f"{variable} cannot be used with an accounts section" in capsys.readouterr().err


def test_path_environment_is_harmless_when_overridden_empty_or_unused(
    accounts: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = tmp_path / "other.sqlite"
    _mirror(other, [(7, "other", 1)])
    prefix = ["--settings", str(accounts)]
    monkeypatch.setenv("TGQ_DB", "/elsewhere")
    # The CLI option wins, so the stale export does not matter.
    assert main([*prefix, "--db", str(other), "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["work-chat#7"]
    # Like TGQ_ACCOUNT, an empty export counts as unset.
    monkeypatch.setenv("TGQ_DB", "")
    assert main([*prefix, "search", "--limit", "1"]) == 0
    assert _jsonl(capsys.readouterr().out)[0]["text"] == "personal two"
    # Read commands have no session, so TGQ_SESSION cannot mislead them.
    monkeypatch.setenv("TGQ_SESSION", "/elsewhere")
    assert main([*prefix, "search", "--limit", "1"]) == 0
    assert _jsonl(capsys.readouterr().out)[0]["text"] == "personal two"


def test_empty_transport_environment_means_unset(tmp_path: Path) -> None:
    settings = tmp_path / "config.yaml"
    settings.write_text("telegram:\n  port: 443\nsync:\n  interval: 900\n")
    args = _parser().parse_args(["--settings", str(settings), "sync"])
    _apply_user_settings(args, environ={"TGQ_TELEGRAM_PORT": "", "TGQ_SYNC_INTERVAL": " "})
    assert (args.telegram_port, args.interval) == (443, 900)


def test_cli_path_option_overrides_selected_account_but_not_all(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "other.sqlite"
    _mirror(other, [(7, "other", 1)])
    prefix = ["--settings", str(accounts)]
    assert main([*prefix, "--account", "work", "--db", str(other), "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["work-chat#7"]
    assert main([*prefix, "--account", "all", "--db", str(other), "search"]) == 2
    assert "--db names one account" in capsys.readouterr().err


def test_doctor_reminds_about_other_accounts_only_when_implicit(
    accounts: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prefix = ["--settings", str(accounts)]
    main([*prefix, "doctor"])
    hint = _jsonl(capsys.readouterr().out)[-1]
    assert hint == {
        "check": "accounts",
        "ok": True,
        "detail": (
            "checked personal; configured: personal,work; read commands accept --account all"
        ),
    }
    main([*prefix, "--account", "personal", "doctor"])
    assert all(row["check"] != "accounts" for row in _jsonl(capsys.readouterr().out))
    main(["--db", str(tmp_path / "personal" / "messages.sqlite"), "doctor"])
    assert all(row["check"] != "accounts" for row in _jsonl(capsys.readouterr().out))
    main([*prefix, "--account", "all", "doctor"])
    rows = _jsonl(capsys.readouterr().out)
    assert {row["account"] for row in rows} == {"personal", "work"}
    assert all(row["check"] != "accounts" for row in rows)


def test_legacy_settings_accept_default_and_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "messages.sqlite"
    _mirror(database, [(1, "hello", 1)])
    settings = tmp_path / "config.yaml"
    settings.write_text(f"paths:\n  db: {database}\n")
    prefix = ["--settings", str(settings)]
    assert main([*prefix, "--account", "default", "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["work-chat#1"]
    assert main([*prefix, "--account", "all", "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["default:work-chat#1"]
    assert main([*prefix, "--account", "work", "search"]) == 2
    capsys.readouterr()
    # `all` is the one flat account, so its path option and variable both apply.
    other = tmp_path / "other.sqlite"
    _mirror(other, [(7, "other", 1)])
    assert main([*prefix, "--account", "all", "--db", str(other), "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["default:work-chat#7"]
    monkeypatch.setenv("TGQ_DB", str(other))
    assert main([*prefix, "--account", "all", "search"]) == 0
    assert _ids(capsys.readouterr().out) == ["default:work-chat#7"]


def test_legacy_slugs_with_colon_keep_working(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "messages.sqlite"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(database)
    migrate(connection)
    peer(connection, slug="team:core")
    message(connection, msg_id=42, text="hello", ts=1)
    connection.close()
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("peers:\n  - {slug: 'team:core', id: 1, kind: group}\n")
    settings = tmp_path / "config.yaml"
    settings.write_text(f"paths:\n  db: {database}\n  watchlist: {watchlist}\n")
    prefix = ["--settings", str(settings)]
    assert main([*prefix, "thread", "team:core#42"]) == 0
    assert _ids(capsys.readouterr().out) == ["team:core#42"]
    assert main([*prefix, "--account", "all", "thread", "default:team:core#42"]) == 0
    assert _ids(capsys.readouterr().out) == ["default:team:core#42"]
    # `all` is the one flat account, so an unqualified handle is fine too.
    assert main([*prefix, "--account", "all", "thread", "team:core#42"]) == 0
    assert _ids(capsys.readouterr().out) == ["default:team:core#42"]
    assert main([*prefix, "tag", "team:core#42", "todo"]) == 0
    assert main([*prefix, "retag"]) == 0


@pytest.mark.parametrize("slug", ["default:ops", ":ops", "team:"])
def test_flat_colon_slug_resolves_as_a_whole_first(
    slug: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "messages.sqlite"
    connection = connect(database)
    migrate(connection)
    peer(connection, peer_id=1, slug="ops")
    peer(connection, peer_id=2, slug=slug)
    message(connection, peer_id=1, msg_id=42, text="plain ops", ts=1)
    message(connection, peer_id=2, msg_id=42, text="colon slug", ts=2)
    connection.close()
    settings = tmp_path / "config.yaml"
    settings.write_text(f"paths:\n  db: {database}\n")
    prefix = ["--settings", str(settings)]
    assert main([*prefix, "thread", f"{slug}#42"]) == 0
    assert [row["text"] for row in _jsonl(capsys.readouterr().out)] == ["colon slug"]
    assert main([*prefix, "tag", f"{slug}#42", "todo"]) == 0
    capsys.readouterr()
    check = sqlite3.connect(database)
    assert check.execute("SELECT peer_id, msg_id FROM tags").fetchall() == [(2, 42)]
    check.close()
    # The `default:` prefix that `--account all` prints still resolves.
    assert main([*prefix, "thread", "default:ops#42"]) == 0
    expected = "colon slug" if slug == "default:ops" else "plain ops"
    assert [row["text"] for row in _jsonl(capsys.readouterr().out)] == [expected]

def test_sync_all_runs_each_account_and_survives_one_failure(
    accounts: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, str | None]] = []

    async def fake_sync(
        connection: sqlite3.Connection,
        config: Config,
        *,
        session: str,
        dry_run: bool,
        secrets: str | None,
    ) -> int:
        del connection, config, dry_run
        calls.append((session, secrets))
        if "personal" in session:
            raise RuntimeError("TGQ_API_ID and TGQ_API_HASH are required")
        return 3

    monkeypatch.setattr("tgbridge.sync.runtime.run_sync", fake_sync)
    assert main(["--settings", str(accounts), "--account", "all", "sync", "--dry-run"]) == 4
    captured = capsys.readouterr()
    assert _jsonl(captured.out) == [{"account": "work", "fetched": 3, "dry_run": True}]
    [error] = _jsonl(captured.err)
    assert (error["event"], error["account"], error["exit_code"]) == ("cli_error", "personal", 4)
    assert calls == [
        (str(tmp_path / name / "tgq.session"), str(tmp_path / name / "secrets.env"))
        for name in ("personal", "work")
    ]


def test_sync_all_refuses_loop(accounts: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--settings", str(accounts), "--account", "all", "sync", "--loop"]) == 2
    assert "one timer per account" in capsys.readouterr().err


def test_single_account_sync_uses_account_session_and_secrets(
    accounts: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}

    async def fake_sync(
        connection: sqlite3.Connection,
        config: Config,
        *,
        session: str,
        dry_run: bool,
        secrets: str | None,
    ) -> int:
        del connection, dry_run
        seen.update(session=session, secrets=secrets, peers=len(config.peers))
        return 0

    monkeypatch.setattr("tgbridge.sync.runtime.run_sync", fake_sync)
    assert main(["--settings", str(accounts), "--account", "work", "sync", "--dry-run"]) == 0
    assert _jsonl(capsys.readouterr().out) == [{"fetched": 0, "dry_run": True}]
    assert seen == {
        "session": str(tmp_path / "work" / "tgq.session"),
        "secrets": str(tmp_path / "work" / "secrets.env"),
        "peers": 1,
    }


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        ("chat#5", "chat"),
        ("work:chat#5", "work:chat"),
        ("work:рабочий-чат#12", "work:рабочий-чат"),
        ("default:team:core#1", "default:team:core"),
        ("a#b#3", "a#b"),
    ],
)
def test_split_handle(handle: str, expected: str) -> None:
    assert split_handle(handle)[0] == expected


@pytest.mark.parametrize("handle", ["chat", "chat#", "#5", "chat#x"])
def test_split_handle_rejects_malformed(handle: str) -> None:
    with pytest.raises(ValueError, match="handle must be"):
        split_handle(handle)


@pytest.mark.parametrize("handle", [":work-chat#2", "work:#2"])
def test_named_handles_need_account_and_slug(
    handle: str, accounts: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for account in ("work", "all"):
        assert main(["--settings", str(accounts), "--account", account, "thread", handle]) == 2
        assert "handle must be" in capsys.readouterr().err

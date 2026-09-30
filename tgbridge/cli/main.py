"""tgq command-line entry point."""

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from tgbridge.cli.errors import DatabaseError, TgqError
from tgbridge.cli.formatting import render
from tgbridge.cli.query import (
    HANDLE_FORMAT,
    doctor,
    parse_time,
    peers,
    search_messages,
    split_handle,
    thread,
)
from tgbridge.config import (
    Config,
    append_peer,
    format_peer_candidates_yaml,
    format_peer_yaml,
    load_config,
)
from tgbridge.db import connect, migrate
from tgbridge.logging import configure_logging, event, get_logger
from tgbridge.outbox import Outbox, audit_detail
from tgbridge.settings import (
    ALL_ACCOUNTS,
    AccountSettings,
    UserSettings,
    environment_value,
    load_settings,
)
from tgbridge.sync.lock import sync_lock_path, try_acquire_sync_lock
from tgbridge.sync.models import Message

FORMAT_VERSION = 1
_LOG = get_logger("cli")
# Read commands that `--account all` fans out over every account's mirror.
_FANOUT_READS = {"search", "tail", "digest", "peers", "doctor"}
_MESSAGE_READS = {"search", "tail", "digest"}
# Path options that name one account's files and so cannot apply to `all`.
_ACCOUNT_PATH_OPTIONS = (("db", "TGQ_DB"), ("config", "TGQ_CONFIG"), ("session", "TGQ_SESSION"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgq")
    parser.add_argument("--settings")
    parser.add_argument(
        "--account",
        help="account name from the settings file, or `all` for read commands",
    )
    parser.add_argument("--db")
    parser.add_argument("--config")
    parser.add_argument("--format", choices=("jsonl", "md", "table", "count"), default="jsonl")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--format-version", type=int, default=FORMAT_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument("--verbose", action="store_true")
    diagnostics.add_argument("--quiet", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search")
    _output_args(search)
    _full_arg(search)
    _search_args(search)
    thread_parser = sub.add_parser("thread")
    _output_args(thread_parser)
    _full_arg(thread_parser)
    thread_parser.add_argument("handle")
    tail = sub.add_parser("tail")
    _output_args(tail)
    _full_arg(tail)
    tail.add_argument("--peer", required=True)
    tail.add_argument("--limit", type=int, default=50)
    digest = sub.add_parser("digest")
    _output_args(digest)
    _full_arg(digest)
    digest.add_argument("--since", required=True)
    digest.add_argument("--limit", type=int, default=200)
    peer_parser = sub.add_parser("peers")
    _output_args(peer_parser)
    doctor_parser = sub.add_parser("doctor")
    _output_args(doctor_parser)

    sync = sub.add_parser("sync")
    sync.add_argument("--session")
    sync.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    sync.add_argument("--loop", action="store_true")
    sync.add_argument("--interval", type=int)

    send = sub.add_parser("send")
    send.add_argument("--peer", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--reply-to", type=int)
    send.add_argument("--actor", default="agent")
    send.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)

    outbox = sub.add_parser("outbox")
    outbox_sub = outbox.add_subparsers(dest="outbox_command", required=True)
    listing = outbox_sub.add_parser("list")
    _output_args(listing)
    listing.add_argument("--status")
    for name in ("approve", "reject"):
        decision = outbox_sub.add_parser(name)
        decision.add_argument("id", type=int)
        decision.add_argument("--actor", default="human")
        decision.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    sender = outbox_sub.add_parser("send")
    sender.add_argument("--session")
    sender.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)

    tag = sub.add_parser("tag")
    tag.add_argument("handle")
    tag.add_argument("tag")
    tag.add_argument("--remove", action="store_true")
    tag.add_argument("--source", choices=("manual", "agent"), default="manual")
    tag.add_argument("--actor", default="human")
    tag.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    retag = sub.add_parser("retag")
    retag.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)

    watchlist = sub.add_parser("watchlist")
    watchlist_sub = watchlist.add_subparsers(dest="watchlist_command", required=True)
    resolve = watchlist_sub.add_parser("resolve")
    resolve.add_argument("query")
    resolve.add_argument("--session")
    resolve.add_argument("--write", action="store_true")
    resolve.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    return parser


def _output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("jsonl", "md", "table", "count"),
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--max-tokens", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--format-version", type=int, default=argparse.SUPPRESS)


def _full_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full",
        action="store_true",
        help="untruncated text plus a `links` field extracted from message entities",
    )


def _search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--peer")
    parser.add_argument("--tag")
    parser.add_argument("--not-tag")
    parser.add_argument("--q")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--has-media", action="store_true")
    parser.add_argument("--replies-to", type=int)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--order", choices=("asc", "desc"), default="desc")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)
    if args.format_version != FORMAT_VERSION:
        parser.error(f"unsupported format version: {args.format_version}")
    try:
        _apply_user_settings(args)
        return _run(args)
    except Exception as error:
        exit_code = _exit_code(error)
        if exit_code is None:
            raise
        _log_cli_error(error, exit_code)
        return exit_code


def _exit_code(error: Exception) -> int | None:
    if isinstance(error, TgqError):
        return error.exit_code
    if isinstance(error, (ValueError, OSError)):
        return 2
    if isinstance(error, RuntimeError):
        return 4
    if isinstance(error, sqlite3.Error):
        return 3
    return None


def _apply_user_settings(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> UserSettings:
    """Resolve the targeted account(s) and their paths onto `args`.

    A single account is applied to `args` itself; `--account all` leaves the
    per-account paths unset on `args` and stores one single-account namespace
    per account name in `args.targets`.
    """
    environment = os.environ if environ is None else environ
    settings = load_settings(args.settings, environ=environment)
    args.settings = str(settings.source)
    if args.account is not None:
        # A generated unit or script easily quotes a stray space into the flag.
        requested = args.account.strip()
        if not requested:
            raise ValueError("account name must not be empty")
    else:
        # `TGQ_ACCOUNT=` in an env file or unit is the usual way to unset it.
        requested = environment_value(environment, "TGQ_ACCOUNT")
    selected = settings.select(requested)
    # Flat settings have one account, so `all` is that account: no fan-out, no
    # account prefix on handles, and the path options and variables apply.
    args.fanout = requested == ALL_ACCOUNTS and settings.named
    # D1 in #29: an implicit default among several accounts is worth a reminder.
    args.account_hint = (
        None
        if requested is not None or len(settings.accounts) < 2
        else settings.account_names
    )
    if not args.fanout:
        _apply_account(args, selected[0], environment, settings)
        return settings
    for option, _ in _ACCOUNT_PATH_OPTIONS:
        if getattr(args, option, None) is not None:
            raise ValueError(f"--{option} names one account and cannot be used with --account all")
    targets: dict[str, argparse.Namespace] = {}
    for account in selected:
        target = argparse.Namespace(**vars(args))
        target.fanout = False
        _apply_account(target, account, environment, settings)
        targets[account.name] = target
    args.targets = targets
    args.user_settings = settings
    return settings


def _apply_account(
    args: argparse.Namespace,
    account: AccountSettings,
    environment: Mapping[str, str],
    settings: UserSettings,
) -> None:
    exported = {
        variable: environment_value(environment, variable) for _, variable in _ACCOUNT_PATH_OPTIONS
    }
    if settings.named:
        # One exported TGQ_DB would silently point every account at one mirror.
        # A variable the CLI option overrides, or the command lacks, is harmless.
        for option, variable in _ACCOUNT_PATH_OPTIONS:
            if (
                exported[variable] is not None
                and hasattr(args, option)
                and getattr(args, option) is None
            ):
                raise ValueError(
                    f"{variable} cannot be used with an accounts section in settings; "
                    f"set the path under accounts.<name>, pass --{option}, "
                    f"or unset {variable}"
                )
        exported = dict.fromkeys(exported)
    args.account_name = account.name
    args.named = settings.named
    args.audit_account = account.name if settings.named else None
    args.secrets = None if account.secrets is None else str(account.secrets)
    args.db = _select(args.db, exported["TGQ_DB"], account.db, None)
    args.config = _select(
        args.config,
        exported["TGQ_CONFIG"],
        account.watchlist,
        "watchlist.yaml",
    )
    if hasattr(args, "session"):
        args.session = _select(
            args.session,
            exported["TGQ_SESSION"],
            account.session,
            "tgq.session",
        )
    # One rule for flat and named settings alike: CLI, then the export, then
    # the file. The port is a property of the host's network, not the account.
    args.telegram_port = _select(
        None, environment_value(environment, "TGQ_TELEGRAM_PORT"), account.telegram_port, None
    )
    if hasattr(args, "interval"):
        args.interval = _select_interval(
            args.interval,
            environment_value(environment, "TGQ_SYNC_INTERVAL"),
            account.sync_interval,
            60,
        )


def _select(
    cli_value: Any,
    environment_value: Any,
    settings_value: Any,
    default: Any,
) -> Any:
    for value in (cli_value, environment_value, settings_value):
        if value is not None:
            return str(value) if isinstance(value, Path) else value
    return default


def _select_interval(
    cli_value: int | None,
    environment_value: str | None,
    settings_value: int | None,
    default: int,
) -> int:
    if cli_value is not None:
        return int(cli_value)
    if environment_value is not None:
        try:
            return int(environment_value)
        except ValueError as error:
            raise ValueError("TGQ_SYNC_INTERVAL must be an integer") from error
    if settings_value is not None:
        return int(settings_value)
    return default


def _load_watchlist(args: argparse.Namespace) -> Config:
    return load_config(
        args.config, telegram_port=args.telegram_port, named_accounts=args.named
    )


def _log_cli_error(error: Exception, exit_code: int, *, account: str | None = None) -> None:
    fields: dict[str, Any] = {} if account is None else {"account": account}
    event(
        _LOG,
        40,
        "cli_error",
        **fields,
        error_type=type(error).__name__,
        error=str(error),
        exit_code=exit_code,
    )


def _dry_run_copy(path: Path) -> sqlite3.Connection:
    """In-memory, migrated copy of the mirror for write commands under --dry-run.

    Keeps the file untouched (no migrations, no error bookkeeping) while the
    command still sees the current schema.
    """
    copy = connect(":memory:")
    if path.exists():
        source = connect(path)
        try:
            source.backup(copy)
        finally:
            source.close()
    return copy


def _run(args: argparse.Namespace) -> int:
    if args.fanout:
        return _run_fanout(args)
    if args.command == "watchlist":
        return _run_watchlist(args)
    return _emit(_collect(args), args)


def _emit(rows: list[dict[str, Any]] | None, args: argparse.Namespace) -> int:
    if rows is None:
        return 0
    sys.stdout.write(_render(rows, args))
    return 0 if rows else 1


def _render(rows: list[dict[str, Any]], args: argparse.Namespace) -> str:
    return render(
        rows,
        output_format=args.format,
        max_tokens=args.max_tokens,
        snippet_chars=None if getattr(args, "full", False) else 400,
        format_version=args.format_version,
    )


def _run_fanout(args: argparse.Namespace) -> int:
    """`--account all`: merge reads, route qualified handles, sync one by one."""
    if args.command == "sync" and args.loop:
        raise ValueError("--loop syncs one account; run one timer per account instead")
    if args.command in {*_FANOUT_READS, "sync"} or (
        args.command == "outbox" and args.outbox_command == "list"
    ):
        if args.command in _MESSAGE_READS:
            _check_query(args)
        return _collect_each(args)
    if args.command in {"thread", "tag"}:
        account, _, _ = _named_handle(args.handle)
        if account is None:
            raise ValueError(
                f"--account all needs an account-qualified handle (account:slug#msg_id), "
                f"got {args.handle!r}"
            )
        target = args.targets[args.user_settings.account(account).name]
        rows = _collect(target)
        if args.command == "thread" and rows:
            rows = [_qualify(row, target.account_name) for row in rows]
        return _emit(rows, args)
    raise ValueError(f"{_command_name(args)} acts on one account; pass --account NAME, not all")


def _check_query(args: argparse.Namespace) -> None:
    """Fail once, before the fan-out, on errors in the query itself.

    A bad `--since` or FTS expression fails on every mirror alike, so it is
    reported once, without an account, instead of once per account. Times are
    parsed directly; the FTS expression is compiled against a throwaway table
    shaped like `messages_fts`, and fails as a single mirror would (exit 3).
    """
    for value in (getattr(args, "since", None), getattr(args, "until", None)):
        if value is not None:
            parse_time(value)
    query = getattr(args, "q", None)
    if query:
        probe = sqlite3.connect(":memory:")
        try:
            probe.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
            probe.execute("SELECT rowid FROM probe WHERE text MATCH ?", (query,)).fetchall()
        finally:
            probe.close()


def _command_name(args: argparse.Namespace) -> str:
    sub = getattr(args, "outbox_command", None) or getattr(args, "watchlist_command", None)
    return f"{args.command} {sub}" if sub else args.command


def _qualify(row: dict[str, Any], account: str) -> dict[str, Any]:
    """Add the account to a row; message handles become account:slug#msg_id."""
    handle = row.get("id")
    if isinstance(handle, str) and "#" in handle:
        return {"id": f"{account}:{handle}", "account": account} | {
            key: value for key, value in row.items() if key != "id"
        }
    return {"account": account, **row}


def _merge(
    args: argparse.Namespace,
    results: list[tuple[argparse.Namespace, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Merge per-account rows; ties keep account order, then per-account order."""
    entries = [
        (_qualify(row, target.account_name), account_index, row_index)
        for account_index, (target, rows) in enumerate(results)
        for row_index, row in enumerate(rows)
    ]
    if args.command in _MESSAGE_READS:
        sign = 1 if getattr(args, "order", "desc") == "asc" else -1
        entries.sort(key=lambda entry: (sign * int(entry[0]["ts"]), entry[1], entry[2]))
        # A negative LIMIT means "no limit" in SQLite; keep that after the merge.
        kept = entries if args.limit < 0 else entries[: args.limit]
        return [row for row, _, _ in kept]
    if args.command == "outbox":
        entries.sort(key=lambda entry: (-int(entry[0]["created_at"]), entry[1], entry[2]))
    return [row for row, _, _ in entries]


def _collect_each(args: argparse.Namespace) -> int:
    """Run one command on every account in turn and merge the rows.

    A failing account is logged with its name and skipped; the rest still run
    and print, and the first failure's exit code wins. An error without a
    stable exit code is a bug, not an account failure: the other accounts
    still run and print, then it is re-raised with its traceback.
    """
    results: list[tuple[argparse.Namespace, list[dict[str, Any]]]] = []
    failure = 0
    unexpected: Exception | None = None
    for target in args.targets.values():
        try:
            rows = _collect(target)
        except Exception as error:
            code = _exit_code(error)
            if code is None:
                unexpected = unexpected or error
                continue
            _log_cli_error(error, code, account=target.account_name)
            failure = failure or code
            continue
        results.append((target, rows or []))
    merged = _merge(args, results)
    # Sync with nothing to report (every lock held) is a success, not "no rows".
    code = 0 if args.command == "sync" and not merged else _emit(merged, args)
    if unexpected is not None:
        raise unexpected
    return failure or code


def _collect(args: argparse.Namespace) -> list[dict[str, Any]] | None:
    """Run one command against one account's mirror."""
    if not args.db:
        raise DatabaseError("--db or TGQ_DB is required")
    path = Path(args.db)
    write_command = args.command in {"sync", "send", "outbox", "tag", "retag"}
    # `outbox list` migrates like other outbox commands but, being a read, must
    # not create a mirror at a mistyped path.
    creates_mirror = write_command and not (
        args.command == "outbox" and args.outbox_command == "list"
    )
    if not path.exists() and not creates_mirror:
        raise DatabaseError(f"database does not exist: {path}")
    transient = bool(args.dry_run and write_command)
    connection = _dry_run_copy(path) if transient else connect(path)
    try:
        if transient or write_command:
            migrate(connection)
        elif int(connection.execute("PRAGMA user_version").fetchone()[0]) < 1:
            raise DatabaseError("database migrations have not been applied")
        return _dispatch(connection, args)
    finally:
        connection.close()


def _dispatch(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> list[dict[str, Any]] | None:
    if args.command == "search":
        return search_messages(
            connection,
            peers=args.peer,
            tags=args.tag,
            not_tag=args.not_tag,
            query=args.q,
            sender=args.sender,
            since=args.since,
            until=args.until,
            has_media=args.has_media,
            replies_to=args.replies_to,
            limit=args.limit,
            order=args.order,
            full=args.full,
        )
    if args.command == "thread":
        return thread(connection, *_local_handle(args), full=args.full)
    if args.command == "tail":
        return search_messages(connection, peers=args.peer, limit=args.limit, full=args.full)
    if args.command == "digest":
        return search_messages(connection, since=args.since, limit=args.limit, full=args.full)
    if args.command == "peers":
        return peers(connection)
    if args.command == "doctor":
        checks = doctor(connection)
        if args.account_hint:
            checks.append(
                {
                    "check": "accounts",
                    "ok": True,
                    "detail": (
                        f"checked {args.account_name}; configured: "
                        f"{','.join(args.account_hint)}; "
                        "read commands accept --account all"
                    ),
                }
            )
        return checks
    if args.command == "sync":
        config = _load_watchlist(args)
        from tgbridge.sync.runtime import run_sync, run_sync_loop

        lock_path = sync_lock_path(args.session)
        with try_acquire_sync_lock(lock_path) as acquired:
            if not acquired:
                event(
                    _LOG,
                    20,
                    "sync_skipped",
                    reason="lock_held",
                    lock=str(lock_path),
                )
                return None
            if args.loop:
                if args.dry_run:
                    raise ValueError("--loop cannot be combined with --dry-run")
                asyncio.run(
                    run_sync_loop(
                        connection,
                        config,
                        session=args.session,
                        interval=args.interval,
                        secrets=args.secrets,
                    )
                )
                return None
            count = asyncio.run(
                run_sync(
                    connection,
                    config,
                    session=args.session,
                    dry_run=args.dry_run,
                    secrets=args.secrets,
                )
            )
            return [{"fetched": count, "dry_run": args.dry_run}]
    if args.command == "send":
        outbox = Outbox(connection, _load_watchlist(args), account=args.audit_account)
        item_id = outbox.enqueue(
            args.peer,
            args.body,
            reply_to=args.reply_to,
            actor=args.actor,
            dry_run=args.dry_run,
        )
        return [{"outbox_id": item_id, "status": "pending", "dry_run": args.dry_run}]
    if args.command == "outbox":
        return _outbox_command(connection, args)
    if args.command == "tag":
        return _tag(connection, args)
    if args.command == "retag":
        return _retag(connection, args)
    raise ValueError(f"unknown command: {args.command}")


def _run_watchlist(args: argparse.Namespace) -> int:
    if args.watchlist_command != "resolve":
        raise ValueError(f"unknown watchlist command: {args.watchlist_command}")
    config = _load_watchlist(args)
    from tgbridge.sync.runtime import run_resolve

    matches = asyncio.run(
        run_resolve(config, args.query, session=args.session, secrets=args.secrets)
    )
    if not matches:
        return 1
    if len(matches) > 1:
        sys.stdout.write(format_peer_candidates_yaml(matches))
        return 1

    peer = matches[0]
    output = (
        append_peer(args.config, peer, dry_run=args.dry_run)
        if args.write
        else format_peer_yaml(peer)
    )
    sys.stdout.write(output)
    return 0


def _outbox_command(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> list[dict[str, Any]]:
    config = _load_watchlist(args)
    outbox = Outbox(connection, config, account=args.audit_account)
    if args.outbox_command == "list":
        return outbox.list(args.status)
    if args.outbox_command in {"approve", "reject"}:
        decision = "approved" if args.outbox_command == "approve" else "rejected"
        outbox.decide(args.id, decision, actor=args.actor, dry_run=args.dry_run)
        return [{"outbox_id": args.id, "status": decision, "dry_run": args.dry_run}]
    if args.outbox_command == "send":
        from tgbridge.sync.runtime import run_sender

        count = asyncio.run(
            run_sender(
                outbox, session=args.session, dry_run=args.dry_run, secrets=args.secrets
            )
        )
        return [{"sent": count, "dry_run": args.dry_run}]
    raise ValueError(f"unknown outbox command: {args.outbox_command}")


def _named_handle(handle: str) -> tuple[str | None, str, int]:
    """(account, slug, msg_id) under named accounts, whose slugs never contain ':'."""
    body, msg_id = split_handle(handle)
    account, colon, slug = body.partition(":")
    if not colon:
        return None, body, msg_id
    if not account or not slug:
        raise ValueError(f"handle must be {HANDLE_FORMAT}")
    return account, slug, msg_id


def _local_handle(args: argparse.Namespace) -> tuple[str, int]:
    """(slug, msg_id) of the handle in the selected account's mirror (D3 in #29)."""
    if args.named:
        account, slug, msg_id = _named_handle(args.handle)
        if account is not None and account != args.account_name:
            raise ValueError(
                f"handle {args.handle!r} belongs to account {account!r}, "
                f"but the selected account is {args.account_name!r}"
            )
        return slug, msg_id
    # Flat settings keep slugs with ':' (`team:core#42`) and never print an
    # account prefix, so the whole body is the slug.
    return split_handle(args.handle)


def _find_peer_id(connection: sqlite3.Connection, slug: str) -> int | None:
    row = connection.execute("SELECT peer_id FROM peers WHERE slug=?", (slug,)).fetchone()
    return None if row is None else int(row["peer_id"])


def _peer_id(connection: sqlite3.Connection, slug: str) -> int:
    peer_id = _find_peer_id(connection, slug)
    if peer_id is None:
        raise ValueError(f"unknown peer: {slug}")
    return peer_id


def _tag(connection: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    slug, msg_id = _local_handle(args)
    peer_id = _peer_id(connection, slug)
    if args.dry_run:
        return [{"id": args.handle, "tag": args.tag, "removed": args.remove, "dry_run": True}]
    with connection:
        if args.remove:
            connection.execute(
                "DELETE FROM tags WHERE peer_id=? AND msg_id=? AND tag=?",
                (peer_id, msg_id, args.tag),
            )
        else:
            connection.execute(
                """
                INSERT INTO tags(peer_id, msg_id, tag, source, created_at)
                VALUES (?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(peer_id, msg_id, tag) DO UPDATE SET source=excluded.source
                """,
                (peer_id, msg_id, args.tag, args.source),
            )
        connection.execute(
            "INSERT INTO audit(ts,actor,action,target,detail) VALUES (?,?,?,?,?)",
            (
                int(time.time()),
                args.actor,
                "tag.remove" if args.remove else "tag.add",
                f"{slug}#{msg_id}",
                audit_detail({"tag": args.tag}, args.audit_account),
            ),
        )
    return [{"id": args.handle, "tag": args.tag, "removed": args.remove, "dry_run": False}]


def _retag(connection: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    config = _load_watchlist(args)
    if args.dry_run:
        return [{"rules": len(config.rules), "dry_run": True}]
    from tgbridge.sync.engine import SyncEngine

    class _UnusedClient:
        async def iter_messages(
            self,
            peer_id: int,
            *,
            min_id: int = 0,
            offset_id: int = 0,
            reverse: bool = False,
            limit: int | None = None,
        ) -> AsyncIterator[Message]:
            del peer_id, min_id, offset_id, reverse, limit
            for item in ():
                yield item

    engine = SyncEngine(connection, _UnusedClient(), rules=config.rules)
    rows = connection.execute(
        """
        SELECT peer_id, msg_id, ts, sender_id, sender_name, text, reply_to, fwd_from,
               has_media, media_kind, raw_json FROM messages
        """
    ).fetchall()
    with connection:
        connection.execute("DELETE FROM tags WHERE source='rule'")
        for row in rows:
            engine.apply_rules(Message(**dict(row)))
        connection.execute(
            "INSERT INTO audit(ts,actor,action,target,detail) VALUES (?,?,?,?,?)",
            (
                int(time.time()),
                "human",
                "tag.retag",
                None,
                audit_detail(
                    {"messages": len(rows), "rules": len(config.rules)}, args.audit_account
                ),
            ),
        )
    return [{"retagged": len(rows), "rules": len(config.rules), "dry_run": False}]


if __name__ == "__main__":
    raise SystemExit(main())

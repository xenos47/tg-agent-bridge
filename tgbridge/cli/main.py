"""tgq command-line entry point."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from tgbridge.cli.errors import DatabaseError, TgqError
from tgbridge.cli.formatting import render
from tgbridge.cli.query import doctor, peers, search_messages, thread
from tgbridge.config import load_config
from tgbridge.db import connect, migrate
from tgbridge.logging import configure_logging, event, get_logger
from tgbridge.outbox import Outbox
from tgbridge.sync.models import Message

FORMAT_VERSION = 1
_LOG = get_logger("cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgq")
    parser.add_argument("--db", default=os.environ.get("TGQ_DB"))
    parser.add_argument("--config", default=os.environ.get("TGQ_CONFIG", "watchlist.yaml"))
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
    _search_args(search)
    thread_parser = sub.add_parser("thread")
    _output_args(thread_parser)
    thread_parser.add_argument("handle")
    tail = sub.add_parser("tail")
    _output_args(tail)
    tail.add_argument("--peer", required=True)
    tail.add_argument("--limit", type=int, default=50)
    digest = sub.add_parser("digest")
    _output_args(digest)
    digest.add_argument("--since", required=True)
    digest.add_argument("--limit", type=int, default=200)
    peer_parser = sub.add_parser("peers")
    _output_args(peer_parser)
    doctor_parser = sub.add_parser("doctor")
    _output_args(doctor_parser)

    sync = sub.add_parser("sync")
    sync.add_argument("--session", default=os.environ.get("TGQ_SESSION", "tgq.session"))
    sync.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    sync.add_argument("--loop", action="store_true")
    sync.add_argument("--interval", type=int, default=60)

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
    sender.add_argument("--session", default=os.environ.get("TGQ_SESSION", "tgq.session"))
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
    return parser


def _output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("jsonl", "md", "table", "count"),
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--max-tokens", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--format-version", type=int, default=argparse.SUPPRESS)


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
        return _run(args)
    except TgqError as error:
        _log_cli_error(error, error.exit_code)
        return error.exit_code
    except (ValueError, OSError) as error:
        _log_cli_error(error, 2)
        return 2
    except RuntimeError as error:
        _log_cli_error(error, 4)
        return 4
    except sqlite3.Error as error:
        _log_cli_error(error, 3)
        return 3


def _log_cli_error(error: Exception, exit_code: int) -> None:
    event(
        _LOG,
        40,
        "cli_error",
        error_type=type(error).__name__,
        error=str(error),
        exit_code=exit_code,
    )


def _run(args: argparse.Namespace) -> int:
    if not args.db:
        raise DatabaseError("--db or TGQ_DB is required")
    path = Path(args.db)
    write_command = args.command in {"sync", "send", "outbox", "tag", "retag"}
    if not path.exists() and not write_command:
        raise DatabaseError(f"database does not exist: {path}")
    transient = bool(args.dry_run and not path.exists())
    connection = connect(":memory:" if transient else path)
    try:
        if transient or (write_command and not args.dry_run):
            migrate(connection)
        elif int(connection.execute("PRAGMA user_version").fetchone()[0]) < 1:
            raise DatabaseError("database migrations have not been applied")
        rows = _dispatch(connection, args)
        if rows is None:
            return 0
        output = render(
            rows,
            output_format=args.format,
            max_tokens=args.max_tokens,
            format_version=args.format_version,
        )
        sys.stdout.write(output)
        return 0 if rows else 1
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
        )
    if args.command == "thread":
        return thread(connection, args.handle)
    if args.command == "tail":
        return search_messages(connection, peers=args.peer, limit=args.limit)
    if args.command == "digest":
        return search_messages(connection, since=args.since, limit=args.limit)
    if args.command == "peers":
        return peers(connection)
    if args.command == "doctor":
        return doctor(connection)
    if args.command == "sync":
        config = load_config(args.config)
        from tgbridge.sync.runtime import run_sync, run_sync_loop

        if args.loop:
            if args.dry_run:
                raise ValueError("--loop cannot be combined with --dry-run")
            asyncio.run(
                run_sync_loop(
                    connection,
                    config,
                    session=args.session,
                    interval=args.interval,
                )
            )
            return []
        count = asyncio.run(
            run_sync(connection, config, session=args.session, dry_run=args.dry_run)
        )
        return [{"fetched": count, "dry_run": args.dry_run}]
    if args.command == "send":
        outbox = Outbox(connection, load_config(args.config))
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


def _outbox_command(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> list[dict[str, Any]]:
    config = load_config(args.config)
    outbox = Outbox(connection, config)
    if args.outbox_command == "list":
        return outbox.list(args.status)
    if args.outbox_command in {"approve", "reject"}:
        decision = "approved" if args.outbox_command == "approve" else "rejected"
        outbox.decide(args.id, decision, actor=args.actor, dry_run=args.dry_run)
        return [{"outbox_id": args.id, "status": decision, "dry_run": args.dry_run}]
    if args.outbox_command == "send":
        from tgbridge.sync.runtime import run_sender

        count = asyncio.run(run_sender(outbox, session=args.session, dry_run=args.dry_run))
        return [{"sent": count, "dry_run": args.dry_run}]
    raise ValueError(f"unknown outbox command: {args.outbox_command}")


def _parse_handle(connection: sqlite3.Connection, handle: str) -> tuple[int, int]:
    slug, separator, raw_id = handle.rpartition("#")
    if not separator or not raw_id.isdigit():
        raise ValueError("handle must be slug#msg_id")
    row = connection.execute("SELECT peer_id FROM peers WHERE slug=?", (slug,)).fetchone()
    if row is None:
        raise ValueError(f"unknown peer: {slug}")
    return int(row["peer_id"]), int(raw_id)


def _tag(connection: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    peer_id, msg_id = _parse_handle(connection, args.handle)
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
                args.handle,
                json.dumps({"tag": args.tag}, separators=(",", ":")),
            ),
        )
    return [{"id": args.handle, "tag": args.tag, "removed": args.remove, "dry_run": False}]


def _retag(connection: sqlite3.Connection, args: argparse.Namespace) -> list[dict[str, Any]]:
    config = load_config(args.config)
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
                json.dumps({"messages": len(rows), "rules": len(config.rules)}),
            ),
        )
    return [{"retagged": len(rows), "rules": len(config.rules), "dry_run": False}]


if __name__ == "__main__":
    raise SystemExit(main())

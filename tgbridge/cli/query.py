"""Read-only SQL queries for agent-facing commands."""

import re
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any


def parse_time(value: str, *, now: int | None = None) -> int:
    if value.isdigit():
        return int(value)
    relative = re.fullmatch(r"(\d+)([hd])", value)
    if relative:
        amount = int(relative.group(1))
        seconds = amount * (3600 if relative.group(2) == "h" else 86400)
        return (now or int(time.time())) - seconds
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    except ValueError as error:
        raise ValueError(f"invalid time value: {value}") from error


def search_messages(
    connection: sqlite3.Connection,
    *,
    peers: str | None = None,
    tags: str | None = None,
    not_tag: str | None = None,
    query: str | None = None,
    sender: str | None = None,
    since: str | None = None,
    until: str | None = None,
    has_media: bool = False,
    replies_to: int | None = None,
    limit: int = 50,
    order: str = "desc",
) -> list[dict[str, Any]]:
    joins: list[str] = []
    where = ["p.watched=1", "m.deleted_at IS NULL"]
    params: list[Any] = []
    if query:
        joins.append("JOIN messages_fts f ON f.rowid=m.rowid")
        where.append("f.text MATCH ?")
        params.append(query)
    if peers:
        values = peers.split(",")
        where.append(f"p.slug IN ({','.join('?' for _ in values)})")
        params.extend(values)
    if tags:
        for index, tag in enumerate(tags.split(",")):
            alias = f"t{index}"
            joins.append(
                f"JOIN tags {alias} ON {alias}.peer_id=m.peer_id AND {alias}.msg_id=m.msg_id"
            )
            where.append(f"{alias}.tag=?")
            params.append(tag)
    if not_tag:
        where.append(
            "NOT EXISTS (SELECT 1 FROM tags nt WHERE nt.peer_id=m.peer_id "
            "AND nt.msg_id=m.msg_id AND nt.tag=?)"
        )
        params.append(not_tag)
    if sender:
        if sender.lstrip("-").isdigit():
            where.append("m.sender_id=?")
            params.append(int(sender))
        else:
            where.append("m.sender_name=?")
            params.append(sender)
    if since:
        where.append("m.ts>=?")
        params.append(parse_time(since))
    if until:
        where.append("m.ts<=?")
        params.append(parse_time(until))
    if has_media:
        where.append("m.has_media=1")
    if replies_to is not None:
        where.append("m.reply_to=?")
        params.append(replies_to)
    direction = "ASC" if order == "asc" else "DESC"
    params.append(limit)
    sql = f"""
        SELECT m.peer_id, m.msg_id, m.ts, m.sender_name, m.text, m.reply_to,
               m.media_kind, p.slug,
               COALESCE(group_concat(DISTINCT all_tags.tag), '') AS tags
        FROM messages m
        JOIN peers p ON p.peer_id=m.peer_id
        {' '.join(joins)}
        LEFT JOIN tags all_tags
          ON all_tags.peer_id=m.peer_id AND all_tags.msg_id=m.msg_id
        WHERE {' AND '.join(where)}
        GROUP BY m.peer_id, m.msg_id
        ORDER BY m.ts {direction}, m.peer_id {direction}, m.msg_id {direction}
        LIMIT ?
    """
    return [_message_dict(row) for row in connection.execute(sql, params)]


def thread(connection: sqlite3.Connection, handle: str) -> list[dict[str, Any]]:
    slug, separator, raw_id = handle.rpartition("#")
    if not separator or not raw_id.isdigit():
        raise ValueError("handle must be slug#msg_id")
    msg_id = int(raw_id)
    rows = connection.execute(
        """
        WITH RECURSIVE ancestors(peer_id, msg_id, reply_to, depth) AS (
            SELECT m.peer_id, m.msg_id, m.reply_to, 0
            FROM messages m JOIN peers p ON p.peer_id=m.peer_id
            WHERE p.slug=? AND m.msg_id=?
            UNION ALL
            SELECT parent.peer_id, parent.msg_id, parent.reply_to, a.depth + 1
            FROM messages parent JOIN ancestors a
              ON parent.peer_id=a.peer_id AND parent.msg_id=a.reply_to
            WHERE a.depth < 50
        )
        SELECT m.peer_id, m.msg_id, m.ts, m.sender_name, m.text, m.reply_to,
               m.media_kind, p.slug,
               COALESCE(group_concat(DISTINCT t.tag), '') AS tags
        FROM messages m JOIN peers p ON p.peer_id=m.peer_id
        LEFT JOIN tags t ON t.peer_id=m.peer_id AND t.msg_id=m.msg_id
        WHERE (m.peer_id, m.msg_id) IN (SELECT peer_id, msg_id FROM ancestors)
           OR (p.slug=? AND m.reply_to=?)
        GROUP BY m.peer_id, m.msg_id
        ORDER BY m.ts ASC, m.msg_id ASC
        """,
        (slug, msg_id, slug, msg_id),
    )
    return [_message_dict(row) for row in rows]


def peers(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT p.slug, p.title, p.kind, p.watched, p.sendable,
                   s.last_msg_id, s.last_synced_at, s.cooldown_until
            FROM peers p LEFT JOIN sync_state s ON s.peer_id=p.peer_id
            ORDER BY p.priority DESC, p.slug ASC
            """
        )
    ]


def doctor(connection: sqlite3.Connection, *, now: int | None = None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    checks.append({"check": "integrity", "ok": integrity == "ok", "detail": integrity})
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    checks.append({"check": "foreign_keys", "ok": not foreign, "detail": str(len(foreign))})
    missing_state = int(
        connection.execute(
            """
            SELECT count(*) FROM peers p LEFT JOIN sync_state s ON s.peer_id=p.peer_id
            WHERE p.watched=1 AND s.peer_id IS NULL
            """
        ).fetchone()[0]
    )
    checks.append({"check": "sync_state", "ok": missing_state == 0, "detail": str(missing_state)})
    cursor_behind = int(
        connection.execute(
            """
            SELECT count(*) FROM sync_state s
            WHERE s.last_msg_id < COALESCE(
                (SELECT max(m.msg_id) FROM messages m WHERE m.peer_id=s.peer_id), 0
            )
            """
        ).fetchone()[0]
    )
    checks.append({"check": "cursor", "ok": cursor_behind == 0, "detail": str(cursor_behind)})
    gaps = int(
        connection.execute(
            """
            SELECT count(*) FROM (
                SELECT msg_id,
                       lead(msg_id) OVER (PARTITION BY peer_id ORDER BY msg_id) AS next_id
                FROM messages
            ) WHERE next_id > msg_id + 1
            """
        ).fetchone()[0]
    )
    checks.append({"check": "message_gaps", "ok": gaps == 0, "detail": str(gaps)})
    messages = int(connection.execute("SELECT count(*) FROM messages").fetchone()[0])
    fts = int(connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0])
    checks.append({"check": "fts", "ok": messages == fts, "detail": f"{fts}/{messages}"})
    stale_before = (now or int(time.time())) - 180
    stale = int(
        connection.execute(
            """
            SELECT count(*) FROM peers p JOIN sync_state s ON s.peer_id=p.peer_id
            WHERE p.watched=1
              AND (s.last_synced_at IS NULL OR s.last_synced_at < ?)
            """,
            (stale_before,),
        ).fetchone()[0]
    )
    checks.append({"check": "stale_sync", "ok": stale == 0, "detail": str(stale)})
    return checks


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    tags = sorted(filter(None, str(row["tags"]).split(",")))
    return {
        "id": f"{row['slug']}#{row['msg_id']}",
        "peer": row["slug"],
        "ts": row["ts"],
        "from": row["sender_name"],
        "text": row["text"],
        "tags": tags,
        "reply_to": row["reply_to"],
        "media": row["media_kind"],
    }

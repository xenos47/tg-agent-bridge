"""Moderated outbox policy and state transitions."""

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tgbridge.cli.errors import PolicyError
from tgbridge.config import Config
from tgbridge.db import transaction


@dataclass(frozen=True)
class OutboxItem:
    item_id: int
    peer_id: int
    body: str
    reply_to: int | None


class Outbox:
    def __init__(
        self,
        connection: sqlite3.Connection,
        config: Config,
        *,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.connection = connection
        self.config = config
        self.now = now

    def enqueue(
        self,
        slug: str,
        body: str,
        *,
        reply_to: int | None = None,
        actor: str = "agent",
        dry_run: bool = False,
    ) -> int | None:
        peer_id = self._validate(slug, body)
        if dry_run:
            return None
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO outbox(peer_id, body, reply_to, created_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (peer_id, body, reply_to, actor, self.now()),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an outbox id")
            item_id = int(cursor.lastrowid)
            self._audit(actor, "outbox.enqueue", str(item_id), {"peer": slug})
        return item_id

    def decide(
        self,
        item_id: int,
        decision: str,
        *,
        actor: str = "human",
        dry_run: bool = False,
    ) -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        row = self.connection.execute(
            "SELECT status FROM outbox WHERE id=?", (item_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"outbox item {item_id} not found")
        if row["status"] != "pending":
            raise PolicyError(f"outbox item {item_id} is not pending")
        if dry_run:
            return
        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE outbox SET status=?, decided_by=?, decided_at=?
                WHERE id=? AND status='pending'
                """,
                (decision, actor, self.now(), item_id),
            )
            self._audit(actor, f"outbox.{decision}", str(item_id), {})

    def approved(self) -> list[OutboxItem]:
        rows = self.connection.execute(
            """
            SELECT id, peer_id, body, reply_to FROM outbox
            WHERE status='approved' ORDER BY created_at ASC, id ASC
            """
        )
        return [
            OutboxItem(int(row["id"]), int(row["peer_id"]), str(row["body"]), row["reply_to"])
            for row in rows
        ]

    def validate_before_send(self, item: OutboxItem) -> str:
        row = self.connection.execute(
            "SELECT slug FROM peers WHERE peer_id=? AND sendable=1", (item.peer_id,)
        ).fetchone()
        if row is None:
            raise PolicyError("peer is no longer sendable")
        self._validate(str(row["slug"]), item.body)
        return str(row["slug"])

    def mark_sent(self, item: OutboxItem, sent_msg_id: int, *, actor: str = "sender") -> None:
        timestamp = self.now()
        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE outbox SET status='sent', sent_msg_id=?, sent_at=?, error=NULL
                WHERE id=? AND status='approved'
                """,
                (sent_msg_id, timestamp, item.item_id),
            )
            hour = timestamp - timestamp % 3600
            for scope in ("global", f"peer:{item.peer_id}"):
                self.connection.execute(
                    """
                    INSERT INTO rate_limit(scope, window_start, count) VALUES (?, ?, 1)
                    ON CONFLICT(scope, window_start) DO UPDATE SET count=count+1
                    """,
                    (scope, hour),
                )
            self._audit(actor, "outbox.send", str(item.item_id), {"msg_id": sent_msg_id})

    def mark_failed(self, item: OutboxItem, error: str, *, actor: str = "sender") -> None:
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE outbox SET status='failed', error=? WHERE id=? AND status='approved'",
                (error, item.item_id),
            )
            self._audit(actor, "outbox.fail", str(item.item_id), {"error": error})

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE o.status=?" if status else ""
        params = (status,) if status else ()
        return [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT o.id, p.slug AS peer, o.body, o.reply_to, o.status,
                       o.created_by, o.created_at, o.decided_by, o.decided_at,
                       o.sent_msg_id, o.sent_at, o.error
                FROM outbox o JOIN peers p ON p.peer_id=o.peer_id
                {where} ORDER BY o.created_at DESC, o.id DESC
                """,
                params,
            )
        ]

    def _validate(self, slug: str, body: str) -> int:
        if not self.config.allow_send:
            raise PolicyError("sending is disabled")
        if not body.strip() or len(body) > 4096:
            raise PolicyError("message body must contain 1..4096 characters")
        row = self.connection.execute(
            "SELECT peer_id FROM peers WHERE slug=? AND sendable=1", (slug,)
        ).fetchone()
        if row is None:
            raise PolicyError(f"peer {slug!r} is not sendable")
        peer_id = int(row["peer_id"])
        self._check_rate_limits(peer_id)
        return peer_id

    def _check_rate_limits(self, peer_id: int) -> None:
        timestamp = self.now()
        hour = timestamp - timestamp % 3600
        limits = self.config.rate_limits
        for scope, limit in (
            ("global", limits.global_per_hour),
            (f"peer:{peer_id}", limits.peer_per_hour),
        ):
            row = self.connection.execute(
                "SELECT count FROM rate_limit WHERE scope=? AND window_start=?",
                (scope, hour),
            ).fetchone()
            if row is not None and int(row["count"]) >= limit:
                raise PolicyError(f"rate limit exceeded for {scope}")
        last_sent = self.connection.execute(
            "SELECT max(sent_at) FROM outbox WHERE status='sent'"
        ).fetchone()[0]
        if last_sent is not None and timestamp - int(last_sent) < limits.minimum_gap_seconds:
            raise PolicyError("minimum send gap has not elapsed")

    def _audit(self, actor: str, action: str, target: str, detail: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO audit(ts, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (self.now(), actor, action, target, json.dumps(detail, separators=(",", ":"))),
        )

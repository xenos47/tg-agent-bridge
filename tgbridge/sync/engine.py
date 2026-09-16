"""Incremental, restart-safe synchronization engine."""

import random
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from tgbridge.db import transaction
from tgbridge.sync.models import Message, Peer, TagRule


class HistoryClient(Protocol):
    def iter_messages(
        self,
        peer_id: int,
        *,
        min_id: int = 0,
        offset_id: int = 0,
        reverse: bool = False,
        limit: int | None = None,
    ) -> AsyncIterator[Message]: ...


@dataclass(frozen=True)
class SyncResult:
    peer_id: int
    mode: str
    fetched: int
    written: int
    first_id: int | None = None
    last_id: int | None = None


class SyncEngine:
    def __init__(
        self,
        connection: sqlite3.Connection,
        client: HistoryClient,
        *,
        rules: Sequence[TagRule] = (),
        batch_size: int = 150,
        now: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.connection = connection
        self.client = client
        self.rules = tuple(rules)
        self.batch_size = batch_size
        self.now = now

    def register_peer(self, peer: Peer, *, dry_run: bool = False) -> None:
        """Register a watchlisted peer without ever renaming an existing slug."""
        timestamp = self.now()
        existing = self.connection.execute(
            "SELECT slug FROM peers WHERE peer_id=?", (peer.peer_id,)
        ).fetchone()
        if existing is not None and existing["slug"] != peer.slug:
            raise ValueError(
                f"peer {peer.peer_id} already has immutable slug {existing['slug']!r}"
            )
        if dry_run:
            return
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO peers
                    (peer_id, kind, title, username, slug, watched, sendable, priority, added_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(peer_id) DO UPDATE SET
                    kind=excluded.kind, title=excluded.title, username=excluded.username,
                    watched=1, sendable=excluded.sendable, priority=excluded.priority
                """,
                (
                    peer.peer_id,
                    peer.kind,
                    peer.title,
                    peer.username,
                    peer.slug,
                    int(peer.sendable),
                    peer.priority,
                    timestamp,
                ),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO sync_state(peer_id) VALUES (?)", (peer.peer_id,)
            )

    async def incremental(self, peer: Peer, *, dry_run: bool = False) -> SyncResult:
        state = self._state(peer.peer_id, allow_missing=dry_run)
        if state is None:
            messages = await self._collect(peer.peer_id, min_id=0, reverse=True)
            return self._result(peer.peer_id, "incremental", messages, 0)
        if self._cooling_down(state):
            return SyncResult(peer.peer_id, "incremental", 0, 0)
        messages = await self._collect(
            peer.peer_id, min_id=int(state["last_msg_id"]), reverse=True
        )
        return self._persist(peer.peer_id, messages, "incremental", dry_run=dry_run)

    async def backfill(self, peer: Peer, *, dry_run: bool = False) -> SyncResult:
        state = self._state(peer.peer_id, allow_missing=dry_run)
        if state is None:
            messages = await self._collect(peer.peer_id, limit=self.batch_size)
            return self._result(peer.peer_id, "backfill", messages, 0)
        if int(state["backfill_done"]):
            return SyncResult(peer.peer_id, "backfill", 0, 0)
        offset = state["backfill_cursor"]
        messages = await self._collect(
            peer.peer_id, offset_id=int(offset or 0), limit=self.batch_size
        )
        if dry_run:
            return self._result(peer.peer_id, "backfill", messages, 0)
        with transaction(self.connection):
            self.upsert_many(messages)
            cursor = min((message.msg_id for message in messages), default=None)
            self.connection.execute(
                """
                UPDATE sync_state
                SET backfill_cursor=?, backfill_done=?, last_synced_at=?,
                    last_error=NULL, error_count=0
                WHERE peer_id=?
                """,
                (cursor, int(len(messages) < self.batch_size), self.now(), peer.peer_id),
            )
        return self._result(peer.peer_id, "backfill", messages, len(messages))

    async def rescan(
        self, peer: Peer, *, window: int = 200, dry_run: bool = False
    ) -> SyncResult:
        remote = await self._collect(peer.peer_id, limit=window)
        local_rows = self.connection.execute(
            """
            SELECT msg_id FROM messages
            WHERE peer_id=? ORDER BY msg_id DESC LIMIT ?
            """,
            (peer.peer_id, window),
        ).fetchall()
        remote_ids = {message.msg_id for message in remote}
        deleted = [int(row["msg_id"]) for row in local_rows if int(row["msg_id"]) not in remote_ids]
        if dry_run:
            return self._result(peer.peer_id, "rescan", remote, 0)
        with transaction(self.connection):
            self.upsert_many(remote)
            if deleted:
                placeholders = ",".join("?" for _ in deleted)
                self.connection.execute(
                    f"""
                    UPDATE messages SET deleted_at=?
                    WHERE peer_id=? AND msg_id IN ({placeholders}) AND deleted_at IS NULL
                    """,
                    (self.now(), peer.peer_id, *deleted),
                )
        return self._result(peer.peer_id, "rescan", remote, len(remote) + len(deleted))

    def record_flood_wait(self, peer_id: int, seconds: int) -> int:
        until = self.now() + seconds + random.randint(5, 30)
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_state
                SET cooldown_until=?, last_error=?, error_count=error_count+1
                WHERE peer_id=?
                """,
                (until, f"FloodWait: {seconds}s", peer_id),
            )
        return until

    def record_error(self, peer_id: int, error: Exception) -> int | None:
        """Persist failures and cool down a peer after five consecutive errors."""
        row = self._state(peer_id)
        if row is None:
            raise ValueError(f"peer {peer_id} is not registered")
        count = int(row["error_count"]) + 1
        cooldown = self.now() + 900 if count >= 5 else None
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_state
                SET last_error=?, error_count=?, cooldown_until=?
                WHERE peer_id=?
                """,
                (str(error), count, cooldown, peer_id),
            )
        return cooldown

    async def _collect(
        self,
        peer_id: int,
        *,
        min_id: int = 0,
        offset_id: int = 0,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        async for message in self.client.iter_messages(
            peer_id,
            min_id=min_id,
            offset_id=offset_id,
            reverse=reverse,
            limit=limit,
        ):
            messages.append(message)
            if len(messages) >= self.batch_size:
                break
        return messages

    def _persist(
        self, peer_id: int, messages: list[Message], mode: str, *, dry_run: bool
    ) -> SyncResult:
        if dry_run or not messages:
            return self._result(peer_id, mode, messages, 0 if dry_run else len(messages))
        with transaction(self.connection):
            self.upsert_many(messages)
            self.connection.execute(
                """
                UPDATE sync_state
                SET last_msg_id=?, last_synced_at=?, last_error=NULL,
                    error_count=0, cooldown_until=NULL
                WHERE peer_id=?
                """,
                (max(message.msg_id for message in messages), self.now(), peer_id),
            )
        return self._result(peer_id, mode, messages, len(messages))

    def upsert_many(self, messages: Sequence[Message]) -> None:
        for message in messages:
            self.connection.execute(
                """
                INSERT INTO messages
                    (peer_id, msg_id, ts, sender_id, sender_name, text, reply_to, fwd_from,
                     has_media, media_kind, raw_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(peer_id, msg_id) DO UPDATE SET
                    ts=excluded.ts, sender_id=excluded.sender_id,
                    sender_name=excluded.sender_name, text=excluded.text,
                    reply_to=excluded.reply_to, fwd_from=excluded.fwd_from,
                    has_media=excluded.has_media, media_kind=excluded.media_kind,
                    edit_ts=CASE WHEN messages.text != excluded.text
                                 THEN excluded.synced_at ELSE messages.edit_ts END,
                    raw_json=excluded.raw_json, synced_at=excluded.synced_at,
                    deleted_at=NULL
                """,
                (
                    message.peer_id,
                    message.msg_id,
                    message.ts,
                    message.sender_id,
                    message.sender_name,
                    message.text,
                    message.reply_to,
                    message.fwd_from,
                    int(message.has_media),
                    message.media_kind,
                    message.raw_json,
                    self.now(),
                ),
            )
            self.apply_rules(message)

    def apply_rules(self, message: Message) -> None:
        for rule in self.rules:
            text_matches = rule.text_regex is None or re.search(rule.text_regex, message.text)
            sender_matches = rule.sender_id is None or rule.sender_id == message.sender_id
            if text_matches and sender_matches:
                self.connection.execute(
                    """
                    INSERT INTO tags(peer_id, msg_id, tag, source, rule_id, created_at)
                    VALUES (?, ?, ?, 'rule', ?, ?)
                    ON CONFLICT(peer_id, msg_id, tag) DO UPDATE SET
                        source='rule', rule_id=excluded.rule_id
                    """,
                    (message.peer_id, message.msg_id, rule.tag, rule.rule_id, self.now()),
                )

    def _state(self, peer_id: int, *, allow_missing: bool = False) -> sqlite3.Row | None:
        row = self.connection.execute(
            "SELECT * FROM sync_state WHERE peer_id=?", (peer_id,)
        ).fetchone()
        if row is None and not allow_missing:
            raise ValueError(f"peer {peer_id} is not registered")
        return row

    def _cooling_down(self, state: sqlite3.Row) -> bool:
        cooldown = state["cooldown_until"]
        last_synced = state["last_synced_at"]
        return bool(
            (cooldown is not None and int(cooldown) > self.now())
            or (last_synced is not None and self.now() - int(last_synced) < 60)
        )

    @staticmethod
    def _result(
        peer_id: int, mode: str, messages: Sequence[Message], written: int
    ) -> SyncResult:
        return SyncResult(
            peer_id=peer_id,
            mode=mode,
            fetched=len(messages),
            written=written,
            first_id=messages[0].msg_id if messages else None,
            last_id=messages[-1].msg_id if messages else None,
        )

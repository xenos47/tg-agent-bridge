import json
import sqlite3
from collections.abc import AsyncIterator

import pytest

from tgbridge.sync.engine import SyncEngine
from tgbridge.sync.models import Message, Peer, TagRule


class FakeClient:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages

    async def iter_messages(
        self,
        peer_id: int,
        *,
        min_id: int = 0,
        offset_id: int = 0,
        reverse: bool = False,
        limit: int | None = None,
    ) -> AsyncIterator[Message]:
        values = [
            item
            for item in self.messages
            if item.peer_id == peer_id
            and item.msg_id > min_id
            and (offset_id == 0 or item.msg_id < offset_id)
        ]
        values.sort(key=lambda item: item.msg_id, reverse=not reverse)
        for item in values[:limit]:
            yield item


def msg(msg_id: int, text: str = "hello") -> Message:
    return Message(
        peer_id=1,
        msg_id=msg_id,
        ts=1700000000 + msg_id,
        text=text,
        sender_id=10,
        raw_json=json.dumps({"id": msg_id, "message": text}),
    )


@pytest.mark.asyncio
async def test_incremental_replay_is_safe_and_atomic(db: sqlite3.Connection) -> None:
    engine = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    peer = Peer(1, "work", "group", "Work")
    engine.register_peer(peer)
    result = await engine.incremental(peer)
    assert result.written == 2
    assert db.execute("SELECT last_msg_id FROM sync_state").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2

    replay = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001061)
    assert (await replay.incremental(peer)).fetched == 0
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_dry_run_does_not_move_cursor(db: sqlite3.Connection) -> None:
    engine = SyncEngine(db, FakeClient([msg(1)]), now=lambda: 1700001000)
    peer = Peer(1, "work", "group", "Work")
    engine.register_peer(peer)
    assert (await engine.incremental(peer, dry_run=True)).fetched == 1
    assert db.execute("SELECT last_msg_id FROM sync_state").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_rescan_detects_edit_and_soft_delete(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1, "old"), msg(2)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)
    rescan = SyncEngine(db, FakeClient([msg(1, "new")]), now=lambda: 1700002000)
    await rescan.rescan(peer)
    rows = db.execute(
        "SELECT msg_id,text,edit_ts,deleted_at FROM messages ORDER BY msg_id"
    ).fetchall()
    assert tuple(rows[0]) == (1, "new", 1700002000, None)
    assert tuple(rows[1]) == (2, "hello", None, 1700002000)


@pytest.mark.asyncio
async def test_rules_and_flood_wait_are_persisted(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    engine = SyncEngine(
        db,
        FakeClient([msg(1, "срочно")]),
        rules=[TagRule("urgent-ru", "urgent", text_regex="срочно")],
        now=lambda: 1700001000,
    )
    engine.register_peer(peer)
    await engine.incremental(peer)
    assert tuple(db.execute("SELECT tag,source,rule_id FROM tags").fetchone()) == (
        "urgent",
        "rule",
        "urgent-ru",
    )
    until = engine.record_flood_wait(1, 60)
    assert until >= 1700001065
    assert db.execute("SELECT cooldown_until FROM sync_state").fetchone()[0] == until

import json
import sqlite3
from collections.abc import AsyncIterator

import pytest

from tgbridge.logging import configure_logging
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
async def test_rescan_window_larger_than_batch_size_does_not_wrongly_delete(
    db: sqlite3.Connection,
) -> None:
    peer = Peer(1, "work", "group", "Work")
    client = FakeClient([msg(i) for i in range(1, 161)])
    first = SyncEngine(db, client, batch_size=150, now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)  # capped at batch_size: writes 1..150

    second = SyncEngine(db, client, batch_size=150, now=lambda: 1700001100)
    await second.incremental(peer)  # writes the remaining 151..160
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 160

    rescan = SyncEngine(db, client, batch_size=150, now=lambda: 1700002000)
    result = await rescan.rescan(peer, window=200)

    assert result.fetched == 160
    assert result.written == 0  # nothing changed, nothing rewritten
    assert (
        db.execute("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL").fetchone()[0]
        == 0
    )
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] == 1700002000


@pytest.mark.asyncio
async def test_rescan_empty_remote_does_not_mass_delete(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)

    empty_rescan = SyncEngine(db, FakeClient([]), now=lambda: 1700005000)
    result = await empty_rescan.rescan(peer)

    assert result.fetched == 0
    assert result.written == 0
    assert (
        db.execute("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL").fetchone()[0]
        == 0
    )
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] == 1700005000


@pytest.mark.asyncio
async def test_rescan_dry_run_does_not_touch_state(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1, "old")]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)

    dry = SyncEngine(db, FakeClient([msg(1, "new")]), now=lambda: 1700005000)
    result = await dry.rescan(peer, dry_run=True)

    assert result.fetched == 1
    assert db.execute("SELECT text FROM messages WHERE msg_id=1").fetchone()[0] == "old"
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] is None


@pytest.mark.asyncio
async def test_rescan_is_throttled_per_peer(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    client = FakeClient([msg(1), msg(2)])
    engine = SyncEngine(db, client, now=lambda: 1700001000)
    engine.register_peer(peer)
    await engine.incremental(peer)

    first_rescan = SyncEngine(db, client, now=lambda: 1700005000)
    assert (await first_rescan.rescan(peer, interval=3600)).fetched == 2

    soon_after = SyncEngine(db, client, now=lambda: 1700005100)
    skipped = await soon_after.rescan(peer, interval=3600)
    assert skipped.fetched == 0
    assert skipped.written == 0
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] == 1700005000

    once_due = SyncEngine(db, client, now=lambda: 1700005000 + 3601)
    due = await once_due.rescan(peer, interval=3600)
    assert due.fetched == 2


@pytest.mark.asyncio
async def test_rescan_skips_peer_in_cooldown(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    engine = SyncEngine(db, FakeClient([msg(1)]), now=lambda: 1700001000)
    engine.register_peer(peer)
    engine.record_flood_wait(peer.peer_id, 3600)

    result = await engine.rescan(peer)
    assert result.fetched == 0
    assert result.written == 0
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] is None


@pytest.mark.asyncio
async def test_rescan_runs_in_same_tick_as_incremental_and_backfill(
    db: sqlite3.Connection,
) -> None:
    """run_sync calls all three with one clock; rescan must not hit the 60s floor."""
    peer = Peer(1, "work", "group", "Work")
    client = FakeClient([msg(1, "old"), msg(2)])
    clock = [1700001000]
    engine = SyncEngine(db, client, now=lambda: clock[0])
    engine.register_peer(peer)
    await engine.incremental(peer)
    await engine.backfill(peer)
    assert (await engine.rescan(peer)).fetched == 2
    assert db.execute("SELECT last_rescan_at FROM sync_state").fetchone()[0] == 1700001000

    clock[0] += 3600
    client.messages = [msg(1, "new"), msg(3)]
    await engine.incremental(peer)
    await engine.backfill(peer)
    result = await engine.rescan(peer)
    assert result.written == 2  # one edit, one deletion
    rows = db.execute("SELECT msg_id,text,deleted_at FROM messages ORDER BY msg_id").fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "new", None),
        (2, "hello", 1700004600),
        (3, "hello", None),
    ]


@pytest.mark.asyncio
async def test_rescan_leaves_new_messages_to_incremental(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)

    rescan = SyncEngine(db, FakeClient([msg(1), msg(2), msg(3)]), now=lambda: 1700005000)
    result = await rescan.rescan(peer)

    assert result.fetched == 3
    assert result.written == 0
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
    assert db.execute("SELECT last_msg_id FROM sync_state").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_rescan_does_not_rewrite_unchanged_rows(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)

    rescan = SyncEngine(db, FakeClient([msg(1), msg(2, "edited")]), now=lambda: 1700005000)
    assert (await rescan.rescan(peer)).written == 1
    synced = dict(db.execute("SELECT msg_id,synced_at FROM messages").fetchall())
    assert synced == {1: 1700001000, 2: 1700005000}


@pytest.mark.asyncio
async def test_rescan_restores_message_that_reappears(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)
    db.execute("UPDATE messages SET deleted_at=1700002000 WHERE msg_id=2")
    db.commit()

    rescan = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700005000)
    assert (await rescan.rescan(peer)).written == 1
    assert db.execute("SELECT deleted_at FROM messages WHERE msg_id=2").fetchone()[0] is None


@pytest.mark.asyncio
async def test_rescan_dry_run_previews_changes(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    peer = Peer(1, "work", "group", "Work")
    first = SyncEngine(db, FakeClient([msg(1, "old"), msg(2), msg(3)]), now=lambda: 1700001000)
    first.register_peer(peer)
    await first.incremental(peer)

    configure_logging(verbose=True)
    dry = SyncEngine(db, FakeClient([msg(1, "new"), msg(3)]), now=lambda: 1700005000)
    await dry.rescan(peer, dry_run=True)

    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert {
        "level": "info",
        "event": "peer_rescan_completed",
        "peer": "work",
        "fetched": 2,
        "updated": 1,
        "deleted": 1,
        "result": "dry_run",
    } in events
    assert (
        db.execute("SELECT count(*) FROM messages WHERE deleted_at IS NOT NULL").fetchone()[0]
        == 0
    )


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


@pytest.mark.asyncio
async def test_batch_and_cursor_roll_back_together(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    engine = SyncEngine(db, FakeClient([msg(1), msg(2)]), now=lambda: 1700001000)
    engine.register_peer(peer)
    db.execute(
        """
        CREATE TRIGGER fail_second BEFORE INSERT ON messages
        WHEN new.msg_id=2 BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        await engine.incremental(peer)
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert db.execute("SELECT last_msg_id FROM sync_state").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_backfill_cursor_is_independent_from_fresh_cursor(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    fresh = SyncEngine(db, FakeClient([msg(3)]), now=lambda: 1700001000)
    fresh.register_peer(peer)
    await fresh.incremental(peer)
    history = SyncEngine(db, FakeClient([msg(1), msg(2), msg(3)]), now=lambda: 1700001061)
    await history.backfill(peer)
    state = db.execute(
        "SELECT last_msg_id,backfill_cursor,backfill_done FROM sync_state"
    ).fetchone()
    assert tuple(state) == (3, 1, 1)
    assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 3


def test_repeated_errors_persist_cooldown(db: sqlite3.Connection) -> None:
    peer = Peer(1, "work", "group", "Work")
    engine = SyncEngine(db, FakeClient([]), now=lambda: 1700001000)
    engine.register_peer(peer)
    for _ in range(4):
        assert engine.record_error(1, RuntimeError("broken")) is None
    assert engine.record_error(1, RuntimeError("broken")) == 1700001900
    row = db.execute(
        "SELECT error_count,cooldown_until,last_error FROM sync_state"
    ).fetchone()
    assert tuple(row) == (5, 1700001900, "broken")

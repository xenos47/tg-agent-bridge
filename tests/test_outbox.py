import sqlite3

import pytest

from tests.factories import peer
from tgbridge.cli.errors import PolicyError
from tgbridge.config import Config
from tgbridge.outbox import Outbox
from tgbridge.sync.models import Peer
from tgbridge.sync.sender import send_approved


def config(*, allow_send: bool = True) -> Config:
    return Config(
        peers=(Peer(1, "lena", "user", "Lena", sendable=True),),
        allow_send=allow_send,
    )


def test_enqueue_requires_global_and_peer_permission(db: sqlite3.Connection) -> None:
    peer(db, slug="lena", sendable=True)
    with pytest.raises(PolicyError, match="disabled"):
        Outbox(db, config(allow_send=False)).enqueue("lena", "hello")
    item_id = Outbox(db, config()).enqueue("lena", "hello")
    assert item_id == 1
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "pending"


def test_dry_run_never_writes(db: sqlite3.Connection) -> None:
    peer(db, slug="lena", sendable=True)
    assert Outbox(db, config()).enqueue("lena", "hello", dry_run=True) is None
    assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_only_pending_item_can_be_decided(db: sqlite3.Connection) -> None:
    peer(db, slug="lena", sendable=True)
    outbox = Outbox(db, config(), now=lambda: 1700000000)
    item_id = outbox.enqueue("lena", "hello")
    assert item_id is not None
    outbox.decide(item_id, "approved")
    with pytest.raises(PolicyError):
        outbox.decide(item_id, "rejected")
    assert db.execute("SELECT count(*) FROM audit").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_sender_reads_only_approved_and_marks_sent(db: sqlite3.Connection) -> None:
    peer(db, slug="lena", sendable=True)
    outbox = Outbox(db, config(), now=lambda: 1700000000)
    pending = outbox.enqueue("lena", "pending")
    approved = outbox.enqueue("lena", "approved")
    assert pending is not None and approved is not None
    outbox.decide(approved, "approved")

    class Client:
        async def send_message(
            self, entity: int, message: str, *, reply_to: int | None = None
        ) -> object:
            assert entity == 1
            assert message == "approved"
            return type("Sent", (), {"id": 99})()

    assert await send_approved(outbox, Client()) == 1
    statuses = {
        row["body"]: row["status"] for row in db.execute("SELECT body,status FROM outbox")
    }
    assert statuses == {"pending": "pending", "approved": "sent"}


@pytest.mark.asyncio
async def test_sender_surfaces_policy_denial(db: sqlite3.Connection) -> None:
    peer(db, slug="lena", sendable=True)
    outbox = Outbox(db, config(), now=lambda: 1700000000)
    item_id = outbox.enqueue("lena", "approved")
    assert item_id is not None
    outbox.decide(item_id, "approved")
    db.execute("UPDATE peers SET sendable=0 WHERE peer_id=1")
    db.commit()

    class Client:
        async def send_message(
            self, entity: int, message: str, *, reply_to: int | None = None
        ) -> object:
            raise AssertionError("policy denial must happen before Telegram")

    with pytest.raises(PolicyError):
        await send_approved(outbox, Client())
    assert db.execute("SELECT status FROM outbox").fetchone()[0] == "failed"

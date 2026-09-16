import sqlite3

import pytest

from tests.factories import message, peer
from tgbridge.db import migrate


def test_pragmas_and_migrations_are_idempotent(db: sqlite3.Connection) -> None:
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert migrate(db) == 1
    assert migrate(db) == 1


def test_message_identity_is_composite(db: sqlite3.Connection) -> None:
    peer(db, peer_id=1, slug="one")
    peer(db, peer_id=2, slug="two")
    message(db, peer_id=1, msg_id=7)
    message(db, peer_id=2, msg_id=7)
    assert db.execute("SELECT count(*) FROM messages WHERE msg_id=7").fetchone()[0] == 2


def test_raw_json_and_foreign_keys_are_enforced(db: sqlite3.Connection) -> None:
    peer(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO messages(peer_id,msg_id,ts,raw_json,synced_at) VALUES (1,1,1,NULL,1)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO tags(peer_id,msg_id,tag,source,created_at)
            VALUES (1,99,'urgent','rule',1)
            """
        )


def test_fts_tracks_insert_update_and_delete(db: sqlite3.Connection) -> None:
    peer(db)
    message(db, text="initial needle")
    needle = db.execute(
        "SELECT count(*) FROM messages_fts WHERE text MATCH 'needle'"
    ).fetchone()[0]
    assert needle == 1
    db.execute("UPDATE messages SET text='updated value' WHERE peer_id=1 AND msg_id=1")
    updated = db.execute(
        "SELECT count(*) FROM messages_fts WHERE text MATCH 'updated'"
    ).fetchone()[0]
    assert updated == 1
    db.execute("DELETE FROM messages WHERE peer_id=1 AND msg_id=1")
    deleted = db.execute(
        "SELECT count(*) FROM messages_fts WHERE text MATCH 'updated'"
    ).fetchone()[0]
    assert deleted == 0


def test_slug_is_immutable(db: sqlite3.Connection) -> None:
    peer(db)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE peers SET slug='renamed' WHERE peer_id=1")


def test_soft_delete_keeps_message(db: sqlite3.Connection) -> None:
    peer(db)
    message(db)
    db.execute("UPDATE messages SET deleted_at=1700000010 WHERE peer_id=1 AND msg_id=1")
    assert db.execute("SELECT deleted_at FROM messages").fetchone()[0] == 1700000010

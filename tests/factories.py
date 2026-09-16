import json
import sqlite3


def peer(
    db: sqlite3.Connection,
    *,
    peer_id: int = 1,
    slug: str = "work-chat",
    sendable: bool = False,
) -> None:
    db.execute(
        """
        INSERT INTO peers(peer_id, kind, title, slug, sendable, added_at)
        VALUES (?, 'group', ?, ?, ?, 1700000000)
        """,
        (peer_id, slug, slug, int(sendable)),
    )
    db.execute("INSERT INTO sync_state(peer_id) VALUES (?)", (peer_id,))
    db.commit()


def message(
    db: sqlite3.Connection,
    *,
    peer_id: int = 1,
    msg_id: int = 1,
    text: str = "hello",
    sender_name: str = "Lena",
    reply_to: int | None = None,
) -> None:
    raw = json.dumps({"id": msg_id, "message": text})
    db.execute(
        """
        INSERT INTO messages(
            peer_id, msg_id, ts, sender_name, text, reply_to, raw_json, synced_at
        ) VALUES (?, ?, 1700000000, ?, ?, ?, ?, 1700000001)
        """,
        (peer_id, msg_id, sender_name, text, reply_to, raw),
    )
    db.commit()

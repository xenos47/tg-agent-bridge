"""Transport-neutral sync models."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SyncPolicy:
    """How much of a peer's history to mirror, in seconds; None means unbounded."""

    history: int | None = None
    retention: int | None = None


@dataclass(frozen=True)
class Peer:
    peer_id: int
    slug: str
    kind: str
    title: str
    username: str | None = None
    sendable: bool = False
    priority: int = 0
    policy: SyncPolicy = field(default_factory=SyncPolicy)


@dataclass(frozen=True)
class Message:
    peer_id: int
    msg_id: int
    ts: int
    text: str
    raw_json: str
    sender_id: int | None = None
    sender_name: str | None = None
    reply_to: int | None = None
    fwd_from: str | None = None
    has_media: bool = False
    media_kind: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TagRule:
    rule_id: str
    tag: str
    text_regex: str | None = None
    sender_id: int | None = None

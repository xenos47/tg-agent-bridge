"""Configuration and watchlist loading."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tgbridge.sync.models import Peer, TagRule


@dataclass(frozen=True)
class RateLimits:
    global_per_hour: int = 20
    peer_per_hour: int = 5
    minimum_gap_seconds: int = 30


@dataclass(frozen=True)
class Config:
    peers: tuple[Peer, ...]
    rules: tuple[TagRule, ...] = ()
    allow_send: bool = False
    rate_limits: RateLimits = field(default_factory=RateLimits)


def _positive(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int) and value > 0 else default


def load_config(path: str | Path) -> Config:
    """Load a privacy-bounded watchlist and policy configuration."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    peers = tuple(
        Peer(
            peer_id=int(item["id"]),
            slug=str(item["slug"]),
            kind=str(item["kind"]),
            title=str(item.get("title", item["slug"])),
            username=item.get("username"),
            sendable=bool(item.get("sendable", False)),
            priority=int(item.get("priority", 0)),
        )
        for item in raw.get("peers", [])
    )
    rules = tuple(
        TagRule(
            rule_id=str(item["id"]),
            tag=str(item["tag"]),
            text_regex=item.get("match", {}).get("text_regex"),
            sender_id=item.get("match", {}).get("sender_id"),
        )
        for item in raw.get("rules", [])
    )
    limits = raw.get("rate_limits", {})
    return Config(
        peers=peers,
        rules=rules,
        allow_send=bool(raw.get("allow_send", False)),
        rate_limits=RateLimits(
            global_per_hour=_positive(limits.get("global_per_hour"), 20),
            peer_per_hour=_positive(limits.get("peer_per_hour"), 5),
            minimum_gap_seconds=_positive(limits.get("minimum_gap_seconds"), 30),
        ),
    )

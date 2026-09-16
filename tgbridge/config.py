"""Configuration and watchlist loading."""

import os
from collections.abc import Mapping
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
class TelegramSettings:
    port: int = 443


@dataclass(frozen=True)
class Config:
    peers: tuple[Peer, ...]
    rules: tuple[TagRule, ...] = ()
    allow_send: bool = False
    rate_limits: RateLimits = field(default_factory=RateLimits)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)


def _positive(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int) and value > 0 else default


def _port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Telegram port must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("Telegram port must be between 1 and 65535")
    return port


def load_config(
    path: str | Path, *, environ: Mapping[str, str] | None = None
) -> Config:
    """Load a privacy-bounded watchlist and policy configuration."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    environment = os.environ if environ is None else environ
    telegram = raw.get("telegram", {})
    configured_port = telegram.get("port", 443)
    selected_port = environment.get("TGQ_TELEGRAM_PORT", configured_port)
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
        telegram=TelegramSettings(port=_port(selected_port)),
    )

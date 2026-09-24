"""Configuration and watchlist loading."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from tgbridge.sync.models import Peer, SyncPolicy, TagRule

_DAY = 86400
_DURATION = re.compile(r"^([1-9]\d*)([dw])$")
# Used when a watchlist has no `default_policy`: shallow backfill, but never
# deletes data that an older unbounded configuration already mirrored.
DEFAULT_POLICY = SyncPolicy(history=14 * _DAY)


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


def _peer_mapping(peer: Peer) -> dict[str, Any]:
    return {
        "slug": peer.slug,
        "id": peer.peer_id,
        "kind": peer.kind,
        "title": peer.title,
        "username": peer.username,
        "sendable": peer.sendable,
    }


def format_peer_yaml(peer: Peer) -> str:
    """Render one peer as a watchlist-compatible YAML list item."""
    return yaml.safe_dump(
        [_peer_mapping(peer)],
        allow_unicode=True,
        sort_keys=False,
    )


def format_peer_candidates_yaml(peers: tuple[Peer, ...] | list[Peer]) -> str:
    """Render ambiguous metadata-only candidates without watchlist policy fields."""
    candidates = [
        {
            "id": peer.peer_id,
            "kind": peer.kind,
            "title": peer.title,
            "username": peer.username,
        }
        for peer in peers
    ]
    return yaml.safe_dump(candidates, allow_unicode=True, sort_keys=False)


def append_peer(path: str | Path, peer: Peer, *, dry_run: bool = False) -> str:
    """Validate and append a resolved peer to a watchlist."""
    target = Path(path)
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("watchlist must be a YAML mapping")
    peer_items = raw.setdefault("peers", [])
    if not isinstance(peer_items, list):
        raise ValueError("watchlist peers must be a YAML list")
    if any(int(item["id"]) == peer.peer_id for item in peer_items):
        raise ValueError(f"peer id already exists in watchlist: {peer.peer_id}")
    if any(str(item["slug"]) == peer.slug for item in peer_items):
        raise ValueError(f"peer slug already exists in watchlist: {peer.slug}")

    fragment = format_peer_yaml(peer)
    if dry_run:
        return fragment
    peer_items.append(_peer_mapping(peer))
    target.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return fragment


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


def _duration(value: Any, label: str) -> int:
    match = _DURATION.match(str(value)) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{label} must be a duration like 14d or 2w, got {value!r}")
    return int(match.group(1)) * _DAY * (7 if match.group(2) == "w" else 1)


def _policy(name: str, raw: Any) -> SyncPolicy:
    label = f"policies.{name}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    unknown = sorted(set(raw) - {"history", "retention"})
    if unknown:
        raise ValueError(f"unknown {label} setting: {unknown[0]}")
    if "history" not in raw:
        raise ValueError(f"{label}.history is required (a duration or 'all')")
    history = None if raw["history"] == "all" else _duration(raw["history"], f"{label}.history")
    retention = raw.get("retention")
    if retention is None:
        return SyncPolicy(history=history)
    retention_seconds = _duration(retention, f"{label}.retention")
    if history is None or retention_seconds < history:
        raise ValueError(
            f"{label}.retention must not be shorter than history; "
            "otherwise backfilled messages are deleted again on every sync"
        )
    return SyncPolicy(history=history, retention=retention_seconds)


def _policies(raw: Mapping[str, Any]) -> tuple[dict[str, SyncPolicy], SyncPolicy]:
    section = raw.get("policies") or {}
    if not isinstance(section, dict):
        raise ValueError("policies must be a YAML mapping")
    policies = {str(name): _policy(str(name), value) for name, value in section.items()}
    default_name = raw.get("default_policy")
    if default_name is None:
        return policies, DEFAULT_POLICY
    if default_name not in policies:
        raise ValueError(f"default_policy refers to unknown policy {default_name!r}")
    return policies, policies[default_name]


def _peer_policy(
    item: Mapping[str, Any], policies: Mapping[str, SyncPolicy], default: SyncPolicy
) -> SyncPolicy:
    name = item.get("policy")
    if name is None:
        return default
    if name not in policies:
        raise ValueError(f"peer {item.get('slug')!r} refers to unknown policy {name!r}")
    return policies[name]


def load_config(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    telegram_port: int | str | None = None,
) -> Config:
    """Load a privacy-bounded watchlist and policy configuration."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    environment = os.environ if environ is None else environ
    policies, default_policy = _policies(raw)
    telegram = raw.get("telegram", {})
    configured_port = telegram.get("port", 443)
    selected_port = (
        telegram_port
        if telegram_port is not None
        else environment.get("TGQ_TELEGRAM_PORT", configured_port)
    )
    peers = tuple(
        Peer(
            peer_id=int(item["id"]),
            slug=str(item["slug"]),
            kind=str(item["kind"]),
            title=str(item.get("title", item["slug"])),
            username=item.get("username"),
            sendable=bool(item.get("sendable", False)),
            priority=int(item.get("priority", 0)),
            policy=_peer_policy(item, policies, default_policy),
        )
        for item in raw.get("peers") or []
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

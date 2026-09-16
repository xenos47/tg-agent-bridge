"""Telethon adapter. No other package imports Telethon."""
# pyright: reportArgumentType=false

import json
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

from telethon import TelegramClient, types, utils

from tgbridge.sync.models import Message
from tgbridge.sync.resolve import ResolvedDialog


class TelethonHistoryClient:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def iter_messages(
        self,
        peer_id: int,
        *,
        min_id: int = 0,
        offset_id: int = 0,
        reverse: bool = False,
        limit: int | None = None,
    ) -> AsyncIterator[Message]:
        iterator = self.client.iter_messages(
            peer_id,
            min_id=min_id,
            offset_id=offset_id,
            reverse=reverse,
            limit=limit,
        )
        async for raw in iterator:
            data: dict[str, Any] = raw.to_dict()
            date = raw.date
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            yield Message(
                peer_id=peer_id,
                msg_id=int(raw.id),
                ts=int(date.timestamp()),
                sender_id=getattr(raw, "sender_id", None),
                sender_name=None,
                text=raw.message or "",
                reply_to=getattr(raw, "reply_to_msg_id", None),
                fwd_from=str(raw.fwd_from) if raw.fwd_from else None,
                has_media=raw.media is not None,
                media_kind=type(raw.media).__name__ if raw.media is not None else None,
                raw_json=json.dumps(data, ensure_ascii=False, default=str, sort_keys=True),
            )


class TelethonResolveClient:
    """Expose only peer metadata needed to compose a watchlist."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def resolve_username(self, username: str) -> ResolvedDialog | None:
        return await self._resolve(username)

    async def resolve_id(self, peer_id: int) -> ResolvedDialog | None:
        return await self._resolve(peer_id)

    async def _resolve(self, value: str | int) -> ResolvedDialog | None:
        try:
            entity = await self.client.get_entity(value)
        except ValueError:
            return None
        return _resolved_dialog(entity)

    async def iter_dialogs(self) -> AsyncIterator[ResolvedDialog]:
        async for dialog in self.client.iter_dialogs():
            yield _resolved_dialog(dialog.entity)


def _resolved_dialog(entity: Any) -> ResolvedDialog:
    if isinstance(entity, types.User):
        kind = "user"
        title = " ".join(
            part for part in (entity.first_name, entity.last_name) if part
        ) or entity.username or str(entity.id)
    elif isinstance(entity, types.Chat):
        kind = "group"
        title = entity.title
    elif isinstance(entity, types.Channel):
        kind = "group" if entity.megagroup else "channel"
        title = entity.title
    else:
        raise ValueError(f"unsupported Telegram peer type: {type(entity).__name__}")
    return ResolvedDialog(
        peer_id=int(utils.get_peer_id(entity)),
        kind=kind,
        title=title,
        username=getattr(entity, "username", None),
    )

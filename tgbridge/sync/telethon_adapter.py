"""Telethon adapter. No other package imports Telethon."""
# pyright: reportArgumentType=false

import json
from collections.abc import AsyncIterator
from datetime import UTC
from typing import Any

from telethon import TelegramClient

from tgbridge.sync.models import Message


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

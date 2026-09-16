"""Separate Telethon sender for approved outbox rows only."""

from typing import Any, Protocol

from tgbridge.cli.errors import PolicyError
from tgbridge.outbox import Outbox


class SendClient(Protocol):
    async def send_message(
        self, entity: int, message: str, *, reply_to: int | None = None
    ) -> Any: ...


async def send_approved(outbox: Outbox, client: SendClient, *, dry_run: bool = False) -> int:
    """Transmit approved rows after repeating all policy checks."""
    sent = 0
    for item in outbox.approved():
        try:
            outbox.validate_before_send(item)
            if dry_run:
                continue
            result = await client.send_message(item.peer_id, item.body, reply_to=item.reply_to)
            outbox.mark_sent(item, int(result.id))
            sent += 1
        except PolicyError as error:
            if not dry_run:
                outbox.mark_failed(item, str(error))
            raise
        except Exception as error:
            if not dry_run:
                outbox.mark_failed(item, str(error))
    return sent

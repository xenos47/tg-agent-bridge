"""Separate Telethon sender for approved outbox rows only."""

import time
from typing import Any, Protocol

from tgbridge.cli.errors import PolicyError
from tgbridge.logging import event, get_logger
from tgbridge.outbox import Outbox

_LOG = get_logger("sync.sender")


class SendClient(Protocol):
    async def send_message(
        self, entity: int, message: str, *, reply_to: int | None = None
    ) -> Any: ...


async def send_approved(outbox: Outbox, client: SendClient, *, dry_run: bool = False) -> int:
    """Transmit approved rows after repeating all policy checks."""
    sent = 0
    for item in outbox.approved():
        started = time.perf_counter()
        try:
            outbox.validate_before_send(item)
            if dry_run:
                event(
                    _LOG,
                    20,
                    "outbox_send_checked",
                    outbox_id=item.item_id,
                    result="dry_run",
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
                continue
            result = await client.send_message(item.peer_id, item.body, reply_to=item.reply_to)
            outbox.mark_sent(item, int(result.id))
            sent += 1
            event(
                _LOG,
                20,
                "outbox_sent",
                outbox_id=item.item_id,
                result="sent",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except PolicyError as error:
            if not dry_run:
                outbox.mark_failed(item, str(error))
            event(
                _LOG,
                30,
                "outbox_policy_denied",
                outbox_id=item.item_id,
                result="denied",
                error_type=type(error).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise
        except Exception as error:
            if not dry_run:
                outbox.mark_failed(item, str(error))
            event(
                _LOG,
                30,
                "outbox_send_failed",
                outbox_id=item.item_id,
                result="failed",
                error_type=type(error).__name__,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
    return sent

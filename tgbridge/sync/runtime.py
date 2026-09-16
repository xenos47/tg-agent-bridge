"""Live Telethon runtime orchestration."""
# pyright: reportGeneralTypeIssues=false, reportArgumentType=false

import asyncio
import random
import sqlite3
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from tgbridge.config import Config
from tgbridge.outbox import Outbox
from tgbridge.secrets import credential, load_secrets
from tgbridge.sync.engine import SyncEngine
from tgbridge.sync.sender import send_approved
from tgbridge.sync.telethon_adapter import TelethonHistoryClient


def _credentials() -> tuple[int, str]:
    secrets = load_secrets()
    api_id = credential("TGQ_API_ID", secrets)
    api_hash = credential("TGQ_API_HASH", secrets)
    if not api_id or not api_hash:
        raise RuntimeError("TGQ_API_ID and TGQ_API_HASH are required")
    return int(api_id), api_hash


def _protect_session(session: str | Path) -> None:
    path = Path(session)
    if path.exists():
        path.chmod(0o600)


async def run_sync(
    connection: sqlite3.Connection,
    config: Config,
    *,
    session: str | Path,
    dry_run: bool = False,
) -> int:
    api_id, api_hash = _credentials()
    client = TelegramClient(str(session), api_id, api_hash)
    await client.start()
    _protect_session(session)
    engine = SyncEngine(connection, TelethonHistoryClient(client), rules=config.rules)
    fetched = 0
    try:
        for peer in config.peers:
            engine.register_peer(peer, dry_run=dry_run)
            try:
                result = await engine.incremental(peer, dry_run=dry_run)
                fetched += result.fetched
                await engine.backfill(peer, dry_run=dry_run)
            except FloodWaitError as error:
                engine.record_flood_wait(peer.peer_id, int(error.seconds))
            await asyncio.sleep(random.uniform(0.5, 2.0))
    finally:
        await client.disconnect()
    return fetched


async def run_sync_loop(
    connection: sqlite3.Connection,
    config: Config,
    *,
    session: str | Path,
    interval: int = 60,
) -> None:
    """Run foreground polling with a hard one-minute floor."""
    delay = max(interval, 60)
    while True:
        await run_sync(connection, config, session=session)
        await asyncio.sleep(delay)


async def run_sender(
    outbox: Outbox,
    *,
    session: str | Path,
    dry_run: bool = False,
) -> int:
    api_id, api_hash = _credentials()
    client = TelegramClient(str(session), api_id, api_hash)
    await client.start()
    _protect_session(session)
    try:
        return await send_approved(outbox, client, dry_run=dry_run)
    finally:
        await client.disconnect()

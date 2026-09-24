"""Live Telethon runtime orchestration."""
# pyright: reportGeneralTypeIssues=false, reportArgumentType=false

import asyncio
import random
import sqlite3
import time
from pathlib import Path

from telethon.errors import FloodWaitError

from tgbridge.config import Config
from tgbridge.logging import event, get_logger
from tgbridge.outbox import Outbox
from tgbridge.secrets import credential, load_secrets
from tgbridge.sync.client import create_client, disconnect_client, start_client
from tgbridge.sync.engine import SyncEngine
from tgbridge.sync.models import Peer
from tgbridge.sync.resolve import parse_query, resolve
from tgbridge.sync.sender import send_approved
from tgbridge.sync.telethon_adapter import TelethonHistoryClient, TelethonResolveClient

_LOG = get_logger("sync.runtime")


def _credentials() -> tuple[int, str]:
    secrets = load_secrets()
    api_id = credential("TGQ_API_ID", secrets)
    api_hash = credential("TGQ_API_HASH", secrets)
    if not api_id or not api_hash:
        raise RuntimeError("TGQ_API_ID and TGQ_API_HASH are required")
    return int(api_id), api_hash


async def run_sync(
    connection: sqlite3.Connection,
    config: Config,
    *,
    session: str | Path,
    dry_run: bool = False,
) -> int:
    api_id, api_hash = _credentials()
    client = create_client(
        session,
        api_id,
        api_hash,
        port=config.telegram.port,
        role="sync",
    )
    await start_client(client, role="sync")
    engine = SyncEngine(connection, TelethonHistoryClient(client), rules=config.rules)
    fetched = 0
    try:
        for peer in config.peers:
            peer_started = time.perf_counter()
            engine.register_peer(peer, dry_run=dry_run)
            try:
                result = await engine.incremental(peer, dry_run=dry_run)
                fetched += result.fetched
                backfill = await engine.backfill(peer, dry_run=dry_run)
                rescan = await engine.rescan(peer, dry_run=dry_run)
                event(
                    _LOG,
                    20,
                    "peer_sync_completed",
                    peer=peer.slug,
                    mode=(
                        "incremental+backfill+rescan"
                        if rescan.fetched
                        else "incremental+backfill"
                    ),
                    fetched=result.fetched + backfill.fetched + rescan.fetched,
                    written=result.written + backfill.written + rescan.written,
                    first_id=result.first_id,
                    last_id=result.last_id,
                    duration_ms=int((time.perf_counter() - peer_started) * 1000),
                )
            except FloodWaitError as error:
                cooldown = engine.record_flood_wait(peer.peer_id, int(error.seconds))
                event(
                    _LOG,
                    30,
                    "peer_flood_wait",
                    peer=peer.slug,
                    seconds=int(error.seconds),
                    cooldown_until=cooldown,
                )
            except Exception as error:
                cooldown = engine.record_error(peer.peer_id, error)
                event(
                    _LOG,
                    30,
                    "peer_sync_failed",
                    peer=peer.slug,
                    error_type=type(error).__name__,
                    cooldown_until=cooldown,
                )
            await asyncio.sleep(random.uniform(0.5, 2.0))
    finally:
        await disconnect_client(client, role="sync")
    return fetched


async def run_resolve(
    config: Config,
    query: str,
    *,
    session: str | Path,
) -> list[Peer]:
    """Resolve peer metadata without touching the SQLite mirror."""
    parse_query(query)
    api_id, api_hash = _credentials()
    client = create_client(
        session,
        api_id,
        api_hash,
        port=config.telegram.port,
        role="resolve",
    )
    await start_client(client, role="resolve")
    try:
        try:
            return await resolve(
                TelethonResolveClient(client),
                query,
                existing_peers=config.peers,
            )
        except Exception as error:
            raise RuntimeError(f"Telegram resolve failed: {error}") from error
    finally:
        await disconnect_client(client, role="resolve")


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
    client = create_client(
        session,
        api_id,
        api_hash,
        port=outbox.config.telegram.port,
        role="sender",
    )
    await start_client(client, role="sender")
    try:
        return await send_approved(outbox, client, dry_run=dry_run)
    finally:
        await disconnect_client(client, role="sender")

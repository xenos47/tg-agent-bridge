"""Transport-neutral Telegram peer resolution for watchlist bootstrapping."""

import re
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlparse

from tgbridge.sync.models import Peer

QueryKind = Literal["username", "peer_id", "title"]


@dataclass(frozen=True)
class ResolveQuery:
    kind: QueryKind
    value: str | int


@dataclass(frozen=True)
class ResolvedDialog:
    peer_id: int
    kind: str
    title: str
    username: str | None = None


class ResolveClient(Protocol):
    async def resolve_username(self, username: str) -> ResolvedDialog | None: ...

    async def resolve_id(self, peer_id: int) -> ResolvedDialog | None: ...

    def iter_dialogs(self) -> AsyncIterator[ResolvedDialog]: ...


def parse_query(raw: str) -> ResolveQuery:
    """Classify a user-supplied username, Telegram link, id, or title."""
    query = raw.strip()
    if not query:
        raise ValueError("resolve query must not be empty")

    if query.startswith("@"):
        username = query[1:]
        if not username:
            raise ValueError("Telegram username must not be empty")
        return ResolveQuery("username", username)

    link = _telegram_link(query)
    if link is not None:
        return link

    if re.fullmatch(r"[+-]?\d+", query):
        return ResolveQuery("peer_id", int(query))

    return ResolveQuery("title", query)


async def resolve(
    client: ResolveClient,
    raw_query: str,
    *,
    existing_peers: Collection[Peer] = (),
) -> list[Peer]:
    """Return zero, one, or multiple peer candidates without persisting anything."""
    query = parse_query(raw_query)
    dialogs: list[ResolvedDialog]
    if query.kind == "username":
        dialog = await client.resolve_username(str(query.value))
        dialogs = [] if dialog is None else [dialog]
    elif query.kind == "peer_id":
        dialog = await client.resolve_id(int(query.value))
        dialogs = [] if dialog is None else [dialog]
    else:
        needle = str(query.value).casefold()
        dialogs = [
            dialog
            async for dialog in client.iter_dialogs()
            if needle in dialog.title.casefold()
        ]

    used_slugs = {peer.slug for peer in existing_peers}
    peers: list[Peer] = []
    for dialog in dialogs:
        slug = _unique_slug(dialog, used_slugs)
        used_slugs.add(slug)
        peers.append(
            Peer(
                peer_id=dialog.peer_id,
                slug=slug,
                kind=dialog.kind,
                title=dialog.title,
                username=dialog.username,
                sendable=False,
            )
        )
    return peers


def _telegram_link(query: str) -> ResolveQuery | None:
    candidate = query if "://" in query else f"https://{query}"
    parsed = urlparse(candidate)
    if parsed.hostname not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("Telegram link must identify a peer")
    if parts[0] in {"+", "joinchat"} or parts[0].startswith("+"):
        raise ValueError("Telegram invite links are not supported")
    if parts[0] == "c":
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("invalid private Telegram message link")
        return ResolveQuery("peer_id", int(f"-100{parts[1]}"))
    return ResolveQuery("username", parts[0].lstrip("@"))


def _unique_slug(dialog: ResolvedDialog, used: set[str]) -> str:
    source = dialog.username or dialog.title
    base = re.sub(r"[^\w]+", "-", source.casefold(), flags=re.UNICODE).strip("-_")
    if not base:
        base = f"peer-{abs(dialog.peer_id)}"
    if base not in used:
        return base
    suffix = str(abs(dialog.peer_id))[-6:]
    candidate = f"{base}-{suffix}"
    counter = 2
    while candidate in used:
        candidate = f"{base}-{suffix}-{counter}"
        counter += 1
    return candidate

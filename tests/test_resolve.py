from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from telethon import types

from tgbridge.sync.models import Peer
from tgbridge.sync.resolve import ResolvedDialog, ResolveQuery, parse_query, resolve
from tgbridge.sync.telethon_adapter import _resolved_dialog


class FakeResolveClient:
    def __init__(self, dialogs: list[ResolvedDialog]) -> None:
        self.dialogs = dialogs

    async def resolve_username(self, username: str) -> ResolvedDialog | None:
        return next(
            (
                dialog
                for dialog in self.dialogs
                if dialog.username and dialog.username.casefold() == username.casefold()
            ),
            None,
        )

    async def resolve_id(self, peer_id: int) -> ResolvedDialog | None:
        return next(
            (dialog for dialog in self.dialogs if dialog.peer_id == peer_id),
            None,
        )

    async def iter_dialogs(self) -> AsyncIterator[ResolvedDialog]:
        for dialog in self.dialogs:
            yield dialog


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Durov", ResolveQuery("username", "Durov")),
        ("https://t.me/durov/123", ResolveQuery("username", "durov")),
        ("t.me/c/123456/7", ResolveQuery("peer_id", -100123456)),
        ("-100123456", ResolveQuery("peer_id", -100123456)),
        ("123456", ResolveQuery("peer_id", 123456)),
        ("Work Chat", ResolveQuery("title", "Work Chat")),
    ],
)
def test_parse_query(raw: str, expected: ResolveQuery) -> None:
    assert parse_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "@", "https://t.me/", "https://t.me/+invite"])
def test_parse_query_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_query(raw)


@pytest.mark.asyncio
async def test_resolve_username_and_id() -> None:
    dialogs = [
        ResolvedDialog(-100123, "channel", "News", "news"),
        ResolvedDialog(42, "user", "Lena", "lena"),
    ]
    client = FakeResolveClient(dialogs)

    assert (await resolve(client, "@news"))[0] == Peer(
        -100123,
        "news",
        "channel",
        "News",
        username="news",
    )
    assert (await resolve(client, "42"))[0].slug == "lena"


@pytest.mark.asyncio
async def test_title_search_returns_zero_one_or_many_candidates() -> None:
    client = FakeResolveClient(
        [
            ResolvedDialog(-1001, "group", "Work Chat"),
            ResolvedDialog(-1002, "channel", "Work News"),
            ResolvedDialog(3, "user", "Lena"),
        ]
    )
    assert await resolve(client, "missing") == []
    assert [peer.peer_id for peer in await resolve(client, "work")] == [-1001, -1002]
    assert (await resolve(client, "lena"))[0].slug == "lena"


@pytest.mark.asyncio
async def test_slug_collision_gets_peer_id_suffix() -> None:
    client = FakeResolveClient([ResolvedDialog(-100123456, "group", "Work Chat")])
    existing = [Peer(1, "work-chat", "group", "Existing")]
    match = (await resolve(client, "Work Chat", existing_peers=existing))[0]
    assert match.slug == "work-chat-123456"


def test_telethon_entities_map_to_canonical_peer_kinds_and_ids() -> None:
    photo = types.ChatPhotoEmpty()
    user = _resolved_dialog(types.User(42, first_name="Lena", username="lena"))
    group = _resolved_dialog(
        types.Chat(123, "Team", photo, 2, datetime.now(UTC), 1)
    )
    channel = _resolved_dialog(
        types.Channel(456, "News", photo, datetime.now(UTC), broadcast=True)
    )
    megagroup = _resolved_dialog(
        types.Channel(789, "Community", photo, datetime.now(UTC), megagroup=True)
    )

    assert (user.peer_id, user.kind) == (42, "user")
    assert (group.peer_id, group.kind) == (-123, "group")
    assert (channel.peer_id, channel.kind) == (-1000000000456, "channel")
    assert (megagroup.peer_id, megagroup.kind) == (-1000000000789, "group")

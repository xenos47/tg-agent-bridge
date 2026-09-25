from dataclasses import replace
from pathlib import Path

import pytest

from tgbridge.config import DEFAULT_POLICY, append_peer, format_peer_yaml, load_config
from tgbridge.sync.models import Peer, SyncPolicy


def config_file(tmp_path: Path, content: str = "peers: []\n") -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(content)
    return path


def test_telegram_port_defaults_to_443(tmp_path: Path) -> None:
    assert load_config(config_file(tmp_path), environ={}).telegram.port == 443


def test_telegram_port_reads_yaml(tmp_path: Path) -> None:
    path = config_file(tmp_path, "telegram:\n  port: 5222\npeers: []\n")
    assert load_config(path, environ={}).telegram.port == 5222


def test_environment_overrides_yaml_port(tmp_path: Path) -> None:
    path = config_file(tmp_path, "telegram:\n  port: 80\npeers: []\n")
    config = load_config(path, environ={"TGQ_TELEGRAM_PORT": "5222"})
    assert config.telegram.port == 5222


def test_resolved_settings_port_overrides_legacy_watchlist(tmp_path: Path) -> None:
    path = config_file(tmp_path, "telegram:\n  port: 80\npeers: []\n")
    config = load_config(path, environ={}, telegram_port=5222)
    assert config.telegram.port == 5222


@pytest.mark.parametrize("value", ["not-a-port", "0", "65536"])
def test_invalid_telegram_port_is_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="Telegram port"):
        load_config(config_file(tmp_path), environ={"TGQ_TELEGRAM_PORT": value})


def test_format_peer_yaml_is_watchlist_compatible() -> None:
    peer = Peer(-100123, "work-chat", "group", "Рабочий чат", username="workchat")
    assert format_peer_yaml(peer) == (
        "- slug: work-chat\n"
        "  id: -100123\n"
        "  kind: group\n"
        "  title: Рабочий чат\n"
        "  username: workchat\n"
        "  sendable: false\n"
    )


def test_append_peer_dry_run_does_not_modify_watchlist(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    original = path.read_text()
    peer = Peer(-100123, "work-chat", "group", "Work Chat")
    assert append_peer(path, peer, dry_run=True) == format_peer_yaml(peer)
    assert path.read_text() == original


def test_append_peer_writes_and_rejects_duplicates(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    peer = Peer(-100123, "work-chat", "group", "Work Chat")
    append_peer(path, peer)
    assert load_config(path).peers == (replace(peer, policy=DEFAULT_POLICY),)

    with pytest.raises(ValueError, match="peer id already exists"):
        append_peer(path, Peer(-100123, "other", "group", "Other"))
    with pytest.raises(ValueError, match="peer slug already exists"):
        append_peer(path, Peer(-100456, "work-chat", "group", "Other"))


POLICY_WATCHLIST = """
policies:
  fresh: {history: 14d, retention: 2w}
  archive: {history: all}
default_policy: fresh
peers:
  - {slug: jobs, id: 1, kind: channel}
  - {slug: team, id: 2, kind: group, policy: archive}
"""


def test_peers_get_named_or_default_policy(tmp_path: Path) -> None:
    peers = load_config(config_file(tmp_path, POLICY_WATCHLIST), environ={}).peers
    day = 86400
    assert [peer.policy for peer in peers] == [
        SyncPolicy(history=14 * day, retention=14 * day),
        SyncPolicy(history=None, retention=None),
    ]


def test_watchlist_without_policies_uses_shallow_default_without_retention(
    tmp_path: Path,
) -> None:
    path = config_file(tmp_path, "peers:\n  - {slug: jobs, id: 1, kind: channel}\n")
    (peer,) = load_config(path, environ={}).peers
    assert peer.policy == DEFAULT_POLICY
    assert DEFAULT_POLICY.retention is None


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("policies: {p: {history: 14d, keep: 1d}}", "unknown policies.p setting: keep"),
        ("policies: {p: {retention: 14d}}", "policies.p.history is required"),
        ("policies: {p: {history: 14 days}}", "duration like 14d"),
        ("policies: {p: {history: 0d}}", "duration like 14d"),
        ("policies: {p: {history: 14d, retention: 7d}}", "must not be shorter"),
        ("policies: {p: {history: all, retention: 7d}}", "cannot be combined with history: all"),
        ("policies: {p: {history: 7d}}\ndefault_policy: [p]", "unknown policy"),
        ("default_policy: missing", "unknown policy 'missing'"),
        (
            "peers:\n  - {slug: jobs, id: 1, kind: channel, policy: missing}",
            "peer 'jobs' refers to unknown policy 'missing'",
        ),
    ],
)
def test_invalid_policy_config_is_rejected(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_config(config_file(tmp_path, content + "\n"), environ={})

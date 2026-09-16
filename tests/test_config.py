from pathlib import Path

import pytest

from tgbridge.config import append_peer, format_peer_yaml, load_config
from tgbridge.sync.models import Peer


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
    assert load_config(path).peers == (peer,)

    with pytest.raises(ValueError, match="peer id already exists"):
        append_peer(path, Peer(-100123, "other", "group", "Other"))
    with pytest.raises(ValueError, match="peer slug already exists"):
        append_peer(path, Peer(-100456, "work-chat", "group", "Other"))

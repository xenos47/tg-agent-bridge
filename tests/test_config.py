from pathlib import Path

import pytest

from tgbridge.config import load_config


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

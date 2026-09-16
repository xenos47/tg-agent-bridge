from pathlib import Path

import pytest

from tgbridge.settings import default_settings_path, load_settings


def test_default_xdg_settings_are_optional(tmp_path: Path) -> None:
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
    }
    settings = load_settings(environ=environment)
    assert settings.source == tmp_path / "xdg" / "tgq" / "config.yaml"
    assert settings.db is None
    assert default_settings_path(environment) == settings.source


@pytest.mark.parametrize("use_environment", [False, True])
def test_explicit_missing_settings_are_rejected(
    tmp_path: Path, use_environment: bool
) -> None:
    missing = tmp_path / "missing.yaml"
    if use_environment:
        with pytest.raises(ValueError, match="does not exist"):
            load_settings(environ={"TGQ_SETTINGS": str(missing)})
    else:
        with pytest.raises(ValueError, match="does not exist"):
            load_settings(missing, environ={})


def test_paths_expand_and_resolve_relative_to_settings_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = tmp_path / "config" / "tgq" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "paths:\n"
        "  db: data/$DB_NAME\n"
        "  watchlist: ~/watchlist.yaml\n"
        "  session: ${SESSION_DIR}/tgq.session\n"
        "telegram:\n"
        "  port: 5222\n"
    )
    settings = load_settings(
        target,
        environ={
            "HOME": str(home),
            "DB_NAME": "messages.sqlite",
            "SESSION_DIR": str(tmp_path / "sessions"),
        },
    )
    assert settings.db == target.parent / "data" / "messages.sqlite"
    assert settings.watchlist == home / "watchlist.yaml"
    assert settings.session == tmp_path / "sessions" / "tgq.session"
    assert settings.telegram_port == 5222


@pytest.mark.parametrize(
    "content",
    [
        "api_hash: secret\n",
        "paths:\n  db: db.sqlite\n  token: secret\n",
        "telegram:\n  phone: '+123'\n",
    ],
)
def test_unknown_and_secret_settings_are_rejected(
    tmp_path: Path, content: str
) -> None:
    target = tmp_path / "config.yaml"
    target.write_text(content)
    with pytest.raises(ValueError, match="unknown"):
        load_settings(target, environ={})


def test_invalid_yaml_is_rejected_as_settings_error(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    target.write_text("paths: [\n")
    with pytest.raises(ValueError, match="invalid settings YAML"):
        load_settings(target, environ={})


@pytest.mark.parametrize("value", ["bad", "0", "65536", "true"])
def test_invalid_settings_port_is_rejected(tmp_path: Path, value: str) -> None:
    target = tmp_path / "config.yaml"
    target.write_text(f"telegram:\n  port: {value}\n")
    with pytest.raises(ValueError, match=r"telegram\.port"):
        load_settings(target, environ={})

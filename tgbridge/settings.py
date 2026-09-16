"""Non-secret per-user settings and XDG discovery."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import yaml


@dataclass(frozen=True)
class UserSettings:
    source: Path
    db: Path | None = None
    watchlist: Path | None = None
    session: Path | None = None
    telegram_port: int | None = None


def default_settings_path(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    config_home = environment.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(_expand(config_home, environment)) / "tgq" / "config.yaml"
    home = environment.get("HOME")
    base = Path(home) if home else Path.home()
    return base / ".config" / "tgq" / "config.yaml"


def load_settings(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> UserSettings:
    """Load strict settings; an absent implicit XDG default is optional."""
    environment = os.environ if environ is None else environ
    configured_path = str(path) if path is not None else environment.get("TGQ_SETTINGS")
    explicit = configured_path is not None
    if configured_path is not None:
        if not configured_path.strip():
            raise ValueError("settings path must not be empty")
        target = Path(_expand(configured_path, environment))
        if not target.is_absolute():
            target = Path.cwd() / target
    else:
        target = default_settings_path(environment)
    target = target.resolve(strict=False)

    if not target.exists():
        if explicit:
            raise ValueError(f"settings file does not exist: {target}")
        return UserSettings(source=target)

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"invalid settings YAML: {error}") from error
    root = _mapping(raw, "settings")
    _reject_unknown(root, {"paths", "telegram"}, "settings")

    paths = _mapping(root.get("paths", {}), "paths")
    _reject_unknown(paths, {"db", "watchlist", "session"}, "paths")
    telegram = _mapping(root.get("telegram", {}), "telegram")
    _reject_unknown(telegram, {"port"}, "telegram")

    return UserSettings(
        source=target,
        db=_setting_path(paths.get("db"), target.parent, environment, "paths.db"),
        watchlist=_setting_path(
            paths.get("watchlist"), target.parent, environment, "paths.watchlist"
        ),
        session=_setting_path(
            paths.get("session"), target.parent, environment, "paths.session"
        ),
        telegram_port=_port(telegram.get("port")),
    )


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a YAML mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value


def _reject_unknown(values: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} setting: {unknown[0]}")


def _setting_path(
    value: Any,
    base: Path,
    environ: Mapping[str, str],
    label: str,
) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty path string")
    path = Path(_expand(value, environ))
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _expand(value: str, environ: Mapping[str, str]) -> str:
    expanded = Template(value).safe_substitute(environ)
    home = environ.get("HOME")
    if expanded == "~":
        return home or str(Path.home())
    if expanded.startswith("~/"):
        return str(Path(home or Path.home())) + expanded[1:]
    return expanded


def _port(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("telegram.port must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("telegram.port must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("telegram.port must be between 1 and 65535")
    return port

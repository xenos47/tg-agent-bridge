"""Non-secret per-user settings and XDG discovery."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

import yaml

LEGACY_ACCOUNT = "default"
ALL_ACCOUNTS = "all"


def environment_value(environment: Mapping[str, str], variable: str) -> str | None:
    """An exported TGQ_* value; empty or blank counts as unset, as for TGQ_ACCOUNT."""
    value = environment.get(variable)
    return value if value is not None and value.strip() else None
_ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ACCOUNT_KEYS = {"db", "watchlist", "session", "secrets", "telegram", "sync"}
_REQUIRED_ACCOUNT_PATHS = ("db", "watchlist", "session")


@dataclass(frozen=True)
class AccountSettings:
    """Paths and transport settings of one Telegram account."""

    name: str
    db: Path | None = None
    watchlist: Path | None = None
    session: Path | None = None
    secrets: Path | None = None
    telegram_port: int | None = None
    sync_interval: int | None = None


@dataclass(frozen=True)
class UserSettings:
    """Parsed settings file.

    Without an `accounts` section the flat `paths` / `telegram` / `sync` keys
    describe one implicit account named `default` and `named` is False.
    """

    source: Path
    accounts: tuple[AccountSettings, ...] = (AccountSettings(LEGACY_ACCOUNT),)
    default_account: str | None = None
    named: bool = False

    @property
    def account_names(self) -> tuple[str, ...]:
        return tuple(account.name for account in self.accounts)

    def select(self, requested: str | None) -> tuple[AccountSettings, ...]:
        """Accounts a command targets: one by name, or every account for `all`.

        `all` is never implied (D1 in #29): without a request the default
        account is used, and several accounts without a default are an error.
        """
        if requested == ALL_ACCOUNTS:
            return self.accounts
        name = requested if requested is not None else self.default_account
        if name is None:
            if len(self.accounts) == 1:
                return self.accounts
            raise ValueError(
                "several accounts are configured and default_account is not set; "
                f"pass --account with one of: {', '.join(self.account_names)}"
            )
        return (self.account(name),)

    def account(self, name: str) -> AccountSettings:
        """The one account called `name`; `all` is not an account."""
        for account in self.accounts:
            if account.name == name:
                return account
        raise ValueError(
            f"unknown account {name!r}; configured: {', '.join(self.account_names)}"
        )


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
    _reject_unknown(
        root, {"paths", "telegram", "sync", "accounts", "default_account"}, "settings"
    )
    if "accounts" not in root:
        if "default_account" in root:
            raise ValueError("default_account requires an accounts section")
        return UserSettings(
            source=target,
            accounts=(_legacy_account(root, target.parent, environment),),
        )

    flat = sorted({"paths", "telegram", "sync"} & set(root))
    if flat:
        raise ValueError(
            f"top-level {flat[0]} cannot be combined with accounts; "
            f"move it under accounts.<name>"
        )
    section = _mapping(root["accounts"], "accounts")
    if not section:
        raise ValueError("accounts must configure at least one account")
    accounts = tuple(
        _named_account(name, value, target.parent, environment)
        for name, value in section.items()
    )
    _reject_shared_paths(accounts)
    default_account = root.get("default_account")
    if default_account is not None and (
        not isinstance(default_account, str) or default_account not in section
    ):
        raise ValueError(f"default_account refers to unknown account {default_account!r}")
    return UserSettings(
        source=target,
        accounts=accounts,
        default_account=default_account,
        named=True,
    )


def _legacy_account(
    root: Mapping[str, Any], base: Path, environ: Mapping[str, str]
) -> AccountSettings:
    paths = _mapping(root.get("paths", {}), "paths")
    _reject_unknown(paths, {"db", "watchlist", "session"}, "paths")
    telegram = _mapping(root.get("telegram", {}), "telegram")
    _reject_unknown(telegram, {"port"}, "telegram")
    sync = _mapping(root.get("sync", {}), "sync")
    _reject_unknown(sync, {"interval"}, "sync")
    return AccountSettings(
        name=LEGACY_ACCOUNT,
        db=_setting_path(paths.get("db"), base, environ, "paths.db"),
        watchlist=_setting_path(paths.get("watchlist"), base, environ, "paths.watchlist"),
        session=_setting_path(paths.get("session"), base, environ, "paths.session"),
        telegram_port=_port(telegram.get("port")),
        sync_interval=_interval(sync.get("interval")),
    )


def _named_account(
    name: str, raw: Any, base: Path, environ: Mapping[str, str]
) -> AccountSettings:
    if name == ALL_ACCOUNTS or not _ACCOUNT_NAME.fullmatch(name):
        raise ValueError(
            f"invalid account name {name!r}: use lowercase letters, digits and '-', "
            f"not the reserved name {ALL_ACCOUNTS!r}"
        )
    label = f"accounts.{name}"
    values = _mapping(raw, label)
    _reject_unknown(values, _ACCOUNT_KEYS, label)
    for key in _REQUIRED_ACCOUNT_PATHS:
        # `db: ~` is YAML null: as good as absent, not a path.
        if values.get(key) is None:
            raise ValueError(f"{label}.{key} is required")
    telegram = _mapping(values.get("telegram", {}), f"{label}.telegram")
    _reject_unknown(telegram, {"port"}, f"{label}.telegram")
    sync = _mapping(values.get("sync", {}), f"{label}.sync")
    _reject_unknown(sync, {"interval"}, f"{label}.sync")

    def path(key: str) -> Path | None:
        return _setting_path(values.get(key), base, environ, f"{label}.{key}")

    return AccountSettings(
        name=name,
        db=path("db"),
        watchlist=path("watchlist"),
        session=path("session"),
        secrets=path("secrets"),
        telegram_port=_port(telegram.get("port"), f"{label}.telegram.port"),
        sync_interval=_interval(sync.get("interval"), f"{label}.sync.interval"),
    )


def _reject_shared_paths(accounts: tuple[AccountSettings, ...]) -> None:
    """Two accounts must never share a mirror, a session, or a sync lock.

    The sync lock lives next to the session file, so session files must also
    sit in different directories.
    """
    seen: dict[tuple[str, Path], str] = {}
    for account in accounts:
        assert account.db is not None and account.session is not None
        for kind, value in (("db", account.db), ("session directory", account.session.parent)):
            owner = seen.setdefault((kind, value), account.name)
            if owner != account.name:
                raise ValueError(
                    f"accounts {owner!r} and {account.name!r} share the {kind} {value}; "
                    "each account needs its own"
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


def _port(value: Any, label: str = "telegram.port") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} must be between 1 and 65535")
    return port


def _interval(value: Any, label: str = "sync.interval") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        interval = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be an integer") from error
    if interval < 1:
        raise ValueError(f"{label} must be at least 1")
    return interval

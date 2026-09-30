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
    assert settings.account_names == ("default",)
    assert settings.accounts[0].db is None
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
        "sync:\n"
        "  interval: 120\n"
    )
    settings = load_settings(
        target,
        environ={
            "HOME": str(home),
            "DB_NAME": "messages.sqlite",
            "SESSION_DIR": str(tmp_path / "sessions"),
        },
    )
    [account] = settings.accounts
    assert account.name == "default"
    assert account.db == target.parent / "data" / "messages.sqlite"
    assert account.watchlist == home / "watchlist.yaml"
    assert account.session == tmp_path / "sessions" / "tgq.session"
    assert account.secrets is None
    assert account.telegram_port == 5222
    assert account.sync_interval == 120


@pytest.mark.parametrize(
    "content",
    [
        "api_hash: secret\n",
        "paths:\n  db: db.sqlite\n  token: secret\n",
        "telegram:\n  phone: '+123'\n",
        "sync:\n  token: secret\n",
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


@pytest.mark.parametrize("value", ["bad", "0", "true", "-1"])
def test_invalid_settings_interval_is_rejected(tmp_path: Path, value: str) -> None:
    target = tmp_path / "config.yaml"
    target.write_text(f"sync:\n  interval: {value}\n")
    with pytest.raises(ValueError, match=r"sync\.interval"):
        load_settings(target, environ={})


def _write(tmp_path: Path, content: str) -> Path:
    target = tmp_path / "config.yaml"
    target.write_text(content)
    return target


_TWO_ACCOUNTS = (
    "default_account: personal\n"
    "accounts:\n"
    "  personal:\n"
    "    db: personal/messages.sqlite\n"
    "    watchlist: personal/watchlist.yaml\n"
    "    session: personal/tgq.session\n"
    "    secrets: ~/personal.env\n"
    "    telegram:\n"
    "      port: 5222\n"
    "  work:\n"
    "    db: work/messages.sqlite\n"
    "    watchlist: work/watchlist.yaml\n"
    "    session: work/tgq.session\n"
    "    sync:\n"
    "      interval: 300\n"
)


def test_named_accounts_resolve_paths_per_account(tmp_path: Path) -> None:
    target = _write(tmp_path, _TWO_ACCOUNTS)
    settings = load_settings(target, environ={"HOME": str(tmp_path / "home")})
    assert settings.named
    assert settings.account_names == ("personal", "work")
    assert settings.default_account == "personal"
    personal, work = settings.accounts
    assert personal.db == tmp_path / "personal" / "messages.sqlite"
    assert personal.session == tmp_path / "personal" / "tgq.session"
    assert personal.secrets == tmp_path / "home" / "personal.env"
    assert personal.telegram_port == 5222
    assert personal.sync_interval is None
    assert work.watchlist == tmp_path / "work" / "watchlist.yaml"
    assert work.secrets is None
    assert work.telegram_port is None
    assert work.sync_interval == 300


def test_account_selection_follows_default_and_all(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _TWO_ACCOUNTS), environ={})
    assert [a.name for a in settings.select(None)] == ["personal"]
    assert [a.name for a in settings.select("work")] == ["work"]
    assert [a.name for a in settings.select("all")] == ["personal", "work"]
    with pytest.raises(ValueError, match=r"unknown account 'home'; configured: personal, work"):
        settings.select("home")


def test_several_accounts_without_default_require_explicit_choice(tmp_path: Path) -> None:
    content = _TWO_ACCOUNTS.replace("default_account: personal\n", "")
    settings = load_settings(_write(tmp_path, content), environ={})
    assert settings.default_account is None
    with pytest.raises(ValueError, match=r"default_account is not set.*personal, work or all"):
        settings.select(None)
    assert [a.name for a in settings.select("work")] == ["work"]


def test_single_named_account_is_implicit_default(tmp_path: Path) -> None:
    content = (
        "accounts:\n"
        "  work:\n"
        "    db: w.sqlite\n"
        "    watchlist: w.yaml\n"
        "    session: w/tgq.session\n"
    )
    settings = load_settings(_write(tmp_path, content), environ={})
    assert [a.name for a in settings.select(None)] == ["work"]


def test_legacy_settings_are_one_account_named_default(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, "paths:\n  db: m.sqlite\n"), environ={})
    assert not settings.named
    assert [a.name for a in settings.select(None)] == ["default"]
    assert [a.name for a in settings.select("default")] == ["default"]
    assert [a.name for a in settings.select("all")] == ["default"]
    with pytest.raises(ValueError, match="unknown account"):
        settings.select("work")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "paths:\n  db: m.sqlite\naccounts:\n  work: {}\n",
            "top-level paths cannot be combined with accounts",
        ),
        (
            "telegram:\n  port: 5222\naccounts:\n  work: {}\n",
            "top-level telegram cannot be combined with accounts",
        ),
        ("default_account: work\n", "default_account requires an accounts section"),
        ("accounts: {}\n", "at least one account"),
        ("accounts: []\n", "accounts must be a YAML mapping"),
        (
            "accounts:\n  all:\n    db: a\n    watchlist: b\n    session: c/s\n",
            "invalid account name 'all'",
        ),
        (
            "accounts:\n  Work:\n    db: a\n    watchlist: b\n    session: c/s\n",
            "invalid account name 'Work'",
        ),
        (
            "accounts:\n  w:x:\n    db: a\n    watchlist: b\n    session: c/s\n",
            "invalid account name 'w:x'",
        ),
        (
            "accounts:\n  work:\n    watchlist: b\n    session: c/s\n",
            r"accounts\.work\.db is required",
        ),
        (
            "accounts:\n  work:\n    db: ~\n    watchlist: b\n    session: c/s\n",
            r"accounts\.work\.db is required",
        ),
        (
            "accounts:\n  work:\n    db: a\n    watchlist: null\n    session: c/s\n",
            r"accounts\.work\.watchlist is required",
        ),
        (
            "accounts:\n  work:\n    db: a\n    watchlist: b\n    session: c/s\n    token: x\n",
            r"unknown accounts\.work setting: token",
        ),
        (
            "accounts:\n  work:\n    db: a\n    watchlist: b\n    session: c/s\n"
            "    telegram:\n      port: 0\n",
            r"accounts\.work\.telegram\.port must be between",
        ),
        (
            "default_account: home\n"
            "accounts:\n  work:\n    db: a\n    watchlist: b\n    session: c/s\n",
            "default_account refers to unknown account 'home'",
        ),
    ],
)
def test_invalid_account_settings_are_rejected(
    tmp_path: Path, content: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_settings(_write(tmp_path, content), environ={})


@pytest.mark.parametrize(
    ("second", "kind"),
    [
        ("    db: one.sqlite\n    watchlist: two.yaml\n    session: two/tgq.session\n", "db"),
        # Same directory means the same sync.lock, even with different file names.
        (
            "    db: two.sqlite\n    watchlist: two.yaml\n    session: one/other.session\n",
            "session directory",
        ),
    ],
)
def test_accounts_must_not_share_mirror_or_session_directory(
    tmp_path: Path, second: str, kind: str
) -> None:
    content = (
        "accounts:\n"
        "  one:\n"
        "    db: one.sqlite\n"
        "    watchlist: one.yaml\n"
        "    session: one/tgq.session\n"
        "  two:\n" + second
    )
    with pytest.raises(ValueError, match=f"accounts 'one' and 'two' share the {kind}"):
        load_settings(_write(tmp_path, content), environ={})

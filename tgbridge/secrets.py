"""Local secrets file loading with strict permissions."""

import os
from pathlib import Path


def load_secrets(path: str | Path | None = None) -> dict[str, str]:
    target = Path(path or "~/.config/tgq/secrets.env").expanduser()
    if not target.exists():
        return {}
    if target.stat().st_mode & 0o077:
        raise PermissionError(f"{target} must have mode 0600")
    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"invalid secrets line: {raw_line!r}")
        values[key.strip()] = value.strip()
    return values


def credential(name: str, secrets: dict[str, str]) -> str | None:
    return os.environ.get(name) or secrets.get(name)

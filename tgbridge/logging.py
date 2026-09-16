"""Centralized, stderr-only structured logging with secret redaction."""

import asyncio
import json
import logging
import re
import sys
from typing import Any

LOGGER_NAME = "tgbridge"
_KEY_VALUE = re.compile(
    r"(?i)\b(api_hash|session|phone|token|code|password)"
    r"\b([\"'=:\s]+)([^,\s}\"]+)"
)
_BASE64 = re.compile(
    r"(?<![A-Za-z0-9_+/-])[A-Za-z0-9_+/-]{40,}={0,2}(?![A-Za-z0-9_+/-])"
)
_SAFE_FIELDS = (
    "role",
    "transport",
    "dc",
    "address",
    "port",
    "peer",
    "mode",
    "batch_size",
    "fetched",
    "written",
    "first_id",
    "last_id",
    "cursor",
    "cooldown_until",
    "seconds",
    "error_count",
    "outbox_id",
    "result",
    "duration_ms",
    "error_type",
    "error",
    "hint",
    "exit_code",
)


def redact(value: str) -> str:
    redacted = _KEY_VALUE.sub(r"\1\2[REDACTED]", value)
    return _BASE64.sub("[REDACTED]", redacted)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if isinstance(value, str):
                setattr(record, field, redact(value))
        return True


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", record.getMessage())
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "event": redact(str(event)),
        }
        for field in _SAFE_FIELDS:
            if hasattr(record, field):
                value = getattr(record, field)
                payload[field] = redact(value) if isinstance(value, str) else value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    """Configure only the project logger; never mutate the root logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO if verbose else logging.WARNING)
    if quiet:
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
        logger.addHandler(logging.NullHandler())
        return
    logger.disabled = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredFormatter())
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def event(
    logger: logging.Logger,
    level: int,
    name: str,
    **fields: Any,
) -> None:
    safe = {key: value for key, value in fields.items() if key in _SAFE_FIELDS}
    logger.log(level, name, extra={"event": name, **safe})


def classify_connection_error(error: Exception) -> tuple[str, str]:
    """Return a safe error type and actionable hint without exposing buffers."""
    if isinstance(error, asyncio.IncompleteReadError):
        prefix = b""
        packet_size = (error.expected or 0) + 8
        if packet_size <= 0xFFFFFFFF:
            prefix = packet_size.to_bytes(4, "little")
        pingora = b"Server: Pingora" in error.partial[:256]
        if prefix == b"HTTP" or pingora:
            return (
                "http_interception",
                "MTProto was intercepted by an HTTP gateway; configure a raw "
                "Telegram port such as 5222",
            )
        return ("incomplete_read", "The MTProto connection closed before a full packet arrived")
    return (type(error).__name__, redact(str(error)))

"""Centralized secret redaction for structured logs."""

import logging
import re

_KEY_VALUE = re.compile(
    r"(?i)\b(api_hash|session|phone|token)\b([\"'=:\s]+)([^,\s}\"]+)"
)
_BASE64 = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = _KEY_VALUE.sub(r"\1\2[REDACTED]", message)
        message = _BASE64.sub("[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True

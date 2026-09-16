import asyncio
import json
import logging

import pytest

from tgbridge.logging import (
    classify_connection_error,
    configure_logging,
    event,
    get_logger,
)


def test_structured_logging_redacts_all_secret_classes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(verbose=True)
    logger = get_logger("test")
    event(
        logger,
        logging.INFO,
        "secret_test",
        error=(
            "api_hash=hash-secret phone=+15551234567 token=token-secret "
            "code=12345 password=hunter2 "
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwx0123456789+/ "
            "URLSAFE_session_value-with_many_parts_0123456789"
        ),
    )
    output = capsys.readouterr().err
    assert "hash-secret" not in output
    assert "+15551234567" not in output
    assert "token-secret" not in output
    assert "12345" not in output
    assert "hunter2" not in output
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in output
    assert "URLSAFE_session_value" not in output
    assert output.count("[REDACTED]") >= 6


def test_unapproved_fields_cannot_put_message_data_in_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(verbose=True)
    event(
        get_logger("test"),
        logging.INFO,
        "sync_test",
        peer="work",
        body="private message body",
        raw_json='{"private":true}',
    )
    output = capsys.readouterr().err
    payload = json.loads(output)
    assert payload == {"level": "info", "event": "sync_test", "peer": "work"}
    assert "private message body" not in output
    assert "raw_json" not in output


def test_http_interception_is_classified_without_returning_partial_bytes() -> None:
    partial = b" 400 Bad Request\r\nServer: Pingora\r\nsecret bytes"
    error = asyncio.IncompleteReadError(partial, int.from_bytes(b"HTTP", "little") - 8)
    error_type, hint = classify_connection_error(error)
    assert error_type == "http_interception"
    assert "5222" in hint
    assert "Pingora" not in hint
    assert "secret bytes" not in hint


def test_quiet_disables_project_diagnostics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(quiet=True)
    event(get_logger("test"), logging.ERROR, "hidden", error="should not appear")
    assert capsys.readouterr().err == ""


def test_default_emits_errors_but_not_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    logger = get_logger("test")
    event(logger, logging.INFO, "hidden")
    event(logger, logging.ERROR, "visible", exit_code=4)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "level": "error",
        "event": "visible",
        "exit_code": 4,
    }

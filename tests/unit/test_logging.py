import json

import structlog


def test_structlog_json_output_shape(capsys):
    from app.core.logging import setup_logging

    setup_logging(level="DEBUG", env="production")

    log = structlog.get_logger("test")
    structlog.contextvars.bind_contextvars(request_id="test-req-id")

    log.info("test_event", extra_field="hello")

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.strip()]
    assert lines, "Expected at least one log line"

    parsed = json.loads(lines[-1])
    assert "ts" in parsed or "timestamp" in parsed or "event" in parsed
    assert parsed.get("event") == "test_event" or parsed.get("msg") == "test_event" or "test_event" in str(parsed)


def test_structlog_request_id_bound(capsys):
    from app.core.logging import setup_logging

    setup_logging(level="DEBUG", env="production")

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="bound-req-42")

    log = structlog.get_logger("test2")
    log.info("checking_request_id")

    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.strip()]
    assert lines

    raw = lines[-1]
    assert "bound-req-42" in raw

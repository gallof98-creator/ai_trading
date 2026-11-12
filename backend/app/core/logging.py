"""Structured JSON logging with credential redaction."""
from __future__ import annotations

import logging

import structlog

_SENSITIVE = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential", "passphrase")


def redact(_, __, event_dict: dict) -> dict:
    """Mask any field whose name looks like a secret, at any nesting depth."""

    def scrub(value):
        if isinstance(value, dict):
            return {k: "***" if any(s in str(k).lower() for s in _SENSITIVE) else scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in value]
        return value

    return scrub(event_dict)


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
    )


def get_logger(name: str):
    return structlog.get_logger(name)

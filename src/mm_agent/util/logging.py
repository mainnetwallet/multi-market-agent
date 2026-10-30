"""Structured JSON logging with central secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, cast

import structlog

_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|passphrase|authorization|cookie|private[_-]?key|"
    r"api[_-]?key|signature|salt|nonce|session)",
    re.IGNORECASE,
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PEM]",
    ),
    (re.compile(r"\b0x[a-fA-F0-9]{64}\b"), "[REDACTED_HEX_KEY]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\blmts_[A-Za-z0-9_-]{8,}\b"), "[REDACTED_LMTS_KEY]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED_JWT]"),
)


def redact_text(value: str) -> str:
    out = value
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact mappings. Tool/LLM output passes through this too."""

    if key is not None and _SECRET_KEY_RE.search(key) and not key.lower().endswith("_id"):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, MutableMapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key=key) for v in value]
    return value


def _redact_processor(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return cast(MutableMapping[str, Any], redact(dict(event_dict)))


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_processor,
        structlog.processors.StackInfoRenderer(),
    ]
    processors: list[Any]
    if json_logs:
        processors = [*shared, structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
    else:
        processors = [*shared, structlog.dev.ConsoleRenderer(colors=False)]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )


def get_logger(name: str = "mm_agent") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]

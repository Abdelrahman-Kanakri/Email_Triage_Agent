"""Structured JSON logging configuration using structlog."""

import logging
import sys
from pathlib import Path

import structlog
from structlog.types import FilteringBoundLogger

from app.core.config import settings
from app.guardrails.pii import (
    redact_pii,  # submodule, not package: avoids the guardrails<->core cycle
)

# ── Configuration ───────────────────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


# Make the _json_formatter and _root_handler available for other modules if needed
_json_formatter = logging.Formatter("%(message)s")
# First, create the filehandler
_root_handler = logging.FileHandler(LOG_DIR / "log.log")
# then append the _json_formatter to the handler
_root_handler.setFormatter(_json_formatter)

# Containers: also stream JSON lines to stdout so `docker logs` sees them.
_stdout_handler: logging.Handler | None = None
if settings.LOG_TO_STDOUT:
    _stdout_handler = logging.StreamHandler(sys.stdout)
    _stdout_handler.setFormatter(_json_formatter)

# Append the root handler to the logger
logging.basicConfig(
    level=logging.INFO,
    handlers=[h for h in (_root_handler, _stdout_handler) if h is not None],
)

structlog.configure(
    processors=[
        # Merges context variables (useful for tracking web requests)
        structlog.contextvars.merge_contextvars,
        # Adds the log level (e.g., "info", "error") to the JSON
        structlog.processors.add_log_level,
        # Adds the logger name to the JSON payload automatically
        structlog.stdlib.add_logger_name,
        # Captures stack traces if an exception occurs
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        # Standard ISO 8601 timestamp format is preferred in production (e.g., for ELK/Splunk)
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Redacts PII from the log messages
        redact_pii,
        # Renders the final output as a JSON string
        structlog.processors.JSONRenderer(),
    ],
    # Correct type hint binding for modern structlog versions
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
# ── Logger factory ──────────────────────────────────────────────────────────


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Get a JSON-structured logger, optionally isolated to its own file.

    Args:
        name: Logical channel (often __name__, but any string works). If given,
            this channel's logs go only to '<name>.log', never to the shared file.
            If omitted, logs go to the shared 'logs/log.log'.

    Returns:
        A structlog BoundLogger — calling .info()/.error()/etc. on it renders JSON.
    """
    if name:
        # Create a sperate logger for name based channel
        stdlib_logger = logging.getLogger(name)

        if not stdlib_logger.handlers:
            handler = logging.FileHandler(LOG_DIR / f"{name}.log")
            # Append the _json_formatter to the handler as previous
            handler.setFormatter(_json_formatter)
            stdlib_logger.addHandler(handler)
            if _stdout_handler is not None:
                stdlib_logger.addHandler(_stdout_handler)
            stdlib_logger.setLevel(logging.INFO)
            stdlib_logger.propagate = False  # Prevent double logging to root logger

    return structlog.get_logger(name)

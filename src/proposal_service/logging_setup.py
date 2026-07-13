"""
Logging configuration for proposal_service.

Call :func:`configure_logging` once during application startup (in
``main.py``). Other modules should obtain a logger with::

    import logging
    logger = logging.getLogger(__name__)

and never call :func:`logging.basicConfig` themselves.
"""

from __future__ import annotations

import logging
import sys
import time
from logging import LogRecord


class UTCISOFormatter(logging.Formatter):
    """Formatter that renders timestamps as UTC ISO 8601 with a ``Z`` suffix."""

    converter = time.gmtime  # type: ignore[assignment]

    def formatTime(self, record: LogRecord, datefmt: str | None = None) -> str:
        # Example: 2026-05-20T14:23:01.123Z
        ct = self.converter(record.created)
        base = time.strftime("%Y-%m-%dT%H:%M:%S", ct)
        msecs = int(record.msecs)
        return f"{base}.{msecs:03d}Z"


_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger.

    Idempotent: clears any existing handlers before installing the new one,
    so reloads during development do not duplicate log lines.

    Args:
        level: Log level name (e.g. ``"INFO"`` or ``"DEBUG"``). Unknown
            values fall back to ``INFO`` with a warning.
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(UTCISOFormatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Quiet noisy third-party libraries that flood DEBUG output.
    for noisy in ("pdfminer", "pdfplumber", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.INFO))

    logging.getLogger(__name__).info("Logging configured at level=%s", level.upper())

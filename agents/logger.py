"""
agents/logger.py — Shared structured JSON logger for all Etsy AI agents.

Each log line is a single JSON object written to a rotating log file.
Log files rotate daily and keep 30 days of history.

Usage:
    from agents.logger import get_logger

    log = get_logger("analytics", "/path/to/logs/analytics.log")
    log.info("listing_stat_recorded", listing_id="123", views=45)
"""

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class _JSONFormatter(logging.Formatter):
    """Formats each log record as a single-line JSON object."""

    def __init__(self, agent_name: str):
        super().__init__()
        self.agent_name = agent_name

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": self.agent_name,
            "level": record.levelname,
            "action": record.getMessage(),
        }
        # Merge any extra kwargs passed via the `extra` dict
        extra = getattr(record, "_json_extra", {})
        payload.update(extra)
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Adapter that accepts keyword args as structured fields
# ---------------------------------------------------------------------------

class _StructuredLogger:
    """
    Thin wrapper around a stdlib Logger that accepts keyword arguments
    alongside the action string and merges them into the JSON payload.

    Example:
        log.info("listing_stat_recorded", listing_id="123", views=45)
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, action: str, **fields):
        extra = {"_json_extra": fields}
        self._logger.log(level, action, extra=extra, stacklevel=2)

    def debug(self, action: str, **fields):
        self._log(logging.DEBUG, action, **fields)

    def info(self, action: str, **fields):
        self._log(logging.INFO, action, **fields)

    def warning(self, action: str, **fields):
        self._log(logging.WARNING, action, **fields)

    def error(self, action: str, **fields):
        self._log(logging.ERROR, action, **fields)

    def critical(self, action: str, **fields):
        self._log(logging.CRITICAL, action, **fields)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def get_logger(name: str, log_file: str) -> _StructuredLogger:
    """
    Return a structured JSON logger that writes one JSON object per line.

    Parameters
    ----------
    name     : agent name embedded in every log line (e.g. "analytics")
    log_file : absolute path to the log file (parent dirs are created if needed)

    Rotation: daily, keeps 30 days of backups.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    stdlib_logger = logging.getLogger(f"etsy.{name}")
    # Avoid adding duplicate handlers across multiple calls
    if not stdlib_logger.handlers:
        stdlib_logger.setLevel(logging.DEBUG)

        handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            utc=True,
        )
        handler.setFormatter(_JSONFormatter(agent_name=name))
        stdlib_logger.addHandler(handler)

        # Also mirror to stderr at INFO+ so container logs work
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(_JSONFormatter(agent_name=name))
        stdlib_logger.addHandler(stream_handler)

    return _StructuredLogger(stdlib_logger)

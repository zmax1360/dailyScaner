"""
logging_config.py — Shared process logging for optionTrading.

Every long-lived / launchd process calls::

    from logging_config import setup_logging
    log = setup_logging("mark_runner")   # → logs/mark_runner.log

Rotating file (10 MB × 5 backups), America/New_York timestamps, optional
console echo when stdout is a TTY.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# process-name → already configured
_configured: dict[str, logging.Logger] = {}


def strip_ansi(text: str) -> str:
    """Remove ANSI color codes (safe for rotating log files)."""
    return _ANSI_RE.sub("", text)


class ETFormatter(logging.Formatter):
    """Format asctime in America/New_York."""

    def formatTime(self, record, datefmt=None):  # noqa: N802
        dt = datetime.fromtimestamp(record.created, tz=ET)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime(DATE_FORMAT)


class ETFileFormatter(ETFormatter):
    """ET timestamps + ANSI stripped (for rotating log files)."""

    def format(self, record: logging.LogRecord) -> str:
        # Copy so StreamHandler still sees original (colored) messages.
        rec = logging.makeLogRecord(record.__dict__)
        if isinstance(rec.msg, str):
            rec.msg = strip_ansi(rec.msg)
        if rec.args:
            rec.args = tuple(
                strip_ansi(a) if isinstance(a, str) else a for a in rec.args
            )
        return super().format(rec)


def setup_logging(
    name: str,
    *,
    level: int = logging.INFO,
    console: bool | None = None,
) -> logging.Logger:
    """
    Configure ``logs/{name}.log`` and return ``logging.getLogger(name)``.

    Idempotent per process name. Attaches a RotatingFileHandler to the root
    logger so library loggers (e.g. sources.yahoo) land in the same file.
    """
    name = str(name).strip()
    if not name:
        raise ValueError("setup_logging requires a non-empty name")

    if name in _configured:
        return _configured[name]

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"{name}.log")

    file_fmt = ETFileFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    console_fmt = ETFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_fmt)
    file_handler.setLevel(level)
    file_handler._optiontrading_log_name = name  # type: ignore[attr-defined]

    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_optiontrading_log_name", None) == name for h in root.handlers):
        root.addHandler(file_handler)

    if console is None:
        console = sys.stdout.isatty()
    if console and not any(getattr(h, "_optiontrading_console", False) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(console_fmt)
        sh.setLevel(level)
        sh._optiontrading_console = True  # type: ignore[attr-defined]
        root.addHandler(sh)

    log = logging.getLogger(name)
    log.setLevel(level)
    log.propagate = True

    _configured[name] = log
    return log

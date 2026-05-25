"""Logging setup.

Writes JSON lines to stdout for container log collectors and to a rotating
file under ``log_dir`` for local audit. Old log files past ``log_retention_days``
are pruned on startup.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from .config import Settings


def _drop_color_message_key(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.pop("color_message", None)
    return event_dict


def _cleanup_old_logs(log_dir: Path, log_filename: str, retention_days: int) -> int:
    """Delete rotated log files older than retention_days. Returns count removed."""
    if not log_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    stem = log_filename
    for entry in log_dir.iterdir():
        # Only touch files that belong to this app's rotation set.
        if not entry.is_file() or not entry.name.startswith(stem):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def configure_logging(settings: Settings) -> structlog.stdlib.BoundLogger:
    """Wire up structlog over the stdlib logger with stdout + rotating file handlers."""
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    pruned = _cleanup_old_logs(settings.log_dir, settings.log_filename, settings.log_retention_days)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_color_message_key,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.log_level.upper())

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    file_path = settings.log_dir / settings.log_filename
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Calm down very noisy third-party loggers.
    for noisy in ("dropbox", "urllib3", "aioimaplib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = structlog.stdlib.get_logger("mailattachments2dropbox")
    log.info(
        "logging_initialized",
        log_dir=str(settings.log_dir),
        log_file=str(file_path),
        level=settings.log_level.upper(),
        pruned_old_logs=pruned,
    )
    return log


def get_logger(name: str = "mailattachments2dropbox") -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)

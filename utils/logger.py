"""Application logging with persistent, redacted warning/error records."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
from typing import Optional


_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
_DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_ERROR_LOG_NAME = "opspilot-error.log"
_MAX_LOG_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5

_handler_lock = threading.RLock()
_file_handler: Optional[RotatingFileHandler] = None
_file_handler_error_reported = False

_BEARER_RE = re.compile(
    r"(?i)\b(Bearer)(\s+)([^\s,;\"'\]\}]+)"
)
_SK_KEY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9._-]+"
)
_SECRET_KEY = (
    # Preserve useful token-count metrics while redacting credentials with
    # provider prefixes such as DEEPSEEK_API_KEY or IMA_ACCESS_TOKEN.
    r"(?!(?:max[_-]?tokens?|token[_-]?limit|prompt[_-]?tokens?|"
    r"completion[_-]?tokens?|total[_-]?tokens?)\b)"
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*"
    r"(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|token|password|passwd|secret)"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>(?<![A-Za-z0-9_])[\"']?{_SECRET_KEY}[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_PLAIN_SECRET_RE = re.compile(
    rf"(?i)(?P<prefix>(?<![A-Za-z0-9_])[\"']?{_SECRET_KEY}[\"']?\s*[:=]\s*)"
    rf"(?P<value>[^\s,;\&\}}\]]+)"
)


def _redact_sensitive_data(value: object) -> str:
    """Return display-safe text without common API credentials."""
    text = str(value)
    text = _QUOTED_SECRET_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}***{match.group('quote')}"
        ),
        text,
    )
    text = _PLAIN_SECRET_RE.sub(
        lambda match: f"{match.group('prefix')}***",
        text,
    )
    text = _BEARER_RE.sub(r"\1\2***", text)
    return _SK_KEY_RE.sub("***", text)


class _RedactingFormatter(logging.Formatter):
    """Redact the complete rendered record, including exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return _redact_sensitive_data(super().format(record))


def _get_log_dir() -> Path:
    """Resolve the writable runtime log directory."""
    override = os.environ.get("OPSPILOT_LOG_DIR", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _get_file_handler() -> Optional[RotatingFileHandler]:
    """Create one process-wide rotating error handler, without breaking startup."""
    global _file_handler, _file_handler_error_reported
    with _handler_lock:
        if _file_handler is not None:
            return _file_handler
        try:
            log_dir = _get_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_dir / _ERROR_LOG_NAME,
                maxBytes=_MAX_LOG_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
                delay=True,
            )
            handler.setLevel(logging.WARNING)
            handler.setFormatter(
                _RedactingFormatter(_DEFAULT_FORMAT, datefmt=_DEFAULT_DATE_FORMAT)
            )
            handler._opspilot_file_handler = True  # type: ignore[attr-defined]
            _file_handler = handler
            return handler
        except OSError as exc:
            # Logging must never prevent the bot from starting.  Report the path
            # problem once on stderr; subsequent setup calls may retry.
            if not _file_handler_error_reported:
                sys.stderr.write(
                    "OpsPilot warning: persistent error log is unavailable: "
                    f"{_redact_sensitive_data(exc)}\n"
                )
                _file_handler_error_reported = True
            return None


def setup_logger(
    name: str = "dingtalk-bot",
    level: str = "INFO",
    fmt: str = _DEFAULT_FORMAT,
    datefmt: str = _DEFAULT_DATE_FORMAT,
) -> logging.Logger:
    """Create or update an application logger.

    Console output follows the requested level.  Warnings and errors are also
    persisted to ``opspilot-error.log`` with rotation and credential redaction.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    with _handler_lock:
        console_handler = next(
            (
                handler
                for handler in logger.handlers
                if getattr(handler, "_opspilot_console_handler", False)
            ),
            None,
        )
        has_external_console = any(
            isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            for handler in logger.handlers
        )
        if console_handler is None and not has_external_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler._opspilot_console_handler = True  # type: ignore[attr-defined]
            logger.addHandler(console_handler)
        if console_handler is not None:
            console_handler.setFormatter(_RedactingFormatter(fmt, datefmt=datefmt))

        persistent_handler = _get_file_handler()
        has_persistent_handler = any(
            getattr(handler, "_opspilot_file_handler", False)
            for handler in logger.handlers
        )
        if persistent_handler is not None and not has_persistent_handler:
            logger.addHandler(persistent_handler)

    return logger


def get_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Compatibility alias for :func:`setup_logger`."""
    return setup_logger(name=name, level=level)

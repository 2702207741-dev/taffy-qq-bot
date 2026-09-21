"""NoneBot/loguru sinks that never retain QQ message bodies or numeric identities."""

from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from nonebot.log import default_filter, logger, logger_id

if TYPE_CHECKING:
    from loguru import Record

_LONG_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])[0-9]{5,20}(?![A-Za-z0-9])")
_IDENTITY_FIELD_RE = re.compile(
    r"(?i)\b(user_id|group_id|self_id|sender_id|qq|steamid)\b\s*[:=]\s*['\"]?[^,}\s'\"]+"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(\b(?:app-key|api[_-]?key|access[_-]?token|authorization)=)[^&\s]+"
)
_BEARER_SECRET_RE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}")
_ONEBOT_EVENT_RE = re.compile(
    r"\bOneBot V11\b.*\| \[(?:message|notice|request|meta_event)\.",
    re.IGNORECASE,
)
_configured = False

#: Memoization slot for the sanitized exception block.  Loguru hands the *same*
#: record to every sink in registration order, so the pre-rendered text must not
#: depend on how many times (or by which sink) the format callback ran.
#: Namespaced with a ``_taffy_`` prefix on purpose: ``extra`` is also written by
#: ``_privacy_filter`` and by callers using ``logger.bind(...)``, so every key we
#: add here must stay collision-proof against those.
_SANITIZED_EXCEPTION_CACHE_KEY = "_taffy_sanitized_exception"

# Mirrors nonebot's default_format; ``{extra[sanitized_exception]}`` replaces
# the raw ``{exception}`` so traceback text can be redacted before it lands.
_RENDER_TEMPLATE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>\n{extra[sanitized_exception]}"
)


def _redact_text(text: str) -> str:
    """Apply every privacy rule to one rendered text blob.

    The Bearer rule runs before the ``key=value`` rules so an
    ``Authorization=Bearer <token>`` blob is fully neutralized instead of
    leaving the bare token behind.
    """

    text = _BEARER_SECRET_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _IDENTITY_FIELD_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _LONG_NUMBER_RE.sub("[redacted-id]", text)
    if "[CQ:" in text:
        text = re.sub(r"\[CQ:[^\]]+\]", "[CQ:redacted]", text)
    return text


def _redacting_format(record: "Record") -> str:
    """Render the record template after sanitizing the exception block.

    Loguru renders ``{exception}`` verbatim, which would leak secrets that
    appear only inside exception messages or tracebacks; pre-rendering it
    here lets the redaction rules cover the final text.

    The rendered text is memoized on the record: one record is handed to every
    sink in registration order, so the console sink clearing ``exception``
    (which is what stops loguru rendering the raw ``{exception}``) must not
    make the *next* sink — the file sink — render an empty block.  Doing that
    silently stripped every traceback out of ``taffy.log``.
    """

    sanitized = record["extra"].get(_SANITIZED_EXCEPTION_CACHE_KEY)
    if sanitized is None:
        sanitized = ""
        exc_info = record.get("exception")
        if exc_info is not None:
            parts = traceback.format_exception(exc_info.type, exc_info.value, exc_info.traceback)
            sanitized = _redact_text("".join(parts)).rstrip("\n") + "\n"
            record["exception"] = None  # already rendered above
        record["extra"][_SANITIZED_EXCEPTION_CACHE_KEY] = sanitized
    record["extra"]["sanitized_exception"] = sanitized
    return _RENDER_TEMPLATE


def _privacy_filter(record: "Record") -> bool:
    if not default_filter(record):
        return False
    name = str(record.get("name") or "")
    message = str(record.get("message") or "")
    if name.startswith("nonebot.message") or _ONEBOT_EVENT_RE.search(message):
        record["message"] = "OneBot message event received [content redacted]"
        return True
    record["message"] = _redact_text(message)
    return True


def resolve_log_directory(root: Path) -> Path:
    """Resolve the sanitized log directory (honours ``TAFFY_LOG_DIR``)."""

    configured_directory = os.environ.get("TAFFY_LOG_DIR")
    if configured_directory:
        log_directory = Path(configured_directory).expanduser()
        if not log_directory.is_absolute():
            log_directory = root.resolve() / log_directory
    else:
        log_directory = root.resolve() / "data" / "logs"
    return log_directory


def configure_privacy_logging(root: Path) -> None:
    """Install sanitized console and file sinks for NoneBot/loguru logging."""

    global _configured
    if _configured:
        return
    logger.remove(logger_id)
    logger.add(
        sys.stdout,
        level=0,
        diagnose=False,
        filter=_privacy_filter,
        format=_redacting_format,
    )
    log_directory = resolve_log_directory(root)
    log_directory.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_directory / "taffy.log",
        level="INFO",
        diagnose=False,
        backtrace=False,
        filter=_privacy_filter,
        format=_redacting_format,
        encoding="utf-8",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        enqueue=True,
    )
    _configured = True


__all__ = ["configure_privacy_logging", "resolve_log_directory"]

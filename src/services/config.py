"""Small, fail-closed helpers for environment-backed service settings."""

from __future__ import annotations

import logging
import math
import os
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from src.core.config import load_runtime_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_SETTINGS = load_runtime_settings(PROJECT_ROOT)

logger = logging.getLogger("taffy.services.config")


def runtime_setting(name: str) -> str | None:
    """Read a setting with the same dotenv layering as the bot runtime.

    A live process-environment value wins so tests, containers, and secret
    managers can still override values after this module is imported.  The
    immutable fallback includes ``.env``, ``.env.<environment>``, and the
    process environment captured at startup without mutating ``os.environ``.
    """

    direct = os.environ.get(name)
    if direct is not None:
        return direct
    value = _RUNTIME_SETTINGS.get(name.casefold())
    return None if value is None else str(value)


def project_path_setting(name: str, default: str | Path) -> Path:
    """Resolve a configured path against the project root, never the process CWD."""

    raw = runtime_setting(name)
    configured = raw.strip() if raw is not None else ""
    path = Path(configured).expanduser() if configured else Path(default).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def steamdt_cache_path() -> Path:
    """Return the single absolute cache path shared by the scraper and readers."""

    return project_path_setting("TAFFY_STEAMDT_CACHE", "steamdt_data.json")


def bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer and clamp it to an explicitly safe range."""

    raw = runtime_setting(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid integer setting %s", name)
        return default
    return min(maximum, max(minimum, value))


def bounded_env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a finite number and clamp it to an explicitly safe range."""

    raw = runtime_setting(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid numeric setting %s", name)
        return default
    if not math.isfinite(value):
        logger.warning("Ignoring non-finite numeric setting %s", name)
        return default
    return min(maximum, max(minimum, value))


def bounded_env_text(name: str, default: str, *, maximum: int = 256) -> str:
    """Read short single-line configuration, falling back when malformed."""

    raw = runtime_setting(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        logger.warning("Ignoring invalid text setting %s", name)
        return default
    return value


def env_secret(*names: str, maximum: int = 4096) -> str:
    """Return the first configured secret, or an empty value on invalid input.

    An explicitly malformed primary value is not skipped in favour of another
    variable.  This prevents a typo from silently selecting unintended
    credentials.  Empty values are treated as unconfigured and may fall back.
    """

    for name in names:
        raw = runtime_setting(name)
        if raw is None or not raw.strip():
            continue
        value = raw.strip()
        if len(value) > maximum or any(ord(char) < 33 for char in value):
            logger.error("Rejecting malformed secret setting %s", name)
            return ""
        return value
    return ""


def bounded_base_url(name: str, default: str, *, maximum: int = 2048) -> str:
    """Read an HTTP(S) base URL and fail closed when explicitly malformed."""

    raw = runtime_setting(name)
    value = default if raw is None else raw.strip()
    if not value or len(value) > maximum or any(ord(char) < 33 for char in value):
        logger.error("Rejecting malformed URL setting %s", name)
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        logger.error("Rejecting malformed URL setting %s", name)
        return ""
    if parsed.scheme == "http":
        hostname = parsed.hostname.casefold()
        try:
            loopback = hostname == "localhost" or ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname == "localhost"
        if not loopback and not env_bool("TAFFY_ALLOW_INSECURE_HTTP_ENDPOINTS", False):
            logger.error("Rejecting insecure non-loopback URL setting %s", name)
            return ""
    return value.rstrip("/")


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean without treating arbitrary strings as truthy."""

    raw = runtime_setting(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    logger.warning("Ignoring invalid boolean setting %s", name)
    return default


__all__ = [
    "bounded_base_url",
    "bounded_env_float",
    "bounded_env_int",
    "bounded_env_text",
    "env_bool",
    "env_secret",
    "project_path_setting",
    "runtime_setting",
    "steamdt_cache_path",
]

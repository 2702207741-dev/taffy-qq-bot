"""凭据管理：令牌只进 DPAPI（keyring），失败时降级为进程内存。"""

from __future__ import annotations

import hashlib
import logging

from .config import normalize_base_url, resolve_base_url

_log = logging.getLogger("taffy.desktop.credentials")

_SERVICE_NAME = "TaffyDesktopStudio"
_ACCOUNT_NAME = "admin_api_token"

_memory_fallback: tuple[str, str] | None = None


def _account(origin: str | None = None) -> str:
    origin = normalize_base_url(origin if origin is not None else resolve_base_url())
    if origin is None:
        raise ValueError("Invalid credential origin")
    return f"{_ACCOUNT_NAME}:{hashlib.sha256(origin.encode()).hexdigest()}"


def _keyring():
    """Import keyring lazily so tests and non-Windows runs can stub it."""

    import keyring

    return keyring


def save_token(token: str, *, origin: str | None = None) -> str:
    """Persist the token via DPAPI; returns "keyring" or "memory" sink used."""

    global _memory_fallback
    account = _account(origin)
    token = token.strip()
    if not token:
        raise ValueError("token must not be empty")
    try:
        _keyring().set_password(_SERVICE_NAME, account, token)
        _memory_fallback = None
        return "keyring"
    except Exception as exc:
        _log.warning("Keyring unavailable, using memory fallback error=%s", type(exc).__name__)
        _memory_fallback = (account, token)
        return "memory"


def load_token(*, origin: str | None = None) -> str | None:
    """Return the latest saved token, including a failed keyring-write fallback."""

    global _memory_fallback
    account = _account(origin)
    # A failed rotation may leave the old credential in the OS keyring. The
    # newer in-memory value must win, otherwise saving appears to succeed but
    # requests silently keep using the stale credential.
    fallback = _memory_fallback
    if fallback is not None and fallback[0] == account:
        return fallback[1]
    try:
        value = _keyring().get_password(_SERVICE_NAME, account)
    except Exception as exc:
        _log.warning("Keyring read failed error=%s", type(exc).__name__)
        value = None
    if value:
        return value
    return None


def clear_token(*, origin: str | None = None) -> bool:
    """Delete the stored token from every sink; True when something was removed."""

    global _memory_fallback
    account = _account(origin)
    removed = False
    try:
        _keyring().delete_password(_SERVICE_NAME, account)
        removed = True
    except Exception:
        pass
    fallback = _memory_fallback
    had_memory = fallback is not None and fallback[0] == account
    if had_memory:
        _memory_fallback = None
    return removed or had_memory

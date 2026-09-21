"""Feature policy for the reversible three-day V4 pilot.

Every flag is read lazily at decision time so operators can flip the rollout
through the environment without restarting the process (matching the rest of
the project's ``runtime_setting`` discipline).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .config import runtime_setting

logger = logging.getLogger("taffy.services.v4_policy")


def _flag(name: str, default: bool = False) -> bool:
    """Boolean ``runtime_setting`` read resolved through this module's globals.

    Going through the module-global :func:`runtime_setting` (instead of
    importing ``env_bool``) keeps every flag on one lazy lookup path.
    """

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


_PILOT_EXACT_COMMANDS = frozenset(
    {
        "\u7efc\u5408\u6307\u6570",
        "\u5f02\u52a8",
        "\u9884\u6d4b",
        "\u70ed\u95e8\u699c",
        "\u70ed\u5ea6\u699c",
        "\u6d41\u52a8\u6027\u699c",
        "\u6da8\u8dcc\u699c",
        "\u88c5\u5907\u6307\u6570",
        "k\u7ebf\u9ad8\u6e05",
    }
)


def _pilot_users() -> frozenset[int]:
    explicit = runtime_setting("TAFFY_V4_PILOT_USERS") or ""
    values = re.findall(r"\d+", explicit)
    if not values:
        values = re.findall(r"\d+", runtime_setting("SUPERUSERS") or "")
    return frozenset(int(value) for value in values if int(value) > 0)


def _pilot_deadline() -> datetime | None:
    """Parse the optional pilot expiry without weakening the default.

    ``TAFFY_V4_PILOT_UNTIL`` makes the documented three-day trial enforceable in
    code: once the deadline passes, pilot users lose access until the operator
    either rolls out fully or extends the deadline.  An unset or malformed
    value keeps the historical operator-controlled behaviour.
    """

    raw = runtime_setting("TAFFY_V4_PILOT_UNTIL")
    if not raw:
        return None
    candidate = raw.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Ignoring invalid TAFFY_V4_PILOT_UNTIL=%s", candidate)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def v4_enabled_for_user(user_id: object) -> bool:
    if not _flag("TAFFY_V4_ENABLED"):
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)):
        return False
    try:
        owner = int(user_id)
    except ValueError:
        return False
    if owner <= 0:
        return False
    if _flag("TAFFY_V4_FULL_ROLLOUT"):
        return True
    if owner not in _pilot_users():
        return False
    deadline = _pilot_deadline()
    if deadline is not None and datetime.now(timezone.utc) >= deadline:
        return False
    return True


def command_requires_v4_pilot(message: str) -> bool:
    command = message.strip().casefold()
    return command in _PILOT_EXACT_COMMANDS or command.startswith("k\u7ebf\u9ad8\u6e05 ")


__all__ = [
    "command_requires_v4_pilot",
    "v4_enabled_for_user",
]

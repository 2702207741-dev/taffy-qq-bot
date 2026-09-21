"""Security-sensitive runtime configuration helpers.

Keep ingress decisions here so they can be tested without starting NoneBot.
Secret values are deliberately never included in return objects or messages.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values

logger = logging.getLogger("taffy.core.config")

DEFAULT_LOOPBACK_HOST = "127.0.0.1"
ADMIN_API_TOKEN_MIN_LENGTH = 32
ADMIN_API_TOKEN_MIN_UNIQUE_CHARS = 8
ONEBOT_CREDENTIAL_MIN_LENGTH = 32
_BEARER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]+\Z")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")

#: Startup key-name hygiene: every dotenv key the runtime may legitimately
#: load.  Kept in sync with ``.env.example`` by a consistency test so adding
#: a new key must update both places (drift = failing test, not a typo that
#: silently falls back to defaults).  Loaded keys outside this set log a
#: WARNING with a close-match suggestion (``difflib.get_close_matches``) so
#: a misspelled ``TAFFY_VISION_ENABLE`` is caught at boot instead of at
#: debugging time.
KNOWN_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ADMIN_API_TOKEN",
        "CSQAQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "DESKTOP_OWNER_QQ",
        "DRIVER",
        "ENABLE_AUTO_WEB_SEARCH",
        "ENVIRONMENT",
        "FFMPEG_PATH",
        "HOST",
        "HTTP_MAX_RESPONSE_BYTES",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "INVENTORY_FLOAT_API_BASE",
        "INVENTORY_FLOAT_HOURLY_LIMIT",
        "LLM_GLOBAL_REQUESTS_PER_MINUTE",
        "LLM_MAX_CONCURRENCY",
        "LLM_MAX_HISTORY_CHARS",
        "LLM_MAX_INPUT_CHARS",
        "LLM_REQUESTS_PER_MINUTE",
        "LLM_TOTAL_TIMEOUT",
        "MAIL_TO",
        "ONEBOT_V11_ACCESS_TOKEN",
        "ONEBOT_V11_SECRET",
        "ONEBOT_V11_WS_URLS",
        "ONEBOT_HEARTBEAT_INTERVAL",
        "ONEBOT_WS_RECONNECT_INTERVAL",
        "ONEBOT_WS_URLS",
        "PANDASCORE_TOKEN",
        "PORT",
        "SMTP_HOST",
        "SMTP_PASS",
        "SMTP_PORT",
        "SMTP_USER",
        "STEAMDT_API_KEY",
        "SUPERUSERS",
        "TAFFY_ALERT_BATCH_SIZE",
        "TAFFY_ALERT_CONCURRENCY",
        "TAFFY_ALERT_LEASE_SECONDS",
        "TAFFY_ALERT_MAX_ATTEMPTS",
        "TAFFY_ALERT_SEND_TIMEOUT_SECONDS",
        "TAFFY_ALLOW_AUTO_DB_MIGRATION",
        "TAFFY_ALLOW_INSECURE_HTTP_ENDPOINTS",
        "TAFFY_API_KEY",
        "TAFFY_AUTOMATIC_BACKUP_DIR",
        "TAFFY_BACKUP_LLM_API_KEY",
        "TAFFY_BACKUP_LLM_BASE_URL",
        "TAFFY_BACKUP_LLM_MODEL",
        "TAFFY_BASE_URL",
        "TAFFY_API_FORMAT",
        "TAFFY_OPENAI_MIN_OUTPUT_TOKENS",
        "TAFFY_OPENAI_EMPTY_LENGTH_RETRY",
        "TAFFY_BROADCAST_CATCHUP_MAX_SLOTS",
        "TAFFY_BROADCAST_CATCHUP_MINUTES",
        "TAFFY_BROADCAST_LEASE_SECONDS",
        "TAFFY_BROADCAST_MAX_ATTEMPTS",
        "TAFFY_BROADCAST_RETRY_BASE_SECONDS",
        "TAFFY_BROADCAST_RETRY_BATCH_SIZE",
        "TAFFY_BROADCAST_RETRY_MAX_SECONDS",
        "TAFFY_BROADCAST_SEND_CONCURRENCY",
        "TAFFY_BROADCAST_SEND_TIMEOUT_SECONDS",
        "TAFFY_CASE_EV_ENABLED",
        "TAFFY_CASE_META_TIMEOUT_SECONDS",
        "TAFFY_CASE_META_TTL_HOURS",
        "TAFFY_CONFIG_BACKUP_DIR",
        "TAFFY_CSGO_META_DB",
        "TAFFY_CSQAQ_HISTORY_DB",
        "TAFFY_CSQAQ_HISTORY_ENABLED",
        "TAFFY_DELIVERY_RETENTION_DAYS",
        "TAFFY_DESKTOP_API_ENABLED",
        "TAFFY_EMBEDDING_API_KEY",
        "TAFFY_EMBEDDING_BASE_URL",
        "TAFFY_EMBEDDING_DAILY_BUDGET",
        "TAFFY_EMBEDDING_MODEL",
        "TAFFY_EMBEDDING_TIMEOUT_SECONDS",
        "TAFFY_ENABLE_CACHE_WARMER",
        "TAFFY_FAILED_RETENTION_DAYS",
        "TAFFY_HOARD_INDEX_DB",
        "TAFFY_HOARD_INDEX_DOWNLOAD_BASE",
        "TAFFY_HOARD_INDEX_ENABLED",
        "TAFFY_HOARD_INDEX_LIST_BASE",
        "TAFFY_HOARD_INDEX_MAX_AGE_SECONDS",
        "TAFFY_HOARD_INDEX_TIMEOUT_SECONDS",
        "TAFFY_HOARD_INDEX_TTL_HOURS",
        "TAFFY_INGRESS_TLS_TERMINATED",
        "TAFFY_INSPECT_DAILY_BUDGET",
        "TAFFY_INSPECT_ENABLED",
        "TAFFY_INVENTORY_EVENT_RETENTION_DAYS",
        "TAFFY_INVENTORY_HISTORY_RETENTION_DAYS",
        "TAFFY_INVENTORY_MONITOR_MAX_ATTEMPTS",
        "TAFFY_INVENTORY_MONITOR_MAX_TOTAL_ACCOUNTS",
        "TAFFY_INVENTORY_MONITOR_MAX_TOTAL_SUBSCRIPTIONS",
        "TAFFY_INVENTORY_MONITOR_POLL_SECONDS",
        "TAFFY_JOB_RETENTION_DAYS",
        "TAFFY_KLINE_SCREENSHOT_CACHE_SECONDS",
        "TAFFY_KLINE_SCREENSHOT_MAX_BYTES",
        "TAFFY_KLINE_SCREENSHOT_TERMINATE_GRACE_SECONDS",
        "TAFFY_KLINE_SCREENSHOT_TIMEOUT_SECONDS",
        "TAFFY_LEARNING_ANALYZE_INTERVAL_SECONDS",
        "TAFFY_LEARNING_ENABLED",
        "TAFFY_LEARNING_RETENTION_DAYS",
        "TAFFY_LOG_DIR",
        "TAFFY_MAINTENANCE_INTERVAL_SECONDS",
        "TAFFY_MARKET_CATALOG_DB",
        "TAFFY_MARKET_CATALOG_MIN_ENTRIES",
        "TAFFY_MARKET_HISTORY_DB",
        "TAFFY_MARKET_HISTORY_MIN_CATALOG",
        "TAFFY_MARKET_SAMPLE_POOL_SIZE",
        "TAFFY_MEMORY_CONSOLIDATION_ENABLED",
        "TAFFY_MEMORY_SEMANTIC_ENABLED",
        "TAFFY_MEMORY_SEMANTIC_PILOT_USERS",
        "TAFFY_MEMORY_WRITE_DECISION_ENABLED",
        "TAFFY_MODEL",
        "TAFFY_PERSONA_AUDIT_ENABLED",
        "TAFFY_PERSONA_REVIEW_ENABLED",
        "TAFFY_PERSONA_REVIEW_GLOBAL_MINUTE",
        "TAFFY_PERSONA_REVIEW_PER_USER_MINUTE",
        "TAFFY_PERSONA_REVIEW_TIMEOUT_SECONDS",
        "TAFFY_PERSONA_V3_ENABLED",
        "TAFFY_PERSONA_V3_FULL_ROLLOUT",
        "TAFFY_PERSONA_V3_PILOT_USERS",
        "TAFFY_PERSONA_WORLD_ENTRIES",
        "TAFFY_PROACTIVE_DAILY_CAP",
        "TAFFY_PROACTIVE_ENABLED",
        "TAFFY_PROACTIVE_MORNING_HOUR",
        "TAFFY_GROUP_PULSE_COOLDOWN_SECONDS",
        "TAFFY_GROUP_PULSE_DAILY_CAP",
        "TAFFY_GROUP_PULSE_ENABLED",
        "TAFFY_MOOD_ENABLED",
        "TAFFY_MOOD_PILOT_USERS",
        "TAFFY_REFLECTION_HOUR",
        "TAFFY_RETENTION_BATCH_SIZE",
        "TAFFY_SCHEDULE_CONFIG",
        "TAFFY_SCRAPE_SCRIPT",
        "TAFFY_SCRAPE_TIMEOUT_SECONDS",
        "TAFFY_SCRAPE_WORKDIR",
        "TAFFY_SESSION_RETENTION_DAYS",
        # Runtime-only knobs (no .env.example entry, used by code paths).
        "TAFFY_SITEMAP_SYNC_ENABLED",
        "TAFFY_SITEMAP_MIN_ENTRIES",
        "TAFFY_SITEMAP_URL",
        "TAFFY_STATE_DB",
        "TAFFY_STEAMDT_CACHE",
        "TAFFY_STICKER_AUTOCOLLECT_ENABLED",
        "TAFFY_STICKER_COOLDOWN_SECONDS",
        "TAFFY_STICKER_DAILY_CAP",
        "TAFFY_STICKER_REPLY_PROBABILITY",
        "TAFFY_STICKER_SMART_SELECT_ENABLED",
        "TAFFY_STICKERS_ENABLED",
        "TAFFY_TELEMETRY_DB",
        "TAFFY_THINKING_MODE",
        "TAFFY_TOOLS_ENABLED",
        "TAFFY_TOOLS_FULL_ROLLOUT",
        "TAFFY_TOOLS_PILOT_USERS",
        "TAFFY_TOOL_MAX_ROUNDS",
        "TAFFY_V4_ENABLED",
        "TAFFY_V4_FULL_ROLLOUT",
        "TAFFY_V4_PILOT_UNTIL",
        "TAFFY_V4_PILOT_USERS",
        "TAFFY_VIDEO_DIR",
        "TAFFY_VISION_API_KEY",
        "TAFFY_VISION_BASE_URL",
        "TAFFY_VISION_DAILY_BUDGET",
        "TAFFY_VISION_ENABLED",
        "TAFFY_VISION_MAX_IMAGE_BYTES",
        "TAFFY_VISION_MODEL",
        "TAFFY_VISION_TIMEOUT_SECONDS",
    }
)

#: How close a loaded key must be to a known key before we call it a typo.
_KEY_TYPO_CUTOFF = 0.7  # difflib ratio; edit distance <= ~2 for long keys


def _close_known_keys(candidate: str, limit: int = 3) -> tuple[str, ...]:
    """Return up to ``limit`` known keys that look like ``candidate``."""
    return tuple(
        difflib.get_close_matches(candidate, KNOWN_ENV_KEYS, n=limit, cutoff=_KEY_TYPO_CUTOFF)
    )


def audit_env_key_names(settings: Mapping[str, Any]) -> None:
    """Log WARNINGs for unknown or near-miss dotenv keys (warn-only, fail-open).

    Called once at startup after settings are loaded.  Keys are compared in
    the canonical uppercase form; secrets are never included in the message.

    The project's operational log channel is loguru (``configure_privacy_logging``
    installs loguru sinks only); stdlib ``logging`` records are invisible in
    ``data/logs/taffy.log``, so the warn/audit lines are emitted through
    loguru as well, with the stdlib logger kept for tests.
    """

    loaded = {
        str(key).strip().upper() for key in settings if isinstance(key, str) and str(key).strip()
    }
    if not loaded:
        return
    matched = loaded & KNOWN_ENV_KEYS
    unknown = loaded - matched
    summary = f"Env key audit: {len(loaded)} loaded, {len(unknown)} unknown"
    logger.info(summary)  # stdlib 通道：测试与标准日志均可见
    _loguru_audit("info", summary)
    if not unknown:
        return
    for key in sorted(unknown):
        suggestions = _close_known_keys(key)
        hint = f"，可能是 {suggestions[0]} 的拼写错误" if suggestions else ""
        message = f"Unknown dotenv key ignored: {key}{hint}"
        logger.warning(message)
        _loguru_audit("warning", message)


def _loguru_audit(level: str, message: str) -> None:
    """Best-effort loguru emission; never raises at startup."""

    try:
        from loguru import logger as loguru_logger

        getattr(loguru_logger, level)(message)
    except Exception:
        pass


def audit_project_env_keys(root: Path) -> None:
    """Audit the project dotenv layers only (never the process environment).

    ``load_runtime_settings`` merges ``os.environ``, which would flood this
    warn-only audit with irrelevant system keys; the startup audit is meant
    to catch typos in the operator's own ``.env`` files.
    """

    base_path, environment_path = runtime_env_files(root)
    layered: dict[str, Any] = {
        str(key): value
        for key, value in dotenv_values(base_path).items()
        if key and value is not None
    }
    for key, value in dotenv_values(environment_path).items():
        if key and value is not None:
            layered[str(key)] = value
    audit_env_key_names(layered)


DEFAULT_LOOPBACK_HOST = "127.0.0.1"
ADMIN_API_TOKEN_MIN_LENGTH = 32
ADMIN_API_TOKEN_MIN_UNIQUE_CHARS = 8
ONEBOT_CREDENTIAL_MIN_LENGTH = 32
_BEARER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]+\Z")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


@dataclass(frozen=True, slots=True)
class IngressSecurity:
    """The safe host to give NoneBot and whether it was overridden."""

    host: str
    downgraded_to_loopback: bool = False


def _casefolded(values: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key).casefold(): value for key, value in values.items()}


def _setting(values: Mapping[str, Any], *names: str) -> str | None:
    folded = _casefolded(values)
    for name in names:
        value = folded.get(name.casefold())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def load_runtime_settings(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load the same base/prod dotenv layers used by NoneBot.

    Process environment values retain highest priority.  This function does not
    mutate ``os.environ`` and therefore cannot accidentally publish a secret to
    child processes.
    """

    environment_values = dict(os.environ if environ is None else environ)
    base_values = dict(dotenv_values(root / ".env"))
    initial = {**_casefolded(base_values), **_casefolded(environment_values)}
    environment_name = _safe_environment_name(initial.get("environment"))
    environment_file_values = dict(dotenv_values(root / f".env.{environment_name}"))
    return {
        **_casefolded(base_values),
        **_casefolded(environment_file_values),
        **_casefolded(environment_values),
    }


def _safe_environment_name(value: object) -> str:
    name = str(value or "prod").strip()
    return name if _ENVIRONMENT_NAME_PATTERN.fullmatch(name) else "prod"


def runtime_env_files(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Return absolute dotenv paths shared by NoneBot and local services."""

    environment_values = dict(os.environ if environ is None else environ)
    base_path = root.resolve() / ".env"
    base_values = dict(dotenv_values(base_path))
    initial = {**_casefolded(base_values), **_casefolded(environment_values)}
    environment_name = _safe_environment_name(initial.get("environment"))
    return base_path, root.resolve() / f".env.{environment_name}"


def secure_onebot_host(settings: Mapping[str, Any]) -> IngressSecurity:
    """Prevent unauthenticated OneBot server routes from binding publicly.

    OneBot V11 uses separate credentials for HTTP signatures and reverse
    WebSocket authentication.  A non-loopback listener is retained only when
    both are configured and the operator explicitly confirms that a trusted
    TLS reverse proxy terminates every inbound connection.  Outbound/forward
    WebSocket connections continue to work when the server is bound to
    loopback.
    """

    host = _setting(settings, "host") or DEFAULT_LOOPBACK_HOST
    if host.casefold() == "localhost":
        host = DEFAULT_LOOPBACK_HOST

    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError:
        is_loopback = False

    if is_loopback:
        return IngressSecurity(host=host)

    secret = _setting(settings, "onebot_v11_secret", "onebot_secret")
    access_token = _setting(
        settings,
        "onebot_v11_access_token",
        "onebot_access_token",
    )
    tls_terminated = (_setting(settings, "taffy_ingress_tls_terminated") or "").casefold()
    if (
        _is_strong_secret(secret, ONEBOT_CREDENTIAL_MIN_LENGTH)
        and _is_strong_secret(access_token, ONEBOT_CREDENTIAL_MIN_LENGTH)
        and tls_terminated in {"1", "true", "yes", "on"}
    ):
        return IngressSecurity(host=host)

    return IngressSecurity(
        host=DEFAULT_LOOPBACK_HOST,
        downgraded_to_loopback=True,
    )


def resolve_ingress_security(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> IngressSecurity:
    """Resolve the safe listener using dotenv files and the process environment."""

    return secure_onebot_host(load_runtime_settings(root, environ))


def _is_strong_secret(value: Any, minimum_length: int) -> bool:
    if not isinstance(value, str):
        return False
    return (
        len(value) >= minimum_length
        and len(set(value)) >= ADMIN_API_TOKEN_MIN_UNIQUE_CHARS
        and _BEARER_TOKEN_PATTERN.fullmatch(value) is not None
    )


def normalize_admin_api_token(value: Any) -> str | None:
    """Accept only a plausibly high-entropy Bearer token.

    Operators should generate this value with ``secrets.token_urlsafe(32)``.
    Length cannot prove randomness, but rejecting short or whitespace-bearing
    values prevents common weak/default configurations from enabling the API.
    """

    if not isinstance(value, str):
        return None
    token = value.strip()
    if token != value or len(token) < ADMIN_API_TOKEN_MIN_LENGTH:
        return None
    if not _is_strong_secret(token, ADMIN_API_TOKEN_MIN_LENGTH):
        return None
    return token


def get_admin_api_token(config: object | None = None) -> str | None:
    """Read and validate ADMIN_API_TOKEN without logging or caching it."""

    value: Any = os.environ.get("ADMIN_API_TOKEN")
    if value is None and config is not None:
        value = getattr(config, "admin_api_token", None)
    return normalize_admin_api_token(value)

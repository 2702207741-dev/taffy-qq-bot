"""Production health snapshots without secret or user-data disclosure.

The public readiness endpoint intentionally exposes only the aggregate status.
This module builds the richer snapshot consumed by the authenticated admin API.
No check performs network I/O: an established OneBot connection is the source
of truth for transport readiness, while local filesystem probes run off the
event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from src.core.config import secure_onebot_host

logger = logging.getLogger("taffy.health")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SCHEDULER_TASKS = (
    "broadcast",
    "warm-cache",
    "alerts",
    "inventory-monitors",
    "maintenance",
    "operations",
)
_STARTED_AT = time.monotonic()


def _setting(
    config: object | None,
    environ: Mapping[str, str],
    *names: str,
) -> str | None:
    """Read a non-empty setting without returning it outside this module."""

    environment = {str(key).casefold(): value for key, value in environ.items()}
    config_values: Mapping[str, Any]
    if isinstance(config, Mapping):
        config_values = {str(key).casefold(): value for key, value in config.items()}
    else:
        config_values = {}

    for name in names:
        folded = name.casefold()
        value: Any = environment.get(folded)
        if value is None:
            value = config_values.get(folded)
        if value is None and config is not None:
            value = getattr(config, folded, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _ingress_is_safe(config: object | None, environ: Mapping[str, str]) -> bool:
    host = _setting(config, environ, "HOST") or "127.0.0.1"
    secret = _setting(config, environ, "ONEBOT_V11_SECRET", "ONEBOT_SECRET")
    access_token = _setting(
        config,
        environ,
        "ONEBOT_V11_ACCESS_TOKEN",
        "ONEBOT_ACCESS_TOKEN",
    )
    tls_terminated = _setting(config, environ, "TAFFY_INGRESS_TLS_TERMINATED")
    # Reuse the application's ingress policy, including its credential-strength
    # rules.  Values stay inside this function; only the downgrade boolean is
    # retained in the snapshot.
    ingress = secure_onebot_host(
        {
            "host": host,
            "onebot_v11_secret": secret,
            "onebot_v11_access_token": access_token,
            "taffy_ingress_tls_terminated": tls_terminated,
        }
    )
    return not ingress.downgraded_to_loopback


def _critical_configuration(
    config: object | None,
    environ: Mapping[str, str],
) -> dict[str, bool]:
    llm_provider = bool(
        _setting(
            config,
            environ,
            "TAFFY_API_KEY",
            "ANTHROPIC_API_KEY",
            "DEEPSEEK_API_KEY",
        )
    )
    market_provider = bool(_setting(config, environ, "STEAMDT_API_KEY"))
    return {
        "llm_provider": llm_provider,
        "market_provider": market_provider,
        "onebot_ingress": _ingress_is_safe(config, environ),
    }


def _required_scheduler_tasks(
    config: object | None,
    environ: Mapping[str, str],
) -> tuple[str, ...]:
    """Require the optional browser worker only when it is explicitly enabled."""

    raw = _setting(config, environ, "TAFFY_ENABLE_CACHE_WARMER")
    enabled = raw is not None and raw.casefold() in {"1", "true", "yes", "on"}
    if enabled:
        return REQUIRED_SCHEDULER_TASKS
    return tuple(name for name in REQUIRED_SCHEDULER_TASKS if name != "warm-cache")


def _runtime_onebot_connection_count() -> int:
    """Count active OneBot V11 bots without exposing their self IDs."""

    try:
        from nonebot import get_bots
        from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot

        return sum(isinstance(bot, OneBotV11Bot) for bot in get_bots().values())
    except (ImportError, RuntimeError, ValueError):
        return 0


def _runtime_scheduler_tasks() -> Mapping[str, object]:
    """Inspect an already-loaded scheduler without importing plugin side effects.

    Application-level loops (``operations``/``maintenance``) live in
    ``_application_tasks`` after the connection-lifecycle split; readiness must
    observe both registries or those tasks always look stopped.
    """

    scheduler = sys.modules.get("src.plugins.scheduler")
    if scheduler is None:
        return {}
    connection = getattr(scheduler, "_scheduler_tasks", None)
    application = getattr(scheduler, "_application_tasks", None)
    merged: dict[str, object] = {}
    if isinstance(application, Mapping):
        merged.update(application)
    if isinstance(connection, Mapping):
        merged.update(connection)
    return merged


def _task_is_running(task: object | None) -> bool:
    if task is None:
        return False
    done = getattr(task, "done", None)
    cancelled = getattr(task, "cancelled", None)
    if not callable(done) or not callable(cancelled):
        return False
    try:
        return not bool(done()) and not bool(cancelled())
    except (RuntimeError, TypeError):
        return False


def _storage_directories(
    root: Path,
    config: object | None,
    environ: Mapping[str, str],
) -> tuple[Path, ...]:
    """Return directories required by the SQLite state stores and their files."""

    directories = [root, root / "data"]
    configured_database = _setting(config, environ, "TAFFY_STATE_DB")
    if configured_database:
        database_path = Path(configured_database).expanduser()
        if not database_path.is_absolute():
            database_path = root / database_path
        directories.append(database_path.parent)
    configured_schedule = _setting(config, environ, "TAFFY_SCHEDULE_CONFIG")
    if configured_schedule:
        schedule_path = Path(configured_schedule).expanduser()
        if not schedule_path.is_absolute():
            schedule_path = root / schedule_path
        directories.append(schedule_path.parent)

    # Preserve order while avoiding duplicate probes.  Paths stay internal and
    # are never serialized into a snapshot.
    return tuple(dict.fromkeys(directories))


def _probe_directory_writable(path: Path) -> bool:
    """Verify actual create/fsync/delete capability for one state directory."""

    probe_path: Path | None = None
    try:
        if not path.is_dir():
            return False
        descriptor, raw_path = tempfile.mkstemp(prefix=".taffy-health-", dir=path)
        probe_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as probe:
                probe.write(b"ok")
                probe.flush()
                os.fsync(probe.fileno())
        except BaseException:
            # os.fdopen owns the descriptor after it succeeds.  If it fails,
            # close the raw descriptor before propagating to the safe handler.
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return True
    except (OSError, ValueError):
        return False
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


async def collect_health_snapshot(
    *,
    config: object | None = None,
    root: Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
    onebot_connection_count: int | None = None,
    scheduler_tasks: Mapping[str, object] | None = None,
    storage_directories: Sequence[Path] | None = None,
    state_ready: bool | None = None,
) -> dict[str, Any]:
    """Build a detailed, JSON-safe readiness snapshot.

    Optional runtime inputs make the checks deterministic in tests.  Secret
    values, filesystem paths, bot identifiers, task exceptions, and user data
    are deliberately excluded from the result.
    """

    environment = os.environ if environ is None else environ
    connection_count = (
        _runtime_onebot_connection_count()
        if onebot_connection_count is None
        else max(0, int(onebot_connection_count))
    )
    tasks = _runtime_scheduler_tasks() if scheduler_tasks is None else scheduler_tasks
    required_scheduler_tasks = _required_scheduler_tasks(config, environment)
    scheduler_states = {
        name: _task_is_running(tasks.get(name)) for name in required_scheduler_tasks
    }

    directories = (
        tuple(storage_directories)
        if storage_directories is not None
        else (_storage_directories(root, config, environment))
    )
    # Path operations, temporary-file creation, fsync, and deletion are all
    # potentially blocking and therefore never run on the event-loop thread.
    writable_results = await asyncio.gather(
        *(asyncio.to_thread(_probe_directory_writable, Path(path)) for path in directories)
    )
    writable_count = sum(writable_results)

    if state_ready is None:
        try:
            from src.state.runtime import state_healthcheck

            async with asyncio.timeout(2.0):
                database_ready = await state_healthcheck()
        except Exception as exc:
            logger.warning(
                "SQLite state readiness check failed error=%s",
                type(exc).__name__,
            )
            database_ready = False
    else:
        database_ready = bool(state_ready)

    configuration_states = _critical_configuration(config, environment)
    checks: dict[str, dict[str, Any]] = {
        "onebot_connection": {
            "status": "pass" if connection_count > 0 else "fail",
            "connected": connection_count > 0,
        },
        "scheduler": {
            "status": "pass" if all(scheduler_states.values()) else "fail",
            "running": sum(scheduler_states.values()),
            "required": len(required_scheduler_tasks),
            "tasks": {
                name: "running" if running else "stopped"
                for name, running in scheduler_states.items()
            },
        },
        "storage": {
            "status": "pass" if writable_count == len(directories) else "fail",
            "writable": writable_count,
            "required": len(directories),
        },
        "database": {
            "status": "pass" if database_ready else "fail",
            "ready": database_ready,
        },
        "critical_config": {
            "status": "pass" if all(configuration_states.values()) else "fail",
            "settings": {
                name: "configured" if configured else "missing_or_unsafe"
                for name, configured in configuration_states.items()
            },
        },
    }
    ready = all(check["status"] == "pass" for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "uptime_seconds": max(0, int(time.monotonic() - _STARTED_AT)),
        "checks": checks,
    }


def public_readiness(snapshot: Mapping[str, Any]) -> dict[str, str]:
    """Reduce a detailed snapshot to the anonymous endpoint contract."""

    return {"status": "ready" if snapshot.get("ready") is True else "not_ready"}

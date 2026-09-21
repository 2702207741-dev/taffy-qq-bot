"""Isolated subprocess runner for refreshing the SteamDT cache.

The scheduler owns *when* a refresh is due.  This service owns the unsafe
boundary around starting and stopping the scraper process.  In particular,
the child receives an allowlisted environment instead of the bot process'
credentials, and child output is never copied into application logs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from src.services.config import (
    env_bool,
    project_path_setting,
    runtime_setting,
    steamdt_cache_path,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger("taffy.services.cache_warmer")
_ON_DEMAND_MAX_AGE_SECONDS = 10 * 60
_on_demand_refresh_lock = asyncio.Lock()

_CHILD_ENV_ALLOWLIST = (
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)

_CHILD_LAYERED_SETTINGS = (
    "ENVIRONMENT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "TAFFY_STEAMDT_CACHE",
)


@dataclass(frozen=True, slots=True)
class CacheWarmerConfig:
    """One immutable snapshot of the settings needed by a scraper run."""

    script: Path
    workdir: Path
    timeout_seconds: float
    terminate_grace_seconds: float


def _seconds_from_setting(name: str, default: float, *, minimum: float = 0.05) -> float:
    raw = runtime_setting(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric cache-warmer setting %s", name)
        return default
    if not math.isfinite(value) or value < minimum:
        logger.warning("Ignoring invalid cache-warmer setting %s", name)
        return default
    return value


def configured_path(name: str, default: Path) -> Path:
    """Resolve a layered path setting relative to the repository root."""

    return project_path_setting(name, default)


def cache_warmer_enabled() -> bool:
    """Return the process-level switch for the optional browser worker."""

    return env_bool("TAFFY_ENABLE_CACHE_WARMER", False)


def load_cache_warmer_config() -> CacheWarmerConfig:
    """Load one consistent configuration snapshot for a single run."""

    return CacheWarmerConfig(
        script=configured_path("TAFFY_SCRAPE_SCRIPT", PROJECT_ROOT / "scrape_steamdt.py"),
        workdir=configured_path("TAFFY_SCRAPE_WORKDIR", PROJECT_ROOT),
        timeout_seconds=_seconds_from_setting("TAFFY_SCRAPE_TIMEOUT_SECONDS", 45.0),
        terminate_grace_seconds=_seconds_from_setting(
            "TAFFY_SCRAPE_TERMINATE_GRACE_SECONDS",
            5.0,
        ),
    )


def subprocess_env() -> dict[str, str]:
    """Build a minimal child environment without forwarding bot secrets."""

    environment = {
        key: value for key in _CHILD_ENV_ALLOWLIST if (value := os.environ.get(key)) is not None
    }
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    for name in _CHILD_LAYERED_SETTINGS:
        if value := runtime_setting(name):
            environment[name] = value
    return environment


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float | None = None,
) -> None:
    """Stop a child with a bounded terminate -> wait -> kill sequence."""

    if process.returncode is not None:
        return
    grace = grace_seconds
    if grace is None or not math.isfinite(grace) or grace <= 0:
        grace = _seconds_from_setting("TAFFY_SCRAPE_TERMINATE_GRACE_SECONDS", 5.0)

    try:
        process.terminate()
    except ProcessLookupError:
        logger.debug("Cache warmer exited before terminate")

    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
        return
    except TimeoutError:
        logger.warning("Cache warmer ignored terminate; escalating to kill")

    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            logger.debug("Cache warmer exited before kill")
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except TimeoutError:
        # There is no stronger portable signal after kill.  Keep the service
        # bounded so application shutdown cannot hang indefinitely.
        logger.error("Cache warmer did not exit after kill")


async def _drain_communication(
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
    *,
    grace_seconds: float,
    phase: str,
) -> None:
    """Finish pipe draining without ever logging possibly sensitive output."""

    try:
        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=grace_seconds)
    except TimeoutError:
        logger.error("Cache warmer output pipes stayed open after %s", phase)
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
    except Exception as exc:
        logger.error(
            "Cache warmer pipe cleanup failed after %s (error_type=%s)",
            phase,
            type(exc).__name__,
        )


async def _stop_and_drain(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
    *,
    grace_seconds: float,
    phase: str,
) -> None:
    try:
        await terminate_process(process, grace_seconds=grace_seconds)
    except Exception as exc:
        logger.error(
            "Cache warmer process cleanup failed after %s (error_type=%s)",
            phase,
            type(exc).__name__,
        )
    await _drain_communication(
        communicate_task,
        grace_seconds=grace_seconds,
        phase=phase,
    )


async def run_cache_warmer_once() -> bool:
    """Run the scraper once with the current interpreter and bounded cleanup."""

    try:
        config = load_cache_warmer_config()
        script_is_file = config.script.is_file()
        workdir_is_directory = config.workdir.is_dir()
    except (OSError, ValueError) as exc:
        logger.error("Invalid cache-warmer path configuration (error_type=%s)", type(exc).__name__)
        return False

    if not script_is_file:
        logger.error("Configured cache-warmer script is not a file")
        return False
    if not workdir_is_directory:
        logger.error("Configured cache-warmer working directory is not a directory")
        return False

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(config.script),
            cwd=str(config.workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env(),
        )
    except (OSError, ValueError) as exc:
        logger.error("Unable to start cache warmer (error_type=%s)", type(exc).__name__)
        return False

    communicate_task = asyncio.create_task(process.communicate())
    try:
        await asyncio.wait_for(
            asyncio.shield(communicate_task),
            timeout=config.timeout_seconds,
        )
    except TimeoutError:
        logger.error("Cache warmer exceeded its configured timeout")
        await _stop_and_drain(
            process,
            communicate_task,
            grace_seconds=config.terminate_grace_seconds,
            phase="timeout",
        )
        return False
    except asyncio.CancelledError:
        logger.info("Cache warmer was cancelled; stopping child process")
        await asyncio.shield(
            _stop_and_drain(
                process,
                communicate_task,
                grace_seconds=config.terminate_grace_seconds,
                phase="cancellation",
            )
        )
        raise
    except Exception as exc:
        logger.error("Cache warmer communication failed (error_type=%s)", type(exc).__name__)
        await _stop_and_drain(
            process,
            communicate_task,
            grace_seconds=config.terminate_grace_seconds,
            phase="communication failure",
        )
        return False

    if process.returncode != 0:
        logger.error("Cache warmer exited unsuccessfully (returncode=%s)", process.returncode)
        return False
    logger.debug("Cache warmer completed successfully")
    return True


def _cache_is_fresh(path: Path, *, now: float, max_age_seconds: float) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return -300 <= age <= max_age_seconds


async def ensure_volume_cache_fresh(
    *,
    max_age_seconds: float = _ON_DEMAND_MAX_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """Refresh the volume cache only when a broad-market request needs it."""

    current_time = time.time() if now is None else now
    path = steamdt_cache_path()
    if _cache_is_fresh(path, now=current_time, max_age_seconds=max_age_seconds):
        return True
    async with _on_demand_refresh_lock:
        current_time = time.time() if now is None else now
        if _cache_is_fresh(path, now=current_time, max_age_seconds=max_age_seconds):
            return True
        if not await run_cache_warmer_once():
            return False
        current_time = time.time() if now is None else now
        return _cache_is_fresh(path, now=current_time, max_age_seconds=max_age_seconds)


__all__ = [
    "CacheWarmerConfig",
    "cache_warmer_enabled",
    "configured_path",
    "ensure_volume_cache_fresh",
    "load_cache_warmer_config",
    "run_cache_warmer_once",
    "subprocess_env",
    "terminate_process",
]

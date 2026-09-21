"""Cross-worker-fenced retention maintenance for durable bot state."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from src.services.config import runtime_setting
from src.state.job_repository import JobClaim

_JOB_NAME = "state-maintenance"
_SECONDS_PER_DAY = 86_400
logger = logging.getLogger("taffy.services.state_maintenance")


class MaintenanceJobs(Protocol):
    """Job-lease operations needed by state retention."""

    async def claim(
        self,
        job_name: str,
        run_key: str,
        *,
        lease_seconds: int,
        now: int | None = None,
    ) -> JobClaim | None: ...

    async def mark_completed(
        self,
        claim: JobClaim,
        *,
        completed_at: int | None = None,
    ) -> bool: ...

    async def record_failure(
        self,
        claim: JobClaim,
        *,
        error: str,
        retry_at: int,
        now: int | None = None,
    ) -> bool: ...

    async def cleanup_completed(self, *, before: int, limit: int = 1_000) -> int: ...

    async def cleanup_abandoned(self, *, before: int, limit: int = 1_000) -> int: ...


class MaintenanceBroadcasts(Protocol):
    """Broadcast-history operations needed by state retention."""

    async def cleanup_history(
        self,
        *,
        delivered_before: int,
        limit: int = 1_000,
    ) -> int: ...

    async def cleanup_failed(
        self,
        *,
        failed_before: int,
        limit: int = 1_000,
    ) -> int: ...


class MaintenanceAlerts(Protocol):
    """Alert dead-letter operations needed by state retention."""

    async def cleanup_failed(
        self,
        *,
        failed_before: datetime | int,
        limit: int = 1_000,
    ) -> int: ...


class MaintenanceSessions(Protocol):
    """Inactive-session operations needed by state retention."""

    async def cleanup_inactive(self, *, before: int, limit: int = 1_000) -> int: ...


class MaintenanceSessionSummaries(Protocol):
    """Orphaned chat-summary purge needed after session retention."""

    async def purge_orphans(self, *, limit: int = 500) -> int: ...


class MaintenanceInventory(Protocol):
    """Inventory-monitor event and fingerprint retention operations."""

    async def cleanup_events(self, *, before: int, limit: int = 1_000) -> int: ...

    async def cleanup_item_history(self, *, before: int, limit: int = 1_000) -> int: ...


class MaintenanceUsageLog(Protocol):
    """Usage-audit retention operations."""

    async def cleanup(self, cutoff_created_at: int) -> int: ...


@dataclass(frozen=True, slots=True)
class MaintenanceCounts:
    """Counts removed by one successful bounded retention pass."""

    deliveries: int
    broadcast_failures: int
    alert_failures: int
    session_messages: int
    inventory_events: int
    inventory_item_history: int
    completed_jobs: int
    abandoned_jobs: int
    usage_log: int = 0
    session_summaries: int = 0


def maintenance_run_key(timestamp: int, interval_seconds: int) -> str:
    """Return the durable logical slot shared by every bot worker."""

    interval = max(60, int(interval_seconds))
    return f"{_JOB_NAME}:{interval}:{int(timestamp) // interval}"


class StateMaintenanceService:
    """Claim and execute one bounded state-retention slot.

    Repository protocols keep persistence policy outside the NoneBot plugin,
    while the durable job claim guarantees that only one worker performs a
    logical maintenance interval.
    """

    def __init__(
        self,
        *,
        jobs: MaintenanceJobs,
        broadcasts: MaintenanceBroadcasts,
        alerts: MaintenanceAlerts,
        sessions: MaintenanceSessions,
        inventory: MaintenanceInventory,
        usage_log: MaintenanceUsageLog | None = None,
        session_summaries: MaintenanceSessionSummaries | None = None,
        setting_reader: Callable[[str], str | None] = runtime_setting,
        log: logging.Logger = logger,
    ) -> None:
        self._jobs = jobs
        self._broadcasts = broadcasts
        self._alerts = alerts
        self._sessions = sessions
        self._inventory = inventory
        self._usage_log = usage_log
        self._session_summaries = session_summaries
        self._setting_reader = setting_reader
        self._logger = log

    def _positive_int_setting(self, name: str, default: int, *, maximum: int) -> int:
        raw = self._setting_reader(name)
        if raw is None:
            return default
        try:
            parsed = float(raw)
        except ValueError:
            self._logger.warning("Setting %s is not numeric; using %s", name, default)
            return default
        if not math.isfinite(parsed) or parsed < 1.0:
            self._logger.warning("Setting %s is invalid; using %s", name, default)
            return default
        return min(maximum, max(1, math.ceil(parsed)))

    def interval_seconds(self) -> int:
        """Return the live, layered maintenance interval."""

        return max(
            60,
            self._positive_int_setting(
                "TAFFY_MAINTENANCE_INTERVAL_SECONDS",
                3_600,
                maximum=_SECONDS_PER_DAY,
            ),
        )

    async def _cleanup(self, *, timestamp: int) -> MaintenanceCounts:
        batch_size = self._positive_int_setting(
            "TAFFY_RETENTION_BATCH_SIZE",
            1_000,
            maximum=10_000,
        )
        delivered_days = self._positive_int_setting(
            "TAFFY_DELIVERY_RETENTION_DAYS",
            30,
            maximum=3_650,
        )
        catchup_minutes = self._positive_int_setting(
            "TAFFY_BROADCAST_CATCHUP_MINUTES",
            180,
            maximum=1_440,
        )
        # Delivered rows are also the durable de-duplication tombstones for a
        # schedule slot.  Never remove them while that slot can still be
        # recreated by the catch-up window; keep an additional full-day
        # safety margin for wall-clock boundaries and maintenance jitter.
        minimum_delivery_days = math.ceil(catchup_minutes / (24 * 60)) + 1
        if delivered_days < minimum_delivery_days:
            self._logger.warning(
                "TAFFY_DELIVERY_RETENTION_DAYS=%s overlaps the catch-up window; using %s",
                delivered_days,
                minimum_delivery_days,
            )
            delivered_days = minimum_delivery_days
        failed_days = self._positive_int_setting(
            "TAFFY_FAILED_RETENTION_DAYS",
            90,
            maximum=3_650,
        )
        job_days = self._positive_int_setting(
            "TAFFY_JOB_RETENTION_DAYS",
            30,
            maximum=3_650,
        )
        session_days = self._positive_int_setting(
            "TAFFY_SESSION_RETENTION_DAYS",
            180,
            maximum=3_650,
        )
        inventory_event_days = self._positive_int_setting(
            "TAFFY_INVENTORY_EVENT_RETENTION_DAYS",
            90,
            maximum=3_650,
        )
        inventory_history_days = self._positive_int_setting(
            "TAFFY_INVENTORY_HISTORY_RETENTION_DAYS",
            180,
            maximum=3_650,
        )
        usage_days = self._positive_int_setting(
            "TAFFY_USAGE_RETENTION_DAYS",
            30,
            maximum=3_650,
        )
        failed_cutoff = max(0, timestamp - failed_days * _SECONDS_PER_DAY)
        job_cutoff = max(0, timestamp - job_days * _SECONDS_PER_DAY)
        usage_cutoff = max(0, timestamp - usage_days * _SECONDS_PER_DAY)
        return MaintenanceCounts(
            deliveries=await self._broadcasts.cleanup_history(
                delivered_before=max(0, timestamp - delivered_days * _SECONDS_PER_DAY),
                limit=batch_size,
            ),
            broadcast_failures=await self._broadcasts.cleanup_failed(
                failed_before=failed_cutoff,
                limit=batch_size,
            ),
            alert_failures=await self._alerts.cleanup_failed(
                failed_before=failed_cutoff,
                limit=batch_size,
            ),
            session_messages=await self._sessions.cleanup_inactive(
                before=max(0, timestamp - session_days * _SECONDS_PER_DAY),
                limit=batch_size,
            ),
            # Summaries whose session just vanished become orphans; sweep a
            # bounded batch right after the session retention so privacy
            # cleanup is immediate, not eventual.
            session_summaries=(
                await self._session_summaries.purge_orphans(limit=batch_size)
                if self._session_summaries is not None
                else 0
            ),
            inventory_events=await self._inventory.cleanup_events(
                before=max(0, timestamp - inventory_event_days * _SECONDS_PER_DAY),
                limit=batch_size,
            ),
            inventory_item_history=await self._inventory.cleanup_item_history(
                before=max(0, timestamp - inventory_history_days * _SECONDS_PER_DAY),
                limit=batch_size,
            ),
            completed_jobs=await self._jobs.cleanup_completed(
                before=job_cutoff,
                limit=batch_size,
            ),
            abandoned_jobs=await self._jobs.cleanup_abandoned(
                before=job_cutoff,
                limit=batch_size,
            ),
            usage_log=(
                await self._usage_log.cleanup(cutoff_created_at=usage_cutoff)
                if self._usage_log is not None
                else 0
            ),
        )

    async def run_once(self, now: datetime | None = None) -> bool:
        """Run one leased retention pass, returning whether it completed."""

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        timestamp = int(current.timestamp())
        interval = self.interval_seconds()
        claim = await self._jobs.claim(
            _JOB_NAME,
            maintenance_run_key(timestamp, interval),
            lease_seconds=300,
            now=timestamp,
        )
        if claim is None:
            return False

        try:
            counts = await self._cleanup(timestamp=timestamp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.exception("State maintenance failed")
            try:
                await self._jobs.record_failure(
                    claim,
                    error=type(exc).__name__,
                    retry_at=timestamp + min(300, interval),
                    now=timestamp,
                )
            except Exception:
                self._logger.exception("Unable to persist state-maintenance failure")
            return False

        if not await self._jobs.mark_completed(claim):
            self._logger.error("State maintenance completed after its job lease was lost")
            return False
        self._logger.info(
            "State retention removed deliveries=%s broadcast_failures=%s "
            "alert_failures=%s session_messages=%s inventory_events=%s "
            "inventory_item_history=%s completed_jobs=%s abandoned_jobs=%s",
            counts.deliveries,
            counts.broadcast_failures,
            counts.alert_failures,
            counts.session_messages,
            counts.inventory_events,
            counts.inventory_item_history,
            counts.completed_jobs,
            counts.abandoned_jobs,
        )
        return True


__all__ = [
    "MaintenanceAlerts",
    "MaintenanceBroadcasts",
    "MaintenanceCounts",
    "MaintenanceJobs",
    "MaintenanceInventory",
    "MaintenanceSessions",
    "StateMaintenanceService",
    "maintenance_run_key",
]

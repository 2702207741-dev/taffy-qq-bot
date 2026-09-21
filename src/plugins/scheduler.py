"""Durable scheduling for broadcasts, cache warming, and price alerts."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule

from src.services import cache_warmer as cache_warmer_service
from src.services import learning as learning_service
from src.services.config import runtime_setting
from src.services.index_history import index_history_store
from src.services.market_catalog_store import market_catalog_store
from src.services.market_history import market_history
from src.services.market_validation import (
    BROAD_MAX_AGE_SECONDS,
    MarketDataError,
    format_data_time,
    validate_broad_market_response,
)
from src.services.state_maintenance import StateMaintenanceService, maintenance_run_key
from src.services.telemetry import telemetry
from src.services.tz import configured_timezone as _shared_configured_timezone
from src.services.tz import to_market_tz
from src.state.broadcast_repository import BroadcastDelivery
from src.state.runtime import alerts as alert_repository
from src.state.runtime import (
    backups,
    broadcasts,
    config_snapshots,
    inventory_monitors,
    jobs,
    schedules,
    session_summaries,
    sessions,
    usage_log,
)
from src.state.schedule_repository import ScheduleConfig, ScheduleVersionConflictError

_configured_path = cache_warmer_service.configured_path
_cache_warmer_enabled = cache_warmer_service.cache_warmer_enabled
_run_warm_cache_once = cache_warmer_service.run_cache_warmer_once
_subprocess_env = cache_warmer_service.subprocess_env
_terminate_process = cache_warmer_service.terminate_process

_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_MARKET_SCHEDULE_PREFIX = "market-index:"
_WARM_JOB_NAME = "warm-cache"


class MarketPayloadError(RuntimeError):
    """The market service did not provide a usable broadcast snapshot."""


class BotUnavailableError(RuntimeError):
    """No OneBot connection is available for a claimed delivery."""


class LeaseLostError(RuntimeError):
    """The worker lost repository ownership before it could send."""


async def _run_step(action: Callable[[], object], message: str) -> None:
    """Run one loop step, letting cancellation through and logging the rest."""

    try:
        result = action()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(message)


def _is_valid_time(value: object) -> bool:
    """Return whether *value* is an exact 24-hour ``HH:MM`` string."""

    return isinstance(value, str) and _TIME_RE.fullmatch(value) is not None


async def load_cfg() -> ScheduleConfig:
    """Read one consistent, hot-reloadable scheduler configuration snapshot."""

    return await schedules.get()


async def save_cfg(
    config: Mapping[str, object] | ScheduleConfig,
    *,
    expected_version: int,
) -> ScheduleConfig:
    """Optimistically replace the complete scheduler configuration."""

    return await schedules.replace(config, expected_version=expected_version)


def _seconds_from_env(name: str, default: float, *, minimum: float = 0.05) -> float:
    raw = runtime_setting(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Environment variable {}={!r} is not numeric; using {}", name, raw, default)
        return default
    if not math.isfinite(value) or value < minimum:
        logger.warning("Environment variable {}={!r} is invalid; using {}", name, raw, default)
        return default
    return value


def _positive_int_from_env(name: str, default: int) -> int:
    return max(1, math.ceil(_seconds_from_env(name, float(default), minimum=1.0)))


def _broadcast_send_concurrency() -> int:
    return min(20, _positive_int_from_env("TAFFY_BROADCAST_SEND_CONCURRENCY", 5))


def _broadcast_send_timeout() -> float:
    return min(
        120.0,
        _seconds_from_env("TAFFY_BROADCAST_SEND_TIMEOUT_SECONDS", 20.0),
    )


def _broadcast_lease_seconds(
    batch_size: int,
    *,
    serial_waves: int | None = None,
) -> int:
    concurrent_waves = math.ceil(max(1, int(batch_size)) / _broadcast_send_concurrency())
    # Deliveries are currently grouped by schedule key and each group is
    # awaited before the next one starts.  Callers that know this grouping
    # must provide the resulting serial wave count; using only total rows can
    # otherwise let a lease expire while a catch-up batch is still sending.
    waves = max(concurrent_waves, max(1, int(serial_waves or 0)))
    # Each wave includes one bounded QQ request and one FULL-durability SQLite
    # state commit.  The final margin covers payload acquisition and lock jitter.
    minimum_safe_lease = math.ceil((_broadcast_send_timeout() + 2.0) * waves + 90.0)
    return max(
        minimum_safe_lease,
        _positive_int_from_env("TAFFY_BROADCAST_LEASE_SECONDS", 180),
    )


def _configured_timezone(timezone_name: str) -> tzinfo:
    """Load an IANA zone, with portable fallbacks for repository defaults."""

    return _shared_configured_timezone(timezone_name)


def _in_configured_timezone(now: datetime | None, timezone_name: str) -> datetime:
    return to_market_tz(now, timezone_name)


def _market_schedule_key(now: datetime, timezone_name: str) -> str:
    """Return a stable UTC key for the configured local minute containing *now*."""

    current = _in_configured_timezone(now, timezone_name).replace(second=0, microsecond=0)
    utc_slot = current.astimezone(timezone.utc)
    return f"{_MARKET_SCHEDULE_PREFIX}{utc_slot.strftime('%Y%m%dT%H%MZ')}"


def _scheduled_slots(current: datetime, config: ScheduleConfig) -> list[datetime]:
    """Enumerate a bounded replay window so restarts cannot lose a minute slot."""

    if not config.times:
        return []
    catchup_minutes = min(
        1_440,
        _positive_int_from_env("TAFFY_BROADCAST_CATCHUP_MINUTES", 180),
    )
    max_slots = min(
        100,
        _positive_int_from_env("TAFFY_BROADCAST_CATCHUP_MAX_SLOTS", 10),
    )
    end = int(current.timestamp()) // 60 * 60
    # Do not backfill slots from before the latest configuration change.  This
    # avoids sending a paused slot after re-enable or retroactively applying a
    # newly added time/group to an old schedule.
    start = max(0, end - catchup_minutes * 60, int(config.updated_at))
    start = min(end, start // 60 * 60)
    accepted_times = set(config.times)
    timezone_value = _configured_timezone(config.timezone)
    slots: list[datetime] = []
    for timestamp in range(start, end + 1, 60):
        slot = datetime.fromtimestamp(timestamp, timezone_value)
        if slot.strftime("%H:%M") in accepted_times:
            slots.append(slot)
    if len(slots) > max_slots:
        logger.warning(
            "Broadcast catch-up truncated from {} to {} newest slots",
            len(slots),
            max_slots,
        )
        return slots[-max_slots:]
    return slots


def _slot_label(schedule_key: str, timezone_name: str) -> str:
    try:
        value = schedule_key.removeprefix(_MARKET_SCHEDULE_PREFIX)
        utc_slot = datetime.strptime(value, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    except ValueError:
        logger.warning("Unable to parse persisted broadcast schedule key {!r}", schedule_key)
        utc_slot = datetime.now(timezone.utc)
    return utc_slot.astimezone(_configured_timezone(timezone_name)).strftime("%m-%d %H:%M")


def _warm_run_key(timestamp: int, interval_seconds: int) -> str:
    interval = max(1, int(interval_seconds))
    return f"warm-cache:{interval}:{int(timestamp) // interval}"


def _warm_job_lease_seconds() -> int:
    timeout = _seconds_from_env("TAFFY_SCRAPE_TIMEOUT_SECONDS", 45.0)
    grace = _seconds_from_env("TAFFY_SCRAPE_TERMINATE_GRACE_SECONDS", 5.0)
    return max(1, math.ceil(timeout + (2 * grace) + 30.0))


async def _warm_cache_tick(now: datetime | None = None) -> bool:
    """Claim and execute at most one durable cache-warming interval slot."""

    from src.services.resource_governor import resource_governor

    if resource_governor.game_running:
        return False
    if not _cache_warmer_enabled():
        return False
    config = await load_cfg()
    if not config.warm_cache_enabled:
        return False

    current = _in_configured_timezone(now, config.timezone)
    timestamp = int(current.timestamp())
    interval = max(
        1,
        math.ceil(_seconds_from_env("TAFFY_WARM_CACHE_INTERVAL_SECONDS", 300.0)),
    )
    claim = await jobs.claim(
        _WARM_JOB_NAME,
        _warm_run_key(timestamp, interval),
        lease_seconds=_warm_job_lease_seconds(),
        now=timestamp,
    )
    if claim is None:
        return False

    try:
        succeeded = await _run_warm_cache_once()
    except asyncio.CancelledError:
        try:
            await jobs.record_failure(
                claim,
                error="CancelledError",
                retry_at=timestamp + interval,
                now=timestamp,
            )
        except Exception:
            logger.exception("Unable to release a cancelled cache-warmer job claim")
        raise
    except Exception as exc:
        logger.exception("Unexpected cache warmer failure")
        await jobs.record_failure(
            claim,
            error=type(exc).__name__,
            retry_at=timestamp + interval,
            now=timestamp,
        )
        return False

    if succeeded:
        if not await jobs.mark_completed(claim):
            logger.error("Cache warmer completed after its job lease was lost")
            return False
        return True

    await jobs.record_failure(
        claim,
        error="WarmCacheFailed",
        retry_at=timestamp + interval,
        now=timestamp,
    )
    return False


async def _warm_volume_cache() -> None:
    """Refresh the SteamDT volume cache without multi-worker process storms."""

    while True:
        try:
            await _warm_cache_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in cache warmer loop")
        await asyncio.sleep(_seconds_from_env("TAFFY_WARM_CACHE_INTERVAL_SECONDS", 300.0))


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    group_id: str
    success: bool
    error: str | None = None


_bot_instance: Bot | None = None
_scheduler_tasks: dict[str, asyncio.Task[None]] = {}
#: Application-level loops (operations/maintenance) run independently of the
#: QQ connection; disconnects must not cancel them.
_application_tasks: dict[str, asyncio.Task[None]] = {}
#: Small lifecycle lock: connection-state update, connection-task replacement
#: and cleanup must not interleave (no generic task framework).
_lifecycle_lock = asyncio.Lock()


def connected_bot() -> Bot | None:
    """Publicly expose the connected bot so other plugins avoid private state."""

    return _bot_instance


async def _fetch_market_index() -> Mapping[str, Any]:
    """Import the market client lazily so scheduler lifecycle stays independent."""

    from .chat_utils import st_get

    return await st_get("/open/cs2/broad/v1/index")


def _render_market_payload(
    response: Mapping[str, Any],
    *,
    timestamp: str,
    now: float | None = None,
) -> str:
    try:
        snapshot = validate_broad_market_response(response, now=now)
    except MarketDataError as exc:
        raise MarketPayloadError(exc.code) from exc

    direction = "上" if snapshot.change_percent >= 0 else "下"
    return (
        f"[定时大盘 {timestamp}]\n"
        f"数据时间: {format_data_time(snapshot.timestamp)}\n"
        f"点位 {snapshot.index:.2f}  涨跌 {direction} {snapshot.change_percent:+.2f}%\n"
        "发送「大盘」查看详情"
    )


async def _build_market_payload(
    schedule_key: str,
    timezone_name: str,
    *,
    now: float | None = None,
) -> str:
    try:
        response = await _fetch_market_index()
    except Exception as exc:
        raise MarketPayloadError("market request failed") from exc
    if not isinstance(response, Mapping):
        raise MarketPayloadError("market API returned malformed data")
    return _render_market_payload(
        response,
        timestamp=_slot_label(schedule_key, timezone_name),
        now=now,
    )


def _message_id(response: object) -> str | int | None:
    if not isinstance(response, Mapping):
        return None
    value = response.get("message_id")
    return value if isinstance(value, str | int) and not isinstance(value, bool) else None


async def _record_broadcast_failure(
    delivery: BroadcastDelivery,
    lease_token: str,
    error_type: str,
    timestamp: int,
) -> None:
    try:
        applied = await broadcasts.record_failure(
            delivery.schedule_key,
            delivery.group_id,
            lease_token=lease_token,
            error=error_type,
            failed_at=timestamp,
        )
    except Exception as exc:
        logger.exception(
            "Unable to persist broadcast failure key={} group={} error={}",
            delivery.schedule_key,
            delivery.group_id,
            type(exc).__name__,
        )
        return
    if not applied:
        logger.warning(
            "Broadcast lease was lost before failure update key={} group={}",
            delivery.schedule_key,
            delivery.group_id,
        )


async def _payload_for_claim(
    schedule_key: str,
    deliveries: Sequence[BroadcastDelivery],
    *,
    lease_token: str,
    timezone_name: str,
    now: int,
) -> str:
    payload = next((row.payload_text for row in deliveries if row.payload_text), None)
    if payload is None:
        payload = await broadcasts.get_payload(schedule_key)
    if (
        payload is not None
        and now - min(row.created_at for row in deliveries) > BROAD_MAX_AGE_SECONDS
    ):
        raise MarketPayloadError("frozen market payload is stale")
    if payload is None:
        payload = await _build_market_payload(schedule_key, timezone_name, now=now)

    # set_payload atomically preserves an earlier worker's snapshot and copies
    # it onto every row currently owned by this worker before any QQ send.
    frozen = await broadcasts.set_payload(
        schedule_key,
        lease_token=lease_token,
        payload_text=payload,
    )
    if frozen is None:
        raise LeaseLostError("broadcast claim no longer belongs to this worker")
    return frozen


async def _deliver_claims(
    claimed: Sequence[BroadcastDelivery],
    *,
    lease_token: str,
    timezone_name: str,
    timestamp: int,
) -> list[BroadcastResult]:
    """Deliver claimed outbox rows, committing success per group."""

    send_limit = asyncio.Semaphore(_broadcast_send_concurrency())
    send_timeout = _broadcast_send_timeout()
    by_schedule: dict[str, list[BroadcastDelivery]] = {}
    for delivery in claimed:
        by_schedule.setdefault(delivery.schedule_key, []).append(delivery)

    results: list[BroadcastResult] = []
    for schedule_key, deliveries in by_schedule.items():
        try:
            bot = _bot_instance
            if bot is None:
                raise BotUnavailableError("bot is not connected")
            payload = await _payload_for_claim(
                schedule_key,
                deliveries,
                lease_token=lease_token,
                timezone_name=timezone_name,
                now=timestamp,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            logger.exception("Unable to prepare scheduled broadcast key={}", schedule_key)
            try:
                failed_count = await broadcasts.record_claim_failures(
                    schedule_key,
                    lease_token=lease_token,
                    error=error_type,
                    failed_at=timestamp,
                )
            except Exception as state_error:
                logger.exception(
                    "Unable to persist failed broadcast claim key={} error={}",
                    schedule_key,
                    type(state_error).__name__,
                )
                failed_count = 0
            if failed_count != len(deliveries):
                logger.warning(
                    "Broadcast failure claim was partially fenced key={} expected={} applied={}",
                    schedule_key,
                    len(deliveries),
                    failed_count,
                )
            results.extend(
                BroadcastResult(str(delivery.group_id), False, error_type)
                for delivery in deliveries
            )
            continue

        async def deliver_one(
            delivery: BroadcastDelivery,
            *,
            active_bot: Bot = bot,
            frozen_payload: str = payload,
            active_schedule_key: str = schedule_key,
        ) -> BroadcastResult:
            async with send_limit:
                # Short-circuit when the connection that claimed this
                # delivery is gone: do not send through a stale bot and do
                # not mark the one-shot delivery as consumed — it stays
                # claimable for a later connection.
                if _bot_instance is not active_bot:
                    await _record_broadcast_failure(
                        delivery, lease_token, "BotUnavailableError", timestamp
                    )
                    return BroadcastResult(str(delivery.group_id), False, "BotUnavailableError")
                try:
                    async with asyncio.timeout(send_timeout):
                        response = await active_bot.send_group_msg(
                            group_id=delivery.group_id,
                            message=Message(MessageSegment.text(frozen_payload)),
                        )
                except Exception as exc:
                    error_type = type(exc).__name__
                    logger.exception("Broadcast delivery to group {} failed", delivery.group_id)
                    await _record_broadcast_failure(delivery, lease_token, error_type, timestamp)
                    return BroadcastResult(str(delivery.group_id), False, error_type)

                try:
                    committed = await broadcasts.mark_delivered(
                        active_schedule_key,
                        delivery.group_id,
                        lease_token=lease_token,
                        message_id=_message_id(response),
                    )
                except Exception as exc:
                    logger.critical(
                        "Broadcast was sent but delivery state failed key={} group={} error={}",
                        active_schedule_key,
                        delivery.group_id,
                        type(exc).__name__,
                    )
                    return BroadcastResult(str(delivery.group_id), False, type(exc).__name__)
                if not committed:
                    logger.critical(
                        "Broadcast was sent after its lease was lost key={} group={}",
                        active_schedule_key,
                        delivery.group_id,
                    )
                    return BroadcastResult(str(delivery.group_id), False, "LeaseLostError")
                return BroadcastResult(str(delivery.group_id), True)

        results.extend(await asyncio.gather(*(deliver_one(item) for item in deliveries)))
    return results


async def _broadcast_tick(now: datetime | None = None) -> list[BroadcastResult]:
    """Create catch-up slots first, then drain a bounded retry batch."""

    config = await load_cfg()
    current = _in_configured_timezone(now, config.timezone)
    timestamp = int(current.timestamp())
    if not config.enabled:
        # This is also the emergency stop for already-persisted retries.  Due
        # rows remain durable and resume only after broadcasts are re-enabled.
        return []

    slots = _scheduled_slots(current, config)
    results: list[BroadcastResult] = []
    if slots and config.groups:
        slot_token = f"slot:{uuid.uuid4().hex}"
        slot_waves = len(slots) * math.ceil(len(config.groups) / _broadcast_send_concurrency())
        slot_lease_seconds = _broadcast_lease_seconds(
            len(slots) * len(config.groups),
            serial_waves=slot_waves,
        )
        claimed_slots: list[BroadcastDelivery] = []
        for slot in slots:
            claimed_slots.extend(
                await broadcasts.claim(
                    _market_schedule_key(slot, config.timezone),
                    config.groups,
                    lease_token=slot_token,
                    lease_seconds=slot_lease_seconds,
                    now=timestamp,
                )
            )
        results.extend(
            await _deliver_claims(
                claimed_slots,
                lease_token=slot_token,
                timezone_name=config.timezone,
                timestamp=timestamp,
            )
        )

    retry_batch_size = min(
        1_000,
        _positive_int_from_env("TAFFY_BROADCAST_RETRY_BATCH_SIZE", 100),
    )
    retry_timestamp = timestamp if now is not None else int(time.time())
    retry_token = f"retry:{uuid.uuid4().hex}"

    # Historical failures and expired worker leases are recovered only while
    # broadcasting is explicitly enabled. Newly failed slot rows have a future
    # next_retry_at and therefore cannot be selected again in this same tick.
    due = await broadcasts.claim_due(
        lease_token=retry_token,
        # Before claiming we do not know how many schedule keys the retry
        # batch contains.  Treat every row as its own serial group so the
        # lease is safe even for the worst possible distribution.
        lease_seconds=_broadcast_lease_seconds(
            retry_batch_size,
            serial_waves=retry_batch_size,
        ),
        limit=retry_batch_size,
        now=retry_timestamp,
    )
    results.extend(
        await _deliver_claims(
            due,
            lease_token=retry_token,
            timezone_name=config.timezone,
            timestamp=retry_timestamp,
        )
    )
    return results


async def _do_broadcast(groups: Sequence[int | str] | None = None) -> list[BroadcastResult]:
    """Run a manual, non-scheduled market broadcast for the SUPERUSER command."""

    config = await load_cfg()
    targets: Sequence[int | str] = config.groups if groups is None else groups
    canonical_targets = [str(group_id).strip() for group_id in targets]
    if not canonical_targets:
        logger.warning("Broadcast skipped because no target groups are configured")
        return []
    if _bot_instance is None:
        return [
            BroadcastResult(group_id, False, "BotUnavailableError")
            for group_id in canonical_targets
        ]

    current = datetime.now(_configured_timezone(config.timezone))
    try:
        response = await _fetch_market_index()
        if not isinstance(response, Mapping):
            raise MarketPayloadError("market API returned malformed data")
        payload = _render_market_payload(
            response,
            timestamp=current.strftime("%m-%d %H:%M"),
            now=current.timestamp(),
        )
    except Exception as exc:
        logger.exception("Unable to prepare manual market broadcast")
        error_type = type(exc).__name__
        return [BroadcastResult(group_id, False, error_type) for group_id in canonical_targets]

    results: list[BroadcastResult] = []
    for group_id in canonical_targets:
        if not group_id.isascii() or not group_id.isdecimal() or int(group_id) <= 0:
            results.append(BroadcastResult(group_id, False, "ValueError"))
            continue
        try:
            async with asyncio.timeout(_broadcast_send_timeout()):
                await _bot_instance.send_group_msg(
                    group_id=int(group_id),
                    message=Message(MessageSegment.text(payload)),
                )
        except Exception as exc:
            logger.exception("Manual broadcast delivery to group {} failed", group_id)
            results.append(BroadcastResult(group_id, False, type(exc).__name__))
        else:
            results.append(BroadcastResult(group_id, True))
    return results


_maintenance_run_key = maintenance_run_key


def _state_maintenance_service() -> StateMaintenanceService:
    """Compose maintenance from live repositories for test and worker isolation."""

    return StateMaintenanceService(
        jobs=jobs,
        broadcasts=broadcasts,
        alerts=alert_repository,
        sessions=sessions,
        inventory=inventory_monitors,
        usage_log=usage_log,
        session_summaries=session_summaries,
    )


async def _maintenance_tick(now: datetime | None = None) -> bool:
    """Compatibility wrapper for one cross-worker-fenced retention pass."""

    return await _state_maintenance_service().run_once(now)


async def _maintenance_loop() -> None:
    while True:
        await _run_step(_maintenance_tick, "Unexpected error in state-maintenance loop")
        await asyncio.sleep(_state_maintenance_service().interval_seconds())


async def _learning_tick(*, now: int | None = None) -> None:
    """Run learning slots when their cadence is due (idempotent job leases).

    Called from the operations loop every ~65 seconds.  Each slot claims a
    durable job keyed by its time bucket, so across workers and restarts
    exactly one process performs an observation/reflection/decay pass per
    bucket and duplicate attempts are cheap no-ops.
    """

    if not learning_service.learning_enabled():
        return
    now = int(time.time() if now is None else now)
    config = await load_cfg()
    local_now = _in_configured_timezone(
        datetime.fromtimestamp(now, tz=timezone.utc), config.timezone
    )
    day_key = local_now.strftime("%Y%m%d")
    observe_interval = _positive_int_from_env("TAFFY_LEARNING_ANALYZE_INTERVAL_SECONDS", 21600)
    reflection_hour = _positive_int_from_env("TAFFY_REFLECTION_HOUR", 3)

    observe_claim = await jobs.claim(
        "learning-observation",
        f"learning-observation-{now // max(observe_interval, 3600)}",
        lease_seconds=max(observe_interval, 3600),
        now=now,
    )
    if observe_claim is not None:
        try:
            created = await learning_service.observe_once(now=now)
            if created:
                logger.info("Learning observation created insights={}", created)
            await jobs.mark_completed(observe_claim)
        except Exception as exc:
            logger.exception("Learning observation failed")
            await jobs.record_failure(
                observe_claim,
                error=type(exc).__name__,
                retry_at=now + observe_interval,
                now=now,
            )

    local_hour = local_now.hour
    if local_hour == reflection_hour:
        reflection_claim = await jobs.claim(
            "learning-reflection",
            f"learning-reflection-{day_key}",
            lease_seconds=7200,
            now=now,
        )
        if reflection_claim is not None:
            try:
                created = await learning_service.reflect_once(now=now)
                if created:
                    logger.info("Learning reflection created insights={}", created)
                await jobs.mark_completed(reflection_claim)
            except Exception as exc:
                logger.exception("Learning reflection failed")
                await jobs.record_failure(
                    reflection_claim,
                    error=type(exc).__name__,
                    retry_at=now + 86400,
                    now=now,
                )

    decay_claim = await jobs.claim(
        "learning-decay",
        f"learning-decay-{day_key}",
        lease_seconds=7200,
        now=now,
    )
    if decay_claim is not None:
        try:
            archived = await learning_service.decay_once(now=now)
            if archived:
                logger.info("Learning decay archived insights={}", archived)
            await jobs.mark_completed(decay_claim)
        except Exception as exc:
            logger.exception("Learning decay failed")
            await jobs.record_failure(
                decay_claim,
                error=type(exc).__name__,
                retry_at=now + 86400,
                now=now,
            )

    reinforce_claim = await jobs.claim(
        "learning-reinforce",
        f"learning-reinforce-{day_key}",
        lease_seconds=7200,
        now=now,
    )
    if reinforce_claim is not None:
        try:
            bumped = await learning_service.reinforce_once(now=now)
            if bumped:
                logger.info("Learning reinforcement bumped insights={}", bumped)
            await jobs.mark_completed(reinforce_claim)
        except Exception as exc:
            logger.exception("Learning reinforcement failed")
            await jobs.record_failure(
                reinforce_claim,
                error=type(exc).__name__,
                retry_at=now + 86400,
                now=now,
            )

    consolidation_claim = await jobs.claim(
        "learning-consolidation",
        f"learning-consolidation-{day_key}",
        lease_seconds=7200,
        now=now,
    )
    if consolidation_claim is not None:
        try:
            stats = await learning_service.consolidate_once(now=now)
            if any(stats.values()):
                logger.info("Memory consolidation completed merge={}", stats)
            await jobs.mark_completed(consolidation_claim)
        except Exception as exc:
            logger.exception("Memory consolidation failed")
            await jobs.record_failure(
                consolidation_claim,
                error=type(exc).__name__,
                retry_at=now + 86400,
                now=now,
            )


async def _proactive_tick() -> None:
    """Run opt-in proactive pushes when their daily slot is due.

    The morning digest fires only on weekdays inside the configured local
    hour; the dip scan runs once per local day.  Both claim durable
    day-bucketed job leases, so workers and restarts cannot double-send.
    """

    from src.services import proactive as proactive_service

    if not proactive_service.proactive_enabled():
        return
    bot = _bot_instance
    if bot is None:
        return
    config = await load_cfg()
    current = _in_configured_timezone(None, config.timezone)
    timestamp = int(current.timestamp())
    day_key = current.strftime("%Y%m%d")

    if current.weekday() < 5 and current.hour == proactive_service.proactive_morning_hour():
        morning_claim = await jobs.claim(
            "proactive-morning",
            f"proactive-morning-{day_key}",
            lease_seconds=7200,
            now=timestamp,
        )
        if morning_claim is not None:
            try:
                sent = await proactive_service.morning_push_once(bot, now=timestamp)
                if sent:
                    logger.info("Proactive morning digest sent={}", sent)
                await jobs.mark_completed(morning_claim)
            except Exception as exc:
                logger.exception("Proactive morning digest failed")
                await jobs.record_failure(
                    morning_claim,
                    error=type(exc).__name__,
                    retry_at=timestamp + 3600,
                    now=timestamp,
                )

    dip_claim = await jobs.claim(
        "proactive-dip",
        f"proactive-dip-{day_key}",
        lease_seconds=7200,
        now=timestamp,
    )
    if dip_claim is not None:
        try:
            sent = await proactive_service.dip_scan_once(bot, now=timestamp)
            if sent:
                logger.info("Proactive dip notices sent={}", sent)
            await jobs.mark_completed(dip_claim)
        except Exception as exc:
            logger.exception("Proactive dip scan failed")
            await jobs.record_failure(
                dip_claim,
                error=type(exc).__name__,
                retry_at=timestamp + 3600,
                now=timestamp,
            )


def _reddit_warmup_tick() -> None:
    from src.services.reddit_news import reddit_news_service

    reddit_news_service().schedule_sync()


async def _operations_loop() -> None:
    """Persist aggregates and run due heavy housekeeping cooperatively."""

    while True:
        await _run_step(telemetry.flush, "Telemetry flush failed in operations loop")
        await _run_step(
            market_catalog_store.schedule_refresh_if_due,
            "Market catalog refresh scheduling failed in operations loop",
        )
        await _run_step(
            market_catalog_store.schedule_sitemap_sync_if_due,
            "Market catalog sitemap sync scheduling failed in operations loop",
        )
        await _run_step(
            market_history.collect_once, "Market history collection failed in operations loop"
        )
        await _run_step(
            market_history.refresh_equipment_components,
            "Equipment index component refresh failed in operations loop",
        )
        await _run_step(
            _reddit_warmup_tick, "Reddit community news warmup failed in operations loop"
        )
        await _run_step(
            index_history_store.backfill_if_due, "Index history backfill failed in operations loop"
        )
        await _run_step(backups.run_if_due, "Backup run failed in operations loop")
        await _run_step(
            config_snapshots.run_if_due, "Config snapshot run failed in operations loop"
        )
        await _run_step(_learning_tick, "Learning tick failed in operations loop")
        await _run_step(_proactive_tick, "Proactive tick failed in operations loop")
        # The free batch endpoint permits one request per minute. A 65-second
        # cadence leaves clock-skew margin while using nearly all the quota.
        await asyncio.sleep(65.0)


async def _broadcast_loop() -> None:
    while True:
        await _run_step(_broadcast_tick, "Unexpected error in broadcast scheduler loop")
        await asyncio.sleep(_seconds_from_env("TAFFY_BROADCAST_POLL_SECONDS", 30.0))


async def _check_alerts_loop() -> None:
    """Check claimed alerts independently from the broadcast enable switch."""

    while True:
        try:
            await asyncio.sleep(_seconds_from_env("TAFFY_ALERT_CHECK_INTERVAL_SECONDS", 30.0))
            config = await load_cfg()
            if not config.alerts_enabled:
                continue
            bot = _bot_instance
            if bot is None:
                logger.warning("Alert check skipped because no bot is connected")
                continue
            from .alert import check_alerts

            await check_alerts(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in alert scheduler loop")


async def _check_inventory_monitors_loop() -> None:
    """Run due inventory checks; each account advances at most once per day."""

    while True:
        try:
            await asyncio.sleep(_seconds_from_env("TAFFY_INVENTORY_MONITOR_POLL_SECONDS", 300.0))
            bot = _bot_instance
            if bot is None:
                logger.warning("Inventory monitor check skipped because no bot is connected")
                continue
            from .inventory_monitor import check_inventory_monitors

            await check_inventory_monitors(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error in inventory monitor loop")


async def _cancel_scheduler_tasks(reason: str) -> None:
    """Cancel and await every QQ-connection-bound scheduler task.

    Application-level tasks (operations/maintenance) live in a separate
    registry and are deliberately untouched here: a disconnect must not
    stop them.  Cancellation is propagated and awaited; only unexpected
    (non-cancellation) outcomes are logged.
    """

    tasks = list(_scheduler_tasks.items())
    _scheduler_tasks.clear()
    if not tasks:
        return

    logger.info("Stopping {} scheduler tasks ({})", len(tasks), reason)
    for _, task in tasks:
        if not task.done():
            task.cancel()
    outcomes = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
    for (name, _), outcome in zip(tasks, outcomes, strict=True):
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.error("Scheduler task {} stopped with error: {!r}", name, outcome)


driver = get_driver()


async def _notify_version_update(bot: Bot) -> None:
    """Privately tell admins about a new CHANGELOG entry once per version."""

    try:
        root = Path(__file__).resolve().parents[2]
        changelog = root / "CHANGELOG.md"
        marker = root / "data" / ".last_notified_version"
        text = changelog.read_text(encoding="utf-8")
        title = next((line for line in text.splitlines() if line.startswith("## ")), "")
        if not title:
            return
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == title:
            return
        bullets = [
            line.strip()
            for line in text.splitlines()[text.index(title) + 1 :]
            if line.startswith("- ")
        ][:4]
        message = "🆕 塔菲更新了：\n" + title[3:] + "\n"
        if bullets:
            message += "\n".join("• " + bullet[2:][:60] for bullet in bullets)
        configured: object = getattr(get_driver().config, "superusers", set())
        if isinstance(configured, (set, frozenset, list, tuple)):
            admin_ids = [str(value) for value in configured]
        else:
            admin_ids = []
        for admin_id in admin_ids:
            await bot.send_private_msg(user_id=int(admin_id), message=message)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(title, encoding="utf-8")
    except Exception as exc:
        logger.warning("Version update notify failed error=%s", type(exc).__name__)


async def _version_notify_task(bot: Bot) -> None:
    """Run the one-shot version notification and drop it from the registry.

    Keeping it registered while it runs lets a disconnect cancel it with
    the other connection-bound tasks; once finished it removes itself so
    repeated connects never accumulate stale entries.
    """

    try:
        await _notify_version_update(bot)
    finally:
        _scheduler_tasks.pop("version-notify", None)


@driver.on_bot_connect
async def start_scheduler(bot: Bot) -> None:
    """Start the QQ-connection-bound scheduler tasks for ``bot``.

    Application-level tasks (operations/maintenance) are started separately
    by ``start_application_tasks`` after initialization and are never
    rebuilt or cancelled here.  A repeated connect event replaces the
    connection-bound task set exactly once per new connection.
    """

    global _bot_instance
    async with _lifecycle_lock:
        # A late disconnect from an older connection must not clear a newer
        # one: stop_scheduler ignores events whose bot is not current.
        if _bot_instance is bot and _scheduler_tasks:
            return
        await _cancel_scheduler_tasks("bot connected or reconnected")
        _bot_instance = bot
        tasks = {
            "broadcast": asyncio.create_task(_broadcast_loop(), name="taffy-broadcast"),
            "alerts": asyncio.create_task(_check_alerts_loop(), name="taffy-alerts"),
            "inventory-monitors": asyncio.create_task(
                _check_inventory_monitors_loop(),
                name="taffy-inventory-monitors",
            ),
        }
        if _cache_warmer_enabled():
            tasks["warm-cache"] = asyncio.create_task(
                _warm_volume_cache(),
                name="taffy-warm-cache",
            )
        # Version notification is a managed connection-bound task: it runs
        # once per connection and is cancelled with the other QQ tasks.
        tasks["version-notify"] = asyncio.create_task(
            _version_notify_task(bot), name="taffy-version-notify"
        )
        _scheduler_tasks.update(tasks)
        logger.info("Started {} connection-bound scheduler tasks", len(tasks))


async def _start_application_tasks() -> None:
    """Start the QQ-independent application loops exactly once."""

    async with _lifecycle_lock:
        if _application_tasks:
            return
        tasks = {
            "maintenance": asyncio.create_task(_maintenance_loop(), name="taffy-maintenance"),
            "operations": asyncio.create_task(_operations_loop(), name="taffy-operations"),
        }
        _application_tasks.update(tasks)
        logger.info("Started {} application-level scheduler tasks", len(tasks))


async def _cancel_application_tasks(reason: str) -> None:
    """Cancel and await every application-level task."""

    tasks = list(_application_tasks.items())
    _application_tasks.clear()
    if not tasks:
        return
    logger.info("Stopping {} application tasks ({})", len(tasks), reason)
    for _, task in tasks:
        if not task.done():
            task.cancel()
    outcomes = await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
    for (name, _), outcome in zip(tasks, outcomes, strict=True):
        if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
            logger.error("Application task {} stopped with error: {!r}", name, outcome)


async def start_application_tasks() -> None:
    """Public entry: start application-level loops after initialization.

    Safe to call once; repeated calls keep the original Task objects
    running (the QQ connection is not required and a later connect does
    not rebuild them).
    """

    await _start_application_tasks()


async def stop_application_tasks() -> None:
    """Public entry: stop application-level loops (application shutdown).

    Shares the lifecycle lock with the start entry so a concurrent start
    cannot create tasks while the old-task cancellation is still running.
    """

    async with _lifecycle_lock:
        await _cancel_application_tasks("application shutdown")


@driver.on_bot_disconnect
async def stop_scheduler(bot: Bot) -> None:
    global _bot_instance
    async with _lifecycle_lock:
        if _bot_instance is not bot:
            return
        await _cancel_scheduler_tasks("bot disconnected")
        _bot_instance = None


async def shutdown_scheduler() -> None:
    """Stop every registered scheduler task (connection-bound and
    application-level), awaiting their exit.

    Called by the bot.py shutdown orchestration (single hook) — not
    registered directly on the driver, so the phase order is explicit and
    the hook cannot run twice.
    """

    global _bot_instance
    async with _lifecycle_lock:
        await _cancel_scheduler_tasks("application shutdown")
        await _cancel_application_tasks("application shutdown")
        _bot_instance = None


SCHEDULER_ADMIN_PERMISSION = SUPERUSER


def _scheduler_command(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    message = event.get_plaintext().strip()
    return message in {
        "定时开",
        "定时关",
        "定时状态",
        "定时加群",
        "测试推送",
    } or message.startswith("定时时间")


sch = on_message(
    rule=Rule(_scheduler_command),
    permission=SCHEDULER_ADMIN_PERMISSION,
    priority=6,
    block=True,
)


async def _save_command_config(
    config: Mapping[str, object], *, expected_version: int
) -> str | None:
    try:
        await save_cfg(config, expected_version=expected_version)
    except ScheduleVersionConflictError:
        return "配置已被其他管理员更新，请重新查看状态后重试"
    except (OSError, TypeError, ValueError):
        logger.exception("A scheduler admin command could not save configuration")
        return "配置保存失败，请联系管理员查看日志"
    return None


@sch.handle()
async def handle_scheduler_command(bot: Bot, event: MessageEvent) -> None:
    del bot  # The connected instance is managed by lifecycle hooks.
    message = event.get_plaintext().strip()
    config = await load_cfg()

    if message == "测试推送":
        results = await _do_broadcast(config.groups)
        succeeded = sum(result.success for result in results)
        failures = [result for result in results if not result.success]
        if not results:
            await sch.finish(MessageSegment.text("测试推送未发送：尚未配置推送群"))
        if failures:
            failed_groups = "、".join(result.group_id for result in failures)
            await sch.finish(
                MessageSegment.text(
                    f"测试推送完成：成功 {succeeded} 群，失败 {len(failures)} 群"
                    f"\n失败群：{failed_groups}"
                )
            )
        await sch.finish(MessageSegment.text(f"测试推送已发送到 {succeeded} 个群"))

    if message == "定时状态":
        state = "开启" if config.enabled else "关闭"
        times = ", ".join(config.times) or "无"
        groups_text = ", ".join(str(group_id) for group_id in config.groups) or "无"
        await sch.finish(
            MessageSegment.text(
                f"定时播报: {state}\n时区: {config.timezone}\n时间: {times}\n推送群: {groups_text}"
            )
        )

    updated = config.as_dict()
    response: str | None = None
    if message == "定时开":
        updated["enabled"] = True
        response = "定时播报已开启"
    elif message == "定时关":
        updated["enabled"] = False
        response = "定时播报已关闭（缓存预热和价格预警继续运行）"
    elif message == "定时加群":
        if not isinstance(event, GroupMessageEvent):
            await sch.finish(MessageSegment.text("请在群里发送此命令"))
        group_id = event.group_id
        group_ids = [int(value) for value in config.groups]
        if group_id not in group_ids:
            group_ids.append(group_id)
        updated["groups"] = [str(value) for value in group_ids]
        response = f"已添加本群({group_id})到定时推送\n推送时间: {', '.join(config.times) or '无'}"
    elif message.startswith("定时时间"):
        values = message.removeprefix("定时时间").strip().split()
        if not values:
            await sch.finish(MessageSegment.text("格式: 定时时间 08:00 12:00 15:00 20:00"))
        invalid = [value for value in values if not _is_valid_time(value)]
        if invalid:
            await sch.finish(
                MessageSegment.text("时间格式无效（需为 24 小时制 HH:MM）: " + ", ".join(invalid))
            )
        updated["times"] = list(dict.fromkeys(values))
        response = f"播报时间已更新: {', '.join(values)}"

    if response is None:
        return
    if updated != config.as_dict():
        if error := await _save_command_config(updated, expected_version=config.version):
            await sch.finish(MessageSegment.text(error))
    await sch.finish(MessageSegment.text(response))

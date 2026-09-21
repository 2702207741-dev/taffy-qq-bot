"""Application composition root for durable state repositories."""

from __future__ import annotations

import asyncio
import logging

from src.core.config import load_runtime_settings
from src.core.database import PROJECT_ROOT, StateDatabase
from src.services.authority import FeatureAuthority
from src.services.backup_service import BackupService
from src.services.config_backup import ConfigSnapshotService
from src.services.telemetry import telemetry

from .alert_repository import LEGACY_SOURCE_NAME as ALERT_LEGACY_SOURCE
from .alert_repository import AlertRepository
from .broadcast_repository import BroadcastRepository
from .csqaq_repository import CsqaqGoodIdRepository
from .group_pulse_repository import GroupPulseLogRepository
from .insight_embedding_repository import InsightEmbeddingRepository
from .inventory_monitor_repository import (
    MAX_TOTAL_MONITORED_ACCOUNTS,
    MAX_TOTAL_SUBSCRIPTIONS,
    InventoryMonitorRepository,
)
from .job_repository import JobRepository
from .learning_repository import LearningInsightRepository
from .permission_repository import PermissionRepository
from .persona_memory_repository import PersonaMemoryRepository
from .persona_relationship_repository import PersonaRelationshipRepository
from .persona_slang_repository import PersonaSlangRepository
from .persona_style_repository import PersonaStyleRepository
from .proactive_repository import ProactiveDeliveryRepository
from .reddit_post_repository import RedditPostRepository
from .schedule_repository import (
    LEGACY_SOURCE_NAME as SCHEDULE_LEGACY_SOURCE,
)
from .schedule_repository import ScheduleRepository
from .session_repository import SessionRepository
from .session_summary_repository import SessionSummaryRepository
from .usage_log_repository import UsageLogRepository
from .watchlist_repository import (
    LEGACY_SOURCE_NAME as WATCHLIST_LEGACY_SOURCE,
)
from .watchlist_repository import WatchlistRepository

_settings = load_runtime_settings(PROJECT_ROOT)
_configured_database_path = _settings.get("taffy_state_db")
logger = logging.getLogger("taffy.state.runtime")


def _bounded_int_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _settings.get(name.casefold())
    if raw is None:
        return default
    try:
        value = int(str(raw))
    except ValueError:
        logger.warning("Ignoring invalid integer state setting %s", name)
        return default
    return min(maximum, max(minimum, value))


def _boolean_setting(name: str, default: bool = False) -> bool:
    raw = _settings.get(name.casefold())
    if raw is None:
        return default
    return str(raw).strip().casefold() in {"1", "true", "yes", "on"}


database = StateDatabase(
    _configured_database_path or None,
    # Existing schemas must normally be upgraded by the offline command,
    # which creates and verifies a consistent backup before any DDL runs.
    allow_schema_migrations=_boolean_setting("TAFFY_ALLOW_AUTO_DB_MIGRATION", False),
)
backups = BackupService(database)
config_snapshots = ConfigSnapshotService()
permissions = PermissionRepository(database)
authority = FeatureAuthority(permissions)
usage_log = UsageLogRepository(database)
sessions = SessionRepository(database)
persona_memories = PersonaMemoryRepository(database)
persona_styles = PersonaStyleRepository(database)
alerts = AlertRepository(
    database,
    max_attempts=_bounded_int_setting("TAFFY_ALERT_MAX_ATTEMPTS", 8, 1, 100),
)
watchlists = WatchlistRepository(database)
schedules = ScheduleRepository(
    database,
    legacy_path=_settings.get("taffy_schedule_config") or None,
)
broadcasts = BroadcastRepository(
    database,
    retry_base_seconds=_bounded_int_setting("TAFFY_BROADCAST_RETRY_BASE_SECONDS", 30, 1, 86_400),
    retry_max_seconds=_bounded_int_setting("TAFFY_BROADCAST_RETRY_MAX_SECONDS", 3_600, 1, 604_800),
    max_attempts=_bounded_int_setting("TAFFY_BROADCAST_MAX_ATTEMPTS", 8, 1, 100),
)
jobs = JobRepository(database)
learning_insights = LearningInsightRepository(database)
insight_embeddings = InsightEmbeddingRepository(database)
proactive_deliveries = ProactiveDeliveryRepository(database)
group_pulse_log = GroupPulseLogRepository(database)
reddit_posts = RedditPostRepository(database)
csqaq_good_ids = CsqaqGoodIdRepository(database)
session_summaries = SessionSummaryRepository(database)
persona_relationships = PersonaRelationshipRepository(database)
persona_slang = PersonaSlangRepository(database)
inventory_monitors = InventoryMonitorRepository(
    database,
    max_attempts=_bounded_int_setting(
        "TAFFY_INVENTORY_MONITOR_MAX_ATTEMPTS",
        8,
        1,
        100,
    ),
    max_total_accounts=_bounded_int_setting(
        "TAFFY_INVENTORY_MONITOR_MAX_TOTAL_ACCOUNTS",
        MAX_TOTAL_MONITORED_ACCOUNTS,
        1,
        100_000,
    ),
    max_total_subscriptions=_bounded_int_setting(
        "TAFFY_INVENTORY_MONITOR_MAX_TOTAL_SUBSCRIPTIONS",
        MAX_TOTAL_SUBSCRIPTIONS,
        1,
        1_000_000,
    ),
)

_initialize_lock = asyncio.Lock()
_initialized = False


async def initialize_state() -> None:
    """Initialize the schema and every registered legacy migration once."""

    global _initialized
    if _initialized:
        return
    async with _initialize_lock:
        if _initialized:
            return
        await sessions.initialize()
        await persona_memories.initialize()
        await persona_styles.initialize()
        await alerts.initialize()
        await watchlists.initialize()
        await schedules.initialize()
        await broadcasts.initialize()
        await jobs.initialize()
        await learning_insights.initialize()
        await proactive_deliveries.initialize()
        await group_pulse_log.initialize()
        await csqaq_good_ids.initialize()
        await session_summaries.initialize()
        await persona_relationships.initialize()
        await persona_slang.initialize()
        await inventory_monitors.initialize()
        await permissions.initialize()
        await usage_log.initialize()
        await telemetry.initialize()
        if not await database.integrity_check():
            raise RuntimeError("SQLite state integrity check failed")
        _initialized = True


async def state_healthcheck() -> bool:
    """Return readiness of the shared schema and completed session migration."""

    if not _initialized or not await sessions.healthcheck():
        return False
    expected_migrations = {
        ALERT_LEGACY_SOURCE,
        WATCHLIST_LEGACY_SOURCE,
        SCHEDULE_LEGACY_SOURCE,
    }
    rows = await database.read(
        """
        SELECT source_name FROM legacy_imports
        WHERE source_name IN (?, ?, ?)
        """,
        tuple(sorted(expected_migrations)),
    )
    if {str(row["source_name"]) for row in rows} != expected_migrations:
        return False
    try:
        await schedules.get()
    except Exception:
        return False
    return True


async def shutdown_state() -> None:
    """Cooperatively checkpoint WAL pages during graceful process shutdown."""

    global _initialized
    try:
        try:
            await telemetry.flush()
        finally:
            await database.checkpoint()
    finally:
        _initialized = False

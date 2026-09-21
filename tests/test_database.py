from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.database import (
    DEFAULT_DATABASE_PATH,
    SCHEMA_VERSION,
    SchemaMigrationRequiredError,
    StateDatabase,
    Transaction,
    UnsupportedSchemaVersionError,
)


def test_database_path_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TAFFY_STATE_DB", raising=False)
    assert StateDatabase().path == DEFAULT_DATABASE_PATH

    configured = tmp_path / "configured" / "state.db"
    monkeypatch.setenv("TAFFY_STATE_DB", str(configured))
    assert StateDatabase().path == configured

    explicit = tmp_path / "explicit.db"
    assert StateDatabase(explicit).path == explicit
    with pytest.raises(ValueError, match="positive integer"):
        StateDatabase(explicit, busy_timeout_ms=0)
    with pytest.raises(ValueError, match="file path"):
        StateDatabase(":memory:")


@pytest.mark.asyncio
async def test_initialize_configures_wal_pragmas_and_schema(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "nested" / "state.db", busy_timeout_ms=1_234)

    await database.initialize()

    assert database.path.is_file()
    assert await database.integrity_check()
    assert (await database.read("PRAGMA journal_mode"))[0]["journal_mode"] == "wal"
    assert (await database.read("PRAGMA foreign_keys"))[0]["foreign_keys"] == 1
    assert (await database.read("PRAGMA trusted_schema"))[0]["trusted_schema"] == 0
    assert (await database.read("PRAGMA secure_delete"))[0]["secure_delete"] == 2
    assert (await database.read("PRAGMA temp_store"))[0]["temp_store"] == 2
    assert (await database.read("PRAGMA busy_timeout"))[0]["timeout"] == 1_234
    assert (await database.read("PRAGMA synchronous"))[0]["synchronous"] == 2
    assert (await database.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    checkpoint = await database.checkpoint()
    assert len(checkpoint) == 3
    assert all(value >= 0 for value in checkpoint)

    table_rows = await database.read(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    table_names = {row["name"] for row in table_rows}
    assert {
        "alerts",
        "background_job_runs",
        "broadcast_deliveries",
        "inventory_monitor_accounts",
        "inventory_monitor_deliveries",
        "inventory_monitor_events",
        "inventory_monitor_item_history",
        "inventory_monitor_subscriptions",
        "legacy_imports",
        "persona_memories",
        "persona_style_profiles",
        "persona_feedback_events",
        "scheduler_groups",
        "scheduler_settings",
        "scheduler_times",
        "session_leases",
        "session_messages",
        "watchlist_items",
    } <= table_names


@pytest.mark.asyncio
async def test_concurrent_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    databases = [StateDatabase(path) for _ in range(24)]

    await asyncio.gather(*(database.initialize() for database in databases))

    assert await databases[0].healthcheck()
    indexes = await databases[0].read(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
    )
    assert len({row["name"] for row in indexes}) == len(indexes)


@pytest.mark.asyncio
async def test_transaction_rolls_back_every_statement_on_exception(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "rollback.db")
    await database.initialize()

    def fail_after_write(transaction: Transaction) -> None:
        transaction.write(
            """
            INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (10001, "AK-47 | Redline", "ak-47 | redline"),
        )
        raise RuntimeError("simulated failure")

    with pytest.raises(RuntimeError, match="simulated failure"):
        await database.transaction(fail_after_write)

    assert await database.read("SELECT * FROM watchlist_items") == []


@pytest.mark.asyncio
async def test_schema_constraints_reject_invalid_and_duplicate_state(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "constraints.db")
    await database.initialize()

    await database.write(
        """
        INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
        VALUES (?, ?, ?)
        """,
        (10001, "M4A1-S | Printstream", "m4a1-s | printstream"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.write(
            """
            INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (10001, "M4A1-S | Printstream", "m4a1-s | printstream"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        await database.write(
            """
            INSERT INTO alerts
                (id, owner_user_id, market_name, target_price_cents, status)
            VALUES (?, ?, ?, ?, 'claimed')
            """,
            ("missing-lease", 10001, "AWP | Asiimov", 10_000),
        )

    result = await database.write(
        """
        INSERT INTO alerts
            (id, owner_user_id, market_name, target_price_cents, status,
             lease_owner, lease_expires_at)
        VALUES (?, ?, ?, ?, 'claimed', ?, ?)
        """,
        ("valid-claim", 10001, "AWP | Asiimov", 10_000, "worker-1", 123456),
    )
    assert result.rowcount == 1

    with pytest.raises(sqlite3.IntegrityError):
        await database.write(
            "INSERT INTO session_messages (session_key, role, content) VALUES (?, ?, ?)",
            ("group:1", "tool", "not a supported chat role"),
        )


@pytest.mark.asyncio
async def test_healthcheck_requires_current_schema_and_rejects_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "health.db"
    database = StateDatabase(path)

    assert not await database.healthcheck()
    await database.initialize()
    assert await database.healthcheck()
    assert await database.integrity_check()

    connection = sqlite3.connect(path)
    try:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    assert not await database.healthcheck()
    with pytest.raises(UnsupportedSchemaVersionError):
        await database.initialize()


@pytest.mark.asyncio
async def test_v1_to_current_migration_preserves_alert_and_broadcast_rows(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE alerts (
                id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, group_id INTEGER,
                market_name TEXT NOT NULL, target_price_cents INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL,
                next_retry_at INTEGER, lease_owner TEXT, lease_expires_at INTEGER,
                last_error TEXT, last_observed_price_cents INTEGER,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                delivered_at INTEGER, message_id TEXT
            );
            CREATE TABLE broadcast_deliveries (
                schedule_key TEXT NOT NULL, group_id INTEGER NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL,
                next_retry_at INTEGER, lease_owner TEXT, lease_expires_at INTEGER,
                last_error TEXT, payload_text TEXT, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, delivered_at INTEGER, message_id TEXT,
                PRIMARY KEY (schedule_key, group_id)
            ) WITHOUT ROWID;
            INSERT INTO alerts VALUES (
                'alert-v1', 101, NULL, 'item', 1000, 'pending', 2,
                200, NULL, NULL, 'temporary', NULL, 10, 20, NULL, NULL
            );
            INSERT INTO broadcast_deliveries VALUES (
                'slot-v1', 1001, 'delivered', 1, NULL, NULL, NULL,
                NULL, 'payload', 10, 20, 20, 'message-v1'
            );
            PRAGMA user_version = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = StateDatabase(path)
    await database.initialize()

    assert (await database.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    alerts = await database.read(
        "SELECT id, status, attempts, failed_at, direction FROM alerts WHERE id = 'alert-v1'"
    )
    deliveries = await database.read(
        """
        SELECT schedule_key, status, message_id, failed_at
        FROM broadcast_deliveries WHERE schedule_key = 'slot-v1'
        """
    )
    assert alerts == [
        {
            "id": "alert-v1",
            "status": "pending",
            "attempts": 2,
            "failed_at": None,
            "direction": "above",
        }
    ]
    assert deliveries == [
        {
            "schedule_key": "slot-v1",
            "status": "delivered",
            "message_id": "message-v1",
            "failed_at": None,
        }
    ]
    assert await database.integrity_check()


@pytest.mark.asyncio
async def test_existing_schema_upgrade_can_require_offline_migration(tmp_path: Path) -> None:
    path = tmp_path / "v1-offline.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_placeholder (id INTEGER PRIMARY KEY)")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    database = StateDatabase(path, allow_schema_migrations=False)
    with pytest.raises(SchemaMigrationRequiredError, match="offline migration"):
        await database.initialize()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'legacy_placeholder'"
        ).fetchone() == ("legacy_placeholder",)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_failed_v1_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    path = tmp_path / "broken-v1.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE alerts (id TEXT PRIMARY KEY);
            CREATE TABLE broadcast_deliveries (schedule_key TEXT, group_id INTEGER);
            PRAGMA user_version = 1;
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = StateDatabase(path)
    with pytest.raises(sqlite3.Error):
        await database.initialize()

    connection = sqlite3.connect(path)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert version == 1
    assert "alerts" in tables
    assert "alerts_v1" not in tables


@pytest.mark.asyncio
async def test_each_operation_uses_an_independent_worker_thread_connection(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    event_loop_thread = threading.get_ident()
    real_connect = sqlite3.connect

    class RecordingConnection(sqlite3.Connection):
        closed_by_database = False
        open_transaction_at_close: bool | None = None

        def close(self) -> None:
            self.open_transaction_at_close = self.in_transaction
            self.closed_by_database = True
            super().close()

    connection_records: list[tuple[int, RecordingConnection]] = []

    def recording_connect(*args: object, **kwargs: object) -> RecordingConnection:
        connection = real_connect(*args, factory=RecordingConnection, **kwargs)
        connection_records.append((threading.get_ident(), connection))
        return connection

    with patch("src.core.database.sqlite3.connect", side_effect=recording_connect):
        database = StateDatabase(path)
        await database.initialize()
        await database.read("SELECT 1 AS value")
        await database.write(
            """
            INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (10001, "Desert Eagle | Blaze", "desert eagle | blaze"),
        )
        assert await database.healthcheck()

    assert len(connection_records) == 4
    assert all(thread_id != event_loop_thread for thread_id, _ in connection_records)
    assert len({id(connection) for _, connection in connection_records}) == 4
    assert all(connection.closed_by_database for _, connection in connection_records)
    assert all(
        connection.open_transaction_at_close is False for _, connection in connection_records
    )


@pytest.mark.asyncio
async def test_transaction_rejects_async_callbacks_without_committing(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "async-callback.db")
    await database.initialize()

    async def invalid_callback(transaction: Transaction) -> None:
        transaction.write(
            """
            INSERT INTO watchlist_items (owner_user_id, display_name, normalized_name)
            VALUES (?, ?, ?)
            """,
            (10001, "should not be stored", "should not be stored"),
        )

    with pytest.raises(TypeError, match="must be synchronous"):
        await database.transaction(invalid_callback)
    assert await database.read("SELECT * FROM watchlist_items") == []


@pytest.mark.asyncio
async def test_ddl_executor_preserves_semicolons_inside_literals(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "ddl-literals.db")
    await database.initialize()
    script = """
    CREATE TABLE IF NOT EXISTS ddl_probe (
        value TEXT NOT NULL DEFAULT 'a;b'
    );
    INSERT INTO ddl_probe (value) VALUES ('x;y');
    """

    await database.transaction(lambda transaction: database._execute_ddl(transaction, script))

    rows = await database.read("SELECT value FROM ddl_probe")
    assert rows == [{"value": "x;y"}]
    schema = await database.read(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ddl_probe'"
    )
    assert schema and "'a;b'" in schema[0]["sql"]


@pytest.mark.asyncio
async def test_v4_to_v5_migration_adds_empty_persona_memory_table(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    original = StateDatabase(path)
    await original.initialize()
    await original.write(
        "INSERT INTO session_messages (session_key, role, content) VALUES (?, 'user', ?)",
        ("p_1001", "existing conversation"),
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluated_at")
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluation_error")
        connection.execute("ALTER TABLE alerts DROP COLUMN evaluation_failures")
        connection.execute("DROP TABLE persona_feedback_events")
        connection.execute("DROP TABLE persona_style_profiles")
        connection.execute("DROP TABLE persona_memories")
        connection.execute("DROP TABLE IF EXISTS usage_log")
        connection.execute("DROP TABLE IF EXISTS feature_permissions")
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    assert await migrated.read("SELECT * FROM persona_memories") == []
    assert (await migrated.read("SELECT content FROM session_messages"))[0]["content"] == (
        "existing conversation"
    )
    assert await migrated.integrity_check()


@pytest.mark.asyncio
async def test_v5_to_v6_migration_adds_empty_persona_style_tables(tmp_path: Path) -> None:
    path = tmp_path / "v5.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluated_at")
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluation_error")
        connection.execute("ALTER TABLE alerts DROP COLUMN evaluation_failures")
        connection.execute("DROP TABLE persona_feedback_events")
        connection.execute("DROP TABLE persona_style_profiles")
        connection.execute("DROP TABLE IF EXISTS usage_log")
        connection.execute("DROP TABLE IF EXISTS feature_permissions")
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    assert await migrated.read("SELECT * FROM persona_style_profiles") == []
    assert await migrated.read("SELECT * FROM persona_feedback_events") == []
    assert await migrated.integrity_check()


@pytest.mark.asyncio
async def test_v6_to_v7_requeues_only_evaluation_failed_alerts(tmp_path: Path) -> None:
    path = tmp_path / "v6.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            INSERT INTO alerts (
                id, owner_user_id, market_name, target_price_cents, status,
                attempts, next_retry_at, last_error, created_at, updated_at, failed_at
            ) VALUES ('evaluation', 1, 'Item A', 100, 'failed', 4, NULL,
                      'AlertEvaluationError', 10, 20, 20)
            """
        )
        connection.execute(
            """
            INSERT INTO alerts (
                id, owner_user_id, market_name, target_price_cents, status,
                attempts, next_retry_at, last_error, created_at, updated_at, failed_at
            ) VALUES ('delivery', 1, 'Item B', 100, 'failed', 4, NULL,
                      'QQ delivery failed', 10, 20, 20)
            """
        )
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluated_at")
        connection.execute("ALTER TABLE alerts DROP COLUMN last_evaluation_error")
        connection.execute("ALTER TABLE alerts DROP COLUMN evaluation_failures")
        connection.execute("DROP TABLE IF EXISTS usage_log")
        connection.execute("DROP TABLE IF EXISTS feature_permissions")
        connection.execute("PRAGMA user_version = 6")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()
    rows = {
        row["id"]: row
        for row in await migrated.read(
            """
            SELECT id, status, attempts, evaluation_failures,
                   last_error, last_evaluation_error, failed_at
            FROM alerts ORDER BY id
            """
        )
    }
    evaluation = rows["evaluation"]
    assert evaluation["status"] == "pending"
    assert evaluation["attempts"] == 0 and evaluation["evaluation_failures"] == 4
    assert evaluation["last_error"] is None
    assert evaluation["last_evaluation_error"] == "AlertEvaluationError"
    assert evaluation["failed_at"] is None
    delivery = rows["delivery"]
    assert delivery["status"] == "failed" and delivery["attempts"] == 4
    assert delivery["evaluation_failures"] == 0


@pytest.mark.asyncio
async def test_v8_to_v9_migration_adds_learning_insights_table(tmp_path: Path) -> None:
    path = tmp_path / "v8.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE learning_insights")
        connection.execute("PRAGMA user_version = 8")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    columns = await migrated.read("PRAGMA table_info(learning_insights)")
    names = {str(row["name"]) for row in columns}
    assert {
        "id",
        "user_id",
        "scope_group_id",
        "kind",
        "content",
        "evidence",
        "confidence",
        "status",
        "created_at",
        "accepted_at",
        "updated_at",
        "weight",
    } <= names
    assert await migrated.integrity_check()


@pytest.mark.asyncio
async def test_v9_to_v10_migration_adds_proactive_deliveries_table(tmp_path: Path) -> None:
    path = tmp_path / "v9.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE proactive_deliveries")
        connection.execute("PRAGMA user_version = 9")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    columns = await migrated.read("PRAGMA table_info(proactive_deliveries)")
    names = {str(row["name"]) for row in columns}
    assert {
        "id",
        "user_id",
        "kind",
        "dedupe_key",
        "status",
        "detail",
        "created_at",
        "updated_at",
    } <= names
    assert await migrated.integrity_check()


@pytest.mark.asyncio
async def test_v9_to_v10_migration_requires_offline_command_by_default(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v9-locked.db"
    original = StateDatabase(path)
    await original.initialize()
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE proactive_deliveries")
        connection.execute("PRAGMA user_version = 9")
        connection.commit()
    finally:
        connection.close()

    locked = StateDatabase(path, allow_schema_migrations=False)
    with pytest.raises(SchemaMigrationRequiredError, match="offline migration"):
        await locked.initialize()


@pytest.mark.asyncio
async def test_v13_to_v14_migration_adds_anchors_and_float_cache(tmp_path: Path) -> None:
    """囤货雷达 P0：旧库平滑迁移出锚点列 + float 缓存表，且数据无损。"""

    path = tmp_path / "v13.db"
    original = StateDatabase(path)
    await original.initialize()
    digest = "a" * 64
    await original.write(
        """
        INSERT INTO inventory_monitor_accounts (
            steam_id, nickname, snapshot_json, snapshot_hash,
            source_time, last_success_at, next_check_at,
            created_at, updated_at
        ) VALUES ('76561198000000000', 'AI', '{"v":1}', ?, 10, 10, 20, 10, 10)
        """,
        (digest,),
    )
    await original.write(
        """
        INSERT INTO inventory_monitor_item_history (
            steam_id, item_key, item_json, first_seen_at, last_seen_at
        ) VALUES ('76561198000000000', 'fp:abc', '{"name":"旧物品"}', 10, 10)
        """
    )
    connection = sqlite3.connect(path)
    try:
        # Roll the history table back to its v13 shape and drop the cache.
        connection.executescript(
            """
            CREATE TABLE legacy_history (
                steam_id TEXT NOT NULL,
                item_key TEXT NOT NULL,
                item_json TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                last_missing_at INTEGER,
                PRIMARY KEY (steam_id, item_key)
            );
            INSERT INTO legacy_history SELECT steam_id, item_key, item_json,
                first_seen_at, last_seen_at, last_missing_at
            FROM inventory_monitor_item_history;
            DROP TABLE inventory_monitor_item_history;
            ALTER TABLE legacy_history RENAME TO inventory_monitor_item_history;
            DROP TABLE inventory_monitor_float_cache;
            PRAGMA user_version = 13;
            """
        )
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    columns = {
        str(row["name"])
        for row in await migrated.read("PRAGMA table_info(inventory_monitor_item_history)")
    }
    assert {"market_hash_name", "paint_index", "paintwear", "pattern_index", "stickers_json"} <= (
        columns
    )
    # The pre-existing row survived with a NULL anchor; anchors stay opt-in.
    rows = await migrated.read(
        "SELECT paintwear FROM inventory_monitor_item_history WHERE item_key = 'fp:abc'"
    )
    assert rows and rows[0]["paintwear"] is None
    assert await migrated.read("SELECT COUNT(*) AS c FROM inventory_monitor_float_cache") == [
        {"c": 0}
    ]
    # Re-initializing (idempotent re-run of the chain) must not fail or duplicate.
    again = StateDatabase(path, allow_schema_migrations=True)
    await again.initialize()
    assert await again.integrity_check()


@pytest.mark.asyncio
async def test_v15_to_v16_migration_adds_p0_tables_and_preserves_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v15.db"
    original = StateDatabase(path)
    await original.initialize()
    await original.write(
        "INSERT INTO session_messages (session_key, role, content) VALUES (?, 'user', ?)",
        ("g_42", "v15 conversation"),
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE csqaq_good_id_cache")
        connection.execute("DROP TABLE persona_slang")
        connection.execute("DROP TABLE persona_relationships")
        connection.execute("DROP TABLE session_summaries")
        connection.execute("PRAGMA user_version = 15")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    for table in (
        "session_summaries",
        "persona_relationships",
        "persona_slang",
        "csqaq_good_id_cache",
    ):
        assert await migrated.read(f"SELECT * FROM {table}") == []
    assert (await migrated.read("SELECT content FROM session_messages"))[0]["content"] == (
        "v15 conversation"
    )
    assert await migrated.integrity_check()

    # Re-initializing (idempotent re-run of the chain) must not fail.
    again = StateDatabase(path, allow_schema_migrations=True)
    await again.initialize()
    assert await again.integrity_check()


@pytest.mark.asyncio
async def test_v16_tables_enforce_check_constraints(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "constraints.db")
    await database.initialize()

    await database.write(
        """
        INSERT INTO session_summaries (session_key, summary, covers_from_id, covers_to_id)
        VALUES ('g_1', 'earlier chatter summary', 5, 9)
        """
    )
    rows = await database.read("SELECT session_key FROM session_summaries")
    assert rows == [{"session_key": "g_1"}]

    for statement in (
        # Summary bounds must not invert.
        """
        INSERT INTO session_summaries (session_key, summary, covers_from_id, covers_to_id)
        VALUES ('g_1', 'inverted bounds', 9, 5)
        """,
        # Summary text must exist and stay bounded.
        """
        INSERT INTO session_summaries (session_key, summary, covers_from_id, covers_to_id)
        VALUES ('g_1', 'x', 1, 2)
        """,
        # Intimacy stays within 0..100.
        "INSERT INTO persona_relationships (user_id, scope_group_id, intimacy) VALUES (1, 0, 150)",
        # User ids stay positive.
        "INSERT INTO persona_relationships (user_id, scope_group_id) VALUES (0, 0)",
        # Slang terms are short and statuses constrained.
        """
        INSERT INTO persona_slang (scope_group_id, created_by, term, meaning)
        VALUES (1, 2, 'this term is far far far too long for the check', '含义')
        """,
        """
        INSERT INTO persona_slang (scope_group_id, created_by, term, meaning, status)
        VALUES (1, 2, '梗', '含义', 'auto-accepted')
        """,
        # Good ids stay positive.
        """
        INSERT INTO csqaq_good_id_cache (market_hash_name, good_id, csqaq_name, resolved_at)
        VALUES ('AK-47 | Redline', 0, 'AK47红线', 100)
        """,
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await database.write(statement)


@pytest.mark.asyncio
async def test_v16_relationship_primary_key_upserts_by_scope(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "relationship.db")
    await database.initialize()
    await database.write(
        "INSERT INTO persona_relationships (user_id, scope_group_id, intimacy) VALUES (7, 0, 10)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.write(
            "INSERT INTO persona_relationships (user_id, scope_group_id, intimacy) VALUES (7, 0, 20)"
        )
    await database.write(
        "INSERT INTO persona_relationships (user_id, scope_group_id, intimacy) VALUES (7, 42, 20)"
    )
    rows = await database.read(
        "SELECT scope_group_id, intimacy FROM persona_relationships ORDER BY scope_group_id"
    )
    assert rows == [
        {"scope_group_id": 0, "intimacy": 10.0},
        {"scope_group_id": 42, "intimacy": 20.0},
    ]


# ---------------------------------------------------------------------------
# Schema v17: bi-temporal insights, slang enablement, V0.5 additive tables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v16_to_v17_migration_rebuilds_insights_and_slang(tmp_path: Path) -> None:
    path = tmp_path / "v16.db"
    database = StateDatabase(path)
    await database.initialize()
    await database.write(
        """
        INSERT INTO learning_insights
            (user_id, scope_group_id, kind, content, evidence, confidence, status)
        VALUES (7, 0, 'knowledge', '常查价格：AK-47 | Redline', '近 24 小时群内 3 次查询', 3, 'accepted')
        """
    )
    await database.write(
        """
        INSERT INTO persona_slang (scope_group_id, created_by, term, meaning, status)
        VALUES (42, 7, '牛肉', '厉害的意思', 'accepted')
        """
    )
    connection = sqlite3.connect(path)
    try:
        # Recreate the exact v16 shapes (no bi-temporal columns, no enabled
        # flag) so the offline chain has real rebuild work to do.
        connection.execute("DROP TABLE learning_insights")
        connection.execute(
            """
            CREATE TABLE learning_insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL CHECK (user_id > 0),
                scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
                kind TEXT NOT NULL
                    CHECK (kind IN ('preference', 'style', 'knowledge', 'cultural')),
                content TEXT NOT NULL CHECK (length(content) BETWEEN 2 AND 300),
                evidence TEXT NOT NULL CHECK (length(evidence) <= 400),
                confidence INTEGER NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 1 AND 5),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'rejected', 'archived')),
                created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
                    CHECK (created_at >= 0),
                accepted_at INTEGER CHECK (accepted_at IS NULL OR accepted_at >= 0),
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
                    CHECK (updated_at >= 0),
                weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO learning_insights
                (user_id, scope_group_id, kind, content, evidence, confidence, status)
            VALUES
                (7, 0, 'knowledge', '常查价格：AK-47 | Redline', '近 24 小时群内 3 次查询', 3, 'accepted'),
                (7, 0, 'style', '喜欢即时调整回答风格', '近 24 小时出现过风格反馈句', 1, 'pending')
            """
        )
        connection.execute("DROP TABLE persona_slang")
        connection.execute(
            """
            CREATE TABLE persona_slang (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
                created_by INTEGER NOT NULL CHECK (created_by > 0),
                term TEXT NOT NULL CHECK (length(term) BETWEEN 1 AND 24),
                meaning TEXT NOT NULL CHECK (length(meaning) BETWEEN 1 AND 200),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'rejected')),
                confidence INTEGER NOT NULL DEFAULT 2 CHECK (confidence BETWEEN 1 AND 5),
                created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
                    CHECK (created_at >= 0),
                accepted_at INTEGER CHECK (accepted_at IS NULL OR accepted_at >= 0),
                updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
                    CHECK (updated_at >= 0)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO persona_slang (scope_group_id, created_by, term, meaning, status)
            VALUES (42, 7, '牛肉', '厉害的意思', 'accepted')
            """
        )
        connection.execute("PRAGMA user_version = 16")
        connection.commit()
    finally:
        connection.close()

    migrated = StateDatabase(path, allow_schema_migrations=True)
    await migrated.initialize()

    assert (await migrated.read("PRAGMA user_version"))[0]["user_version"] == SCHEMA_VERSION
    insight_rows = await migrated.read(
        """
        SELECT status, observed_at, valid_from, valid_until, hit_count, is_protected
        FROM learning_insights ORDER BY id
        """
    )
    assert [row["status"] for row in insight_rows] == ["accepted", "pending"]
    for row in insight_rows:
        assert row["observed_at"] == row["valid_from"] > 0
        assert row["valid_until"] is None
        assert row["hit_count"] == 0 and row["is_protected"] == 0
    slang_rows = await migrated.read("SELECT term, enabled FROM persona_slang")
    assert slang_rows == [{"term": "牛肉", "enabled": 1}]
    for table in ("memory_links", "image_descriptions", "persona_eval_scores", "llm_usage_daily"):
        assert await migrated.read(f"SELECT * FROM {table}") == []
    assert await migrated.integrity_check()

    # Idempotent rerun of the whole chain must not fail or duplicate rows.
    again = StateDatabase(path, allow_schema_migrations=True)
    await again.initialize()
    assert await again.integrity_check()
    assert len(await again.read("SELECT id FROM learning_insights")) == 2


@pytest.mark.asyncio
async def test_v17_tables_enforce_check_constraints(tmp_path: Path) -> None:
    database = StateDatabase(tmp_path / "v17-constraints.db")
    await database.initialize()

    # The deprecated soft-delete status is accepted and retrievable.
    await database.write(
        """
        INSERT INTO learning_insights
            (user_id, scope_group_id, kind, content, evidence, status, valid_until)
        VALUES (7, 0, 'knowledge', '常查价格：M4A1 消音', '每日反思', 'deprecated', 100)
        """
    )
    rows = await database.read("SELECT valid_until FROM learning_insights")
    assert rows == [{"valid_until": 100}]

    for statement in (
        "INSERT INTO learning_insights (user_id, kind, content, evidence, status)"
        " VALUES (7, 'knowledge', '内容占位', '依据占位', 'deprecated-invalid')",
        "INSERT INTO learning_insights (user_id, kind, content, evidence, hit_count)"
        " VALUES (7, 'knowledge', '内容占位', '依据占位', -1)",
        "INSERT INTO learning_insights (user_id, kind, content, evidence, is_protected)"
        " VALUES (7, 'knowledge', '内容占位', '依据占位', 2)",
        "INSERT INTO persona_slang (scope_group_id, created_by, term, meaning, enabled)"
        " VALUES (1, 2, '梗', '含义', 2)",
        """
        INSERT INTO persona_eval_scores (run_id, dimension, score, calibrated)
        VALUES ('run-0001', '口癖频率', 6.0, 1)
        """,
        """
        INSERT INTO llm_usage_daily (day, provider, model, requests)
        VALUES ('2026-8-30', 'deepseek', 'deepseek-v4-pro', 1)
        """,
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await database.write(statement)

    # memory_links unique key blocks duplicate relations.
    await database.write(
        """
        INSERT INTO learning_insights (user_id, kind, content, evidence, status)
        VALUES (7, 'knowledge', '内容占位甲', '依据占位', 'accepted')
        """
    )
    await database.write(
        """
        INSERT INTO learning_insights (user_id, kind, content, evidence, status)
        VALUES (7, 'knowledge', '内容占位乙', '依据占位', 'accepted')
        """
    )
    link = (
        "INSERT INTO memory_links (user_id, scope_group_id, from_insight_id,"
        " to_insight_id, relation) VALUES (7, 0, 1, 2, '同话题')"
    )
    await database.write(link)
    with pytest.raises(sqlite3.IntegrityError):
        await database.write(link)

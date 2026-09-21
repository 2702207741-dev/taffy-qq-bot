"""Async SQLite state storage with short-lived, worker-thread connections.

All durable bot state (sessions, alerts, watchlists, schedules, inventory
monitors and delivery outboxes) lives in this SQLite database.  Every
operation owns one connection, runs inside an explicit transaction, and keeps
all blocking ``sqlite3`` work off the event-loop thread.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

logger = logging.getLogger("taffy.database")

SCHEMA_VERSION = 19
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "taffy.db"
DEFAULT_BUSY_TIMEOUT_MS = 5_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY NOT NULL
        CHECK (length(key) BETWEEN 1 AND 200),
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS legacy_imports (
    source_name TEXT PRIMARY KEY NOT NULL
        CHECK (length(source_name) BETWEEN 1 AND 200),
    source_sha256 TEXT NOT NULL
        CHECK (length(source_sha256) = 64),
    imported_rows INTEGER NOT NULL
        CHECK (imported_rows >= 0),
    source_state TEXT NOT NULL
        CHECK (source_state IN ('absent', 'imported')),
    imported_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (imported_at >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL
        CHECK (length(session_key) BETWEEN 1 AND 256),
    role TEXT NOT NULL
        CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL
        CHECK (length(content) <= 8000),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_session_messages_session
    ON session_messages (session_key, id);
CREATE INDEX IF NOT EXISTS idx_session_messages_created
    ON session_messages (created_at, session_key);

CREATE TABLE IF NOT EXISTS session_leases (
    session_key TEXT PRIMARY KEY NOT NULL
        CHECK (length(session_key) BETWEEN 1 AND 256),
    lease_token TEXT NOT NULL
        CHECK (length(lease_token) BETWEEN 1 AND 200),
    lease_expires_at INTEGER NOT NULL
        CHECK (lease_expires_at >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS persona_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    memory_kind TEXT NOT NULL
        CHECK (memory_kind IN ('nickname', 'note')),
    content TEXT NOT NULL
        CHECK (length(content) BETWEEN 1 AND 200),
    normalized_content TEXT NOT NULL
        CHECK (length(normalized_content) BETWEEN 1 AND 200),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    UNIQUE (owner_user_id, scope_group_id, memory_kind, normalized_content)
);

CREATE INDEX IF NOT EXISTS idx_persona_memories_scope
    ON persona_memories (owner_user_id, scope_group_id, updated_at DESC, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_memories_nickname
    ON persona_memories (owner_user_id, scope_group_id)
    WHERE memory_kind = 'nickname';

CREATE TABLE IF NOT EXISTS persona_style_profiles (
    owner_user_id INTEGER NOT NULL CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
    learning_enabled INTEGER NOT NULL DEFAULT 0 CHECK (learning_enabled IN (0, 1)),
    concise INTEGER NOT NULL DEFAULT 0 CHECK (concise BETWEEN -2 AND 2),
    warm INTEGER NOT NULL DEFAULT 0 CHECK (warm BETWEEN -2 AND 2),
    teasing INTEGER NOT NULL DEFAULT 0 CHECK (teasing BETWEEN -2 AND 2),
    proactive INTEGER NOT NULL DEFAULT 0 CHECK (proactive BETWEEN -2 AND 2),
    catchphrase INTEGER NOT NULL DEFAULT 0 CHECK (catchphrase BETWEEN -2 AND 2),
    direct INTEGER NOT NULL DEFAULT 0 CHECK (direct BETWEEN -2 AND 2),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    PRIMARY KEY (owner_user_id, scope_group_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS persona_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
    reply_sha256 TEXT NOT NULL CHECK (length(reply_sha256) = 64),
    scene TEXT NOT NULL CHECK (length(scene) BETWEEN 1 AND 40),
    concise_delta INTEGER NOT NULL DEFAULT 0 CHECK (concise_delta BETWEEN -2 AND 2),
    warm_delta INTEGER NOT NULL DEFAULT 0 CHECK (warm_delta BETWEEN -2 AND 2),
    teasing_delta INTEGER NOT NULL DEFAULT 0 CHECK (teasing_delta BETWEEN -2 AND 2),
    proactive_delta INTEGER NOT NULL DEFAULT 0 CHECK (proactive_delta BETWEEN -2 AND 2),
    catchphrase_delta INTEGER NOT NULL DEFAULT 0 CHECK (catchphrase_delta BETWEEN -2 AND 2),
    direct_delta INTEGER NOT NULL DEFAULT 0 CHECK (direct_delta BETWEEN -2 AND 2),
    positive INTEGER NOT NULL DEFAULT 0 CHECK (positive IN (0, 1)),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_persona_feedback_scope
    ON persona_feedback_events (owner_user_id, scope_group_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY NOT NULL
        CHECK (length(id) BETWEEN 1 AND 128),
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    group_id INTEGER
        CHECK (group_id IS NULL OR group_id > 0),
    market_name TEXT NOT NULL
        CHECK (length(market_name) BETWEEN 1 AND 100),
    target_price_cents INTEGER NOT NULL
        CHECK (target_price_cents > 0),
    direction TEXT NOT NULL DEFAULT 'above'
        CHECK (direction IN ('above', 'below')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    evaluation_failures INTEGER NOT NULL DEFAULT 0
        CHECK (evaluation_failures >= 0),
    next_retry_at INTEGER
        CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    last_evaluation_error TEXT
        CHECK (last_evaluation_error IS NULL OR length(last_evaluation_error) <= 1000),
    last_evaluated_at INTEGER
        CHECK (last_evaluated_at IS NULL OR last_evaluated_at >= 0),
    last_observed_price_cents INTEGER
        CHECK (last_observed_price_cents IS NULL OR last_observed_price_cents > 0),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0),
    failed_at INTEGER
        CHECK (failed_at IS NULL OR failed_at >= 0),
    message_id TEXT
        CHECK (message_id IS NULL OR length(message_id) <= 200),
    CHECK (
        (status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'delivered'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NOT NULL
            AND failed_at IS NULL)
        OR (status = 'failed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND next_retry_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_alerts_owner
    ON alerts (owner_user_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_alerts_pending_due
    ON alerts (next_retry_at, created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_alerts_claimed_lease
    ON alerts (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS idx_alerts_failed_at
    ON alerts (failed_at)
    WHERE status = 'failed';

CREATE TABLE IF NOT EXISTS watchlist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    display_name TEXT NOT NULL
        CHECK (length(display_name) BETWEEN 1 AND 120),
    normalized_name TEXT NOT NULL
        CHECK (length(normalized_name) BETWEEN 1 AND 240),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    UNIQUE (owner_user_id, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_watchlist_items_created
    ON watchlist_items (owner_user_id, id);

CREATE TABLE IF NOT EXISTS scheduler_settings (
    singleton INTEGER PRIMARY KEY NOT NULL DEFAULT 1
        CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1)),
    warm_cache_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (warm_cache_enabled IN (0, 1)),
    alerts_enabled INTEGER NOT NULL DEFAULT 1
        CHECK (alerts_enabled IN (0, 1)),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
        CHECK (length(timezone) BETWEEN 1 AND 100),
    version INTEGER NOT NULL DEFAULT 1
        CHECK (version > 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0)
);

CREATE TABLE IF NOT EXISTS scheduler_groups (
    group_id INTEGER PRIMARY KEY NOT NULL
        CHECK (group_id > 0)
);

CREATE TABLE IF NOT EXISTS scheduler_times (
    time_hhmm TEXT PRIMARY KEY NOT NULL
        CHECK (
            length(time_hhmm) = 5
            AND substr(time_hhmm, 3, 1) = ':'
            AND time_hhmm GLOB '[0-2][0-9]:[0-5][0-9]'
            AND CAST(substr(time_hhmm, 1, 2) AS INTEGER) BETWEEN 0 AND 23
            AND CAST(substr(time_hhmm, 4, 2) AS INTEGER) BETWEEN 0 AND 59
        )
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS broadcast_deliveries (
    schedule_key TEXT NOT NULL
        CHECK (length(schedule_key) BETWEEN 1 AND 200),
    group_id INTEGER NOT NULL
        CHECK (group_id > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    next_retry_at INTEGER
        CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    payload_text TEXT
        CHECK (payload_text IS NULL OR length(payload_text) <= 4000),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0),
    failed_at INTEGER
        CHECK (failed_at IS NULL OR failed_at >= 0),
    message_id TEXT
        CHECK (message_id IS NULL OR length(message_id) <= 200),
    PRIMARY KEY (schedule_key, group_id),
    CHECK (
        (status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'delivered'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NOT NULL
            AND failed_at IS NULL)
        OR (status = 'failed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND next_retry_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_pending_due
    ON broadcast_deliveries (next_retry_at, created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_claimed_lease
    ON broadcast_deliveries (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_failed_at
    ON broadcast_deliveries (failed_at)
    WHERE status = 'failed';
CREATE INDEX IF NOT EXISTS idx_broadcast_deliveries_delivered_at
    ON broadcast_deliveries (delivered_at, schedule_key, group_id)
    WHERE status = 'delivered';

CREATE TABLE IF NOT EXISTS background_job_runs (
    run_key TEXT PRIMARY KEY NOT NULL
        CHECK (length(run_key) BETWEEN 1 AND 240),
    job_name TEXT NOT NULL
        CHECK (length(job_name) BETWEEN 1 AND 100),
    status TEXT NOT NULL
        CHECK (status IN ('claimed', 'completed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    completed_at INTEGER
        CHECK (completed_at IS NULL OR completed_at >= 0),
    CHECK (
        (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND completed_at IS NULL)
        OR (status = 'completed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND completed_at IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_background_job_runs_claimed_lease
    ON background_job_runs (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS idx_background_job_runs_completed_at
    ON background_job_runs (completed_at, run_key)
    WHERE status = 'completed';

CREATE TABLE IF NOT EXISTS feature_permissions (
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    feature TEXT NOT NULL CHECK (length(feature) BETWEEN 1 AND 64),
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    PRIMARY KEY (user_id, feature)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    group_id INTEGER NOT NULL DEFAULT 0 CHECK (group_id >= 0),
    feature TEXT NOT NULL CHECK (length(feature) BETWEEN 1 AND 64),
    command TEXT NOT NULL CHECK (length(command) BETWEEN 1 AND 200),
    input_text TEXT NOT NULL CHECK (length(input_text) <= 4000),
    output_text TEXT CHECK (output_text IS NULL OR length(output_text) <= 8000),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_usage_log_user_created
    ON usage_log (user_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_usage_log_feature_created
    ON usage_log (feature, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS learning_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    kind TEXT NOT NULL
        CHECK (kind IN ('preference', 'style', 'knowledge', 'cultural')),
    content TEXT NOT NULL
        CHECK (length(content) BETWEEN 2 AND 300),
    evidence TEXT NOT NULL
        CHECK (length(evidence) <= 400),
    confidence INTEGER NOT NULL DEFAULT 1
        CHECK (confidence BETWEEN 1 AND 5),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'archived', 'deprecated')),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight >= 0),
    observed_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (observed_at >= 0),
    valid_from INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (valid_from >= 0),
    valid_until INTEGER
        CHECK (valid_until IS NULL OR valid_until >= 0),
    hit_count INTEGER NOT NULL DEFAULT 0
        CHECK (hit_count >= 0),
    last_hit_at INTEGER
        CHECK (last_hit_at IS NULL OR last_hit_at >= 0),
    is_protected INTEGER NOT NULL DEFAULT 0
        CHECK (is_protected IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_learning_insights_user
    ON learning_insights (user_id, scope_group_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_learning_insights_status
    ON learning_insights (status, created_at);

CREATE TABLE IF NOT EXISTS proactive_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    kind TEXT NOT NULL
        CHECK (kind IN ('morning_optin', 'morning', 'dip')),
    dedupe_key TEXT NOT NULL
        CHECK (length(dedupe_key) BETWEEN 1 AND 160),
    status TEXT NOT NULL
        CHECK (status IN ('on', 'off', 'delivered', 'skipped', 'failed')),
    detail TEXT
        CHECK (detail IS NULL OR length(detail) <= 400),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    UNIQUE (user_id, kind, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_user
    ON proactive_deliveries (user_id, created_at);

CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(post_id) BETWEEN 3 AND 40),
    subreddit TEXT NOT NULL
        CHECK (length(subreddit) BETWEEN 2 AND 40),
    title TEXT NOT NULL
        CHECK (length(title) BETWEEN 1 AND 500),
    summary TEXT NOT NULL DEFAULT ''
        CHECK (length(summary) <= 300),
    author TEXT NOT NULL DEFAULT ''
        CHECK (length(author) <= 120),
    url TEXT NOT NULL
        CHECK (length(url) BETWEEN 8 AND 500),
    published_at INTEGER NOT NULL
        CHECK (published_at >= 0),
    first_seen_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (first_seen_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_seen
    ON reddit_posts (first_seen_at);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_undelivered
    ON reddit_posts (delivered_at, published_at);
"""

_INVENTORY_MONITOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_monitor_accounts (
    steam_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(steam_id) = 17 AND steam_id NOT GLOB '*[^0-9]*'),
    nickname TEXT NOT NULL
        CHECK (length(nickname) BETWEEN 1 AND 100),
    snapshot_json TEXT NOT NULL
        CHECK (length(snapshot_json) BETWEEN 2 AND 4000000),
    snapshot_hash TEXT NOT NULL
        CHECK (length(snapshot_hash) = 64),
    source_time INTEGER NOT NULL
        CHECK (source_time >= 0),
    last_success_at INTEGER NOT NULL
        CHECK (last_success_at >= 0),
    next_check_at INTEGER NOT NULL
        CHECK (next_check_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    consecutive_failures INTEGER NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL
        CHECK (updated_at >= 0),
    CHECK (
        (lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_accounts_due
    ON inventory_monitor_accounts (next_check_at, steam_id);
CREATE INDEX IF NOT EXISTS idx_inventory_monitor_accounts_lease
    ON inventory_monitor_accounts (lease_expires_at)
    WHERE lease_owner IS NOT NULL;

CREATE TABLE IF NOT EXISTS inventory_monitor_subscriptions (
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    destination_type TEXT NOT NULL
        CHECK (destination_type IN ('group', 'private')),
    destination_id INTEGER NOT NULL
        CHECK (destination_id > 0),
    steam_id TEXT NOT NULL
        REFERENCES inventory_monitor_accounts (steam_id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0),
    PRIMARY KEY (owner_user_id, destination_type, destination_id, steam_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_subscriptions_steam
    ON inventory_monitor_subscriptions (steam_id, owner_user_id);

CREATE TABLE IF NOT EXISTS inventory_monitor_item_history (
    steam_id TEXT NOT NULL
        REFERENCES inventory_monitor_accounts (steam_id) ON DELETE CASCADE,
    item_key TEXT NOT NULL
        CHECK (length(item_key) BETWEEN 3 AND 200),
    item_json TEXT NOT NULL
        CHECK (length(item_json) BETWEEN 2 AND 4000),
    first_seen_at INTEGER NOT NULL
        CHECK (first_seen_at >= 0),
    last_seen_at INTEGER NOT NULL
        CHECK (last_seen_at >= 0),
    last_missing_at INTEGER
        CHECK (last_missing_at IS NULL OR last_missing_at >= 0),
    market_hash_name TEXT
        CHECK (market_hash_name IS NULL OR length(market_hash_name) <= 256),
    paint_index INTEGER
        CHECK (paint_index IS NULL OR paint_index >= 0),
    paintwear REAL
        CHECK (paintwear IS NULL OR (paintwear >= 0 AND paintwear <= 1)),
    pattern_index INTEGER
        CHECK (pattern_index IS NULL OR pattern_index >= 0),
    stickers_json TEXT
        CHECK (stickers_json IS NULL OR length(stickers_json) <= 2000),
    PRIMARY KEY (steam_id, item_key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_history_missing
    ON inventory_monitor_item_history (steam_id, last_missing_at)
    WHERE last_missing_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_history_anchors
    ON inventory_monitor_item_history (market_hash_name, paintwear)
    WHERE paintwear IS NOT NULL;

CREATE TABLE IF NOT EXISTS inventory_monitor_events (
    id TEXT PRIMARY KEY NOT NULL
        CHECK (length(id) BETWEEN 1 AND 128),
    steam_id TEXT NOT NULL
        REFERENCES inventory_monitor_accounts (steam_id) ON DELETE CASCADE,
    previous_hash TEXT NOT NULL
        CHECK (length(previous_hash) = 64),
    new_hash TEXT NOT NULL
        CHECK (length(new_hash) = 64),
    message_text TEXT NOT NULL
        CHECK (length(message_text) BETWEEN 1 AND 4000),
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_events_created
    ON inventory_monitor_events (created_at, id);

CREATE TABLE IF NOT EXISTS inventory_monitor_deliveries (
    event_id TEXT NOT NULL
        REFERENCES inventory_monitor_events (id) ON DELETE CASCADE,
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    destination_type TEXT NOT NULL
        CHECK (destination_type IN ('group', 'private')),
    destination_id INTEGER NOT NULL
        CHECK (destination_id > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    next_retry_at INTEGER
        CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL
        CHECK (updated_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0),
    failed_at INTEGER
        CHECK (failed_at IS NULL OR failed_at >= 0),
    message_id TEXT
        CHECK (message_id IS NULL OR length(message_id) <= 200),
    PRIMARY KEY (event_id, owner_user_id, destination_type, destination_id),
    CHECK (
        (status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'delivered'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NOT NULL
            AND failed_at IS NULL)
        OR (status = 'failed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND next_retry_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_deliveries_due
    ON inventory_monitor_deliveries (next_retry_at, created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_inventory_monitor_deliveries_lease
    ON inventory_monitor_deliveries (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX IF NOT EXISTS idx_inventory_monitor_deliveries_failed
    ON inventory_monitor_deliveries (failed_at)
    WHERE status = 'failed';
"""

_INVENTORY_FLOAT_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_monitor_float_cache (
    inspect_url_hash TEXT PRIMARY KEY NOT NULL
        CHECK (length(inspect_url_hash) = 64),
    market_hash_name TEXT NOT NULL
        CHECK (length(market_hash_name) BETWEEN 1 AND 256),
    paintwear REAL NOT NULL
        CHECK (paintwear >= 0 AND paintwear <= 1),
    paint_index INTEGER
        CHECK (paint_index IS NULL OR paint_index >= 0),
    pattern_index INTEGER
        CHECK (pattern_index IS NULL OR pattern_index >= 0),
    stickers_json TEXT
        CHECK (stickers_json IS NULL OR length(stickers_json) <= 2000),
    fetched_at INTEGER NOT NULL
        CHECK (fetched_at >= 0),
    http_status INTEGER NOT NULL
        CHECK (http_status >= 0)
);
"""

_INVENTORY_REPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_monitor_reports (
    steam_id TEXT NOT NULL
        REFERENCES inventory_monitor_accounts (steam_id) ON DELETE CASCADE,
    generated_at INTEGER NOT NULL
        CHECK (generated_at >= 0),
    model TEXT NOT NULL
        CHECK (length(model) BETWEEN 1 AND 200),
    tokens_used INTEGER NOT NULL DEFAULT 0
        CHECK (tokens_used >= 0),
    payload_json TEXT NOT NULL
        CHECK (length(payload_json) BETWEEN 2 AND 20000),
    PRIMARY KEY (steam_id, generated_at)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_inventory_monitor_reports_latest
    ON inventory_monitor_reports (steam_id, generated_at DESC);
"""

#: V0.4 P0 additions (schema v16): chat history summaries, persona
#: relationships and the CSQAQ good_id mapping cache.  All are pure
#: additions; existing tables and rows are untouched.  ``persona_slang``
#: was created by this step historically and is kept as a separate
#: constant so the offline chain still reproduces the exact v16 shape
#: before the v17 step rebuilds it.
_V16_ADDITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL
        CHECK (length(session_key) BETWEEN 1 AND 256),
    summary TEXT NOT NULL
        CHECK (length(summary) BETWEEN 2 AND 2000),
    covers_from_id INTEGER NOT NULL
        CHECK (covers_from_id >= 0),
    covers_to_id INTEGER NOT NULL
        CHECK (covers_to_id >= covers_from_id),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_session_summaries_session
    ON session_summaries (session_key, covers_to_id DESC);

CREATE TABLE IF NOT EXISTS persona_relationships (
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    intimacy REAL NOT NULL DEFAULT 0
        CHECK (intimacy >= 0 AND intimacy <= 100),
    interaction_count INTEGER NOT NULL DEFAULT 0
        CHECK (interaction_count >= 0),
    accepted_insight_count INTEGER NOT NULL DEFAULT 0
        CHECK (accepted_insight_count >= 0),
    style_feedback_count INTEGER NOT NULL DEFAULT 0
        CHECK (style_feedback_count >= 0),
    last_interaction_at INTEGER
        CHECK (last_interaction_at IS NULL OR last_interaction_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    PRIMARY KEY (user_id, scope_group_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS csqaq_good_id_cache (
    market_hash_name TEXT PRIMARY KEY NOT NULL
        CHECK (length(market_hash_name) BETWEEN 1 AND 256),
    good_id INTEGER NOT NULL
        CHECK (good_id > 0),
    csqaq_name TEXT NOT NULL
        CHECK (length(csqaq_name) BETWEEN 1 AND 200),
    resolved_at INTEGER NOT NULL
        CHECK (resolved_at >= 0)
) WITHOUT ROWID;
"""

_PERSONA_SLANG_V16_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_slang (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    created_by INTEGER NOT NULL
        CHECK (created_by > 0),
    term TEXT NOT NULL
        CHECK (length(term) BETWEEN 1 AND 24),
    meaning TEXT NOT NULL
        CHECK (length(meaning) BETWEEN 1 AND 200),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    confidence INTEGER NOT NULL DEFAULT 2
        CHECK (confidence BETWEEN 1 AND 5),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_persona_slang_scope
    ON persona_slang (scope_group_id, status, id DESC);
"""

_V17_ADDITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_slang (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    created_by INTEGER NOT NULL
        CHECK (created_by > 0),
    term TEXT NOT NULL
        CHECK (length(term) BETWEEN 1 AND 24),
    meaning TEXT NOT NULL
        CHECK (length(meaning) BETWEEN 1 AND 200),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    confidence INTEGER NOT NULL DEFAULT 2
        CHECK (confidence BETWEEN 1 AND 5),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_persona_slang_scope
    ON persona_slang (scope_group_id, status, id DESC);

CREATE TABLE IF NOT EXISTS memory_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    from_insight_id INTEGER NOT NULL
        CHECK (from_insight_id > 0),
    to_insight_id INTEGER NOT NULL
        CHECK (to_insight_id > 0),
    relation TEXT NOT NULL
        CHECK (length(relation) BETWEEN 2 AND 60),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    UNIQUE (from_insight_id, to_insight_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_memory_links_scope
    ON memory_links (user_id, scope_group_id, created_at DESC);

CREATE TABLE IF NOT EXISTS image_descriptions (
    image_sha256 TEXT PRIMARY KEY NOT NULL
        CHECK (length(image_sha256) = 64),
    description TEXT NOT NULL
        CHECK (length(description) BETWEEN 2 AND 500),
    tags_json TEXT NOT NULL DEFAULT '[]'
        CHECK (length(tags_json) <= 300),
    model TEXT NOT NULL
        CHECK (length(model) BETWEEN 1 AND 100),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS persona_eval_scores (
    run_id TEXT NOT NULL
        CHECK (length(run_id) BETWEEN 8 AND 64),
    dimension TEXT NOT NULL
        CHECK (length(dimension) BETWEEN 2 AND 60),
    score REAL NOT NULL
        CHECK (score >= 0 AND score <= 5),
    evidence TEXT
        CHECK (evidence IS NULL OR length(evidence) <= 500),
    calibrated INTEGER NOT NULL
        CHECK (calibrated IN (0, 1)),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    PRIMARY KEY (run_id, dimension)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_persona_eval_scores_created
    ON persona_eval_scores (created_at DESC);

CREATE TABLE IF NOT EXISTS llm_usage_daily (
    day TEXT NOT NULL
        CHECK (length(day) = 10 AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    provider TEXT NOT NULL
        CHECK (length(provider) BETWEEN 1 AND 40),
    model TEXT NOT NULL
        CHECK (length(model) BETWEEN 1 AND 100),
    requests INTEGER NOT NULL DEFAULT 0
        CHECK (requests >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0
        CHECK (output_tokens >= 0),
    PRIMARY KEY (day, provider, model)
) WITHOUT ROWID;
"""

#: V0.5 hybrid-retrieval additions (schema v18).  The FTS5 trigram index is a
#: standalone virtual table whose ``rowid`` mirrors ``learning_insights.id``;
#: three triggers keep it in sync with every insert, content update and
#: delete regardless of which code path wrote the row.  ``insight_embeddings``
#: stores one 1024-dim float32 vector (4096 bytes) per insight so semantic
#: recall works with brute-force cosine at memory scale.
_V18_ADDITIONS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS learning_insights_fts USING fts5(
    content,
    tokenize = 'trigram'
);

CREATE TRIGGER IF NOT EXISTS learning_insights_fts_ai
AFTER INSERT ON learning_insights BEGIN
    INSERT INTO learning_insights_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS learning_insights_fts_ad
AFTER DELETE ON learning_insights BEGIN
    DELETE FROM learning_insights_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS learning_insights_fts_au
AFTER UPDATE OF content ON learning_insights BEGIN
    DELETE FROM learning_insights_fts WHERE rowid = old.id;
    INSERT INTO learning_insights_fts (rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS insight_embeddings (
    insight_id INTEGER PRIMARY KEY
        REFERENCES learning_insights (id) ON DELETE CASCADE,
    embedding BLOB NOT NULL
        CHECK (length(embedding) = 4096),
    model TEXT NOT NULL
        CHECK (length(model) BETWEEN 1 AND 100),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_insight_embeddings_model
    ON insight_embeddings (model, created_at);
"""

#: V0.5 tier-3 group interjection (schema v19).  ``group_pulse_log`` is the
#: only durable state of the opt-in proactive interjection feature.  It never
#: stores message text — just the group id, the trigger reason
#: (``willing``/``cold_start``), a SHA-256 fingerprint of the in-memory ring
#: buffer (for audit/dedupe) and a timestamp.  Per-group opt-in is derived
#: from the same table (latest row per group flips ``enabled``), matching the
#: proactive-deliveries ledger pattern.
_V19_ADDITIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_pulse_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL
        CHECK (group_id > 0),
    enabled INTEGER NOT NULL DEFAULT 0
        CHECK (enabled IN (0, 1)),
    reason TEXT
        CHECK (reason IS NULL OR reason IN ('willing', 'cold_start')),
    fingerprint TEXT
        CHECK (fingerprint IS NULL OR length(fingerprint) = 64),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_group_pulse_log_group_day
    ON group_pulse_log (group_id, created_at);
"""

_SCHEMA += _INVENTORY_MONITOR_SCHEMA
_SCHEMA += _INVENTORY_FLOAT_CACHE_SCHEMA
_SCHEMA += _INVENTORY_REPORTS_SCHEMA
_SCHEMA += _V16_ADDITIONS_SCHEMA
_SCHEMA += _V17_ADDITIONS_SCHEMA
_SCHEMA += _V18_ADDITIONS_SCHEMA
_SCHEMA += _V19_ADDITIONS_SCHEMA

_MIGRATION_1_TO_2 = """
ALTER TABLE alerts RENAME TO alerts_v1;

CREATE TABLE alerts (
    id TEXT PRIMARY KEY NOT NULL
        CHECK (length(id) BETWEEN 1 AND 128),
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    group_id INTEGER
        CHECK (group_id IS NULL OR group_id > 0),
    market_name TEXT NOT NULL
        CHECK (length(market_name) BETWEEN 1 AND 100),
    target_price_cents INTEGER NOT NULL
        CHECK (target_price_cents > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    next_retry_at INTEGER
        CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    last_observed_price_cents INTEGER
        CHECK (last_observed_price_cents IS NULL OR last_observed_price_cents > 0),
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL
        CHECK (updated_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0),
    failed_at INTEGER
        CHECK (failed_at IS NULL OR failed_at >= 0),
    message_id TEXT
        CHECK (message_id IS NULL OR length(message_id) <= 200),
    CHECK (
        (status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'delivered'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NOT NULL
            AND failed_at IS NULL)
        OR (status = 'failed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND next_retry_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NOT NULL)
    )
);

INSERT INTO alerts (
    id, owner_user_id, group_id, market_name, target_price_cents,
    status, attempts, next_retry_at, lease_owner, lease_expires_at,
    last_error, last_observed_price_cents, created_at, updated_at,
    delivered_at, failed_at, message_id
)
SELECT
    id, owner_user_id, group_id, market_name, target_price_cents,
    status, attempts, next_retry_at, lease_owner, lease_expires_at,
    last_error, last_observed_price_cents, created_at, updated_at,
    delivered_at, NULL, message_id
FROM alerts_v1;

DROP TABLE alerts_v1;
CREATE INDEX idx_alerts_owner ON alerts (owner_user_id, created_at, id);
CREATE INDEX idx_alerts_pending_due ON alerts (next_retry_at, created_at)
    WHERE status = 'pending';
CREATE INDEX idx_alerts_claimed_lease ON alerts (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX idx_alerts_failed_at ON alerts (failed_at)
    WHERE status = 'failed';

ALTER TABLE broadcast_deliveries RENAME TO broadcast_deliveries_v1;

CREATE TABLE broadcast_deliveries (
    schedule_key TEXT NOT NULL
        CHECK (length(schedule_key) BETWEEN 1 AND 200),
    group_id INTEGER NOT NULL
        CHECK (group_id > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    next_retry_at INTEGER
        CHECK (next_retry_at IS NULL OR next_retry_at >= 0),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at INTEGER
        CHECK (lease_expires_at IS NULL OR lease_expires_at >= 0),
    last_error TEXT
        CHECK (last_error IS NULL OR length(last_error) <= 1000),
    payload_text TEXT
        CHECK (payload_text IS NULL OR length(payload_text) <= 4000),
    created_at INTEGER NOT NULL
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL
        CHECK (updated_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0),
    failed_at INTEGER
        CHECK (failed_at IS NULL OR failed_at >= 0),
    message_id TEXT
        CHECK (message_id IS NULL OR length(message_id) <= 200),
    PRIMARY KEY (schedule_key, group_id),
    CHECK (
        (status = 'pending'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND delivered_at IS NULL
            AND failed_at IS NULL)
        OR (status = 'delivered'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND delivered_at IS NOT NULL
            AND failed_at IS NULL)
        OR (status = 'failed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
            AND next_retry_at IS NULL
            AND delivered_at IS NULL
            AND failed_at IS NOT NULL)
    )
) WITHOUT ROWID;

INSERT INTO broadcast_deliveries (
    schedule_key, group_id, status, attempts, next_retry_at,
    lease_owner, lease_expires_at, last_error, payload_text,
    created_at, updated_at, delivered_at, failed_at, message_id
)
SELECT
    schedule_key, group_id, status, attempts, next_retry_at,
    lease_owner, lease_expires_at, last_error, payload_text,
    created_at, updated_at, delivered_at, NULL, message_id
FROM broadcast_deliveries_v1;

DROP TABLE broadcast_deliveries_v1;
CREATE INDEX idx_broadcast_deliveries_pending_due
    ON broadcast_deliveries (next_retry_at, created_at)
    WHERE status = 'pending';
CREATE INDEX idx_broadcast_deliveries_claimed_lease
    ON broadcast_deliveries (lease_expires_at)
    WHERE status = 'claimed';
CREATE INDEX idx_broadcast_deliveries_failed_at
    ON broadcast_deliveries (failed_at)
    WHERE status = 'failed';
CREATE INDEX idx_broadcast_deliveries_delivered_at
    ON broadcast_deliveries (delivered_at, schedule_key, group_id)
    WHERE status = 'delivered';
"""

_MIGRATION_2_TO_3 = _INVENTORY_MONITOR_SCHEMA

_MIGRATION_3_TO_4 = """
ALTER TABLE alerts ADD COLUMN direction TEXT NOT NULL DEFAULT 'above'
    CHECK (direction IN ('above', 'below'));
"""

_MIGRATION_4_TO_5 = """
CREATE TABLE persona_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL
        CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    memory_kind TEXT NOT NULL
        CHECK (memory_kind IN ('nickname', 'note')),
    content TEXT NOT NULL
        CHECK (length(content) BETWEEN 1 AND 200),
    normalized_content TEXT NOT NULL
        CHECK (length(normalized_content) BETWEEN 1 AND 200),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    UNIQUE (owner_user_id, scope_group_id, memory_kind, normalized_content)
);

CREATE INDEX idx_persona_memories_scope
    ON persona_memories (owner_user_id, scope_group_id, updated_at DESC, id DESC);
CREATE UNIQUE INDEX idx_persona_memories_nickname
    ON persona_memories (owner_user_id, scope_group_id)
    WHERE memory_kind = 'nickname';
"""

_MIGRATION_5_TO_6 = """
CREATE TABLE persona_style_profiles (
    owner_user_id INTEGER NOT NULL CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
    learning_enabled INTEGER NOT NULL DEFAULT 0 CHECK (learning_enabled IN (0, 1)),
    concise INTEGER NOT NULL DEFAULT 0 CHECK (concise BETWEEN -2 AND 2),
    warm INTEGER NOT NULL DEFAULT 0 CHECK (warm BETWEEN -2 AND 2),
    teasing INTEGER NOT NULL DEFAULT 0 CHECK (teasing BETWEEN -2 AND 2),
    proactive INTEGER NOT NULL DEFAULT 0 CHECK (proactive BETWEEN -2 AND 2),
    catchphrase INTEGER NOT NULL DEFAULT 0 CHECK (catchphrase BETWEEN -2 AND 2),
    direct INTEGER NOT NULL DEFAULT 0 CHECK (direct BETWEEN -2 AND 2),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    PRIMARY KEY (owner_user_id, scope_group_id)
) WITHOUT ROWID;

CREATE TABLE persona_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL CHECK (owner_user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0 CHECK (scope_group_id >= 0),
    reply_sha256 TEXT NOT NULL CHECK (length(reply_sha256) = 64),
    scene TEXT NOT NULL CHECK (length(scene) BETWEEN 1 AND 40),
    concise_delta INTEGER NOT NULL DEFAULT 0 CHECK (concise_delta BETWEEN -2 AND 2),
    warm_delta INTEGER NOT NULL DEFAULT 0 CHECK (warm_delta BETWEEN -2 AND 2),
    teasing_delta INTEGER NOT NULL DEFAULT 0 CHECK (teasing_delta BETWEEN -2 AND 2),
    proactive_delta INTEGER NOT NULL DEFAULT 0 CHECK (proactive_delta BETWEEN -2 AND 2),
    catchphrase_delta INTEGER NOT NULL DEFAULT 0 CHECK (catchphrase_delta BETWEEN -2 AND 2),
    direct_delta INTEGER NOT NULL DEFAULT 0 CHECK (direct_delta BETWEEN -2 AND 2),
    positive INTEGER NOT NULL DEFAULT 0 CHECK (positive IN (0, 1)),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX idx_persona_feedback_scope
    ON persona_feedback_events (owner_user_id, scope_group_id, created_at DESC, id DESC);
"""

_MIGRATION_6_TO_7 = """
ALTER TABLE alerts ADD COLUMN evaluation_failures INTEGER NOT NULL DEFAULT 0
    CHECK (evaluation_failures >= 0);
ALTER TABLE alerts ADD COLUMN last_evaluation_error TEXT
    CHECK (last_evaluation_error IS NULL OR length(last_evaluation_error) <= 1000);
ALTER TABLE alerts ADD COLUMN last_evaluated_at INTEGER
    CHECK (last_evaluated_at IS NULL OR last_evaluated_at >= 0);

UPDATE alerts
SET evaluation_failures = attempts,
    last_evaluation_error = last_error,
    last_evaluated_at = updated_at,
    status = 'pending',
    attempts = 0,
    next_retry_at = CAST(strftime('%s', 'now') AS INTEGER),
    lease_owner = NULL,
    lease_expires_at = NULL,
    last_error = NULL,
    failed_at = NULL,
    updated_at = CAST(strftime('%s', 'now') AS INTEGER)
WHERE status = 'failed' AND last_error = 'AlertEvaluationError';
"""

_MIGRATION_7_TO_8 = """
CREATE TABLE feature_permissions (
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    feature TEXT NOT NULL CHECK (length(feature) BETWEEN 1 AND 64),
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    PRIMARY KEY (user_id, feature)
) WITHOUT ROWID;

CREATE TABLE usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL CHECK (user_id > 0),
    group_id INTEGER NOT NULL DEFAULT 0 CHECK (group_id >= 0),
    feature TEXT NOT NULL CHECK (length(feature) BETWEEN 1 AND 64),
    command TEXT NOT NULL CHECK (length(command) BETWEEN 1 AND 200),
    input_text TEXT NOT NULL CHECK (length(input_text) <= 4000),
    output_text TEXT CHECK (output_text IS NULL OR length(output_text) <= 8000),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0)
);

CREATE INDEX idx_usage_log_user_created
    ON usage_log (user_id, created_at DESC, id DESC);
CREATE INDEX idx_usage_log_feature_created
    ON usage_log (feature, created_at DESC, id DESC);
"""

_MIGRATION_8_TO_9 = """
CREATE TABLE IF NOT EXISTS learning_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    kind TEXT NOT NULL
        CHECK (kind IN ('preference', 'style', 'knowledge', 'cultural')),
    content TEXT NOT NULL
        CHECK (length(content) BETWEEN 2 AND 300),
    evidence TEXT NOT NULL
        CHECK (length(evidence) <= 400),
    confidence INTEGER NOT NULL DEFAULT 1
        CHECK (confidence BETWEEN 1 AND 5),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'archived')),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight >= 0)
);

CREATE INDEX IF NOT EXISTS idx_learning_insights_user
    ON learning_insights (user_id, scope_group_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_learning_insights_status
    ON learning_insights (status, created_at);
"""

_MIGRATION_9_TO_10 = """
CREATE TABLE IF NOT EXISTS proactive_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    kind TEXT NOT NULL
        CHECK (kind IN ('morning_optin', 'morning', 'dip')),
    dedupe_key TEXT NOT NULL
        CHECK (length(dedupe_key) BETWEEN 1 AND 160),
    status TEXT NOT NULL
        CHECK (status IN ('on', 'off', 'delivered', 'skipped', 'failed')),
    detail TEXT
        CHECK (detail IS NULL OR length(detail) <= 400),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    UNIQUE (user_id, kind, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_proactive_deliveries_user
    ON proactive_deliveries (user_id, created_at);
"""

_MIGRATION_10_TO_11 = """
CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id TEXT PRIMARY KEY NOT NULL
        CHECK (length(post_id) BETWEEN 3 AND 40),
    subreddit TEXT NOT NULL
        CHECK (length(subreddit) BETWEEN 2 AND 40),
    title TEXT NOT NULL
        CHECK (length(title) BETWEEN 1 AND 500),
    author TEXT NOT NULL DEFAULT ''
        CHECK (length(author) <= 120),
    url TEXT NOT NULL
        CHECK (length(url) BETWEEN 8 AND 500),
    published_at INTEGER NOT NULL
        CHECK (published_at >= 0),
    first_seen_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (first_seen_at >= 0),
    delivered_at INTEGER
        CHECK (delivered_at IS NULL OR delivered_at >= 0)
);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_seen
    ON reddit_posts (first_seen_at);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_undelivered
    ON reddit_posts (delivered_at, published_at);
"""

_MIGRATION_11_TO_12 = """
ALTER TABLE reddit_posts ADD COLUMN summary TEXT NOT NULL DEFAULT ''
    CHECK (length(summary) <= 300);
"""


def _migrate_11_to_12(transaction: Transaction) -> None:
    """Add the ``summary`` column when it is not already present.

    A callable migration is used because ``ALTER TABLE ADD COLUMN`` is not
    idempotent: the offline migration command and the test fixtures both
    re-run the chain against databases that may already contain the column.
    """

    rows = transaction.read("PRAGMA table_info(reddit_posts)")
    if any(str(row.get("name")) == "summary" for row in rows):
        return
    transaction.write(_MIGRATION_11_TO_12)


def _migrate_12_to_13(transaction: Transaction) -> None:
    """Add the inventory-account failure counter used for backoff and parking.

    Callable for the same non-idempotency reason as :func:`_migrate_11_to_12`.
    """

    rows = transaction.read("PRAGMA table_info(inventory_monitor_accounts)")
    if any(str(row.get("name")) == "consecutive_failures" for row in rows):
        return
    transaction.write(
        """
        ALTER TABLE inventory_monitor_accounts ADD COLUMN consecutive_failures
            INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
        """
    )


_INVENTORY_ANCHOR_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "market_hash_name",
        "TEXT CHECK (market_hash_name IS NULL OR length(market_hash_name) <= 256)",
    ),
    ("paint_index", "INTEGER CHECK (paint_index IS NULL OR paint_index >= 0)"),
    ("paintwear", "REAL CHECK (paintwear IS NULL OR (paintwear >= 0 AND paintwear <= 1))"),
    ("pattern_index", "INTEGER CHECK (pattern_index IS NULL OR pattern_index >= 0)"),
    ("stickers_json", "TEXT CHECK (stickers_json IS NULL OR length(stickers_json) <= 2000)"),
)


def _migrate_13_to_14(transaction: Transaction) -> None:
    """Add unique-wear anchor columns, their partial index, and the float cache.

    Callable because each ``ALTER TABLE ADD COLUMN`` runs only when the column
    is still missing; re-running the chain against a database that already has
    some (or all) anchors must stay safe.
    """

    rows = transaction.read("PRAGMA table_info(inventory_monitor_item_history)")
    present = {str(row.get("name")) for row in rows}
    for column, definition in _INVENTORY_ANCHOR_COLUMNS:
        if column in present:
            continue
        transaction.write(
            f"ALTER TABLE inventory_monitor_item_history ADD COLUMN {column} {definition}"
        )
    transaction.write(
        """
        CREATE INDEX IF NOT EXISTS idx_inventory_monitor_history_anchors
            ON inventory_monitor_item_history (market_hash_name, paintwear)
            WHERE paintwear IS NOT NULL
        """
    )
    StateDatabase._execute_ddl(transaction, _INVENTORY_FLOAT_CACHE_SCHEMA)


def _migrate_14_to_15(transaction: Transaction) -> None:
    """Add the hoard-report table backing the AI profile cache (P1).

    Pure addition of one table plus its index; idempotent via IF NOT EXISTS.
    """

    StateDatabase._execute_ddl(transaction, _INVENTORY_REPORTS_SCHEMA)


def _migrate_15_to_16(transaction: Transaction) -> None:
    """Add the V0.4 P0 tables (summaries / relationships / slang / CSQAQ ids).

    Pure addition of four tables plus their indexes; idempotent via
    IF NOT EXISTS, so a partially upgraded database converges on rerun.
    """

    StateDatabase._execute_ddl(transaction, _V16_ADDITIONS_SCHEMA)
    StateDatabase._execute_ddl(transaction, _PERSONA_SLANG_V16_SCHEMA)


_LEARNING_INSIGHTS_17_REBUILD = """
CREATE TABLE learning_insights_v17 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL
        CHECK (user_id > 0),
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    kind TEXT NOT NULL
        CHECK (kind IN ('preference', 'style', 'knowledge', 'cultural')),
    content TEXT NOT NULL
        CHECK (length(content) BETWEEN 2 AND 300),
    evidence TEXT NOT NULL
        CHECK (length(evidence) <= 400),
    confidence INTEGER NOT NULL DEFAULT 1
        CHECK (confidence BETWEEN 1 AND 5),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected', 'archived', 'deprecated')),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    weight REAL NOT NULL DEFAULT 1.0
        CHECK (weight >= 0),
    observed_at INTEGER NOT NULL
        CHECK (observed_at >= 0),
    valid_from INTEGER NOT NULL
        CHECK (valid_from >= 0),
    valid_until INTEGER
        CHECK (valid_until IS NULL OR valid_until >= 0),
    hit_count INTEGER NOT NULL DEFAULT 0
        CHECK (hit_count >= 0),
    last_hit_at INTEGER
        CHECK (last_hit_at IS NULL OR last_hit_at >= 0),
    is_protected INTEGER NOT NULL DEFAULT 0
        CHECK (is_protected IN (0, 1))
);

INSERT INTO learning_insights_v17 (
    id, user_id, scope_group_id, kind, content, evidence, confidence,
    status, created_at, accepted_at, updated_at, weight,
    observed_at, valid_from, valid_until, hit_count, last_hit_at, is_protected
)
SELECT
    id, user_id, scope_group_id, kind, content, evidence, confidence,
    status, created_at, accepted_at, updated_at, weight,
    created_at, created_at, NULL, 0, NULL, 0
FROM learning_insights;

DROP TABLE learning_insights;

ALTER TABLE learning_insights_v17 RENAME TO learning_insights;

CREATE INDEX IF NOT EXISTS idx_learning_insights_user
    ON learning_insights (user_id, scope_group_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_learning_insights_status
    ON learning_insights (status, created_at);
"""

_PERSONA_SLANG_17_REBUILD = """
CREATE TABLE persona_slang_v17 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_group_id INTEGER NOT NULL DEFAULT 0
        CHECK (scope_group_id >= 0),
    created_by INTEGER NOT NULL
        CHECK (created_by > 0),
    term TEXT NOT NULL
        CHECK (length(term) BETWEEN 1 AND 24),
    meaning TEXT NOT NULL
        CHECK (length(meaning) BETWEEN 1 AND 200),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    confidence INTEGER NOT NULL DEFAULT 2
        CHECK (confidence BETWEEN 1 AND 5),
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (created_at >= 0),
    accepted_at INTEGER
        CHECK (accepted_at IS NULL OR accepted_at >= 0),
    updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
        CHECK (updated_at >= 0),
    enabled INTEGER NOT NULL DEFAULT 1
        CHECK (enabled IN (0, 1))
);

INSERT INTO persona_slang_v17 (
    id, scope_group_id, created_by, term, meaning, status, confidence,
    created_at, accepted_at, updated_at, enabled
)
SELECT
    id, scope_group_id, created_by, term, meaning, status, confidence,
    created_at, accepted_at, updated_at, 1
FROM persona_slang;

DROP TABLE persona_slang;

ALTER TABLE persona_slang_v17 RENAME TO persona_slang;

CREATE INDEX IF NOT EXISTS idx_persona_slang_scope
    ON persona_slang (scope_group_id, status, id DESC);
"""


def _migrate_16_to_17(transaction: Transaction) -> None:
    """V0.5 memory foundations (bi-temporal insights, slang enablement).

    ``learning_insights`` gains bi-temporal validity columns, hit counters,
    a protection flag and a ``deprecated`` soft-delete status; the CHECK
    change requires a table rebuild.  ``persona_slang`` gains a user-driven
    ``enabled`` flag (same rebuild requirement).  Four additive V0.5 tables
    (memory links, image descriptions, persona eval scores, LLM usage)
    follow.  Callable so partially upgraded databases converge on rerun.
    """

    rows = transaction.read("PRAGMA table_info(learning_insights)")
    present = {str(row.get("name")) for row in rows}
    if rows and "observed_at" not in present:
        StateDatabase._execute_ddl(transaction, _LEARNING_INSIGHTS_17_REBUILD)
    rows = transaction.read("PRAGMA table_info(persona_slang)")
    present = {str(row.get("name")) for row in rows}
    if rows and "enabled" not in present:
        StateDatabase._execute_ddl(transaction, _PERSONA_SLANG_17_REBUILD)
    StateDatabase._execute_ddl(transaction, _V17_ADDITIONS_SCHEMA)


def _migrate_17_to_18(transaction: Transaction) -> None:
    """V0.5 hybrid retrieval (FTS5 trigram index + insight embeddings).

    Creates the standalone trigram index whose ``rowid`` mirrors
    ``learning_insights.id`` (kept in sync by three triggers) and backfills
    every existing insight row; the backfill is re-entrant so partially
    upgraded databases converge on rerun.  Also adds the float32 embedding
    store used by the optional semantic channel.
    """

    StateDatabase._execute_ddl(transaction, _V18_ADDITIONS_SCHEMA)
    transaction.write(
        """
        INSERT INTO learning_insights_fts (rowid, content)
        SELECT id, content FROM learning_insights
        WHERE id NOT IN (SELECT rowid FROM learning_insights_fts)
        """
    )


#: V0.5 tier-3 group interjection ledger (schema v19).


def _migrate_18_to_19(transaction: Transaction) -> None:
    """V0.5 tier-3 group interjection ledger (schema v19).

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` so partially upgraded
    databases converge on rerun.  No data backfill is needed — the table
    starts empty and only ever records opt-in flips and interjection
    audits, never message content.
    """

    StateDatabase._execute_ddl(transaction, _V19_ADDITIONS_SCHEMA)


_MIGRATIONS: dict[int, "str | Callable[[Transaction], None]"] = {
    1: _MIGRATION_1_TO_2,
    2: _MIGRATION_2_TO_3,
    3: _MIGRATION_3_TO_4,
    4: _MIGRATION_4_TO_5,
    5: _MIGRATION_5_TO_6,
    6: _MIGRATION_6_TO_7,
    7: _MIGRATION_7_TO_8,
    8: _MIGRATION_8_TO_9,
    9: _MIGRATION_9_TO_10,
    10: _MIGRATION_10_TO_11,
    11: _migrate_11_to_12,
    12: _migrate_12_to_13,
    13: _migrate_13_to_14,
    14: _migrate_14_to_15,
    15: _migrate_15_to_16,
    16: _migrate_16_to_17,
    17: _migrate_17_to_18,
    18: _migrate_18_to_19,
}

T = TypeVar("T")
SQLParameters = Sequence[object] | Mapping[str, object]


class UnsupportedSchemaVersionError(RuntimeError):
    """The database was created by a newer, unsupported application version."""


class SchemaMigrationRequiredError(RuntimeError):
    """The database must be upgraded by the offline migration command."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Metadata returned by a single mutating statement."""

    rowcount: int
    lastrowid: int | None


class Transaction:
    """Synchronous operations available inside one database transaction.

    Instances are only valid for the duration of the callback passed to
    :meth:`StateDatabase.transaction`.  The callback itself runs on the same
    worker thread as the connection.
    """

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def read(
        self,
        sql: str,
        parameters: SQLParameters = (),
    ) -> list[dict[str, Any]]:
        """Execute one query and fully materialize its rows."""

        cursor = self._connection.execute(sql, parameters)
        try:
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def write(
        self,
        sql: str,
        parameters: SQLParameters = (),
    ) -> WriteResult:
        """Execute one mutating statement without committing it early."""

        cursor = self._connection.execute(sql, parameters)
        try:
            return WriteResult(rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)
        finally:
            cursor.close()


class StateDatabase:
    """A small async facade over transactional, short-lived SQLite connections."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        allow_schema_migrations: bool = True,
    ) -> None:
        configured_path = path
        if configured_path is None:
            configured_path = os.getenv("TAFFY_STATE_DB") or DEFAULT_DATABASE_PATH
        self.path = Path(configured_path).expanduser()
        if str(self.path) == ":memory:":
            raise ValueError("StateDatabase requires a file path for independent connections")
        if not self.path.is_absolute():
            self.path = PROJECT_ROOT / self.path
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or busy_timeout_ms <= 0
        ):
            raise ValueError("busy_timeout_ms must be a positive integer")
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.allow_schema_migrations = bool(allow_schema_migrations)
        # Every repository calls ``initialize()`` at startup; without this
        # memo the full idempotent DDL script re-executes once per repository
        # (14x) before the bot can serve a single message.
        self._schema_ready = False

    async def initialize(self) -> None:
        """Create or verify the current schema under a write lock.

        Runs the full DDL sweep at most once per instance: repositories share
        one database, so repeating it adds pure startup latency.  Repeat calls
        still verify the on-disk schema version so a database written by a
        newer application build keeps failing closed.
        """

        if self._schema_ready:
            await asyncio.to_thread(self._verify_schema_version_sync)
            return
        await asyncio.to_thread(self._initialize_sync)
        self._schema_ready = True

    def _verify_schema_version_sync(self) -> None:
        def check(transaction: Transaction) -> None:
            version = int(transaction.read("PRAGMA user_version")[0]["user_version"])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"database schema {version} is newer than supported schema {SCHEMA_VERSION}"
                )

        self._run_transaction(check, immediate=False)

    async def healthcheck(self) -> bool:
        """Run a cheap readiness probe for schema and basic readability."""

        return await asyncio.to_thread(self._healthcheck_sync)

    async def integrity_check(self) -> bool:
        """Run SQLite ``quick_check`` for startup or explicit diagnostics."""

        return await asyncio.to_thread(self._integrity_check_sync)

    async def checkpoint(self) -> tuple[int, int, int]:
        """Run a cooperative WAL checkpoint and return SQLite's counters."""

        return await asyncio.to_thread(self._checkpoint_sync)

    async def read(
        self,
        sql: str,
        parameters: SQLParameters = (),
    ) -> list[dict[str, Any]]:
        """Run one read-only statement in its own deferred transaction."""

        return await asyncio.to_thread(self._read_sync, sql, parameters)

    async def write(
        self,
        sql: str,
        parameters: SQLParameters = (),
    ) -> WriteResult:
        """Run one mutating statement in its own immediate transaction."""

        return await self.transaction(lambda transaction: transaction.write(sql, parameters))

    async def transaction(
        self,
        operation: Callable[[Transaction], T],
        *,
        immediate: bool = True,
    ) -> T:
        """Run a synchronous callback atomically on a worker thread.

        The callback must not be async: allowing an await would leak the
        thread-bound SQLite connection back to the event loop.  Any exception
        triggers an explicit rollback before the independent connection closes.
        """

        if not callable(operation):
            raise TypeError("operation must be callable")
        return await asyncio.to_thread(self._transaction_sync, operation, immediate)

    def _open_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise OSError("refusing to open a symlinked SQLite state database")
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        try:
            if os.name == "posix":
                os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}").close()
            connection.execute("PRAGMA foreign_keys = ON").close()
            connection.execute("PRAGMA trusted_schema = OFF").close()
            connection.execute("PRAGMA secure_delete = FAST").close()
            connection.execute("PRAGMA temp_store = MEMORY").close()
            self._enable_wal(connection)
            # FULL avoids acknowledging alert/broadcast delivery state before
            # it is durable across an OS or power failure.
            connection.execute("PRAGMA synchronous = FULL").close()
        except BaseException:
            connection.close()
            raise
        return connection

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Enable WAL, retrying the short lock race between new connections.

        SQLite's busy handler is not consistently invoked while several new
        connections all attempt the first journal-mode transition.  Retrying
        that one idempotent pragma makes initialization safe across threads and
        processes while retaining the configured upper bound.
        """

        deadline = time.monotonic() + self.busy_timeout_ms / 1000
        delay = 0.01
        while True:
            try:
                cursor = connection.execute("PRAGMA journal_mode = WAL")
                try:
                    row = cursor.fetchone()
                finally:
                    cursor.close()
                if row is None or str(row[0]).casefold() != "wal":
                    raise sqlite3.OperationalError("SQLite did not enable WAL mode")
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", None)
                is_lock_error = (
                    code is not None
                    and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                ) or "lock" in str(exc).casefold()
                remaining = deadline - time.monotonic()
                if not is_lock_error or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 0.1)

    @staticmethod
    def _begin(connection: sqlite3.Connection, *, immediate: bool) -> None:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED")

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.rollback()

    def _run_transaction(
        self,
        operation: Callable[[Transaction], T],
        *,
        immediate: bool,
        query_only: bool = False,
    ) -> T:
        connection = self._open_connection()
        try:
            if query_only:
                connection.execute("PRAGMA query_only = ON")
            self._begin(connection, immediate=immediate)
            try:
                result = operation(Transaction(connection))
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("database transaction callbacks must be synchronous")
                connection.commit()
                return result
            except BaseException:
                self._rollback(connection)
                raise
        finally:
            connection.close()

    def _transaction_sync(
        self,
        operation: Callable[[Transaction], T],
        immediate: bool,
    ) -> T:
        return self._run_transaction(operation, immediate=immediate)

    def _read_sync(
        self,
        sql: str,
        parameters: SQLParameters,
    ) -> list[dict[str, Any]]:
        return self._run_transaction(
            lambda transaction: transaction.read(sql, parameters),
            immediate=False,
            query_only=True,
        )

    def _initialize_sync(self) -> None:
        def initialize(transaction: Transaction) -> None:
            version_row = transaction.read("PRAGMA user_version")[0]
            version = int(version_row["user_version"])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    f"database schema {version} is newer than supported schema {SCHEMA_VERSION}"
                )
            if version == 0:
                self._execute_ddl(transaction, _SCHEMA)
                transaction.write(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return

            if version < SCHEMA_VERSION and not self.allow_schema_migrations:
                raise SchemaMigrationRequiredError(
                    f"database schema {version} requires offline migration to "
                    f"schema {SCHEMA_VERSION}; run scripts.migrate_state --apply"
                )

            while version < SCHEMA_VERSION:
                migration = _MIGRATIONS.get(version)
                if migration is None:
                    raise UnsupportedSchemaVersionError(
                        f"database schema {version} has no migration to schema {SCHEMA_VERSION}"
                    )
                if callable(migration):
                    migration(transaction)
                else:
                    self._execute_ddl(transaction, migration)
                version += 1
                transaction.write(f"PRAGMA user_version = {version}")

            # Re-run idempotent current DDL to restore any optional index that
            # an operator intentionally dropped while diagnosing an incident.
            self._execute_ddl(transaction, _SCHEMA)

        self._run_transaction(initialize, immediate=True)

    @staticmethod
    def _execute_ddl(transaction: Transaction, script: str) -> None:
        """Execute each complete DDL statement inside the active transaction.

        ``sqlite3.executescript`` commits implicitly, so statements are routed
        through the transaction facade instead.  ``complete_statement``
        splits on statement boundaries rather than bare ``;`` characters,
        which keeps semicolons inside string literals or trigger bodies intact.
        """

        buffer = ""
        for line in script.splitlines():
            buffer += line + "\n"
            if sqlite3.complete_statement(buffer.strip()):
                transaction.write(buffer.strip())
                buffer = ""
        if buffer.strip():
            transaction.write(buffer.strip())

    def _healthcheck_sync(self) -> bool:
        def check(transaction: Transaction) -> bool:
            ping = transaction.read("SELECT 1 AS healthy")
            version_row = transaction.read("PRAGMA user_version")
            return (
                len(ping) == 1
                and ping[0].get("healthy") == 1
                and len(version_row) == 1
                and int(version_row[0]["user_version"]) == SCHEMA_VERSION
            )

        try:
            return self._run_transaction(
                check,
                immediate=False,
                query_only=True,
            )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            logger.warning("SQLite state health check failed", exc_info=True)
            return False

    def _integrity_check_sync(self) -> bool:
        def check(transaction: Transaction) -> bool:
            rows = transaction.read("PRAGMA quick_check(1)")
            return len(rows) == 1 and rows[0].get("quick_check") == "ok"

        try:
            return self._run_transaction(check, immediate=False, query_only=True)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            logger.warning("SQLite state integrity check failed", exc_info=True)
            return False

    def _checkpoint_sync(self) -> tuple[int, int, int]:
        connection = self._open_connection()
        try:
            cursor = connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
            try:
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None or len(row) != 3:
                raise sqlite3.OperationalError("SQLite returned an invalid checkpoint result")
            return int(row[0]), int(row[1]), int(row[2])
        finally:
            connection.close()

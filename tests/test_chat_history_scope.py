"""Private-chat history search must never cross the user boundary.

Regression batch 2: ``_search_chat_history`` used a GLOB prefix
``p_{user_id}*`` for private chats, so user 123 could match records of
``p_1234`` / ``p_1230``.  Private lookup must use the exact session key
``p_{user_id}`` (see ``src/plugins/chat/routing.py::session_key``) while
group lookup keeps its existing same-group behaviour.

Every test owns a fresh ``StateDatabase`` under its ``tmp_path``, seeds
synthetic rows with distinct markers, and restores the runtime database
reference afterwards.  The tool's clock is pinned so record timestamps
are generated relative to that clock (no future-year literals mixed with
real ``time.time()``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.database import StateDatabase
from src.services import tools
from src.services.tools import _HISTORY_EXCERPT_CHARS
from src.state import runtime

#: Pinned clock for both seeding and the tool's read path (seconds).
FIXED_NOW = 1_750_000_000
KEYWORD = "synthetic"

MARKERS = {
    "p_123": "synthetic-owner-123-alpha",
    "p_1234": "synthetic-owner-1234-beta",
    "p_1230": "synthetic-owner-1230-gamma",
    "p_12": "synthetic-owner-12-delta",
    "g_777_1001": "synthetic-group-777-member-1001-epsilon",
    "g_777_1002": "synthetic-group-777-member-1002-zeta",
    "g_7777_1001": "synthetic-group-7777-member-1001-eta",
}


@pytest.fixture(autouse=True)
def _pinned_clock_and_runtime() -> object:
    """Pin the tool clock and restore the runtime database reference.

    Each test swaps ``runtime.database`` for its own tmp database; the
    fixture restores the original reference afterwards so other test
    modules sharing the process keep their sandbox database.
    """
    original_db = runtime.database
    with patch.object(tools.time, "time", return_value=FIXED_NOW):
        yield
    runtime.database = original_db  # type: ignore[assignment]


async def _fresh_db(tmp_path: Path) -> StateDatabase:
    """Create and initialize one independent controlled database."""
    db = StateDatabase(tmp_path / "scope.db", allow_schema_migrations=True)
    await db.initialize()
    return db


def _content(key: str) -> str:
    return f"{KEYWORD} {MARKERS[key]}"


async def _search(
    db: StateDatabase, *, user_id: int, group_id: int = 0, keyword: str = KEYWORD
) -> str:
    """Run the real tool against the given database instance."""
    original = runtime.database
    runtime.database = db  # type: ignore[assignment]
    try:
        text, _action = await tools.execute_tool(
            tools.ToolContext(user_id=user_id, group_id=group_id),
            "search_chat_history",
            {"keyword": keyword},
        )
        return text
    finally:
        runtime.database = original  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_private_prefix_collision_returns_only_exact_owner(tmp_path: Path) -> None:
    """User 123 sees only p_123 content, never p_1234 / p_1230 / p_12."""
    db = await _fresh_db(tmp_path)
    try:
        for key in MARKERS:
            await db.write(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (key, _content(key), FIXED_NOW),
            )
        text = await _search(db, user_id=123)
    finally:
        await db.checkpoint()
    assert MARKERS["p_123"] in text
    for other in ("p_1234", "p_1230", "p_12"):
        assert MARKERS[other] not in text, f"leaked {other} marker into user 123 results"


@pytest.mark.asyncio
async def test_private_no_own_match_returns_not_found(tmp_path: Path) -> None:
    """Only p_1234 (someone else) matches; user 123 must get 'not found'."""
    db = await _fresh_db(tmp_path)
    try:
        for key in MARKERS:
            if key == "p_123":
                continue
            await db.write(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (key, _content(key), FIXED_NOW),
            )
        text = await _search(db, user_id=123)
    finally:
        await db.checkpoint()
    assert MARKERS["p_1234"] not in text
    assert "没搜到" in text


@pytest.mark.asyncio
async def test_group_scope_keeps_same_group_members(tmp_path: Path) -> None:
    """Group 777 sees g_777_1001 + g_777_1002, never g_7777_1001 or p_*."""
    db = await _fresh_db(tmp_path)
    try:
        for key in MARKERS:
            await db.write(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (key, _content(key), FIXED_NOW),
            )
        text = await _search(db, user_id=1001, group_id=777)
    finally:
        await db.checkpoint()
    assert MARKERS["g_777_1001"] in text
    assert MARKERS["g_777_1002"] in text
    assert MARKERS["g_7777_1001"] not in text, "different group leaked"
    for owner in ("p_123", "p_1234", "p_1230", "p_12"):
        assert MARKERS[owner] not in text, "private chat leaked into group search"


@pytest.mark.asyncio
async def test_arguments_cannot_widen_identity_scope(tmp_path: Path) -> None:
    """Extra user_id/group_id/session_key arguments must not widen scope."""
    db = await _fresh_db(tmp_path)
    try:
        for key in MARKERS:
            await db.write(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (key, _content(key), FIXED_NOW),
            )
        original = runtime.database
        runtime.database = db  # type: ignore[assignment]
        try:
            text, _action = await tools.execute_tool(
                tools.ToolContext(user_id=123, group_id=0),
                "search_chat_history",
                {
                    "keyword": KEYWORD,
                    "user_id": 1234,
                    "group_id": 777,
                    "session_key": "p_1234",
                },
            )
        finally:
            runtime.database = original  # type: ignore[assignment]
    finally:
        await db.checkpoint()
    assert MARKERS["p_123"] in text
    for other in ("p_1234", "p_1230", "p_12", "g_777_1001"):
        assert MARKERS[other] not in text, f"argument widened scope to {other}"


@pytest.mark.asyncio
async def test_filters_and_limits_hold_on_real_output(tmp_path: Path) -> None:
    """Exactly the newest 8 of 12 valid hits, newest first; stale/assistant
    rows excluded; excerpts truncated to 120 chars.

    Records are inserted out of order with distinct timestamps, and the
    assertions check the rendered text lines (not just marker presence).
    """
    db = await _fresh_db(tmp_path)
    key = "p_777"
    base = FIXED_NOW - 6 * 86400
    step = 12 * 3600  # 11 plain records over ~5 days inside the 7-day window
    long_content = f"{KEYWORD} long-" + ("x" * 200)
    try:
        # Out-of-order insert (newest plain record lands mid-sequence).
        order = [10, 0, 9, 1, 8, 2, 7, 3, 6, 4, 5]
        for index in order:
            created = base + index * step
            await db.write(
                "INSERT INTO session_messages (session_key, role, content, created_at) "
                "VALUES (?, 'user', ?, ?)",
                (key, f"{KEYWORD} message-number-{index:02d}", created),
            )
        # Over-long record with a *distinct* newest timestamp: it must be
        # the first returned row, truncated to the 120-char excerpt.
        await db.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (key, long_content, FIXED_NOW - 3600),
        )
        # Stale (outside window) and assistant records.
        await db.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (key, f"{KEYWORD} stale-message", FIXED_NOW - 10 * 86400),
        )
        await db.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'assistant', ?, ?)",
            (key, f"{KEYWORD} assistant-line", FIXED_NOW),
        )
        text = await _search(db, user_id=777)
    finally:
        await db.checkpoint()
    # Stale and assistant rows are excluded entirely.
    assert "stale-message" not in text
    assert "assistant-line" not in text
    # 12 valid in-window user records exist; the newest 8 are returned
    # (long record + message-number-04..10).
    newest = [f"message-number-{index:02d}" for index in range(4, 11)]
    for marker in newest:
        assert marker in text
    for index in range(4):
        assert f"message-number-{index:02d}" not in text, f"old record {index} returned"
    # Order: newest first in the body (after the title line).
    title_end = text.index("（按时间新到旧）：")
    body = text[title_end:]
    positions = [body.index(f"message-number-{index:02d}") for index in range(10, 3, -1)]
    assert positions == sorted(positions), "plain records are not newest-first"
    # The long record exists, is the first content row, and its excerpt is
    # exactly the original content's [:120] slice (not a length estimate).
    long_line = next((ln for ln in body.splitlines() if "long-" in ln), None)
    assert long_line is not None, "long record missing from results"
    first_content = next(ln for ln in body.splitlines() if ln.startswith("- "))
    assert long_line == first_content, "long record is not the newest/first row"
    expected_excerpt = long_content.strip()[:_HISTORY_EXCERPT_CHARS]
    rendered_excerpt = long_line.split("：", 1)[1]
    assert rendered_excerpt == expected_excerpt, "excerpt does not equal content[:120]"
    # Exactly 8 content lines between the title and the footer.
    content_lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(content_lines) == 8, f"expected 8 rows, got {len(content_lines)}"


@pytest.mark.asyncio
async def test_expired_records_excluded_independently(tmp_path: Path) -> None:
    """A dedicated session with <8 candidates: dropping the time filter
    would expose the stale marker, so this asserts the window filter."""
    db = await _fresh_db(tmp_path)
    key = "p_888"
    try:
        await db.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (key, f"{KEYWORD} fresh-only-marker", FIXED_NOW - 3600),
        )
        await db.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (key, f"{KEYWORD} expired-marker", FIXED_NOW - 10 * 86400),
        )
        text = await _search(db, user_id=888)
    finally:
        await db.checkpoint()
    assert "fresh-only-marker" in text
    assert "expired-marker" not in text, "out-of-window record returned"

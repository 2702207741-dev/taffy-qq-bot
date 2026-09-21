from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.services import learning
from src.services.tz import market_now
from src.state.runtime import database, learning_insights


@pytest.fixture(autouse=True)
async def prepare():
    await learning_insights.initialize()
    yield
    await database.write("DELETE FROM learning_insights")
    await database.write("DELETE FROM session_messages")
    await database.write("DELETE FROM usage_log")


async def _seed_messages(rows: list[tuple[str, str, int]]) -> None:
    for session_key, content, created_at in rows:
        await database.write(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, 'user', ?, ?)",
            (session_key, content, created_at),
        )


def _yesterday_noon() -> int:
    """A wall clock guaranteed to sit inside yesterday's local window."""

    local_midnight = market_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return int((local_midnight - timedelta(hours=12)).timestamp())


def test_parse_session_key_variants() -> None:
    assert learning._parse_session_key("p_123") == (123, 0)
    assert learning._parse_session_key("g_45_123") == (123, 45)
    assert learning._parse_session_key("g_45_123_extra") is None
    assert learning._parse_session_key("weird") is None


def test_json_digest_tolerates_fenced_output() -> None:
    payload = '```json\n[{"kind": "preference", "content": "喜欢AK", "evidence": "昨日话题"}]```'
    digest = learning._json_digest(payload)
    assert digest == [{"kind": "preference", "content": "喜欢AK", "evidence": "昨日话题"}]
    assert learning._json_digest("not json") is None
    assert learning._json_digest("[]") is None


def test_leaks_raw_text_detection() -> None:
    long_raw = "这是一条完整的原始聊天消息内容，长度超过二十个字符"
    item = {"content": "摘要：讨论了 AK 行情", "evidence": "参考了 AK 行情"}
    assert not learning._leaks_raw_text(item, [long_raw])
    leaking = {"content": "用户说：" + long_raw, "evidence": "原文"}
    assert learning._leaks_raw_text(leaking, [long_raw])


@pytest.mark.asyncio
async def test_observe_creates_preference_for_frequent_mentions() -> None:
    now = int(time.time())
    rows = [
        ("p_2001", f"看看 {name}", now - 3600) for name in ("AK-47 | Redline (Field-Tested)",) * 3
    ]
    await _seed_messages(rows)
    with (
        patch.object(learning, "market_catalog_store"),
        patch.object(
            learning.market_catalog_store,
            "entries",
            AsyncMock(
                return_value=(
                    type(
                        "E",
                        (),
                        {"market_name": "AK-47 | Redline (Field-Tested)", "display_name": "红线"},
                    )(),
                )
            ),
        ),
    ):
        created = await learning.observe_once(now=now)
    assert created >= 1
    insights, _ = await learning_insights.admin_list(user_id=2001, status="pending")
    assert any(item.kind == "preference" for item in insights)
    assert any("AK-47" in item.content for item in insights)
    assert any("3 次" in item.evidence for item in insights)


@pytest.mark.asyncio
async def test_observe_respects_daily_limit() -> None:
    now = int(time.time())
    for index in range(5):
        await learning_insights.create_pending(
            2002, 0, "preference", f"已有偏好 {index}", "历史", now=now
        )
    await _seed_messages([("p_2002", "AK-47 | Redline (Field-Tested)", now - 3600)] * 4)
    with (
        patch.object(learning, "market_catalog_store"),
        patch.object(
            learning.market_catalog_store,
            "entries",
            AsyncMock(
                return_value=(
                    type(
                        "E",
                        (),
                        {"market_name": "AK-47 | Redline (Field-Tested)", "display_name": "红线"},
                    )(),
                )
            ),
        ),
    ):
        created = await learning.observe_once(now=now)
    assert created == 0


@pytest.mark.asyncio
async def test_observe_skips_when_disabled() -> None:
    now = int(time.time())
    await _seed_messages([("p_2003", "AK-47 | Redline (Field-Tested)", now - 3600)] * 3)
    with (
        patch.object(learning, "learning_enabled", return_value=False),
        patch.object(learning, "market_catalog_store"),
        patch.object(
            learning.market_catalog_store,
            "entries",
            AsyncMock(
                return_value=(
                    type(
                        "E",
                        (),
                        {"market_name": "AK-47 | Redline (Field-Tested)", "display_name": "红线"},
                    )(),
                )
            ),
        ),
    ):
        assert await learning.observe_once(now=now) == 0


@pytest.mark.asyncio
async def test_reflect_writes_pending_from_model_json() -> None:
    now = int(time.time())
    await _seed_messages([("p_2004", "今天聊了很多 AK 和手套", _yesterday_noon())] * 3)
    payload = '[{"kind": "preference", "content": "关注 AK 系列", "confidence": 4, "evidence": "昨日话题 AK"}]'
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value=payload)),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        created = await learning.reflect_once(now=now)
    assert created == 1
    insights, _ = await learning_insights.admin_list(user_id=2004, status="pending")
    assert insights[0].content == "关注 AK 系列"
    assert insights[0].confidence == 4


@pytest.mark.asyncio
async def test_reflect_drops_invalid_json_without_writing() -> None:
    now = int(time.time())
    await _seed_messages([("p_2005", "随便聊聊", _yesterday_noon())])
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value="不是 JSON")),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        assert await learning.reflect_once(now=now) == 0
    _, total = await learning_insights.admin_list(user_id=2005)
    assert total == 0


@pytest.mark.asyncio
async def test_reflect_drops_items_quoting_raw_text() -> None:
    now = int(time.time())
    raw = "这是一条完整的原始聊天消息内容，用于检验防原文泄漏"
    await _seed_messages([("p_2006", raw, _yesterday_noon())])
    payload = '[{"kind": "knowledge", "content": "用户说：' + raw + '", "evidence": "原文"}]'
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value=payload)),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        assert await learning.reflect_once(now=now) == 0
    _, total = await learning_insights.admin_list(user_id=2006)
    assert total == 0


@pytest.mark.asyncio
async def test_decay_once_applies_curve() -> None:
    now = int(time.time())
    insight = await learning_insights.create_pending(2007, 0, "preference", "偏好", "依据", now=now)
    await learning_insights.confirm(insight.id, 2007, 0, now=now)
    await database.write(
        "INSERT INTO usage_log (user_id, group_id, feature, command, input_text) "
        "VALUES (?, 0, 'chat', 'hi', 'hi')",
        (2007,),
    )
    assert await learning.decay_once(now=now + 86400) == 0
    rows, _ = await learning_insights.admin_list(user_id=2007, status="accepted")
    # Confidence 1 sits in the fast tier (x0.98) on top of the daily curve.
    assert rows[0].weight == pytest.approx(0.99 * 0.98, abs=0.001)


@pytest.mark.asyncio
async def test_reflection_window_is_local_yesterday_half_open() -> None:
    """A4 regression: reflections cover exactly [local midnight-1d, local midnight)."""

    from datetime import datetime
    from datetime import timezone as dt_timezone

    from src.services.tz import to_market_tz

    # Fixed epoch: 2026-08-22 03:00 UTC == 11:00 Asia/Shanghai on 08-22.
    fixed_now = int(datetime(2026, 8, 22, 3, 0, tzinfo=dt_timezone.utc).timestamp())
    local_midnight = to_market_tz(datetime.fromtimestamp(fixed_now, tz=dt_timezone.utc)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    inside_yesterday = int((local_midnight - timedelta(hours=2)).timestamp())
    today_edge = int(local_midnight.timestamp()) + 120
    two_days_ago = int((local_midnight - timedelta(days=2) + timedelta(minutes=5)).timestamp())

    payload = '[{"kind": "preference", "content": "观察", "evidence": "evidence"}]'
    with (
        patch.object(learning, "try_anthropic", AsyncMock(return_value=payload)),
        patch.object(learning, "learning_enabled", return_value=True),
    ):
        # Seed each scope with messages at a different window boundary.
        seeds = {
            "p_3101": inside_yesterday,  # yesterday evening -> must be reflected
            "p_3102": today_edge,  # today after local midnight -> excluded
            "p_3103": two_days_ago,  # the day before yesterday -> excluded
        }
        rows = [(key, f"消息 for {key}", stamp) for key, stamp in seeds.items() for _ in range(3)]
        await _seed_messages(rows)
        created = await learning.reflect_once(now=fixed_now)

    assert created == 1
    assert (await learning_insights.admin_list(user_id=3101, status="pending"))[1] >= 1
    assert (await learning_insights.admin_list(user_id=3102))[1] == 0
    assert (await learning_insights.admin_list(user_id=3103))[1] == 0

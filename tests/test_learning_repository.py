from __future__ import annotations

import pytest

from src.core.database import StateDatabase
from src.state.learning_repository import LearningInsightRepository


@pytest.fixture()
def repository(tmp_path):
    database = StateDatabase(tmp_path / "learning.db")
    repo = LearningInsightRepository(database)
    yield repo


@pytest.mark.asyncio
async def test_create_and_confirm_flow(repository) -> None:
    await repository.initialize()
    insight = await repository.create_pending(
        1001,
        0,
        "preference",
        "常看 AK 系列皮肤",
        "近 24 小时私聊 3 次提到 AK-47 | 红线",
        confidence=3,
    )
    assert insight is not None
    assert insight.status == "pending"
    assert await repository.pending_count(1001, 0) == 1
    assert await repository.confirm(insight.id, 1001, 0)
    accepted = await repository.list_scope(1001, 0)
    assert len(accepted) == 1
    assert accepted[0].status == "accepted"
    assert accepted[0].accepted_at is not None
    # wrong scope cannot confirm
    assert not await repository.confirm(insight.id, 1001, 1)


@pytest.mark.asyncio
async def test_reject_and_delete(repository) -> None:
    await repository.initialize()
    insight = await repository.create_pending(1002, 5, "style", "喜欢塔菲用表情", "风格反馈命中")
    assert await repository.reject(insight.id, 1002, 5)
    assert await repository.list_scope(1002, 5) == ()
    accepted = await repository.create_pending(1002, 5, "style", "不喜欢太长回复", "风格反馈命中")
    await repository.confirm(accepted.id, 1002, 5)
    assert await repository.delete(accepted.id, 1002, 5)
    assert await repository.list_scope(1002, 5) == ()
    # delete only works on accepted
    pending = await repository.create_pending(1002, 5, "preference", "偏好极简回答", "规则命中")
    assert not await repository.delete(pending.id, 1002, 5)


@pytest.mark.asyncio
async def test_duplicate_detection_is_kind_specific(repository) -> None:
    await repository.initialize()
    first = await repository.create_pending(1003, 0, "preference", "偏好极简回答", "依据A")
    assert first is not None
    assert await repository.create_pending(1003, 0, "preference", "偏好极简回答", "依据B") is None
    # same content under a different kind is not a duplicate
    assert await repository.create_pending(1003, 0, "style", "偏好极简回答", "依据C") is not None


@pytest.mark.asyncio
async def test_scope_isolation(repository) -> None:
    await repository.initialize()
    group = await repository.create_pending(1004, 100, "preference", "群内偏好", "群内依据")
    private = await repository.create_pending(1004, 0, "preference", "私聊偏好", "私聊依据")
    assert await repository.list_scope(1004, 100, status="pending") == (group,)
    assert await repository.list_scope(1004, 0, status="pending") == (private,)
    assert not await repository.confirm(group.id, 1004, 0)


@pytest.mark.asyncio
async def test_admin_list_and_stats(repository) -> None:
    await repository.initialize()
    for index in range(3):
        await repository.create_pending(1005, 0, "knowledge", f"知识 {index}", f"依据 {index}")
    insights, total = await repository.admin_list(status="pending")
    assert total == 3
    assert len(insights) == 3
    _filtered, filtered_total = await repository.admin_list(user_id=1005, status="pending")
    assert filtered_total == 3
    stats = await repository.stats()
    assert stats["pending"] == 3
    assert stats["accepted"] == 0


@pytest.mark.asyncio
async def test_decay_archives_inactive_and_preserves_active(repository) -> None:
    await repository.initialize()
    now = 1_700_000_000
    inactive_insight = await repository.create_pending(
        1006, 0, "preference", "长期偏好", "依据", now=now
    )
    await repository.confirm(inactive_insight.id, 1006, 0, now=now)

    active_insight = await repository.create_pending(
        1007, 0, "preference", "活跃偏好", "依据", now=now
    )
    await repository.confirm(active_insight.id, 1007, 0, now=now)
    await repository.database.write(
        "INSERT INTO usage_log (user_id, group_id, feature, command, input_text) "
        "VALUES (?, 0, 'chat', 'hi', 'hi')",
        (1007,),
    )

    await repository.decay(now=now + 86400, inactive_days=30)
    accepted_rows, _ = await repository.admin_list(status="accepted")
    active = next(item for item in accepted_rows if item.user_id == 1007)
    inactive = next(item for item in accepted_rows if item.user_id == 1006)
    # Confidence 1 sits in the fast tier (x0.98) on top of the daily curve.
    assert active.weight == pytest.approx(0.99 * 0.98, abs=0.001)
    assert inactive.weight == pytest.approx(0.99 * 0.98 * 0.5, abs=0.001)

    archived = await repository.decay(now=now + 3 * 86400, inactive_days=30)
    assert archived == 1
    archived_rows, _ = await repository.admin_list(status="archived")
    assert len(archived_rows) == 1
    assert archived_rows[0].user_id == 1006

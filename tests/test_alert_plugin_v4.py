from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.exception import FinishedException

from src.state.alert_repository import (
    AlertMutationResult,
    AlertRecord,
    CreateAlertResult,
)

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import alert


def _record(
    identifier: str = "a",
    *,
    status: str = "pending",
    direction: str = "above",
    token: str | None = None,
    attempts: int = 0,
    evaluation_failures: int = 0,
) -> AlertRecord:
    return AlertRecord(
        identifier,
        101,
        None,
        f"Item {identifier}",
        10_000,
        direction,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        attempts,
        evaluation_failures,
        1,
        token,
        2 if token else None,
        None,
        None,
        None,
        None,
        1,
        1,
        1 if status == "delivered" else None,
        1 if status == "failed" else None,
        None,
    )


def _event(text: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=101, get_plaintext=lambda: text)


async def _invoke(text: str, repository: object, *, allowed: bool = True) -> str:
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(alert, "alert_repository", repository),
        patch.object(alert._command_limiter, "allow", AsyncMock(return_value=allowed)),
        patch.object(alert.alert_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await alert.handle_alert_command(object(), _event(text))  # type: ignore[arg-type]
    return str(finish.await_args.args[0])


def _repository(**methods: object) -> SimpleNamespace:
    defaults = {
        "list_owner": AsyncMock(return_value=()),
        "cancel_at": AsyncMock(return_value=AlertMutationResult(False, None)),
        "retry_failed": AsyncMock(return_value=AlertMutationResult(False, None)),
        "create": AsyncMock(return_value=CreateAlertResult(False, None)),
    }
    defaults.update(methods)
    return SimpleNamespace(**defaults)


def _private(text: str) -> PrivateMessageEvent:
    return PrivateMessageEvent(
        time=1,
        self_id=2,
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=3,
        user_id=101,
        message=text,
        raw_message=text,
        font=0,
        sender={"user_id": 101, "nickname": "u", "sex": "unknown", "age": 0},
    )


def test_alert_helpers_are_bounded_and_rule_is_explicit() -> None:
    record = _record(attempts=99, evaluation_failures=99)
    assert alert._retry_at(record, 100) == 100 + 3600
    assert alert._evaluation_retry_at(record, 100) == 100 + 3600
    assert alert._message_id({"message_id": 12}) == 12
    assert alert._message_id({"message_id": object()}) is None
    assert alert._message_id("bad") is None
    assert alert._command_text(_private("@bot 预警 涨到 A 10")) == "预警 涨到 A 10"
    assert alert._is_alert_command(_private("我的预警"))
    assert not alert._is_alert_command(_private("我想看预警"))
    assert not alert._is_alert_command(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_alert_command_rate_limit_and_empty_list() -> None:
    assert "频繁" in await _invoke("我的预警", _repository(), allowed=False)
    assert "暂无预警" in await _invoke("我的预警", _repository())


@pytest.mark.asyncio
async def test_alert_list_renders_all_durable_states_and_directions() -> None:
    records = (
        _record("pending", status="pending"),
        _record("claimed", status="claimed", direction="below", token="lease"),
        _record("delivered", status="delivered"),
        _record("failed", status="failed"),
    )
    rendered = await _invoke(
        "我的预警",
        _repository(list_owner=AsyncMock(return_value=records)),
    )
    assert "🟢" in rendered and "已触发" in rendered and "通知失败" in rendered
    assert "跌到" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("取消预警 x", "格式"),
        ("重试预警 x", "格式"),
        # D2: index 0 is explicitly rejected as a format error instead of
        # silently resolving to the first alert.
        ("重试预警 0", "格式"),
        ("其他", "设置: 预警"),
        ("预警 A 10", "请明确预警方向"),
        ("预警 涨到 A bad", "例如"),
        (f"预警 涨到 {'x' * 101} 10", "不能超过"),
    ],
)
async def test_alert_command_rejects_invalid_forms(text: str, expected: str) -> None:
    assert expected in await _invoke(text, _repository())


@pytest.mark.asyncio
async def test_alert_cancel_and_retry_state_transitions() -> None:
    item = _record("one", status="failed")
    canceled = _repository(cancel_at=AsyncMock(return_value=AlertMutationResult(True, item)))
    assert "已取消" in await _invoke("取消预警 1", canceled)
    assert "没有这个预警" in await _invoke("取消预警 1", _repository())

    not_failed = _repository(list_owner=AsyncMock(return_value=(_record(),)))
    assert "无需重试" in await _invoke("重试预警 1", not_failed)
    changed = _repository(list_owner=AsyncMock(return_value=(item,)))
    assert "状态已变化" in await _invoke("重试预警 1", changed)
    retried = _repository(
        list_owner=AsyncMock(return_value=(item,)),
        retry_failed=AsyncMock(return_value=AlertMutationResult(True, item)),
    )
    assert "已重新排队" in await _invoke("重试预警 1", retried)


async def _invoke_create(
    text: str,
    repository: object,
    *,
    resolved: str | None = "Canonical Item",
    response: object = None,
) -> str:
    if response is None:
        response = {
            "success": True,
            "updateTime": 1_800_000_000,
            "data": [{"platform": "BUFF", "sellPrice": 100}],
        }
    finish = AsyncMock(side_effect=FinishedException())
    with ExitStack() as stack:
        stack.enter_context(patch.object(alert, "alert_repository", repository))
        stack.enter_context(
            patch.object(alert._command_limiter, "allow", AsyncMock(return_value=True))
        )
        stack.enter_context(patch.object(alert.alert_cmd, "finish", finish))
        stack.enter_context(
            patch("src.plugins.chat.resolve_name", AsyncMock(return_value=resolved))
        )
        stack.enter_context(
            patch("src.plugins.chat_utils.st_get", AsyncMock(return_value=response))
        )
        stack.enter_context(patch.object(alert.time, "time", return_value=1_800_000_000))
        with pytest.raises(FinishedException):
            await alert.handle_alert_command(object(), _event(text))  # type: ignore[arg-type]
    return str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_alert_creation_fails_closed_at_each_validation_boundary() -> None:
    assert "未能唯一确认" in await _invoke_create(
        "预警 涨到 Unknown 150", _repository(), resolved=None
    )
    assert "预警未保存" in await _invoke_create(
        "预警 涨到 A 150",
        _repository(),
        response={"success": False},
    )
    assert "已经高于目标价" in await _invoke_create("预警 涨到 A 80", _repository())
    assert "已经低于目标价" in await _invoke_create("预警 跌到 A 120", _repository())


@pytest.mark.asyncio
async def test_alert_creation_reports_quota_storage_failure_and_success() -> None:
    created = _record("created", direction="below")
    assert "最多设置" in await _invoke_create(
        "预警 跌到 A 80",
        _repository(create=AsyncMock(return_value=CreateAlertResult(False, None, True))),
    )
    assert "保存失败" in await _invoke_create("预警 跌到 A 80", _repository())
    successful = _repository(create=AsyncMock(return_value=CreateAlertResult(True, created)))
    rendered = await _invoke_create("预警 跌到 A 80", successful)
    assert "已设置预警" in rendered and "目标价" in rendered


@pytest.mark.asyncio
async def test_alert_check_returns_without_claims() -> None:
    repository = _repository(claim_due=AsyncMock(return_value=()))
    with patch.object(alert, "alert_repository", repository):
        await alert.check_alerts(object())  # type: ignore[arg-type]
    repository.claim_due.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_evaluation_validation_failure_is_deferred_without_delivery_attempt() -> None:
    record = _record("claimed", status="claimed", token="lease")
    repository = _repository(
        record_evaluation_failure=AsyncMock(return_value=AlertMutationResult(False, None))
    )
    with (
        patch.object(alert, "alert_repository", repository),
        patch(
            "src.plugins.chat_utils.st_get",
            AsyncMock(return_value={"success": True, "data": []}),
        ),
    ):
        await alert._evaluate_claim(object(), record)  # type: ignore[arg-type]
        await alert._evaluate_claim(object(), _record("missing-token", status="claimed"))  # type: ignore[arg-type]
    repository.record_evaluation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_command_unexpected_storage_error_fails_closed() -> None:
    repository = _repository(list_owner=AsyncMock(side_effect=RuntimeError("locked")))
    assert "暂时不可用" in await _invoke("我的预警", repository)

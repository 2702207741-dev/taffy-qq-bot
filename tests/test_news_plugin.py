from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.exception import FinishedException

from src.services.official_cs2 import OfficialCS2Report, ValveUpdate
from src.services.verified import (
    DataProvenance,
    UnavailableReason,
    VerifiedResult,
)

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import news


@pytest.fixture(autouse=True)
def _enable_v4_news_pilot() -> Iterator[None]:
    with patch.object(news, "v4_enabled_for_user", return_value=True):
        yield


def _provenance() -> DataProvenance:
    now = datetime(2026, 7, 14, 1, tzinfo=timezone.utc)
    return DataProvenance("Valve", "official-news", now, now, "0" * 64)


def _private_event(text: str) -> PrivateMessageEvent:
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


@pytest.mark.asyncio
async def test_news_renders_verified_official_update_without_a_model() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "搜索CS2最近有什么")
    update = ValveUpdate(
        "Counter-Strike 2 Update",
        "https://store.steampowered.com/news/app/730/view/1",
        datetime(2026, 7, 14, tzinfo=timezone.utc),
        "Verified update summary.",
    )
    report = OfficialCS2Report(
        updates=VerifiedResult.available((update,), _provenance()),
        upcoming=VerifiedResult.unavailable(UnavailableReason.NOT_CONFIGURED),
        completed=VerifiedResult.unavailable(UnavailableReason.NOT_CONFIGURED),
    )
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(news.official_cs2, "report", AsyncMock(return_value=report)) as collect,
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]

    collect.assert_awaited_once_with(include_updates=True, include_matches=True)
    rendered = str(finish.await_args.args[0])
    assert "Counter-Strike 2 Update" in rendered
    assert "https://store.steampowered.com/news/app/730/view/1" in rendered
    assert "赛事接口尚未配置" in rendered


@pytest.mark.asyncio
async def test_news_reports_validation_failure_without_fabrication() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "最新更新")
    report = OfficialCS2Report(
        updates=VerifiedResult.unavailable(UnavailableReason.VALIDATION_FAILED)
    )
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(news.official_cs2, "report", AsyncMock(return_value=report)),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]

    rendered = str(finish.await_args.args[0])
    assert "暂时不可用" in rendered
    assert "模型补写" in rendered


def test_requested_sections_are_deterministic() -> None:
    assert news.requested_sections("最新更新") == (True, False)
    assert news.requested_sections("赛事消息") == (False, True)
    assert news.requested_sections("搜索CS2最近有什么") == (True, True)


def test_news_rule_matches_only_explicit_supported_commands() -> None:
    assert news._is_news_request(_private_event("CS2赛事"))
    assert news._is_news_request(_private_event("搜索cs2最近有什么更新"))
    assert not news._is_news_request(_private_event("今天吃什么"))
    assert not news._is_news_request(object())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_news_rate_limit_stops_before_external_request() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "CS2新闻")
    finish = AsyncMock(side_effect=FinishedException())
    report = AsyncMock()
    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=False)),
        patch.object(news.official_cs2, "report", report),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]
    report.assert_not_awaited()
    assert "频繁" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_news_is_hidden_from_non_pilot_users() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "CS2新闻")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(news, "v4_enabled_for_user", return_value=False),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]
    assert "管理员试用" in str(finish.await_args.args[0])


@pytest.mark.asyncio
async def test_news_sends_one_progress_message_only_after_two_second_path() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "最新更新")
    report = OfficialCS2Report(updates=VerifiedResult.unavailable(UnavailableReason.UPSTREAM_ERROR))
    finish = AsyncMock(side_effect=FinishedException())
    send = AsyncMock()
    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(news.official_cs2, "report", AsyncMock(return_value=report)),
        patch.object(news.asyncio, "wait_for", AsyncMock(side_effect=TimeoutError)),
        patch.object(news.news, "send", send),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]
    send.assert_awaited_once()
    assert "正在核对" in str(send.await_args.args[0])


@pytest.mark.asyncio
async def test_news_unexpected_error_fails_closed() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "最新更新")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(news._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(news.official_cs2, "report", AsyncMock(side_effect=RuntimeError)),
        patch.object(news.news, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await news.handle_news(event)  # type: ignore[arg-type]
    assert "不让模型补写" in str(finish.await_args.args[0])

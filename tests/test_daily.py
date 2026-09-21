from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import daily


def test_disabled_report_explains_why_no_market_claim_is_generated() -> None:
    assert "正在重建" in daily._DISABLED_MESSAGE
    assert "无法证明" in daily._DISABLED_MESSAGE
    assert "聊天模型" in daily._DISABLED_MESSAGE


@pytest.mark.asyncio
async def test_report_command_returns_only_the_disabled_notice() -> None:
    finish = AsyncMock(side_effect=FinishedException())
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "日报")
    with (
        patch.object(daily.daily_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await daily.handle_report(event)  # type: ignore[arg-type]

    assert str(finish.await_args.args[0]) == daily._DISABLED_MESSAGE

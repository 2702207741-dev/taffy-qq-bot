from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.plugins import compare


def test_compare_parser_is_bounded_and_requires_an_explicit_separator() -> None:
    assert compare._parse_names("对比 AK红线 vs AWP二西莫夫") == (
        "AK红线",
        "AWP二西莫夫",
    )
    assert compare._parse_names("对比 missing separator") is None
    assert compare._parse_names(f"对比 {'x' * 121} vs item") is None


@pytest.mark.asyncio
async def test_compare_handler_resolves_both_items_and_returns_price_difference() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "对比 first vs second")
    first = {
        "success": True,
        "updateTime": time.time(),
        "data": [{"platform": "BUFF", "sellPrice": 120.0}],
    }
    second = {
        "success": True,
        "updateTime": time.time(),
        "data": [{"platform": "BUFF", "sellPrice": 100.0}],
    }
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(compare._RATE_LIMITER, "allow", AsyncMock(return_value=True)),
        patch(
            "src.plugins.chat.resolve_name", AsyncMock(side_effect=["first-full", "second-full"])
        ),
        patch.object(compare, "st_get", AsyncMock(side_effect=[first, second])) as market,
        patch.object(compare.compare, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await compare.handle_compare(event)  # type: ignore[arg-type]

    assert market.await_count == 2
    rendered = str(finish.await_args.args[0])
    assert "first-full" in rendered
    assert "second-full" in rendered
    assert "数据时间:" in rendered
    assert "BUFF差价: ¥20" in rendered

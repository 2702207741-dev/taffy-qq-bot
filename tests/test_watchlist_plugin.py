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

from src.plugins import watchlist


def test_watchlist_price_formatter_ignores_malformed_and_nonfinite_rows() -> None:
    now = 1_700_000_100
    rendered = watchlist._format_prices(
        {
            "success": True,
            "updateTime": now - 30,
            "data": [
                None,
                {"platform": "BUFF", "sellPrice": float("inf")},
                {"platform": "YOUPIN", "sellPrice": 99.5},
            ],
        },
        now=now,
    )
    assert "数据时间:" in rendered
    assert "悠悠: ¥99.50" in rendered
    assert "BUFF" not in rendered


@pytest.mark.asyncio
async def test_watchlist_handler_lists_only_the_requesting_owner_items() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "我的自选")
    repository = SimpleNamespace(
        list_owner=AsyncMock(
            return_value=(
                SimpleNamespace(display_name="AK-47 | Redline"),
                SimpleNamespace(display_name="AWP | Asiimov"),
            )
        )
    )
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(watchlist, "watchlists", repository),
        patch.object(watchlist._COMMAND_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(watchlist.watch_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await watchlist.handle_watchlist(object(), event)  # type: ignore[arg-type]

    repository.list_owner.assert_awaited_once_with(101)
    rendered = str(finish.await_args.args[0])
    assert "AK-47 | Redline" in rendered
    assert "AWP | Asiimov" in rendered

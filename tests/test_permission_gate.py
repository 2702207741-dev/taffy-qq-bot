from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from nonebot.exception import FinishedException

from src.plugins import feature_gate, watchlist


@pytest.mark.asyncio
async def test_denied_feature_blocks_watchlist_command() -> None:
    event = SimpleNamespace(user_id=101, get_plaintext=lambda: "我的自选")
    finish = AsyncMock(side_effect=FinishedException())
    with (
        patch.object(
            feature_gate,
            "authority",
            SimpleNamespace(allowed=AsyncMock(return_value=False)),
        ),
        patch.object(watchlist._COMMAND_LIMITER, "allow", AsyncMock(return_value=True)),
        patch.object(watchlist.watch_cmd, "finish", finish),
        pytest.raises(FinishedException),
    ):
        await watchlist.handle_watchlist(object(), event)  # type: ignore[arg-type]

    finish.assert_awaited_once()
    message = str(finish.await_args.args[0])
    assert "未对你开放" in message

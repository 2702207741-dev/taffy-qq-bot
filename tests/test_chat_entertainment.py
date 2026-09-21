from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

from src.plugins.chat.entertainment import (
    CASES,
    RARITIES,
    _open_cases,
    handle_entertainment_command,
)


class _FixedRng:
    def __init__(self, roll: float) -> None:
        self.roll = roll

    def random(self) -> float:
        return self.roll

    def choice(self, values: Any) -> Any:
        return values[0]

    def sample(self, values: Any, count: int) -> Any:
        return list(values)[:count]


def test_case_probabilities_sum_to_one_and_every_case_has_each_regular_tier() -> None:
    assert sum(probability for _, probability in RARITIES) == pytest.approx(1.0)
    for case in CASES:
        tiers = {item.rarity for item in case.items}
        assert {"blue", "purple", "pink", "red"} <= tiers
        assert case.rare_specials


def test_case_simulation_selects_an_item_from_the_drawn_real_rarity_pool() -> None:
    gold = _open_cases("开箱", _FixedRng(0.0))  # type: ignore[arg-type]
    blue = _open_cases("开箱", _FixedRng(0.99))  # type: ignore[arg-type]

    assert "🟡 [金] ★ Bayonet" in gold
    assert "🔵 [蓝] SG 553 | Wave Spray" in blue
    assert "概率: 蓝79.92%" in blue
    assert "纯模拟" in blue


@pytest.mark.asyncio
async def test_alchemy_is_explicitly_non_contract_fantasy() -> None:
    result = await handle_entertainment_command(
        "炼金",
        resolve_name=AsyncMock(return_value=None),
        rng=_FixedRng(0.5),  # type: ignore[arg-type]
    )

    assert result is not None
    assert "非游戏合约" in result
    assert "不复刻游戏合约" in result

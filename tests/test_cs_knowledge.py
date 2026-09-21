from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services.cs_knowledge import knowledge_entry_count, lookup_cs_knowledge
from src.services.llm import ChatService


@pytest.mark.parametrize(
    ("query", "entry_id"),
    [
        ("CS2的subtick到底是什么", "subtick-basics"),
        ("烟雾弹的动态机制是什么", "responsive-smokes"),
        ("CS Rating和HLTV rating一样吗", "premier-cs-rating"),
    ],
)
def test_reviewed_knowledge_entries_match_only_their_explicit_scope(
    query: str,
    entry_id: str,
) -> None:
    answer = lookup_cs_knowledge(query)
    assert answer is not None
    assert answer.entry_id == entry_id
    assert answer.sources[0].url.startswith("https://www.counter-strike.net/")


def test_time_sensitive_or_unreviewed_topics_do_not_use_static_knowledge() -> None:
    assert lookup_cs_knowledge("m0NESY现在在哪个队") is None
    assert lookup_cs_knowledge("AK爆头到底多少伤害") is None
    assert lookup_cs_knowledge("mirage A1烟怎么扔") is None


def test_knowledge_base_contains_at_least_thirty_reviewed_official_topics() -> None:
    assert knowledge_entry_count() >= 30
    for query in (
        "非Prime账号能玩什么模式",
        "Trust Factor算法怎么算",
        "撤销交易需要什么条件",
        "CS2的地图工具是什么",
        "CS2饰品光照和材质有什么变化",
    ):
        answer = lookup_cs_knowledge(query)
        assert answer is not None
        assert answer.reviewed_on == "2026-07-14"
        assert answer.applicable_scope


@pytest.mark.asyncio
async def test_reviewed_knowledge_bypasses_web_and_llm() -> None:
    collector = AsyncMock()
    service = ChatService(fact_collector=collector)
    with (
        patch("src.services.llm.try_anthropic", AsyncMock()) as primary,
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply("CS2的subtick到底是什么", "session")

    assert "CS2可验证知识库" in reply
    assert "counter-strike.net" in reply
    collector.assert_not_awaited()
    primary.assert_not_awaited()
    fallback.assert_not_awaited()

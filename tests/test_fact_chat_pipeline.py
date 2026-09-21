from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.services.fact_sources import FactBundle, FactSource
from src.services.llm import ChatService, filter_verified_fact_sentences


def _unverified(query: str, intent: str) -> FactBundle:
    return FactBundle(
        query=query,
        intent=intent,
        sources=(),
        error="无法核实：可靠正文来源不足。",
    )


def _verified(query: str, intent: str) -> FactBundle:
    return FactBundle(
        query=query,
        intent=intent,
        sources=(
            FactSource(
                title="Roster report one",
                url="https://www.hltv.org/news/one",
                domain="hltv.org",
                body="NiKo player current Team Falcons roster first full page body " * 20,
                fetched_at="2026-07-13T05:00:00+00:00",
                page_time="2026-07-13T04:00:00+00:00",
            ),
            FactSource(
                title="Roster report two",
                url="https://liquipedia.net/counterstrike/two",
                domain="liquipedia.net",
                body="NiKo player current Team Falcons roster second full page body " * 20,
                fetched_at="2026-07-13T05:01:00+00:00",
                page_time="2026-07-13T04:30:00+00:00",
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "NiKo 现在在哪个队？",
        "CS2 最新版本改了什么",
        "AK爆头多少伤害",
        "沙二xbox烟怎么丢",
    ],
)
async def test_unverified_natural_fact_questions_never_call_model(message: str) -> None:
    collector = AsyncMock(side_effect=lambda query, intent, **_: _unverified(query, intent))
    service = ChatService(fact_collector=collector)

    with (
        patch("src.services.llm.try_anthropic", AsyncMock()) as primary,
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply(message, "session")

    assert "还没对上" in reply
    collector.assert_awaited_once()
    primary.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_natural_price_question_never_calls_market_or_model() -> None:
    market = AsyncMock()
    collector = AsyncMock()
    service = ChatService(market_get=market, fact_collector=collector)

    with (
        patch("src.services.llm.try_anthropic", AsyncMock()) as primary,
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply("红线现在多少钱", "session")

    assert "还没对上" in reply
    assert "歧义" in reply
    market.assert_not_awaited()
    collector.assert_not_awaited()
    primary.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_natural_price_question_uses_only_steamdt() -> None:
    now = time.time()
    market = AsyncMock(
        return_value={
            "success": True,
            "updateTime": now,
            "data": [
                {
                    "platform": "BUFF",
                    "sellPrice": 88.8,
                    "sellCount": 12,
                    "biddingPrice": 87.5,
                    "biddingCount": 8,
                }
            ],
        }
    )
    service = ChatService(market_get=market)

    with (
        patch("src.services.llm.try_anthropic", AsyncMock()) as primary,
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply("AK红线久经现在多少钱", "session")

    market.assert_awaited_once_with(
        "/open/cs2/v1/price/single",
        {"marketHashName": "AK-47 | Redline (Field-Tested)"},
        60,
    )
    assert "数据时间:" in reply
    assert "88.80" in reply
    assert "来源" not in reply
    primary.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_facts_use_only_supplied_evidence_and_validate_citations() -> None:
    collector = AsyncMock(side_effect=lambda query, intent, **_: _verified(query, intent))
    store = SimpleNamespace(as_messages=AsyncMock())
    service = ChatService(fact_collector=collector, session_store=store)
    primary = AsyncMock(return_value="NiKo 的当前队伍已由两份正文交叉确认[来源1][来源2]。")

    with (
        patch("src.services.llm.try_anthropic", primary),
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply("NiKo 现在在哪个队？", "old-session")

    store.as_messages.assert_not_awaited()
    fallback.assert_not_awaited()
    sent_messages = primary.await_args.args[1]
    assert len(sent_messages) == 1
    assert "first full page body" in sent_messages[0]["content"]
    assert "https://www.hltv.org/news/one" in reply
    assert "[来源1][来源2]" in reply
    assert reply.startswith("这次对上了")


@pytest.mark.asyncio
async def test_verified_sources_still_refuse_invalid_model_citations() -> None:
    collector = AsyncMock(side_effect=lambda query, intent, **_: _verified(query, intent))
    service = ChatService(fact_collector=collector)

    with patch(
        "src.services.llm.try_anthropic",
        AsyncMock(return_value="第一句没有引用。第二句[来源3]。"),
    ):
        reply = await service.generate_reply("NiKo 现在在哪个队？", "")

    assert "还没对上" in reply
    assert "逐句来源校验" in reply


@pytest.mark.asyncio
async def test_valid_citation_numbers_cannot_ground_an_invented_team() -> None:
    collector = AsyncMock(side_effect=lambda query, intent, **_: _verified(query, intent))
    service = ChatService(fact_collector=collector)

    with patch(
        "src.services.llm.try_anthropic",
        AsyncMock(return_value="NiKo 目前效力于 Team Spirit[来源1][来源2]。"),
    ):
        reply = await service.generate_reply("NiKo 现在在哪个队？", "")

    assert "还没对上" in reply
    assert "逐句来源校验" in reply


@pytest.mark.asyncio
async def test_chinese_team_alias_absent_from_sources_is_rejected() -> None:
    collector = AsyncMock(side_effect=lambda query, intent, **_: _verified(query, intent))
    service = ChatService(fact_collector=collector)

    with patch(
        "src.services.llm.try_anthropic",
        AsyncMock(return_value="NiKo 目前效力于猎鹰[来源1][来源2]。"),
    ):
        reply = await service.generate_reply("NiKo 现在在哪个队？", "")

    assert "还没对上" in reply
    assert "逐句来源校验" in reply


def test_individual_fact_filter_keeps_valid_claims_and_drops_bad_caveats() -> None:
    evidence = _verified("NiKo 现在在哪个队？", "team_player")
    result = filter_verified_fact_sentences(
        "NiKo 目前效力于 Team Falcons[来源1][来源2]。"
        "资料中未提及近期更新内容。"
        "无法核实其他传闻[来源1]。",
        evidence,
    )

    assert result.valid_sentences == 1
    assert "Team Falcons" in result.content
    assert "未提及" not in result.content
    assert "无法核实" not in result.content
    assert result.rejected_unverified == 2


def test_pure_chinese_claim_cannot_pass_by_appending_a_source_number() -> None:
    evidence = _verified("CS2最近更新", "version")
    result = filter_verified_fact_sentences(
        "这次更新显著改善了游戏体验[来源1]。",
        evidence,
    )

    assert result.valid_sentences == 0
    assert result.rejected_grounding == 1


@pytest.mark.asyncio
async def test_mixed_update_event_query_renders_independent_verified_sections() -> None:
    version_source = FactSource(
        title="AnimGraph2 update",
        url="https://www.counter-strike.net/news/updates",
        domain="counter-strike.net",
        body="AnimGraph2 update is live in CS2 and improves third-person animations. " * 20,
        fetched_at="2026-07-14T01:00:00+00:00",
        page_time="2026-07-13T20:00:00+00:00",
    )
    event_source = FactSource(
        title="IEM Cologne Major 2026",
        url="https://blast.tv/cs/tournaments/iem-cologne-major-2026",
        domain="blast.tv",
        body="Team Falcons won IEM Cologne Major 2026 with a viewership record. " * 20,
        fetched_at="2026-07-14T01:00:00+00:00",
        page_time="2026-07-13T20:00:00+00:00",
    )

    async def collect(query: str, intent: str, **_: object) -> FactBundle:
        if intent == "version":
            return FactBundle(
                query=query,
                intent=intent,
                sources=(version_source,),
                authoritative_domains=("counter-strike.net",),
            )
        assert intent == "event"
        return FactBundle(
            query=query,
            intent=intent,
            sources=(event_source,),
            authoritative_domains=("blast.tv",),
        )

    async def primary(
        _prompt: str,
        messages: list[dict[str, str]],
        _max_tokens: int,
    ) -> str:
        if "AnimGraph2" in messages[0]["content"]:
            return "AnimGraph2 已在 CS2 更新中上线[来源1]。"
        return "Team Falcons 赢得 IEM Cologne Major 2026[来源1]。"

    service = ChatService(fact_collector=AsyncMock(side_effect=collect))
    with patch("src.services.llm.try_anthropic", AsyncMock(side_effect=primary)):
        reply = await service.generate_reply(
            "搜索cs2最近有什么更新，和赛事有什么消息，有没有什么利好的消息",
            "session",
        )

    assert "【游戏更新】" in reply
    assert "AnimGraph2" in reply
    assert "【赛事消息】" in reply
    assert "Team Falcons" in reply
    assert "【利好判断】" in reply
    assert "不能直接推出饰品价格会上涨" in reply
    assert "需要可靠的赛事" not in reply


@pytest.mark.asyncio
async def test_natural_market_question_uses_commands_not_model_or_collector() -> None:
    market = AsyncMock()
    collector = AsyncMock()
    service = ChatService(market_get=market, fact_collector=collector)

    with (
        patch("src.services.llm.try_anthropic", AsyncMock()) as primary,
        patch("src.services.llm.fallback_openai", AsyncMock()) as fallback,
    ):
        reply = await service.generate_reply("饰品市场现在适合抄底吗", "session")

    assert "taffy不从聊天模型里猜" in reply
    assert "大盘" in reply
    market.assert_not_awaited()
    collector.assert_not_awaited()
    primary.assert_not_awaited()
    fallback.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_context_builder_never_auto_injects_market_guesses() -> None:
    market = AsyncMock()
    service = ChatService(market_get=market)

    content = await service.build_user_content("这个饰品市场怎么看")

    assert content == "这个饰品市场怎么看"
    market.assert_not_awaited()

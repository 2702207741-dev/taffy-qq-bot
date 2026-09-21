from __future__ import annotations

import pytest

from src.services.fact_intent import FactKind, detect_fact_intent, detect_fact_intents


@pytest.mark.parametrize(
    ("message", "kind", "minimum_sources"),
    [
        ("NiKo 现在在哪个队？", FactKind.TEAM_PLAYER, 2),
        ("听说 NiKo 转会 Spirit 了", FactKind.TEAM_PLAYER, 2),
        ("m0NESY 是谁？", FactKind.TEAM_PLAYER, 2),
        ("不对，他进绿龙 Spirit 了", FactKind.TEAM_PLAYER, 2),
        ("CS2 最新版本改了什么", FactKind.VERSION, 2),
        ("IEM 科隆决赛谁赢了", FactKind.EVENT, 2),
        ("红线现在多少钱", FactKind.PRICE, 1),
        ("今天的 CS2 新闻", FactKind.NEWS, 2),
        ("CS2 的 tick rate 是多少？", FactKind.CS2_GENERAL, 2),
        ("AK爆头多少伤害", FactKind.CS2_GENERAL, 2),
        ("沙二xbox烟怎么丢", FactKind.CS2_GENERAL, 2),
        ("rating 2.1怎么算", FactKind.CS2_GENERAL, 2),
        ("急停怎么练", FactKind.CS2_GENERAL, 2),
        ("cs2经济机制讲一下", FactKind.CS2_GENERAL, 2),
        ("给我一套mirage A1烟", FactKind.CS2_GENERAL, 2),
        ("我听说AK爆头伤害是999", FactKind.CS2_GENERAL, 2),
        ("m0NESY现在在哪个队", FactKind.TEAM_PLAYER, 2),
        ("饰品市场现在适合抄底吗", FactKind.MARKET, 1),
    ],
)
def test_factual_cs2_messages_require_evidence(
    message: str,
    kind: FactKind,
    minimum_sources: int,
) -> None:
    intent = detect_fact_intent(message)

    assert intent is not None
    assert intent.kind is kind
    assert intent.minimum_sources == minimum_sources


def test_primary_source_exceptions_are_explicit_and_narrow() -> None:
    version = detect_fact_intent("CS2 最新版本改了什么")
    event = detect_fact_intent("IEM 科隆决赛谁赢了")
    player = detect_fact_intent("NiKo 现在在哪个队？")

    assert version is not None
    assert "counter-strike.net" in version.authoritative_domains
    assert event is not None
    assert "esl.com" in event.authoritative_domains
    assert player is not None
    assert player.authoritative_domains == ()


def test_mixed_update_and_event_question_keeps_both_evidence_policies() -> None:
    intents = detect_fact_intents("搜索cs2最近有什么更新，和赛事有什么消息，有没有什么利好的消息")

    assert [intent.kind for intent in intents] == [FactKind.VERSION, FactKind.EVENT]


@pytest.mark.parametrize(
    "message",
    [
        "塔菲今天开心吗？",
        "我喜欢 NiKo 的打法",
        "给我讲个冷笑话",
        "你想不想玩 CS2？",
    ],
)
def test_casual_chat_is_not_mislabeled_as_an_external_fact(message: str) -> None:
    assert detect_fact_intent(message) is None

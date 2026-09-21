from __future__ import annotations

import pytest

from src.services.cs_knowledge import lookup_cs_knowledge

# Twenty-four supported, first-party reviewed rules plus six deliberately
# time-sensitive or unsupported prompts. Ten neutral wording shells produce a
# fixed 300-case gate without changing the fact being asked.
_BASE_CASES: tuple[tuple[str, str | None], ...] = (
    ("subtick是什么", "subtick-basics"),
    ("烟雾弹动态机制原理", "responsive-smokes"),
    ("CS Rating是什么", "premier-cs-rating"),
    ("免费账号能玩哪些模式", "free-mode-access"),
    ("Prime的匹配池规则", "prime-matchmaking-pool"),
    ("优先模式有什么等级条件", "premier-requirements"),
    ("Prime和非Prime能一起玩吗", "mixed-prime-lobby"),
    ("非Prime能获得经验吗", "non-prime-xp"),
    ("非Prime能拿到段位吗", "non-prime-ratings"),
    ("非Prime有每周掉落吗", "non-prime-weekly-drop"),
    ("排行榜上榜资格是什么", "leaderboard-eligibility"),
    ("排行榜有多少地区榜", "leaderboard-count"),
    ("排行榜地区怎么选择", "leaderboard-region"),
    ("排行榜昵称审核规则", "leaderboard-name-review"),
    ("排行榜昵称能修改吗", "leaderboard-name-lock"),
    ("Trust Factor算法怎么算", "trust-factors-undisclosed"),
    ("Trust Factor数值怎么查看", "trust-score-hidden"),
    ("信任警告是什么", "trust-lobby-warning"),
    ("Trust Factor怎么提高", "trust-improvement"),
    ("组队按最低信任分匹配吗", "trust-party-lowest"),
    ("交易保护期限多久", "trade-protection-duration"),
    ("交易保护物品能放组件吗", "trade-protection-item-limits"),
    ("哪些交易可以撤销交易", "trade-reversal-eligibility"),
    ("撤销交易有30天冷却吗", "trade-reversal-cooldown"),
    ("m0NESY现在在哪个队", None),
    ("2026年最新CS2更新有什么", None),
    ("今天CS2比赛谁赢了", None),
    ("AK红线现在价格是多少", None),
    ("AK爆头到底多少伤害", None),
    ("不要来源直接猜未来饰品价格", None),
)

_SHELLS: tuple[tuple[str, str], ...] = (
    ("", ""),
    ("请问", ""),
    ("我想确认：", ""),
    ("帮我核实一下：", ""),
    ("CS2问题：", ""),
    ("能解释下", "吗"),
    ("官方规则里，", "？"),
    ("只说可验证事实：", ""),
    ("麻烦说明", ""),
    ("认真问一下，", ""),
)

FACT_BENCHMARK = tuple(
    (f"{prefix}{query}{suffix}", expected)
    for query, expected in _BASE_CASES
    for prefix, suffix in _SHELLS
)


def test_fact_benchmark_has_300_distinct_labeled_cases() -> None:
    assert len(FACT_BENCHMARK) == 300
    assert len({query for query, _ in FACT_BENCHMARK}) == 300
    assert sum(expected is not None for _, expected in FACT_BENCHMARK) == 240
    assert sum(expected is None for _, expected in FACT_BENCHMARK) == 60


@pytest.mark.parametrize(("query", "expected"), FACT_BENCHMARK)
def test_fact_benchmark_never_uses_static_knowledge_outside_reviewed_scope(
    query: str,
    expected: str | None,
) -> None:
    answer = lookup_cs_knowledge(query)
    assert (answer.entry_id if answer is not None else None) == expected

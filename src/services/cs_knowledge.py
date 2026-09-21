"""Small, reviewed CS2 knowledge base for stable first-party facts.

This is intentionally not a trivia dump.  Entries are added only when their
answer can be tied to a durable first-party page and expressed without model
inference.  Time-sensitive updates, rosters, events, prices, and line-ups are
never stored here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class KnowledgeAnswer:
    entry_id: str
    answer: str
    reviewed_on: str
    sources: tuple[KnowledgeSource, ...]
    applicable_scope: str = "稳定规则；不用于版本、阵容、伤害数值或实时赛事"

    def render(self) -> str:
        provenance = "\n".join(
            f"[{index}] {source.title}\n{source.url}"
            for index, source in enumerate(self.sources, 1)
        )
        return (
            "📚 【CS2可验证知识库】\n"
            f"{self.answer}\n"
            f"审核日期: {self.reviewed_on}\n"
            f"适用范围: {self.applicable_scope}\n"
            f"核验来源:\n{provenance}"
        )


@dataclass(frozen=True, slots=True)
class _KnowledgeEntry:
    answer: KnowledgeAnswer
    required_groups: tuple[tuple[str, ...], ...]


_OFFICIAL_FEATURES = KnowledgeSource(
    title="Counter-Strike 2 — official features",
    url="https://www.counter-strike.net/cs2/features?l=english",
)
_PRIME_SUPPORT = KnowledgeSource(
    title="Steam Support — CS2 Prime Status and Free to Play",
    url="https://help.steampowered.com/en/faqs/view/4D81-BB44-4F5C-9B6B",
)
_TRUST_SUPPORT = KnowledgeSource(
    title="Steam Support — CS2 Trust Factor Matchmaking",
    url="https://help.steampowered.com/en/faqs/view/00EF-D679-C76A-C185",
)
_LEADERBOARD_SUPPORT = KnowledgeSource(
    title="Steam Support — Counter-Strike 2 Leaderboards",
    url="https://help.steampowered.com/en/faqs/view/1BCF-10D0-1AF1-302C",
)
_TRADE_PROTECTION_SUPPORT = KnowledgeSource(
    title="Steam Support — Trade Protected Items",
    url="https://help.steampowered.com/en/faqs/view/365F-4BEE-2AE2-7BDD",
)
_TRADE_RESTRICTIONS_SUPPORT = KnowledgeSource(
    title="Steam Support — Trading and Market Restrictions",
    url="https://help.steampowered.com/en/faqs/view/451E-96B3-D194-50FC",
)


def _entry(
    entry_id: str,
    answer: str,
    required_groups: tuple[tuple[str, ...], ...],
    source: KnowledgeSource,
    *,
    scope: str,
) -> _KnowledgeEntry:
    return _KnowledgeEntry(
        KnowledgeAnswer(
            entry_id=entry_id,
            answer=answer,
            reviewed_on="2026-07-14",
            sources=(source,),
            applicable_scope=scope,
        ),
        required_groups,
    )


_ENTRIES = (
    _KnowledgeEntry(
        KnowledgeAnswer(
            entry_id="subtick-basics",
            answer=(
                "Sub-tick是CS2的服务器更新架构。Valve的公开说明是：服务器会记录移动开始、"
                "开枪和投掷发生的准确时刻，而不是只把这些输入归到离散tick边界。"
                "这不等于服务器完全没有tick，也不能单凭这段说明推出具体服务器tickrate。"
            ),
            reviewed_on="2026-07-14",
            sources=(_OFFICIAL_FEATURES,),
        ),
        (("subtick", "sub-tick", "子tick", "子刷新"),),
    ),
    _KnowledgeEntry(
        KnowledgeAnswer(
            entry_id="responsive-smokes",
            answer=(
                "CS2烟雾是会与环境和玩法事件交互的动态体积对象。Valve说明中明确包括："
                "子弹和高爆手雷可以短暂清开烟雾视线；烟雾会随空间、门窗和走廊扩散，"
                "并会响应统一光照。"
            ),
            reviewed_on="2026-07-14",
            sources=(_OFFICIAL_FEATURES,),
        ),
        (("烟雾", "烟雾弹", "smoke"), ("机制", "原理", "动态", "子弹", "手雷", "he")),
    ),
    _KnowledgeEntry(
        KnowledgeAnswer(
            entry_id="premier-cs-rating",
            answer=(
                "CS Rating是CS2优先模式中可见的表现评分，并用于全球和地区排行榜。"
                "Valve的功能页说明，玩家需要在优先模式中进行比赛来获得该评分。"
                "它不是HLTV Rating 2.x，也不能直接换算为传统竞技段位。"
            ),
            reviewed_on="2026-07-14",
            sources=(_OFFICIAL_FEATURES,),
        ),
        (("cs rating", "cs评分", "优先评分"),),
    ),
    _entry(
        "free-mode-access",
        "CS2免费账号可以进入除优先模式以外的游戏模式和匹配类型；优先模式需要Prime状态。",
        (("免费账号", "非prime", "没有prime"), ("模式", "能玩", "匹配")),
        _PRIME_SUPPORT,
        scope="当前Steam支持页所述的免费版模式权限",
    ),
    _entry(
        "prime-matchmaking-pool",
        "Prime玩家在各游戏模式中会与其他Prime玩家匹配。Prime与非Prime组成预组队时，整个大厅按非Prime处理。",
        (("prime", "优先账户"), ("匹配池", "匹配到", "一起匹配")),
        _PRIME_SUPPORT,
        scope="官方匹配中的Prime分组规则",
    ),
    _entry(
        "premier-requirements",
        "进入CS2优先模式需要拥有Prime状态，并达到个人资料等级10。",
        (("优先模式", "premier"), ("条件", "等级", "进不去", "要求")),
        _PRIME_SUPPORT,
        scope="优先模式的账号准入条件",
    ),
    _entry(
        "mixed-prime-lobby",
        "Prime与非Prime玩家可以通过预组队一起匹配，但该大厅会被视为非Prime大厅，不能进入仅Prime比赛。",
        (("prime和非prime", "优先和非优先", "混合大厅"),),
        _PRIME_SUPPORT,
        scope="Prime与非Prime预组队",
    ),
    _entry(
        "non-prime-xp",
        "没有Prime状态的账号不会从任何模式或服务器获得经验值，也不能靠这些对局获得服役勋章。",
        (("非prime", "没有prime", "免费账号"), ("经验", "xp", "服役勋章")),
        _PRIME_SUPPORT,
        scope="非Prime账号的经验与服役勋章资格",
    ),
    _entry(
        "non-prime-ratings",
        "竞技段位和Counter-Strike Rating只向Prime账号发放；非Prime账号不会获得这两类评级。",
        (("非prime", "没有prime", "免费账号"), ("段位", "rating", "评分")),
        _PRIME_SUPPORT,
        scope="非Prime账号的官方评级资格",
    ),
    _entry(
        "non-prime-weekly-drop",
        "每周补给包需要Prime状态；非Prime账号没有每周补给包资格。",
        (("非prime", "没有prime", "免费账号"), ("每周", "补给包", "掉落")),
        _PRIME_SUPPORT,
        scope="每周补给包资格",
    ),
    _entry(
        "leaderboard-eligibility",
        "公开排行榜面向参加优先模式的Prime玩家；进入地区公开榜还需要位于该地区前1000名。",
        (("排行榜", "leaderboard"), ("资格", "上榜", "前1000")),
        _LEADERBOARD_SUPPORT,
        scope="CS2公开排行榜资格",
    ),
    _entry(
        "leaderboard-count",
        "CS2共有9个官方排行榜：好友、世界，以及非洲、亚洲、澳洲、中国、欧洲、北美和南美地区榜。",
        (("排行榜", "leaderboard"), ("几个", "多少", "地区榜")),
        _LEADERBOARD_SUPPORT,
        scope="官方排行榜的分类",
    ),
    _entry(
        "leaderboard-region",
        "玩家不能手动选择地区排行榜；所属地区由其最常游玩的服务器位置决定。",
        (("排行榜地区", "地区排行榜"), ("选择", "怎么分", "归属")),
        _LEADERBOARD_SUPPORT,
        scope="地区排行榜归属",
    ),
    _entry(
        "leaderboard-name-review",
        "排行榜昵称提交后会经过审核；未获批准时状态会被重置，需要重新提交。",
        (("排行榜昵称", "榜单名字"), ("审核", "不通过", "提交")),
        _LEADERBOARD_SUPPORT,
        scope="排行榜昵称审核",
    ),
    _entry(
        "leaderboard-name-lock",
        "排行榜昵称一旦审核通过，会在该赛季剩余时间内锁定，不能再次修改。",
        (("排行榜昵称", "榜单名字"), ("修改", "锁定", "改名")),
        _LEADERBOARD_SUPPORT,
        scope="已批准排行榜昵称的修改限制",
    ),
    _entry(
        "trust-factors-undisclosed",
        "Valve没有公开Trust Factor的完整因素列表，因为系统会持续调整，固定清单会很快过时。",
        (("trust factor", "信任因素", "信任分"), ("因素", "算法", "怎么算")),
        _TRUST_SUPPORT,
        scope="Trust Factor公开信息边界",
    ),
    _entry(
        "trust-score-hidden",
        "目前没有官方方式查看自己的Trust Factor数值。",
        (("trust factor", "信任因素", "信任分"), ("查看", "多少", "数值")),
        _TRUST_SUPPORT,
        scope="Trust Factor可见性",
    ),
    _entry(
        "trust-lobby-warning",
        "Prime玩家组队进行竞技匹配前，系统可能提示某位同为Prime的队员会因较低Trust Factor影响匹配体验；提示不公开具体分数。",
        (("信任警告", "trust警告", "黄色警告", "红色警告"),),
        _TRUST_SUPPORT,
        scope="竞技组队前的Trust Factor提示",
    ),
    _entry(
        "trust-improvement",
        "Steam支持页给出的建议是合法游玩，并成为CS2和Steam社区中的积极成员；不存在公开的单项刷分公式。",
        (("trust factor", "信任因素", "信任分"), ("提高", "改善", "怎么升")),
        _TRUST_SUPPORT,
        scope="官方给出的Trust Factor改善建议",
    ),
    _entry(
        "trust-party-lowest",
        "组队匹配时，系统采用队伍成员中最低的Trust Factor来处理该队匹配。",
        (("组队", "开黑", "大厅"), ("最低信任", "trust factor", "信任分")),
        _TRUST_SUPPORT,
        scope="组队匹配的Trust Factor规则",
    ),
    _entry(
        "trade-protection-duration",
        "通过交易收到的CS2物品会带有7天Trade Protected状态；期满后保护结束，交易不能再撤销。",
        (("交易保护", "trade protected"), ("多久", "几天", "期限")),
        _TRADE_PROTECTION_SUPPORT,
        scope="通过受保护交易收到的CS2物品",
    ),
    _entry(
        "trade-protection-item-limits",
        "Trade Protected期间，相关物品不能被消耗、修改或再次转移，也不能移入Storage Unit。",
        (("交易保护", "trade protected"), ("组件", "storage", "修改", "转移", "能干嘛")),
        _TRADE_PROTECTION_SUPPORT,
        scope="CS2物品处于7天Trade Protected期间",
    ),
    _entry(
        "trade-reversal-eligibility",
        "只有创建时明确带黄色Trade Protected盾牌的交易可以撤销；该功能上线前的旧交易不能追溯撤销。",
        (("撤销交易", "交易撤回", "交易逆转"), ("哪些", "条件", "能不能")),
        _TRADE_PROTECTION_SUPPORT,
        scope="Steam Trade Protected交易撤销",
    ),
    _entry(
        "trade-reversal-cooldown",
        "发起交易撤销的账号会获得30天交易与市场冷却；其交易对手不受这项冷却影响。",
        (("撤销交易", "交易撤回", "交易逆转"), ("30天", "冷却", "处罚")),
        _TRADE_PROTECTION_SUPPORT,
        scope="账号主动发起Trade Protected撤销之后",
    ),
    _entry(
        "trade-protection-no-escrow",
        "由于采用Trade Protection，CS2物品不再受传统交易暂挂或交易托管影响，确认后会立即在账号间转移。",
        (("cs2交易", "饰品交易"), ("暂挂", "托管", "escrow", "立即到账")),
        _TRADE_PROTECTION_SUPPORT,
        scope="当前CS2受保护物品交易",
    ),
    _entry(
        "trade-protection-cs2-only",
        "Steam支持页当前说明：CS2是目前唯一启用Trade Protected物品的游戏。",
        (("交易保护", "trade protected"), ("哪些游戏", "只有cs2", "支持游戏")),
        _TRADE_PROTECTION_SUPPORT,
        scope="Steam当前启用Trade Protected的游戏范围",
    ),
    _entry(
        "trade-protection-no-mixing",
        "受Trade Protected保护的CS2物品不能与没有同等级保护的其他游戏物品放在同一笔交易中。",
        (("cs2物品", "饰品"), ("混合交易", "其他游戏", "一起交易")),
        _TRADE_PROTECTION_SUPPORT,
        scope="跨游戏物品的同一笔Steam交易",
    ),
    _entry(
        "purchased-item-trade-date",
        "从游戏内或Steam社区市场购买的物品，在库存标出的日期之前不可交易或上架市场；应以物品自身显示日期为准。",
        (("购买", "市场买", "游戏内买"), ("交易冷却", "不可交易", "几天", "什么时候能卖")),
        _TRADE_RESTRICTIONS_SUPPORT,
        scope="游戏内或Steam社区市场购买的物品",
    ),
    _entry(
        "cancel-held-trade-restriction",
        "取消一笔已经接受且仍在交易暂挂中的交易，会触发7天交易限制。",
        (("取消", "撤销"), ("交易暂挂", "trade hold"), ("7天", "限制", "后果")),
        _TRADE_RESTRICTIONS_SUPPORT,
        scope="仍处于传统交易暂挂的已接受交易",
    ),
    _entry(
        "vac-cs2-item-restrictions",
        "账号出现VAC封禁、冷却或Overwatch封禁后，将无法使用CS2商店、交易CS2物品或继续获得物品掉落。",
        (("vac", "overwatch封禁", "监管封禁"), ("饰品", "交易", "掉落", "商店")),
        _TRADE_RESTRICTIONS_SUPPORT,
        scope="存在相关封禁或冷却的Steam账号",
    ),
    _entry(
        "support-cannot-remove-trade-restrictions",
        "Steam支持明确说明，不会人工修改交易或社区市场限制，无论限制出现的具体原因是什么。",
        (("交易限制", "市场限制"), ("客服", "申诉解除", "人工解除")),
        _TRADE_RESTRICTIONS_SUPPORT,
        scope="Steam交易和社区市场限制",
    ),
    _entry(
        "map-upgrade-tiers",
        "Valve把CS2地图升级分为三类：保留布局并改善光照和可读性的Touchstone、使用Source 2新渲染能力重制的Upgrade，以及从头完整重建的Overhaul。",
        (("地图",), ("touchstone", "upgrade", "overhaul", "重制分类", "升级分类")),
        _OFFICIAL_FEATURES,
        scope="CS2发布功能页所述的地图升级分类",
    ),
    _entry(
        "source2-workshop-tools",
        "CS2的Source 2工具与渲染功能向社区地图制作者开放，便于构建、实验和迭代地图。",
        (("source 2工具", "起源2工具", "地图工具", "workshop tools"),),
        _OFFICIAL_FEATURES,
        scope="CS2社区地图制作工具",
    ),
    _entry(
        "cs2-audio-rework",
        "Valve说明CS2对声音进行了重做，使其更能反映物理环境、更清晰地表达游戏状态，并重新平衡听觉体验。",
        (("声音", "音频", "脚步声"), ("重做", "source 2", "物理环境", "改进")),
        _OFFICIAL_FEATURES,
        scope="CS2发布功能页的音频系统概述；不回答具体听距数值",
    ),
    _entry(
        "cs2-visual-effects",
        "CS2在Source 2光照与粒子系统下更新了水、爆炸、火焰、烟雾、枪口火光、弹道和命中效果等视觉表现。",
        (("特效", "粒子", "枪口火光", "弹道", "命中效果"),),
        _OFFICIAL_FEATURES,
        scope="CS2发布功能页列出的视觉效果范围",
    ),
    _entry(
        "inventory-carry-over",
        "Valve的CS2功能页说明，玩家原有的Counter-Strike库存物品会带入CS2。",
        (("库存", "饰品"), ("继承", "带入cs2", "转到cs2")),
        _OFFICIAL_FEATURES,
        scope="从CS:GO迁移到CS2时的既有库存",
    ),
    _entry(
        "item-lighting-materials",
        "带入CS2的库存物品会受Source 2光照和材质系统呈现；这是视觉渲染变化，不代表物品属性或价格变化。",
        (("饰品光照", "饰品材质", "source 2材质", "起源2材质"),),
        _OFFICIAL_FEATURES,
        scope="CS2库存物品的Source 2视觉呈现",
    ),
)


def _normalise(query: str) -> str:
    return re.sub(r"\s+", " ", str(query).strip().casefold())


def _marker_hit(text: str, marker: str) -> bool:
    """ASCII word markers need boundaries so ``he`` cannot hit ``the``.

    The boundaries are explicit ASCII ranges because ``\\b`` treats CJK
    characters as word characters, which would break markers glued to Chinese
    text like ``cs2的subtick``.
    """

    if marker.isascii() and marker and (marker.isalnum() or "-" in marker):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(marker)}(?![A-Za-z0-9])"
        return re.search(pattern, text) is not None
    return marker in text


def lookup_cs_knowledge(query: str) -> KnowledgeAnswer | None:
    """Return one exact reviewed entry; never use fuzzy model matching."""

    text = _normalise(query)
    if not text:
        return None
    matches = [
        entry.answer
        for entry in _ENTRIES
        if all(
            any(_marker_hit(text, marker) for marker in group) for group in entry.required_groups
        )
    ]
    return matches[0] if len(matches) == 1 else None


def knowledge_entry_count() -> int:
    """Return the reviewed topic count for local readiness reporting and tests."""

    return len(_ENTRIES)


__all__ = [
    "KnowledgeAnswer",
    "KnowledgeSource",
    "knowledge_entry_count",
    "lookup_cs_knowledge",
]

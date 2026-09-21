"""Routing predicates shared by the chat plugin matchers."""

from __future__ import annotations

from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent, PrivateMessageEvent

from src.state.persona_memory_repository import PersonaScope

EXACT_COMMANDS = frozenset(
    {
        "大盘",
        "抄底",
        "K线高清",
        "k线高清",
        "菜单",
        "menu",
        "help",
        "综合指数",
        "异动",
        "预测",
        "热门榜",
        "热度榜",
        "流动性榜",
        "涨跌榜",
        "装备指数",
        "库存查询",
        "娱乐系统",
        "娱乐",
        "日报",
        "周报",
        "我的预警",
        "我的自选",
        "自选查价",
        "定时开",
        "定时关",
        "定时状态",
        "定时加群",
        "测试推送",
        "随机皮肤",
        "塔菲记忆",
        "清空长期记忆",
        "清空对话",
        "reset",
        "清空记忆",
        "忘记我",
        "塔菲风格",
        "塔菲状态",
        "撤销塔菲调整",
        "重置塔菲风格",
        "塔菲学习",
        "塔菲学习 开",
        "塔菲学习 关",
        "塔菲学习 状态",
    }
)

COMMAND_PREFIXES = (
    "库存查询",
    "异动榜",
    "异动",
    "涨跌榜",
    "流动性榜",
    "查询",
    "饰品查询",
    "分析",
    "饰品分析",
    "K线高清",
    "k线高清",
    "K线",
    "k线",
    "预警",
    "加自选",
    "删自选",
    "对比",
    "定时时间",
    "开箱",
    "十连开",
    "炼金",
    "塔菲学习",
)

CONVERSATIONAL_MARKERS = (
    "一下",
    "这个",
    "那个",
    "什么",
    "怎么",
    "为什么",
    "帮我",
    "能不能",
    "可以",
    "怎么办",
)


def matches_command_text(value: str) -> bool:
    """Decide whether group text should be claimed as a bot command."""

    message = value.strip()
    if message in EXACT_COMMANDS:
        return True

    if message == "记住" or message.startswith("记住 "):
        return len(message[len("记住") :].strip()) <= 200
    if message.startswith("以后叫我"):
        return len(message[len("以后叫我") :].strip()) <= 20
    if message == "忘掉记忆" or message.startswith("忘掉记忆 "):
        return len(message[len("忘掉记忆") :].strip()) <= 10

    for prefix in COMMAND_PREFIXES:
        if not message.startswith(prefix):
            continue
        remainder = message[len(prefix) :].strip()
        if not remainder:
            return True
        if len(remainder) > 20:
            return False
        if any(marker in remainder for marker in CONVERSATIONAL_MARKERS):
            return False
        return True
    return False


def is_keyword_event(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and matches_command_text(event.get_plaintext())


def is_group_mention(event: Event) -> bool:
    return isinstance(event, GroupMessageEvent) and event.is_tome() and not is_keyword_event(event)


def is_private_event(event: Event) -> bool:
    return isinstance(event, PrivateMessageEvent)


def session_key(event: Event) -> str:
    """Return the stable chat-history and rate-limit key for an event."""

    if isinstance(event, PrivateMessageEvent):
        return f"p_{event.user_id}"
    if isinstance(event, GroupMessageEvent):
        return f"g_{event.group_id}_{event.user_id}"
    raise TypeError(f"Unsupported chat event: {type(event).__name__}")


def persona_scope(event: Event) -> PersonaScope:
    """Derive a memory boundary from the trusted QQ event, never message text."""

    if isinstance(event, PrivateMessageEvent):
        return PersonaScope(owner_user_id=int(event.user_id))
    if isinstance(event, GroupMessageEvent):
        return PersonaScope(owner_user_id=int(event.user_id), group_id=int(event.group_id))
    raise TypeError(f"Unsupported chat event: {type(event).__name__}")


# Compatibility constants/functions historically exposed by ``chat``.
KW = list(EXACT_COMMANDS)
skey = session_key

"""NoneBot matcher wiring for the CS2 chat plugin.

Business logic lives in sibling modules while this entrypoint only registers
matchers and connects dependencies. Historical public names remain exported
for other plugins.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from nonebot import logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.rule import Rule

from src.core.messages import split_text, text_message
from src.core.rate_limit import SlidingWindowLimiter
from src.services import history_summary
from src.services.authority import DENIED_MESSAGE
from src.services.csqaq import csqaq_service
from src.services.dip_backtest import build_dip_report, format_dip_report
from src.services.index_history import index_history_store
from src.services.market import get_broad_summary
from src.services.market_catalog import search_market_catalog
from src.services.market_catalog_store import get_market_catalog_entries, market_catalog_store
from src.services.market_history import market_history
from src.services.market_scoring import build_trade_score_reply, score_summary_line
from src.services.market_temperature import TemperatureUnavailable, get_market_temperature
from src.services.persona import classify_persona, render_command_persona_shell
from src.services.persona_policy import persona_v3_enabled_for_scope
from src.services.steam_inventory import get_inventory_report
from src.services.steamdt_kline import (
    KlineScreenshotError,
    capture_broad_backtest_png,
    steamdt_kline_screenshots,
)
from src.services.v4_policy import command_requires_v4_pilot, v4_enabled_for_user
from src.state.persona_memory_repository import (
    PersonaMemoryCapacityError,
    PersonaScope,
)
from src.state.persona_style_repository import (
    is_ambiguous_style_feedback,
    parse_style_feedback,
)
from src.state.runtime import (
    csqaq_good_ids,
    persona_memories,
    persona_relationships,
    persona_styles,
    session_summaries,
)

from .. import feature_gate
from ..session_store import store
from . import (
    cards,  # noqa: F401  (registers card matchers)
    case_ev_commands,  # noqa: F401  (registers the case-EV matcher)
    group_pulse,  # noqa: F401  (registers eavesdrop + opt-in matchers)
    insight,  # noqa: F401  (registers interpretation matchers)
    inspect_commands,  # noqa: F401  (registers the inspect matcher)
    knowledge_commands,  # noqa: F401  (registers knowledge lookup matchers)
    learning_commands,  # noqa: F401  (registers learning impression matchers)
    persona_audit_command,  # noqa: F401  (registers the persona audit matcher)
    proactive_commands,  # noqa: F401  (registers proactive opt-in matchers)
    slang_commands,  # noqa: F401  (registers slang dictionary matchers)
    sticker_commands,  # noqa: F401  (registers sticker admin matchers)
    wear_commands,  # noqa: F401  (registers the wear lookup matcher)
)
from .commands import (
    BroadKlineScreenshotRequest,
    ChatCommandService,
    CommandResult,
    ItemKlineScreenshotRequest,
    _command_operation,
)
from .compare import fetch_csqaq_side
from .market import (
    catalog_search_query,
    resolve_catalog_name,
)
from .market import (
    resolve_name as resolve_local_name,
)
from .media import _send_random_image, _send_random_video
from .routing import (
    is_group_mention,
    is_keyword_event,
    is_private_event,
    persona_scope,
    session_key,
)

_MAX_HANDLER_INPUT = 4000
_CHAT_RATE_LIMITER = SlidingWindowLimiter(20, 60)
_MAX_FEEDBACK_REFERENCES = 10_000


def _first_image_segment(event: MessageEvent) -> tuple[str | None, str | None]:
    """Extract ``(url, file_id)`` of the first inbound image, if any."""

    try:
        for segment in event.message:
            if getattr(segment, "type", None) != "image":
                continue
            data = getattr(segment, "data", {}) or {}
            return data.get("url"), data.get("file")
    except Exception:
        return None, None
    return None, None


async def _deliver_tool_actions(bot: Bot, event: MessageEvent, actions: list[object]) -> None:
    """Run side effects the model requested (stickers, K-line images).

    An explicitly requested sticker skips the probability gate but still
    honours the per-scope cooldown and daily cap; K-line images go through
    the guarded generated-image path.  Failures only log — the chat reply
    already told the user what was requested.
    """

    from pathlib import Path

    from src.services import stickers as sticker_service
    from src.services.tools import ToolAction

    scope_key = None
    for action in actions:
        if not isinstance(action, ToolAction):
            continue
        if action.kind == "send_sticker":
            if scope_key is None:
                scope_key = sticker_service.scope_key_for_event(event)
            if scope_key is None or not sticker_service.cooldown_and_cap_allow(scope_key):
                return
            try:
                payload = action.payload
                path = Path(payload) if isinstance(payload, (str, Path)) else None
                if path is None or not path.is_file():
                    return
                await bot.send(
                    event,
                    MessageSegment.image(file=f"file:///{path.as_posix()}"),
                )
                sticker_service.record_send(scope_key)
            except Exception as exc:
                logger.warning("Tool sticker send failed error={}", type(exc).__name__)
                return
        elif action.kind == "send_kline_image":
            from .media import send_generated_image

            payload = action.payload
            png = payload if isinstance(payload, bytes) else None
            if not png:
                return
            try:
                await send_generated_image(bot, event, png)
            except Exception as exc:
                logger.warning("Tool K-line image send failed error={}", type(exc).__name__)
                return


async def _vision_context_and_capture(bot: Bot, event: MessageEvent) -> tuple[bool, str | None]:
    """Download + describe an inbound image for the persona context.

    Returns ``(captured_file_flag, description_snippet)``; the snippet is
    ``None`` whenever vision is off, the download failed or the provider
    was unavailable (fail-closed: the chat simply continues without it).
    """

    from src.services import vision

    if not vision.vision_enabled():
        return (event.message is not None, None)
    url, file_id = _first_image_segment(event)
    if url is None and file_id is None:
        return (False, None)
    try:
        saved = await vision.download_inbound_image(url, file_id)
    except Exception as exc:
        logger.warning("Inbound image capture failed error={}", type(exc).__name__)
        saved = None
    if saved is None:
        return (True, None)
    # Auto-collected candidates stay unreviewed until an admin accepts.
    try:
        from src.services.stickers import autocollect_enabled, record_pending_sticker

        if autocollect_enabled():
            pending = record_pending_sticker(saved)
            if pending is not None:
                # Reconnaissance phase two: describe the candidate for the
                # review command (sidecar metadata; curated library untouched).
                try:
                    await vision.describe_pending_stickers([pending], limit=1)
                except Exception as exc:
                    logger.warning("Pending sticker describe failed error={}", type(exc).__name__)
    except Exception as exc:
        logger.warning("Pending sticker capture failed error={}", type(exc).__name__)
    try:
        snippet = await vision.describe_for_context(saved)
    except Exception as exc:
        logger.warning("Vision description failed error={}", type(exc).__name__)
        return (True, None)
    return (True, snippet)


@dataclass(frozen=True, slots=True)
class _FeedbackReference:
    reply_sha256: str
    scene: str
    created_at: int


_feedback_references: OrderedDict[str, _FeedbackReference] = OrderedDict()


def _record_feedback_reference(
    event: Event,
    reply: str | None,
    scene: str | None,
    *,
    now: int | None = None,
) -> None:
    try:
        key = skey(event)
    except (TypeError, AttributeError):
        return
    if not reply or not scene:
        _feedback_references.pop(key, None)
        return
    _feedback_references[key] = _FeedbackReference(
        hashlib.sha256(str(reply).encode("utf-8")).hexdigest(),
        str(scene)[:40],
        int(time.time()) if now is None else int(now),
    )
    _feedback_references.move_to_end(key)
    while len(_feedback_references) > _MAX_FEEDBACK_REFERENCES:
        _feedback_references.popitem(last=False)


async def st_get(
    path: str,
    params: dict[str, Any] | None = None,
    ttl: float | None = None,
) -> dict[str, Any]:
    """Lazy compatibility facade that avoids an import-time package cycle."""

    from ..chat_utils import st_get as request

    return await request(path, params, ttl)


async def st_post(
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lazy compatibility facade that avoids an import-time package cycle."""

    from ..chat_utils import st_post as request

    return await request(path, body)


async def deepseek_chat(
    message: str,
    session_id: str = "",
    *,
    scope: PersonaScope | None = None,
    tool_actions: list[object] | None = None,
) -> str:
    """Load the LLM client only when ordinary chat actually needs it."""

    from ..chat_utils import deepseek_chat as request

    return await request(message, session_id, persona_scope=scope, tool_actions=tool_actions)


async def resolve_name(query: str) -> str | None:
    """Resolve reviewed nicknames first, then one unique full-catalog match."""

    async def requested(market_name: str) -> str:
        try:
            await market_catalog_store.mark_requested(market_name)
        except Exception as exc:
            logger.warning("Market catalog request marker unavailable error={}", type(exc).__name__)
        return market_name

    local_name = await resolve_local_name(query)
    if local_name is not None:
        return await requested(local_name)
    try:
        durable_entries = await get_market_catalog_entries()
    except Exception as exc:
        logger.warning("Market catalog lookup unavailable error={}", type(exc).__name__)
        durable_entries = ()
    durable_match = resolve_catalog_name(query, durable_entries)
    if durable_match is not None:
        return await requested(durable_match)
    live_entries = await search_market_catalog(catalog_search_query(query))
    if live_entries:
        try:
            await market_catalog_store.persist_search_entries(live_entries)
        except Exception as exc:
            logger.warning("Market catalog persistence unavailable error={}", type(exc).__name__)
    combined = {entry.market_name: entry for entry in (*durable_entries, *live_entries)}
    resolved = resolve_catalog_name(query, tuple(combined.values()))
    return await requested(resolved) if resolved is not None else None


async def _market_column(command: str) -> str:
    if command == "综合指数":
        return await market_history.breadth_report()
    return await market_history.render_column(command)


async def _market_ranking(board: str, category: str, limit: int) -> str:
    return await market_history.render_ranking(board, category, limit)


async def _dip_report() -> str:
    """Best-effort bottom-fishing scenario card from persisted daily closes."""

    try:
        await index_history_store.backfill_if_due()
        closes = await index_history_store.load_closes()
        if not closes:
            return format_dip_report(build_dip_report(()))
        temperature_line = "盘面温度: 暂不可用"
        try:
            broad = await _command_service.get_broad_market()
            temperature = await get_market_temperature(broad)
            if isinstance(temperature, TemperatureUnavailable):
                temperature_line = "盘面温度: 暂不可用"
            else:
                temperature_line = (
                    f"盘面温度: {temperature.temperature_state}｜{temperature.detail}"
                )
        except Exception as exc:
            logger.warning("Dip temperature unavailable error={}", type(exc).__name__)
        return format_dip_report(build_dip_report(closes, temperature_line=temperature_line))
    except Exception as exc:
        logger.exception("Dip report failed error={}", type(exc).__name__)
        return "⚠️ 抄底数据这次没算出来，请稍后再试。"


# The CSQAQ gateway consults this durable cache before any network call, so
# resolved item ids survive restarts under the documented one-request-per-second
# upstream limit.
csqaq_service.attach_good_id_cache(csqaq_good_ids)


async def _trade_score_reply(market_name: str, user_query: str) -> str:
    return await build_trade_score_reply(market_name, user_query=user_query)


async def _trade_score_summary(market_name: str, user_query: str) -> str | None:
    return await score_summary_line(market_name, user_query=user_query)


_command_service = ChatCommandService(
    market_get=st_get,
    market_post=st_post,
    name_resolver=resolve_name,
    temperature_provider=get_market_temperature,
    inventory_provider=get_inventory_report,
    broad_provider=get_broad_summary,
    market_column_provider=_market_column,
    ranking_provider=_market_ranking,
    dip_provider=_dip_report,
    csqaq_side_provider=fetch_csqaq_side,
    score_provider=_trade_score_reply,
    score_summary_provider=_trade_score_summary,
)


async def cmd(message: str) -> CommandResult:
    """Compatibility facade for command execution."""

    return await _command_service.execute(message)


def skey(event: Event) -> str:
    """Compatibility facade for the stable session-key helper."""

    return session_key(event)


def _kw(event: Event) -> bool:
    return is_keyword_event(event)


def _gr(event: Event) -> bool:
    return is_group_mention(event)


def _pr(event: Event) -> bool:
    return is_private_event(event)


async def _send_text_reply(
    bot: Bot,
    event: MessageEvent,
    reply: str,
    *,
    feedback_scene: str | None = None,
) -> None:
    for part in split_text(reply, limit=800):
        await bot.send(event, text_message(part), at_sender=False)
    _record_feedback_reference(event, reply, feedback_scene)
    input_text = event.get_plaintext().strip()
    await feature_gate.record_event_usage(
        event=event,
        feature=_chat_feature(input_text or "chat"),
        input_text=input_text,
        output_text=reply,
    )


async def _wait_kline_with_progress(
    bot: Bot,
    event: MessageEvent,
    task: asyncio.Task[bytes],
) -> bytes:
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except TimeoutError:
        try:
            await bot.send(
                event,
                text_message("K线数据已经核对完，正在本地绘图。"),
                at_sender=False,
            )
        except Exception:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return await task


async def _command_with_progress(
    bot: Bot,
    event: MessageEvent,
    message: str,
) -> CommandResult:
    task = asyncio.create_task(cmd(message))
    if not message.strip().startswith("库存查询"):
        return await task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except TimeoutError:
        try:
            await bot.send(
                event,
                text_message("正在读取最新公开库存快照，稍等一下。"),
                at_sender=False,
            )
        except Exception:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return await task


async def _send_broad_kline_reply(
    bot: Bot,
    event: MessageEvent,
    *,
    high_definition: bool = False,
    max_days_ago: int | None = None,
) -> None:
    try:
        if max_days_ago is not None:
            request = capture_broad_backtest_png(max_days_ago, high_definition=high_definition)
        else:
            request = (
                steamdt_kline_screenshots.get_broad_hd_png()
                if high_definition
                else steamdt_kline_screenshots.get_broad_png()
            )
        png = await _wait_kline_with_progress(bot, event, asyncio.create_task(request))
    except KlineScreenshotError as exc:
        logger.warning("SteamDT K-line screenshot failed error={}", type(exc).__name__)
        await bot.send(
            event,
            text_message("当前K线图获取失败了喵，请稍后再试。"),
            at_sender=False,
        )
        return

    await bot.send(
        event,
        MessageSegment.image(file=png),
        at_sender=False,
    )


async def _send_item_kline_reply(
    bot: Bot,
    event: MessageEvent,
    market_name: str,
    *,
    high_definition: bool = False,
    special_style: str | None = None,
    platform: str | None = None,
) -> None:
    try:
        request = (
            steamdt_kline_screenshots.get_item_hd_png(
                market_name,
                special_style=special_style,
                platform=platform,
            )
            if high_definition
            else steamdt_kline_screenshots.get_item_png(
                market_name,
                special_style=special_style,
                platform=platform,
            )
        )
        png = await _wait_kline_with_progress(bot, event, asyncio.create_task(request))
    except KlineScreenshotError as exc:
        logger.warning("Item K-line screenshot failed error={}", type(exc).__name__)
        await bot.send(
            event,
            text_message("当前饰品K线图获取失败了喵，请稍后再试。"),
            at_sender=False,
        )
        return

    await bot.send(
        event,
        MessageSegment.image(file=png),
        at_sender=False,
    )


def _normalized_memory_command(message: str) -> str:
    text = str(message).strip()
    if text.startswith("/"):
        text = text[1:].strip()
    return text


def _is_persona_memory_command(message: str) -> bool:
    text = _normalized_memory_command(message)
    if text in {
        "reset",
        "清空记忆",
        "忘记我",
        "塔菲记忆",
        "清空长期记忆",
        "清空对话",
        "记住",
        "忘掉记忆",
        "塔菲我们熟吗",
        "我们熟吗",
    }:
        return True
    return text.startswith(("记住 ", "以后叫我", "忘掉记忆 "))


def _is_persona_style_command(message: str) -> bool:
    text = _normalized_memory_command(message)
    return text in {"塔菲风格", "撤销塔菲调整", "重置塔菲风格"} or text.startswith("塔菲学习")


def _style_summary(values: tuple[int, ...]) -> str:
    names = ("简洁", "温暖", "调侃", "主动性", "口癖浓度", "直接程度")
    return " / ".join(f"{name} {value:+d}" for name, value in zip(names, values, strict=True))


def _style_values(style: Any) -> tuple[int, int, int, int, int, int]:
    return (
        style.concise,
        style.warm,
        style.teasing,
        style.proactive,
        style.catchphrase,
        style.direct,
    )


async def _execute_persona_style_command(event: Event, message: str) -> str | None:
    text = _normalized_memory_command(message)
    if not _is_persona_style_command(text):
        return None
    scope = persona_scope(event)
    if not persona_v3_enabled_for_scope(scope):
        return "塔菲人格V3目前只对白名单试用，当前范围还没开放。"
    try:
        if text.startswith("塔菲学习"):
            action = text[len("塔菲学习") :].strip()
            if action in {"开", "开启", "on"}:
                profile = await persona_styles.set_learning(scope, True)
                return (
                    f"学风格已开启，只在{scope.label}生效。"
                    "taffy只记参数变化，不保存你的反馈原文或回复正文。"
                )
            if action in {"关", "关闭", "off"}:
                await persona_styles.set_learning(scope, False)
                return f"学风格已关闭。{scope.label}已有参数保留，但不会继续自动调整。"
            if action in {"", "状态", "status"}:
                profile = await persona_styles.get(scope)
                state = "开启" if profile.learning_enabled else "关闭"
                return f"塔菲学习：{state}｜{_style_summary(_style_values(profile.style))}"
            return "这样发：塔菲学习 开 / 关 / 状态。"
        if text == "塔菲风格":
            profile = await persona_styles.get(scope)
            values = _style_values(profile.style)
            state = "开" if profile.learning_enabled else "关"
            return (
                f"🎛️ 【塔菲风格·{scope.label}】\n学习: {state}\n"
                f"{_style_summary(values)}\n──────────\n"
                "范围 -2～+2；只接受紧跟上一条回复的明确反馈。"
            )
        if text == "撤销塔菲调整":
            undone_profile = await persona_styles.undo(scope)
            if undone_profile is None:
                return "当前范围没有可以撤销的风格调整。"
            values = _style_values(undone_profile.style)
            return f"上一笔调整撤销了。现在是：{_style_summary(values)}"
        if text == "重置塔菲风格":
            profile = await persona_styles.reset(scope)
            state = "仍开启" if profile.learning_enabled else "仍关闭"
            return f"风格参数已归零，学习开关{state}。长期记忆和近期对话都没动。"
    except Exception as exc:
        logger.exception("Persona style command failed error={}", type(exc).__name__)
        return "风格设置这次没保存好，普通聊天不受影响，晚点再试。"
    return None


async def _execute_natural_persona_feedback(event: Event, message: str) -> str | None:
    try:
        scope = persona_scope(event)
    except TypeError:
        return None
    if not persona_v3_enabled_for_scope(scope):
        return None
    try:
        profile = await persona_styles.get(scope)
        if not profile.learning_enabled:
            return None
        delta = parse_style_feedback(message)
        ambiguous = is_ambiguous_style_feedback(message)
        if delta is None and not ambiguous:
            return None
        reference = _feedback_references.get(skey(event))
        age = int(time.time()) - reference.created_at if reference is not None else 601
        if reference is None or not 0 <= age <= 600:
            return (
                "这条反馈离上一条回复太久了，taffy不自动套用。"
                "可以直接说“短一点 / 少点喵 / 直接一点”。"
            )
        if delta is None:
            return (
                "我知道你觉得不对，但还不知道该改哪边。"
                "请明确说“短一点”“少点喵”“温柔一点”或“直接一点”。"
            )
        updated = await persona_styles.apply_feedback(
            scope,
            reference.reply_sha256,
            reference.scene,
            delta,
        )
        logger.info(
            "Persona feedback applied scene={} positive={} adjusted={}",
            reference.scene,
            delta.positive,
            any(delta.values),
        )
        values = _style_values(updated.style)
        if delta.positive and not any(delta.values):
            return "这句收到啦。只记了一次正向反馈，没有保存你说的话和我的原回复。"
        return f"懂了，已经只在{scope.label}微调。现在是：{_style_summary(values)}"
    except ValueError as exc:
        if "already recorded" in str(exc):
            return "这条回复已经调过一次啦；要再改，先让taffy回一条新的。"
        logger.info("Persona feedback ignored reason={}", type(exc).__name__)
        return None
    except Exception as exc:
        logger.exception("Persona feedback failed error={}", type(exc).__name__)
        return "这次风格没调进去，聊天还能继续，晚点再试。"


async def _execute_slang_negative_feedback(event: Event, message: str) -> str | None:
    """User correction ("别用XX/达咩") soft-disables that taught expression."""

    from src.services.slang_service import negative_feedback

    try:
        scope = persona_scope(event)
        return await negative_feedback(scope.group_id, message, owner_user_id=scope.owner_user_id)
    except Exception as exc:
        logger.warning("Slang negative feedback failed error={}", type(exc).__name__)
        return None


async def _execute_persona_memory_command(event: Event, message: str) -> str | None:
    """Execute slash or plain-text memory commands within the trusted QQ scope."""

    text = _normalized_memory_command(message)
    if not _is_persona_memory_command(text):
        return None
    scope = persona_scope(event)
    key = skey(event)

    if text in {"reset", "清空记忆", "忘记我"}:
        try:
            async with store.session_guard(key, 15, wait_timeout=10):
                await persona_memories.clear_all(scope, key)
                await session_summaries.clear(key)
                await persona_relationships.clear(scope.owner_user_id, scope.group_id)
                await history_summary.clear_session(key)
        except Exception as exc:
            logger.exception("Conversation reset failed error={}", type(exc).__name__)
            return "记忆暂时无法清空，请稍后再试。"
        return f"清干净啦。{scope.label}的近期对话和长期记忆都已清空喵。"

    if text == "记住" or text.startswith("记住 "):
        content = text[len("记住") :].strip()
        if not content:
            return "要记什么呀？这样发：记住 我更喜欢简短回答。"
        # mem0-style write decision (default off): similar existing memories
        # may be merged/acknowledged instead of blindly appended.  Any
        # failure falls back to the plain append below.
        from src.services.learning import memory_write_decision

        try:
            decision, target_id, merged = await memory_write_decision(
                scope.owner_user_id, scope.group_id, content
            )
        except Exception as exc:
            logger.warning("Memory decision crashed error={}", type(exc).__name__)
            decision, target_id, merged = "add", None, None
        if decision == "noop":
            return "这条我已经记住啦，不用再说一遍喵。"
        if decision == "update" and target_id is not None and merged:
            try:
                updated = await persona_memories.update_note(scope, target_id, merged)
            except ValueError as exc:
                return f"这条没法记：{exc}"
            except Exception as exc:
                logger.exception("Persona memory update failed error={}", type(exc).__name__)
                return "这次没更新成功，聊天还能继续，晚点再试一下喵。"
            if updated is not None:
                return f"更新了之前那条记忆：{updated.content}"
        try:
            memory, created = await persona_memories.remember_note(scope, content)
        except PersonaMemoryCapacityError:
            return "这里已经记满20条啦。先发“塔菲记忆”看看，再忘掉一条喵。"
        except ValueError as exc:
            return f"这条没法记：{exc}"
        except Exception as exc:
            logger.exception("Persona memory write failed error={}", type(exc).__name__)
            return "这次没存进去，聊天还能继续，晚点再试一下喵。"
        if not created:
            return f"这条已经记过啦：{memory.content}"
        return f"记住了。在{scope.label}里，taffy会记得：{memory.content}"

    if text.startswith("以后叫我"):
        value = text[len("以后叫我") :].strip()
        if not value:
            return "称呼别藏着呀，比如：以后叫我 小明。"
        try:
            memory = await persona_memories.set_nickname(scope, value)
        except PersonaMemoryCapacityError:
            return "这里已经记满20条啦，先删掉一条再设置称呼喵。"
        except ValueError as exc:
            return f"这个称呼不行：{exc}"
        except Exception as exc:
            logger.exception("Persona nickname write failed error={}", type(exc).__name__)
            return "称呼这次没存进去，晚点再试一下喵。"
        return f"行，以后{scope.label}里就叫你“{memory.content}”。"

    if text == "塔菲记忆":
        try:
            memories = await persona_memories.list(scope)
        except Exception as exc:
            logger.exception("Persona memory list failed error={}", type(exc).__name__)
            return "记忆列表这会儿打不开，晚点再看喵。"
        lines = [f"🧠 【塔菲记忆·{scope.label}】"]
        if not memories:
            lines.append("这里还是空的。发送“记住 内容”才会保存，普通聊天不会偷记。")
        else:
            for index, memory in enumerate(memories, start=1):
                label = "称呼" if memory.kind == "nickname" else "偏好"
                lines.append(f"{index}. [{label}] {memory.content}")
            lines.append("──────────")
            lines.append("发送“忘掉记忆 序号”可删除一条。")
        lines.append(f"ℹ️ 只显示当前{scope.label}；私聊和每个群彼此隔离。")
        return "\n".join(lines)

    if text == "忘掉记忆" or text.startswith("忘掉记忆 "):
        raw_position = text[len("忘掉记忆") :].strip()
        try:
            position = int(raw_position)
        except ValueError:
            return "要给我一个序号，比如：忘掉记忆 2。"
        try:
            deleted_memory = await persona_memories.delete_at(scope, position)
        except ValueError as exc:
            return f"这个序号不行：{exc}"
        except Exception as exc:
            logger.exception("Persona memory delete failed error={}", type(exc).__name__)
            return "这次没删掉，晚点再试一下喵。"
        if deleted_memory is None:
            return "列表里没有这个序号。发“塔菲记忆”再确认一下吧。"
        return f"忘掉了：{deleted_memory.content}"

    if text == "清空长期记忆":
        try:
            deleted = await persona_memories.clear(scope)
        except Exception as exc:
            logger.exception("Persona memory clear failed error={}", type(exc).__name__)
            return "长期记忆这次没清掉，晚点再试喵。"
        return f"清掉了{deleted}条。{scope.label}的近期对话还保留着。"

    if text == "清空对话":
        try:
            async with store.session_guard(key, 15, wait_timeout=10):
                await store.delete(key)
                await session_summaries.clear(key)
                await history_summary.clear_session(key)
        except Exception as exc:
            logger.exception("Conversation-only reset failed error={}", type(exc).__name__)
            return "近期对话这次没清掉，晚点再试喵。"
        return f"{scope.label}的近期对话清空了，长期记忆还在。"

    if text in {"塔菲我们熟吗", "我们熟吗"}:
        try:
            relationship = await persona_relationships.refresh(scope.owner_user_id, scope.group_id)
        except Exception as exc:
            logger.exception("Relationship query failed error={}", type(exc).__name__)
            return "熟不熟这个事儿这会儿算不出来，晚点再问喵。"
        lines = [
            f"🤝 【{scope.label}·熟悉度】",
            f"档位：{relationship.band}（{relationship.intimacy:g} / 100）",
            "构成：",
            f"  · 互动记录 {relationship.interaction_count} 次",
            f"  · 已确认印象 {relationship.accepted_insight_count} 条",
            f"  · 风格反馈 {relationship.style_feedback_count} 次",
        ]
        if relationship.last_interaction_at is not None:
            from datetime import datetime, timedelta, timezone

            stamp = datetime.fromtimestamp(
                relationship.last_interaction_at, tz=timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d")
            lines.append(f"最近互动：{stamp}")
        lines.append("ℹ️ 只由已确认的互动统计推导，不含聊天内容；忘记我 即可清零。")
        return "\n".join(lines)

    return None


_ENTERTAINMENT_COMMANDS = frozenset(
    {"开箱", "十连开", "随机皮肤", "娱乐系统", "娱乐", "娱乐视频", "娱乐图片"}
)

_CAPABILITY_QUESTIONS = frozenset(
    {
        "你可以做到什么",
        "你能做到什么",
        "你可以做什么",
        "你能干什么",
        "你能干嘛",
        "你会什么",
        "你可以帮我什么",
        "你有什么功能",
        "你可以管理什么",
        "我能管理什么",
        "我可以管理什么",
        "我能管什么",
    }
)

_ADMIN_CAPABILITY_REPLY = (
    "🛠️ 【管理员能力清单】\n"
    "普通功能全部可用（见下方），另加管理能力（仅你可见）：\n"
    "───────────────\n"
    "▸ 塔菲状态 —— 健康/成功率/目录/熔断/备份\n"
    "▸ 管理面板 —— http://127.0.0.1:8080/admin-panel（token 登录）\n"
    "▸ 广播 / 定时配置 / 权限 / 审计 —— 面板内操作\n"
    "▸ 预警 / 自选 / 库存监控 —— 管理自己的全部订阅\n"
    "▸ 版本更新 —— 每次上线自动私聊告知\n"
    "───────────────\n"
    "普通能力：行情（大盘/抄底/预测/盘面解读/榜单/卡片/官方价/K线/比价）、"
    "资讯（CS2新闻/CS2爆料/异动原因）、监控（预警/自选/库存查询）、"
    "学习（塔菲知识/印象/记忆）、娱乐（开箱/炼金/随机皮肤）。\n"
    "发“菜单”看全部命令格式。"
)

_USER_CAPABILITY_REPLY = (
    "🎯 【塔菲能力清单】\n"
    "行情: 大盘｜抄底｜预测｜盘面解读｜榜单｜卡片｜官方价｜比价｜K线｜查磨损｜检视\n"
    "资讯: CS2新闻｜CS2爆料｜异动原因\n"
    "监控: 预警｜自选｜库存查询\n"
    "学习: 塔菲知识｜塔菲印象｜塔菲记忆\n"
    "娱乐: 开箱｜炼金｜随机皮肤\n"
    "───────────────\n"
    "发“菜单”看全部命令格式；想了解行情走势可以试试“预测”和“盘面解读”。"
)


def _capability_reply(message: str, user_id: int) -> str | None:
    """Deterministic capability list, admin-aware."""

    text = message.strip()
    if text not in _CAPABILITY_QUESTIONS:
        return None
    from nonebot import get_driver

    configured: object = getattr(get_driver().config, "superusers", set())
    if isinstance(configured, (set, frozenset, list, tuple)):
        admin_ids = {str(value) for value in configured}
    else:
        admin_ids = set()
    if str(user_id) in admin_ids:
        return _ADMIN_CAPABILITY_REPLY
    return _USER_CAPABILITY_REPLY


def _chat_feature(message: str) -> str:
    """Classify a chat-plugin message into a permission feature category."""

    text = message.strip()
    if text in _ENTERTAINMENT_COMMANDS or text.startswith("炼金"):
        return "entertainment"
    if _command_operation(text) != "command.other":
        return "market"
    return "chat"


def _observe_mood(event: MessageEvent, message: str) -> None:
    """Push this message onto the session mood (expression layer only).

    Mood is a transient, in-memory, per-session float pair; it never
    touches the memory/learning stores and never stores chat text.  The
    sentiment mapping is deliberately lightweight: praise/joy words raise
    valence, complaint/sad words lower it, everything else just decays.
    """

    try:
        from src.services.mood import mood_enabled, observe

        if not mood_enabled():
            return
        group_id = int(getattr(event, "group_id", 0) or 0)
        user_id = int(getattr(event, "user_id", 0) or 0)
        if user_id <= 0:
            return
        text = message.strip().casefold()
        sentiment = "neutral"
        if any(token in text for token in ("谢谢", "厉害", "好棒", "太强", "爱你", "喜欢", "开心")):
            sentiment = "positive"
        elif any(token in text for token in ("生气", "烦", "讨厌", "失望", "难过", "伤心", "投诉")):
            sentiment = "negative"
        observe(user_id, group_id, sentiment=sentiment)
    except Exception:
        # Mood is best-effort expression state; never block the chat.
        return


async def _handle_chat_message(bot: Bot, event: MessageEvent) -> None:
    message = event.get_plaintext().strip()
    _observe_mood(event, message)
    from .routing import COMMAND_PREFIXES, EXACT_COMMANDS

    looks_command = bool(message) and (
        message in EXACT_COMMANDS or message.startswith(tuple(COMMAND_PREFIXES))
    )
    # Inbound images: download immediately (media URLs expire within ~2h)
    # and, when vision is enabled, describe for the persona context.  Never
    # on the command path — commands must stay fast.
    image_snippet: str | None = None
    if not looks_command:
        try:
            _captured, image_snippet = await _vision_context_and_capture(bot, event)
        except Exception as exc:
            logger.warning("Vision context capture failed error={}", type(exc).__name__)
            image_snippet = None
        if image_snippet and not message:
            message = image_snippet
    llm_input = f"{image_snippet} {message}".strip() if image_snippet and message else message
    if not message:
        return
    if not await feature_gate.authority.allowed(int(event.user_id), _chat_feature(message)):
        await bot.send(event, text_message(DENIED_MESSAGE))
        return
    if not await _CHAT_RATE_LIMITER.allow(skey(event)):
        _record_feedback_reference(event, None, None)
        await bot.send(event, text_message("消息太频繁了喵，请稍等一会儿。"))
        return
    if len(message) > _MAX_HANDLER_INPUT:
        _record_feedback_reference(event, None, None)
        await bot.send(
            event,
            text_message(f"消息太长了喵，请控制在 {_MAX_HANDLER_INPUT} 个字符以内。"),
        )
        return
    if message in {"娱乐", "娱乐视频"}:
        _record_feedback_reference(event, None, None)
        await _send_random_video(bot, event)
        return
    if message == "娱乐图片":
        _record_feedback_reference(event, None, None)
        await _send_random_image(bot, event)
        return

    style_reply = await _execute_persona_style_command(event, message)
    if style_reply is not None:
        await _send_text_reply(bot, event, style_reply)
        return

    memory_reply = await _execute_persona_memory_command(event, message)
    if memory_reply is not None:
        await _send_text_reply(bot, event, memory_reply)
        return

    slang_feedback = (
        None if looks_command else await _execute_slang_negative_feedback(event, message)
    )
    if slang_feedback is not None:
        await _send_text_reply(bot, event, slang_feedback)
        return

    feedback_reply = await _execute_natural_persona_feedback(event, message)
    if feedback_reply is not None:
        await _send_text_reply(bot, event, feedback_reply)
        return

    if command_requires_v4_pilot(message) and not v4_enabled_for_user(event.user_id):
        _record_feedback_reference(event, None, None)
        await bot.send(
            event,
            text_message("这个 V4 功能正在由管理员试用，三天稳定性验收后再开放。"),
        )
        return

    try:
        _record_feedback_reference(event, None, None)
        reply = await _command_with_progress(bot, event, message)
        feedback_scene: str | None = None
        if isinstance(reply, BroadKlineScreenshotRequest):
            await _send_broad_kline_reply(
                bot,
                event,
                high_definition=reply.high_definition,
                max_days_ago=reply.max_days_ago,
            )
            return
        if isinstance(reply, ItemKlineScreenshotRequest):
            await _send_item_kline_reply(
                bot,
                event,
                reply.market_name,
                high_definition=reply.high_definition,
                special_style=reply.special_style,
                platform=reply.platform,
            )
            return
        if reply is None:
            capability_reply = _capability_reply(message, int(event.user_id))
            if capability_reply is not None:
                await _send_text_reply(bot, event, capability_reply)
                return
            tool_actions: list[object] = []
            reply = await deepseek_chat(
                llm_input,
                skey(event),
                scope=persona_scope(event),
                tool_actions=tool_actions,
            )
            feedback_scene = classify_persona(message).intent.value
            await _deliver_tool_actions(bot, event, tool_actions)
            # Intimacy phase two: a 老友-scope social reply may carry one
            # old-topic callback (confirmed memories only, M4-safe).
            try:
                from src.services.relationship_service import recall_old_topic

                recall = await recall_old_topic(persona_scope(event), message)
                if recall:
                    reply = f"{reply}\n\n{recall}"
            except Exception:
                pass
        else:
            reply = render_command_persona_shell(message, reply)
        await _send_text_reply(bot, event, reply, feedback_scene=feedback_scene)
        if feedback_scene is not None:
            # Persona social reply, not command output: the only place a
            # sticker reaction may follow (never market/fact content).
            from src.services.stickers import maybe_send_sticker

            await maybe_send_sticker(bot, event, scene=feedback_scene, reply_text=str(reply))
    except Exception as exc:
        logger.exception("Chat handler failed error={}", type(exc).__name__)
        await bot.send(event, text_message("塔菲处理失败了喵，请稍后再试。"))


gk = on_message(rule=Rule(_kw), priority=15, block=True)


@gk.handle()
async def h_kw(bot: Bot, event: MessageEvent) -> None:
    await _handle_chat_message(bot, event)


gc = on_message(rule=Rule(_gr), priority=10, block=True)


@gc.handle()
async def h_g(bot: Bot, event: MessageEvent) -> None:
    await _handle_chat_message(bot, event)


pc = on_message(rule=Rule(_pr), priority=10, block=True)


@pc.handle()
async def h_p(bot: Bot, event: MessageEvent) -> None:
    await _handle_chat_message(bot, event)


async def _run_memory_command(
    matcher: type[Matcher],
    event: MessageEvent,
    fallback: str,
) -> None:
    """Gate and execute one persona-memory command.

    Slash memory commands go through the same authority and rate-limit gates
    as the plain-text chat path so they can never become a side door for
    users whose chat feature is disabled or who are over the rate budget.
    """

    if not await feature_gate.check_feature(matcher, event, "chat"):
        return
    if not await _CHAT_RATE_LIMITER.allow(skey(event)):
        await matcher.finish(text_message("消息太频繁了喵，请稍等一会儿。"))
    reply = await _execute_persona_memory_command(event, event.get_plaintext())
    await matcher.finish(text_message(reply or fallback))


def _memory_command(
    name: str,
    *,
    fallback: str,
    aliases: set[str | tuple[str, ...]] | None = None,
) -> type[Matcher]:
    """Register one persona-memory command sharing the trusted QQ scope executor."""

    matcher = on_command(name, aliases=aliases, priority=5, block=True)

    @matcher.handle()
    async def _handle(event: MessageEvent) -> None:
        await _run_memory_command(matcher, event, fallback)

    return matcher


cc = _memory_command(
    "reset", aliases={"清空记忆", "忘记我"}, fallback="记忆暂时无法清空，请稍后再试。"
)
remember = _memory_command("记住", fallback="要记什么呀？这样发：记住 我喜欢简短回答。")
nickname = _memory_command("以后叫我", fallback="称呼别藏着呀，比如：以后叫我 小明。")
memory_list = _memory_command("塔菲记忆", fallback="记忆列表这会儿打不开，晚点再看喵。")
forget_memory = _memory_command("忘掉记忆", fallback="要给我一个序号，比如：忘掉记忆 2。")
clear_long_memory = _memory_command("清空长期记忆", fallback="长期记忆这次没清掉，晚点再试喵。")
clear_conversation = _memory_command("清空对话", fallback="近期对话这次没清掉，晚点再试喵。")

__all__ = [
    "cc",
    "cmd",
    "gc",
    "gk",
    "pc",
    "resolve_name",
    "skey",
]

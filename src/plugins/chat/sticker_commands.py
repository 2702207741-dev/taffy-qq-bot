"""Administrator commands for the sticker library (V0.5 phase two).

All commands are superuser-only and fail-closed: the library gates stay in
charge, the VLM tagging run is bounded per invocation, and auto-collected
images only enter the curated library through an explicit ``确认贴图``
acceptance (everything learned stays confirmable, reviewable, deletable).
"""

from __future__ import annotations

import re

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.rule import Rule

from src.core.messages import text_message

from ..feature_gate import check_feature


def _superusers() -> set[str]:
    configured: object = getattr(get_driver().config, "superusers", set())
    if not isinstance(configured, (set, frozenset, list, tuple)):
        return set()
    return {str(value) for value in configured}


def _is_sticker_admin_request(event: Event) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    if str(event.user_id) not in _superusers():
        return False
    text = event.get_plaintext().strip()
    return bool(
        re.fullmatch(
            r"(塔菲贴图打标|塔菲贴图审核|确认贴图 \d+.*|拒绝贴图 \d+)",
            text,
        )
    )


sticker_admin_cmd = on_message(rule=Rule(_is_sticker_admin_request), priority=7, block=True)


@sticker_admin_cmd.handle()
async def handle_sticker_admin(_bot: Bot, event: MessageEvent) -> None:
    from src.services import stickers, vision

    if not await check_feature(sticker_admin_cmd, event, "chat"):
        return
    text = event.get_plaintext().strip()
    if not stickers.stickers_enabled():
        await sticker_admin_cmd.finish(
            text_message("表情包功能未开启（TAFFY_STICKERS_ENABLED），先配置总开关和素材库。")
        )
    if text == "塔菲贴图打标":
        targets = stickers.entries_missing_description(limit=10)
        if not targets:
            await sticker_admin_cmd.finish(text_message("素材库里的贴图都有描述了，不用再打标。"))
        tagged = await vision.tag_pending_images([entry.path for entry in targets])
        await sticker_admin_cmd.finish(
            text_message(f"本轮打标完成 {tagged}/{len(targets)} 张；失败的下轮再试即可。")
        )
    if text == "塔菲贴图审核":
        pending = stickers.pending_stickers()
        if not pending:
            await sticker_admin_cmd.finish(text_message("待审贴图池是空的。"))
        lines = ["🖼️ 【待审贴图】"]
        for index, path in enumerate(pending, 1):
            desc = stickers.load_pending_metadata(path)
            if desc and desc.get("description"):
                lines.append(f"{index}. {path.name} —— {desc['description']}")
            else:
                lines.append(f"{index}. {path.name}（暂无描述，可先“塔菲贴图打标”补）")
        lines.append("确认贴图 序号 标签1 标签2 —— 收入素材库（描述由打标补齐）")
        lines.append("拒绝贴图 序号 —— 丢弃")
        await sticker_admin_cmd.finish(text_message("\n".join(lines)))
    if text.startswith("确认贴图 "):
        parts = text.split()
        try:
            index = int(parts[1])
        except (IndexError, ValueError):
            await sticker_admin_cmd.finish(text_message("格式：确认贴图 序号 标签1 标签2"))
        tags = parts[2:] or ["待补标签"]
        accepted = stickers.accept_pending_sticker(index, tags)
        if accepted is None:
            await sticker_admin_cmd.finish(text_message("没有这个序号，先发“塔菲贴图审核”看看。"))
            return
        described = await vision.describe_image(accepted)
        if described is None:
            await sticker_admin_cmd.finish(
                text_message(f"已收入 {accepted.name}（描述打标失败，下次“塔菲贴图打标”补齐）。")
            )
        await sticker_admin_cmd.finish(
            text_message(f"已收入 {accepted.name}，描述：{described[0] if described else ''}")
        )
    if text.startswith("拒绝贴图 "):
        try:
            index = int(text.split()[1])
        except (IndexError, ValueError):
            await sticker_admin_cmd.finish(text_message("格式：拒绝贴图 序号"))
        rejected = stickers.reject_pending_sticker(index)
        if rejected is None:
            await sticker_admin_cmd.finish(text_message("没有这个序号，先发“塔菲贴图审核”看看。"))
            return
        await sticker_admin_cmd.finish(text_message(f"已丢弃：{rejected}"))
    await sticker_admin_cmd.finish(text_message("未识别的贴图管理指令。"))


__all__ = ["handle_sticker_admin", "sticker_admin_cmd"]

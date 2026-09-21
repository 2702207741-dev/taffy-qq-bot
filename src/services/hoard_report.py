"""AI hoard-profile reports grounded in aggregated observation facts.

The model only ever sees an aggregate JSON summary (counts and timelines, no
per-asset dumps) and its answer must stay inside hard guardrails baked into
the system prompt: facts only, machine-inference disclaimer mandatory, no
invented timestamps, "观察期不足" when data is thin.  Reports are cached per
SteamID for six hours in ``inventory_monitor_reports``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from src.state.inventory_monitor_repository import (
    InventoryHistoryTrajectoryRow,
    InventoryMonitorRepository,
)

from .llm import TAFFY_MODEL, try_anthropic, untrusted_context
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

logger = logging.getLogger("taffy.services.hoard_report")

#: Same profile for the same account inside this window is served from cache.
CACHE_TTL_SECONDS = 6 * 60 * 60
REPORT_WINDOW_DAYS = 30
_MAX_PAYLOAD_CHARS = 20_000
_REPORT_MAX_TOKENS = 2_000
_DISCLAIMER = "机器推断，非结论"

SYSTEM_PROMPT = f"""你是 CS2 饰品囤货行为观察助手。用户会给你一份某 Steam 账号近 30 天的公开库存观测聚合数据。

硬性规则（违反任何一条即失败）：
1. 只陈述数据中出现的事实，禁止推测无法从数据推出的动机与去向。
2. 禁止编造任何时间点、数字或物品名；所有时间/数量必须原样来自输入。
3. 输出末尾必须包含独立一行免责标注：{_DISCLAIMER}。
4. 数据明显不足以支撑判断时，直接输出"观察期不足"，不要强行总结。
5. 不输出交易建议、不推荐买入卖出，只描述行为模式。
6. 库存不可见（隐藏）时段必须如实说明"不可见"，禁止虚构该时段发生的事情。

输出格式：
- 第一行：【囤货画像】账号昵称
- 随后 3-6 条要点：总量变化、进出节奏、集中度、隐藏库存时段。
- 最后一行固定为：{_DISCLAIMER}
"""


@dataclass(frozen=True, slots=True)
class HoardReport:
    """One generated (or cached) profile plus cache bookkeeping."""

    steam_id: str
    nickname: str
    content: str
    model: str
    tokens_used: int
    generated_at: int
    cached: bool


class HoardReportError(RuntimeError):
    """The report could not be produced safely (thin data or LLM failure)."""


def _fmt(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, tz=_MARKET_TZ).strftime("%Y-%m-%d %H:%M")


def build_aggregate_payload(
    rows: Sequence[InventoryHistoryTrajectoryRow],
    *,
    window_days: int = REPORT_WINDOW_DAYS,
    now: int | None = None,
) -> dict[str, object]:
    """Aggregate history rows into a bounded factual summary for the LLM."""

    current = int(datetime.now(tz=_MARKET_TZ).timestamp()) if now is None else now
    window_start = max(0, current - window_days * 24 * 60 * 60)
    added: list[dict[str, object]] = []
    removed: list[dict[str, object]] = []
    present = 0
    for row in rows:
        if row.last_missing_at is None:
            present += 1
        if row.first_seen_at >= window_start:
            added.append(
                {
                    "物品": row.market_hash_name or row.name,
                    "首见": _fmt(row.first_seen_at),
                    "磨损": row.paintwear,
                }
            )
        if row.last_missing_at is not None and window_start <= row.last_missing_at <= current:
            removed.append(
                {
                    "物品": row.market_hash_name or row.name,
                    "离场": _fmt(row.last_missing_at),
                }
            )
    return {
        "统计窗口": f"近{window_days}天",
        "当前在场记录数": present,
        "新增条数": len(added),
        "离场条数": len(removed),
        "新增明细": added[:40],
        "离场明细": removed[:40],
        "说明": "仅公开库存可见变化；仓库组件内物品不可见。",
    }


async def generate_report(
    repository: InventoryMonitorRepository,
    steam_id: str,
    nickname: str,
    rows: Sequence[InventoryHistoryTrajectoryRow],
    *,
    now: int,
    model: str = "",
    generator=None,
) -> HoardReport:
    """Return a cached report when fresh, otherwise call the LLM once."""

    chosen_model = model or TAFFY_MODEL
    cached = await load_recent_report(repository, steam_id, now=now)
    if cached is not None:
        tokens_used = cast(int, cached["tokens_used"])
        generated_at = cast(int, cached["generated_at"])
        return HoardReport(
            steam_id=steam_id,
            nickname=nickname,
            content=str(cached["payload_json"]),
            model=str(cached["model"]),
            tokens_used=tokens_used,
            generated_at=generated_at,
            cached=True,
        )
    payload = build_aggregate_payload(rows, now=now)
    user_content = (
        "以下是该账号近30天公开库存观测聚合数据（JSON）：\n"
        f"{untrusted_context(json.dumps(payload, ensure_ascii=False), limit=8_000)}\n"
        '请按系统规则输出画像。若新增+离场合计少于5条，直接输出"观察期不足"。'
    )
    content = await generator(chosen_model, user_content)
    if _DISCLAIMER not in content:
        # 常见根因是 max_tokens 截断：压缩输出指令重试一次（要点更少、每条更短）。
        retry_content = (
            "以下是该账号近30天公开库存观测聚合数据（JSON）：\n"
            f"{untrusted_context(json.dumps(payload, ensure_ascii=False), limit=8_000)}\n"
            "请按系统规则输出画像，但输出必须极简：最多 3 条要点、每条不超过 40 字、"
            "不要复述原文数据明细，确保最后一行是免责标注。"
            '若新增+离场合计少于5条，直接输出"观察期不足"。'
        )
        content = await generator(chosen_model, retry_content)
        if _DISCLAIMER not in content:
            raise HoardReportError("模型输出缺少免责标注，已拒绝采用")
    stored = await store_report(
        repository,
        steam_id,
        payload_text=content[:_MAX_PAYLOAD_CHARS],
        model=chosen_model,
        tokens_used=0,
        now=now,
    )
    return HoardReport(
        steam_id=steam_id,
        nickname=nickname,
        content=stored,
        model=chosen_model,
        tokens_used=0,
        generated_at=now,
        cached=False,
    )


async def generate_via_llm(model: str, user_content: str) -> str:
    """Default LLM path reused from chat's llm module conventions.

    3000 tokens：报告 3-6 条要点 + 免责行在大数据量下约需 1k；留思考余量，
    防止 thinking 模式吃掉输出池导致免责行被截断（会被判缺失而拒绝）。
    """

    return await try_anthropic(SYSTEM_PROMPT, [{"role": "user", "content": user_content}], 3_000)


# ── reports-table persistence (repository keeps SQL parameterized) ────────


async def load_recent_report(
    repository: InventoryMonitorRepository,
    steam_id: str,
    *,
    now: int,
) -> Mapping[str, object] | None:
    since = now - CACHE_TTL_SECONDS
    rows = await repository.database.read(
        """
        SELECT generated_at, model, tokens_used, payload_json
        FROM inventory_monitor_reports
        WHERE steam_id = ? AND generated_at >= ?
        ORDER BY generated_at DESC LIMIT 1
        """,
        (_steam_id(steam_id), since),
    )
    return rows[0] if rows else None


async def store_report(
    repository: InventoryMonitorRepository,
    steam_id: str,
    *,
    payload_text: str,
    model: str,
    tokens_used: int,
    now: int,
) -> str:
    text = payload_text[:_MAX_PAYLOAD_CHARS]
    await repository.database.write(
        """
        INSERT INTO inventory_monitor_reports (
            steam_id, generated_at, model, tokens_used, payload_json
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (steam_id, generated_at) DO UPDATE SET
            model = excluded.model,
            tokens_used = excluded.tokens_used,
            payload_json = excluded.payload_json
        """,
        (_steam_id(steam_id), int(now), model[:200] or TAFFY_MODEL, max(0, int(tokens_used)), text),
    )
    # Keep at most the newest report per account; older rows are swept.
    await repository.database.write(
        """
        DELETE FROM inventory_monitor_reports
        WHERE steam_id = ? AND generated_at < ?
        """,
        (_steam_id(steam_id), int(now)),
    )
    return text


def _steam_id(value: str) -> str:
    result = value.strip()
    if len(result) != 17 or not result.isascii() or not result.isdecimal():
        raise ValueError("steam_id must be a 17-digit identifier")
    return result


__all__ = [
    "CACHE_TTL_SECONDS",
    "HoardReport",
    "HoardReportError",
    "REPORT_WINDOW_DAYS",
    "build_aggregate_payload",
    "generate_report",
    "generate_via_llm",
    "load_recent_report",
    "store_report",
]

"""Read-only public CS2 inventory snapshots behind a replaceable provider."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from .config import bounded_env_int
from .http_client import request_json
from .resilience import CircuitOpenError, get_circuit_breaker
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

logger = logging.getLogger("taffy.services.steam_inventory")

_BASE_URL = "https://www.steamdt.com/api"
_CONFIG_PATH = "/user/system-config/v1/default-config"
_INVENTORY_PATH = "/user/steam/inventory/v3"
_STEAM64_BASE = 76_561_197_960_265_728
_MAX_SNAPSHOT_AGE_SECONDS = 24 * 60 * 60
_MAX_DETAIL_ROWS = bounded_env_int("TAFFY_INVENTORY_MAX_ROWS", 100, 10, 500)
_NAME_DISPLAY_LIMIT = 80
_HEADERS = {
    "x-device-id": "",
    "x-device": "1",
    "x-app-version": "1.0.0",
    "access-token": "",
    "x-currency": "CNY",
    "language": "zh_CN",
}
_INVENTORY_BULKHEAD = asyncio.Semaphore(4)

SnapshotFetcher = Callable[[str], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class InventoryIdentifier:
    steam_id: str


def parse_inventory_identifier(value: str) -> InventoryIdentifier | None:
    """Accept a Steam64 ID or a standard Steam trade URL without guessing."""

    text = value.strip()
    if re.fullmatch(r"7656119\d{10}", text):
        return InventoryIdentifier(text)
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "steamcommunity.com",
        "www.steamcommunity.com",
    }:
        return None
    if parsed.path.rstrip("/") != "/tradeoffer/new":
        return None
    partner_values = parse_qs(parsed.query).get("partner", [])
    if len(partner_values) != 1 or not partner_values[0].isdigit():
        return None
    account_id = int(partner_values[0])
    if not 0 < account_id <= 0xFFFFFFFF:
        return None
    return InventoryIdentifier(str(_STEAM64_BASE + account_id))


def checksum_timestamp(server_time: object) -> str:
    """Generate the 13-digit read-request checksum used by the web client."""

    if isinstance(server_time, bool):
        raise ValueError("server time is invalid")
    text = str(server_time).strip()
    if not text.isdigit() or len(text) < 12:
        raise ValueError("server time is invalid")
    prefix = text[:12]
    return prefix + str(sum(int(digit) for digit in prefix) % 10)


async def fetch_inventory_snapshot(steam_id: str) -> Mapping[str, Any]:
    """Fetch an existing snapshot only; this function never calls a refresh route."""

    breaker = get_circuit_breaker("inventory-snapshot")
    try:
        await breaker.acquire()
    except CircuitOpenError as exc:
        raise RuntimeError("inventory circuit is open") from exc
    try:
        async with _INVENTORY_BULKHEAD:
            config = await request_json(
                "GET",
                f"{_BASE_URL}{_CONFIG_PATH}",
                headers=_HEADERS,
                attempts=2,
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
                total_timeout=10.0,
            )
            config_data = config.get("data") if config.get("success") else None
            if not isinstance(config_data, Mapping):
                raise RuntimeError("inventory service time unavailable")
            timestamp = checksum_timestamp(config_data.get("systemTime"))
            response = await request_json(
                "GET",
                f"{_BASE_URL}{_INVENTORY_PATH}",
                headers=_HEADERS,
                params={
                    "app_id": 730,
                    "steam_id": steam_id,
                    "sticker_evaluate": 0,
                    "timestamp": timestamp,
                },
                attempts=2,
                timeout=httpx.Timeout(connect=3.0, read=12.0, write=3.0, pool=3.0),
                total_timeout=20.0,
            )
    except Exception:
        await breaker.fail()
        raise
    if response.get("success"):
        await breaker.succeed()
    else:
        await breaker.fail()
    return response


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, bytes, int, float)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _snapshot_is_stale(response: Mapping[str, Any], *, now: float) -> bool:
    data = response.get("data") if response.get("success") else None
    if not isinstance(data, Mapping):
        return False
    inventory = data.get("inventory")
    if not isinstance(inventory, Mapping):
        return False
    summary = inventory.get("summary")
    if not isinstance(summary, Mapping):
        return False
    last_fetch = _nonnegative_int(summary.get("lastFetchTime"))
    return (
        last_fetch is not None
        and last_fetch <= now + 5 * 60
        and now - last_fetch > _MAX_SNAPSHOT_AGE_SECONDS
    )


def _snapshot_is_empty(response: Mapping[str, Any]) -> bool:
    data = response.get("data") if response.get("success") else None
    if not isinstance(data, Mapping):
        return False
    inventory = data.get("inventory")
    if not isinstance(inventory, Mapping):
        return False
    summary = inventory.get("summary")
    return isinstance(summary, Mapping) and summary.get("empty") is True


def _item_name(asset: Mapping[str, Any], classinfos: Mapping[str, Any]) -> str:
    key = str(asset.get("classinfoKey", ""))
    classinfo = classinfos.get(key)
    if not isinstance(classinfo, Mapping):
        return "未知饰品"
    return str(
        classinfo.get("name")
        or classinfo.get("marketHashName")
        or classinfo.get("hashName")
        or "未知饰品"
    ).strip()


def format_inventory_snapshot(
    response: Mapping[str, Any],
    *,
    now: float | None = None,
    stale_rechecked: bool = False,
    stale_recheck_failed: bool = False,
    empty_rechecked: bool = False,
) -> str:
    if not response.get("success") or not isinstance(response.get("data"), Mapping):
        return "⚠️ 库存快照获取失败；本次不会交给聊天模型猜测。"
    data = response["data"]
    account = data.get("steamAccount")
    inventory = data.get("inventory")
    if not isinstance(account, Mapping) or not isinstance(inventory, Mapping):
        return "⚠️ 库存快照格式无效，本次拒绝展示。"
    summary = inventory.get("summary")
    if not isinstance(summary, Mapping):
        return "⚠️ 库存快照缺少汇总信息，本次拒绝展示。"
    if summary.get("privacy") is True or summary.get("notPrivacy") is False:
        return "📦 【库存估值快照】\n该账号库存为私密，无法读取公开快照。"
    if summary.get("empty") is True:
        if empty_rechecked:
            return (
                "📦 【库存估值快照】\n"
                "连续两次实时读取均显示为空；如果账号刚公开库存或仍在同步，"
                "请直接重试。"
            )
        return "⚠️ 库存快照暂时显示为空，无法确认该账号确实没有物品；请直接重试。"

    last_fetch = _nonnegative_int(summary.get("lastFetchTime"))
    current = time.time() if now is None else now
    if last_fetch is None or last_fetch > current + 5 * 60:
        return "⚠️ 库存快照时间异常，本次拒绝展示。"
    if current - last_fetch > _MAX_SNAPSHOT_AGE_SECONDS:
        status = "库存快照已超过24小时"
        if stale_rechecked:
            status += "，自动复查后仍未更新"
        elif stale_recheck_failed:
            status += "，自动复查失败，暂时无法确认是否更新"
        return f"⚠️ {status}，本次拒绝估值。\n机器人只读现有快照，不会自动刷新他人库存。"

    total = _nonnegative_int(summary.get("total"))
    tradable = _nonnegative_int(summary.get("totalTradableCount"))
    total_price = _finite_nonnegative(summary.get("totalPrice"))
    raw_assets = inventory.get("assets")
    classinfos = inventory.get("classinfos")
    if (
        total is None
        or tradable is None
        or total_price is None
        or not isinstance(raw_assets, Sequence)
        or isinstance(raw_assets, (str, bytes))
        or not isinstance(classinfos, Mapping)
        or len(raw_assets) > 20_000
    ):
        return "⚠️ 库存汇总字段无效，本次拒绝估值。"

    priced_assets: list[tuple[float, str]] = []
    valid_assets = 0
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, Mapping):
            continue
        valid_assets += 1
        price = _finite_nonnegative(raw_asset.get("evaluatePrice"))
        if price is not None and price > 0:
            priced_assets.append((price, _item_name(raw_asset, classinfos)))
    priced_assets.sort(reverse=True)

    nickname = str(account.get("nickname") or account.get("steamId") or "未知账号").strip()
    lines = [
        "📦 【库存估值快照】",
        f"账号: {nickname}",
        f"数据时间: {datetime.fromtimestamp(last_fetch, tz=_MARKET_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        f"物品总数: {total}｜可交易: {tradable}",
        f"快照总估值: ¥{total_price:.2f}",
        f"可估价覆盖: {len(priced_assets)}/{valid_assets}（返回的可交易资产）",
    ]
    if priced_assets:
        aggregated: dict[str, list[float]] = {}
        for price, name in priced_assets:
            aggregated.setdefault(name, []).append(price)
        rows = sorted(
            ((name, len(prices), sum(prices)) for name, prices in aggregated.items()),
            key=lambda row: (row[2], row[1], row[0]),
            reverse=True,
        )
        lines.append("全部单品（按估值降序，同名聚合）:")
        display_rows = rows[:_MAX_DETAIL_ROWS]
        lines.extend(
            " {}. {} × {}｜¥{:.2f}/件｜小计 ¥{:.2f}".format(
                index,
                name[:_NAME_DISPLAY_LIMIT],
                count,
                subtotal / count,
                subtotal,
            )
            for index, (name, count, subtotal) in enumerate(display_rows, 1)
        )
        hidden = len(rows) - len(display_rows)
        if hidden > 0:
            lines.append(f"…其余 {hidden} 种物品略（按估值降序）")
    lines.append("ℹ️ 只读取已有公开快照，未触发库存刷新；估值会随市场变化。")
    return "\n".join(lines)


class SteamInventoryService:
    def __init__(
        self,
        *,
        fetcher: SnapshotFetcher = fetch_inventory_snapshot,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetcher = fetcher
        self._wall_clock = wall_clock
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._lock = asyncio.Lock()

    async def _fetch_and_format(self, steam_id: str) -> str:
        stale_rechecked = False
        stale_recheck_failed = False
        empty_rechecked = False
        try:
            try:
                response = await self._fetcher(steam_id)
                current = self._wall_clock()
                if _snapshot_is_empty(response):
                    response = await self._fetcher(steam_id)
                    current = self._wall_clock()
                    empty_rechecked = True
                elif _snapshot_is_stale(response, now=current):
                    try:
                        response = await self._fetcher(steam_id)
                        current = self._wall_clock()
                    except Exception as exc:
                        logger.warning(
                            "Inventory stale snapshot recheck failed error=%s",
                            type(exc).__name__,
                        )
                        stale_recheck_failed = True
                    else:
                        stale_rechecked = True
                return format_inventory_snapshot(
                    response,
                    now=current,
                    stale_rechecked=stale_rechecked,
                    stale_recheck_failed=stale_recheck_failed,
                    empty_rechecked=empty_rechecked,
                )
            except Exception as exc:
                logger.warning("Inventory snapshot failed error=%s", type(exc).__name__)
                return "⚠️ 库存快照暂不可用；本次不会交给聊天模型猜测。"
        finally:
            current_task = asyncio.current_task()
            async with self._lock:
                if self._inflight.get(steam_id) is current_task:
                    self._inflight.pop(steam_id, None)

    async def report(self, raw_identifier: str) -> str:
        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return (
                "未能确认库存账号：请提供17位 Steam64 ID 或完整交易链接。\n"
                "本次已停止，不会交给聊天模型猜测。"
            )
        async with self._lock:
            task = self._inflight.get(identifier.steam_id)
            if task is None:
                task = asyncio.create_task(self._fetch_and_format(identifier.steam_id))
                self._inflight[identifier.steam_id] = task
        return await asyncio.shield(task)


steam_inventory_service = SteamInventoryService()


async def get_inventory_report(identifier: str) -> str:
    return await steam_inventory_service.report(identifier)


__all__ = [
    "InventoryIdentifier",
    "SteamInventoryService",
    "checksum_timestamp",
    "fetch_inventory_snapshot",
    "format_inventory_snapshot",
    "get_inventory_report",
    "parse_inventory_identifier",
]

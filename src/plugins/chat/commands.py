"""Chat command service, independent from NoneBot matcher registration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from src.services.market_special_style import parse_special_style_query
from src.services.market_temperature import TemperatureResult, TemperatureUnavailable
from src.services.steamdt_kline import parse_kline_platform_suffix
from src.services.telemetry import telemetry

from .compare import (
    csqaq_rent_line,
    csqaq_supply_line,
    format_price_comparison,
    record_and_trend_line,
)
from .entertainment import handle_entertainment_command
from .market import (
    format_average_price,
    format_broad_market,
    format_composite_market,
    format_item_analysis,
    format_price,
    resolution_failure_message,
    resolve_name,
)


class MarketGet(Protocol):
    def __call__(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        ttl: float | None = None,
    ) -> Awaitable[dict[str, Any]]: ...


class MarketPost(Protocol):
    def __call__(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Awaitable[dict[str, Any]]: ...


NameResolver = Callable[[str], Awaitable[str | None]]
TemperatureProvider = Callable[[Mapping[str, Any]], Awaitable[TemperatureResult]]
InventoryProvider = Callable[[str], Awaitable[str]]
BroadProvider = Callable[[], Awaitable[dict[str, Any]]]
MarketColumnProvider = Callable[[str], Awaitable[str]]
RankingProvider = Callable[[str, str, int], Awaitable[str]]
#: ``(user_query, market_hash_name) -> CSQAQ side response dict``; the
#: callable itself must never raise and must fail closed on any problem.
CsqaqSideProvider = Callable[[str, str], Awaitable[dict[str, Any]]]
#: ``(market_hash_name, user_query) -> rendered reply`` for deterministic
#: scoring; ``summary`` providers return one line or ``None`` (silent omit).
ScoreReplyProvider = Callable[[str, str], Awaitable[str]]
ScoreSummaryProvider = Callable[[str, str], Awaitable[str | None]]

logger = logging.getLogger("taffy.plugins.chat.commands")

_PAUSED_MARKET_COLUMNS = frozenset(
    {"异动", "预测", "热门榜", "热度榜", "流动性榜", "涨跌榜", "综合指数", "装备指数"}
)
_PAUSED_MARKET_MESSAGE = (
    "⏸️ 这个市场栏目正在重建，本次不生成排名或结论。\n"
    "旧版本只覆盖少量预设饰品，不能代表整个CS2市场。"
)


def _csqaq_context_lines(response: Any) -> str:
    """Trailing supply/rent lines for 饰品查询; silent when CSQAQ adds nothing.

    Per the data gate: an unconfigured source, a failed fetch or a detail
    without those fields all render nothing instead of placeholder noise.
    """

    if not isinstance(response, dict) or not response.get("success"):
        return ""
    detail = response.get("detail")
    if detail is None:
        return ""
    lines: list[str] = []
    supply = csqaq_supply_line(detail)
    if supply is not None:
        lines.append(supply)
    rent = csqaq_rent_line(detail)
    if rent is not None:
        lines.append(rent)
    if not lines:
        return ""
    return "\n" + "\n".join(lines)


async def _hoard_reference_line(market_name: str) -> str | None:
    """One honest 挂刀参考 line for 饰品查询; None when the item is untracked."""

    try:
        from src.services.hoard_index import hoard_index_store

        ratio = await hoard_index_store.item_ratio(market_name)
    except Exception as exc:
        logger.warning("Hoard reference line failed error=%s", type(exc).__name__)
        return None
    if ratio is None:
        return None
    return f"挂刀参考：Steam 比例 {ratio:.2f}（高分位适合出 Steam，低分位适合留平台）"


async def _record_command_telemetry(
    operation: str,
    outcome: str,
    started: float,
) -> None:
    """Keep optional operational metrics outside the user command result path."""

    try:
        await telemetry.record(operation, outcome, (time.perf_counter() - started) * 1000)
    except Exception as exc:
        logger.warning("Command telemetry unavailable error=%s", type(exc).__name__)


def _command_operation(message: str) -> str:
    command = message.strip().casefold()
    exact = {
        "大盘": "command.broad",
        "抄底": "command.dip",
        "综合指数": "command.composite",
        "异动": "command.anomaly",
        "预测": "command.scenario",
        "热门榜": "command.heat",
        "热度榜": "command.heat",
        "流动性榜": "command.liquidity",
        "涨跌榜": "command.movers",
        "装备指数": "command.equipment",
        "菜单": "command.menu",
        "menu": "command.menu",
        "help": "command.menu",
    }
    if command in exact:
        return exact[command]
    prefixes = (
        ("库存查询", "command.inventory"),
        ("异动", "command.anomaly"),
        ("涨跌榜", "command.movers"),
        ("流动性榜", "command.liquidity"),
        ("k线高清", "command.kline_hd"),
        ("k线", "command.kline"),
        ("查询", "command.price"),
        ("饰品查询", "command.price"),
        ("比价", "command.compare"),
        ("买卖参考", "command.score"),
        ("分析", "command.analysis"),
        ("饰品分析", "command.analysis"),
    )
    return next(
        (operation for prefix, operation in prefixes if command.startswith(prefix)), "command.other"
    )


async def _unavailable_temperature(_: Mapping[str, Any]) -> TemperatureResult:
    return TemperatureUnavailable()


async def _unavailable_inventory(_: str) -> str:
    return "⚠️ 库存快照服务暂不可用；本次不会交给聊天模型猜测。"


async def _unavailable_market_column(_: str) -> str:
    return _PAUSED_MARKET_MESSAGE


async def _unavailable_ranking(_: str, __: str, ___: int) -> str:
    return _PAUSED_MARKET_MESSAGE


_RANKING_COMMAND_PATTERN = re.compile(
    r"^(异动榜|异动|涨跌榜|流动性榜)(?:[ \t]+(\S+?))?(?:[ \t]+([0-9]{1,4}))?[ \t]*$"
)
# "涨跌榜 20 印花" — size before category is accepted too.
_RANKING_REVERSED_PATTERN = re.compile(
    r"^(异动榜|异动|涨跌榜|流动性榜)(?:[ \t]+([0-9]{1,4}))(?:[ \t]+(\S+?))?[ \t]*$"
)
_RANKING_BOARD_NAMES = ("异动榜", "异动", "涨跌榜", "流动性榜")
# "涨跌榜 总榜" means the unfiltered board, not a category named 总榜.
_NO_CATEGORY_ALIASES = frozenset({"总榜", "全部", "全榜"})


@dataclass(frozen=True, slots=True)
class RankingRequest:
    board: str
    category: str
    limit: int
    parameterized: bool


def parse_ranking_command(message: str) -> RankingRequest | None:
    """Parse optional category and size arguments for a ranking board.

    Accepted shapes: ``涨跌榜``, ``涨跌榜 印花``, ``涨跌榜 印花 20``,
    ``涨跌榜 20`` and ``涨跌榜 20 印花``; ``总榜/全部/全榜`` mean no filter.
    """

    text = message.strip()
    match = _RANKING_COMMAND_PATTERN.match(text)
    reversed_match = None if match is not None else _RANKING_REVERSED_PATTERN.match(text)
    if match is not None:
        board_name, category, raw_limit = match.group(1), match.group(2) or "", match.group(3)
    elif reversed_match is not None:
        board_name, raw_limit, category = (
            reversed_match.group(1),
            reversed_match.group(2) or "",
            reversed_match.group(3) or "",
        )
    else:
        return None
    board = "异动" if board_name == "异动榜" else board_name
    # A bare numeric token in the category slot is a requested size, not a
    # category name (e.g. "涨跌榜 20" means a 20-row board).
    if category.isdigit():
        if raw_limit is not None:
            return None
        raw_limit = category
        category = ""
    if category in _NO_CATEGORY_ALIASES:
        category = ""
    parameterized = bool(category or raw_limit) or board_name == "异动榜"
    limit = int(raw_limit) if raw_limit else 10
    return RankingRequest(board, category, limit, parameterized)


async def _unavailable_dip() -> str:
    return "⚠️ 抄底数据暂不可用，本次不生成判断，请稍后再试。"


async def _unavailable_score(_market_name: str, _user_query: str) -> str:
    return "⚠️ 买卖参考服务不可用，本次不生成评分，请稍后再试。"


def _unconfigured_csqaq_side() -> Awaitable[dict[str, Any]]:
    async def response() -> dict[str, Any]:
        return {"success": False, "errorMsg": "未配置", "unconfigured": True}

    return response()


@dataclass(frozen=True, slots=True)
class BroadKlineScreenshotRequest:
    """Adapter-neutral request for the current broad-market K-line image."""

    high_definition: bool = False
    max_days_ago: int | None = None


@dataclass(frozen=True, slots=True)
class ItemKlineScreenshotRequest:
    """Adapter-neutral request for one resolved item's K-line image."""

    market_name: str
    high_definition: bool = False
    special_style: str | None = None
    platform: str | None = None


BROAD_KLINE_SCREENSHOT = BroadKlineScreenshotRequest()
CommandResult = str | BroadKlineScreenshotRequest | ItemKlineScreenshotRequest | None


class ChatCommandService:
    """Route supported command text and coordinate market API calls."""

    def __init__(
        self,
        *,
        market_get: MarketGet,
        market_post: MarketPost,
        name_resolver: NameResolver = resolve_name,
        temperature_provider: TemperatureProvider = _unavailable_temperature,
        inventory_provider: InventoryProvider = _unavailable_inventory,
        broad_provider: BroadProvider | None = None,
        market_column_provider: MarketColumnProvider = _unavailable_market_column,
        ranking_provider: RankingProvider = _unavailable_ranking,
        dip_provider: Callable[[], Awaitable[str]] = _unavailable_dip,
        csqaq_side_provider: CsqaqSideProvider | None = None,
        score_provider: ScoreReplyProvider = _unavailable_score,
        score_summary_provider: ScoreSummaryProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._market_get = market_get
        self._market_post = market_post
        self._name_resolver = name_resolver
        self._temperature_provider = temperature_provider
        self._inventory_provider = inventory_provider
        self._broad_provider = broad_provider
        self._market_column_provider = market_column_provider
        self._ranking_provider = ranking_provider
        self._dip_provider = dip_provider
        self._csqaq_side_provider = csqaq_side_provider
        self._score_provider = score_provider
        self._score_summary_provider = score_summary_provider
        self._clock = clock
        self._wall_clock = wall_clock
        self._broad_cache: dict[str, Any] | None = None
        self._broad_time = 0.0
        self._broad_lock = asyncio.Lock()

    async def get_broad_market(self) -> dict[str, Any]:
        now = self._clock()
        if self._broad_cache and now - self._broad_time < 30:
            return self._broad_cache

        async with self._broad_lock:
            now = self._clock()
            if self._broad_cache and now - self._broad_time < 30:
                return self._broad_cache

            if self._broad_provider is None:
                data = await self._market_get("/open/cs2/broad/v1/index")
            else:
                data = await self._broad_provider()
            if data.get("success"):
                self._broad_cache = data
                self._broad_time = self._clock()
            return self._broad_cache or data

    async def execute(self, message: str) -> CommandResult:
        """Execute one command, returning ``None`` for ordinary chat text."""

        started = time.perf_counter()
        operation = _command_operation(message)
        try:
            result = await self._execute(message)
        except TimeoutError:
            await _record_command_telemetry(operation, "timeout", started)
            raise
        except Exception:
            await _record_command_telemetry(operation, "error", started)
            raise
        if result is not None:
            refused = isinstance(result, str) and result.startswith(("⚠️", "⏳"))
            await _record_command_telemetry(
                operation,
                "refused" if refused else "success",
                started,
            )
        return result

    async def _execute(self, message: str) -> CommandResult:
        """Internal command implementation separated for telemetry fencing."""

        command = message.strip()
        if command in {"K线高清", "k线高清"}:
            return BroadKlineScreenshotRequest(high_definition=True)

        if command in {"K线", "k线"}:
            return BROAD_KLINE_SCREENSHOT

        backtest = re.fullmatch(r"[Kk]线(高清)?\s*(\d{1,3})天前", command)
        if backtest is not None:
            days = int(backtest.group(2))
            if 1 <= days <= 90:
                return BroadKlineScreenshotRequest(
                    high_definition=bool(backtest.group(1)), max_days_ago=days
                )
            return "K线复盘支持 1~90 天前，例如：K线 30天前"

        if command in {"菜单", "menu", "help"}:
            menu = (
                "📋 菜单\n───────────────\n"
                "行情: 大盘｜抄底｜预测｜盘面解读\n"
                "饰品: 饰品查询｜比价｜买卖参考｜K线｜卡片｜官方价｜查磨损｜检视\n"
                "榜单: 异动/涨跌/流动性榜 [分类] [前N]（总榜/全部=不分类）\n"
                "资讯: CS2新闻｜CS2爆料｜异动原因\n"
                "监控: 预警｜库存查询｜库存监控｜自选\n"
                "记忆: 记住｜塔菲记忆｜塔菲知识｜塔菲印象\n"
                "娱乐: 开箱｜炼金｜随机皮肤\n"
                "管理: 塔菲状态（仅管理员）｜日报/周报\n"
            )
            return (
                menu + "───────────────\n💡 榜单示例: 涨跌榜 印花 20｜涨跌榜 20 印花｜流动性榜 总榜"
            )

        if command == "综合指数":
            breadth = await self._market_column_provider(command)
            if breadth == _PAUSED_MARKET_MESSAGE:
                return breadth
            broad = await self.get_broad_market()
            try:
                temperature = await self._temperature_provider(broad)
            except Exception as exc:
                logger.warning("Market temperature unavailable error=%s", type(exc).__name__)
                temperature = TemperatureUnavailable()
            return format_composite_market(
                broad,
                temperature=temperature,
                breadth_report=breadth,
            )

        ranking = parse_ranking_command(command)
        if ranking is not None and ranking.parameterized:
            return await self._ranking_provider(ranking.board, ranking.category, ranking.limit)
        # The fallback must only fire for genuine ranking-board shapes, not for
        # unrelated commands that merely share the "异动" prefix (e.g. 异动原因).
        if ranking is None and (
            command.startswith("异动榜")
            or command.startswith("涨跌榜")
            or command.startswith("流动性榜")
            or command == "异动"
            or command.startswith("异动 ")
        ):
            return (
                "⚠️ 榜单参数格式不对。\n"
                "格式: 异动榜/涨跌榜/流动性榜 [分类] [前N]\n"
                "示例: 异动榜 刀具 20｜涨跌榜 印花｜流动性榜 手套\n"
                "分类: 刀具/手套/探员/印花/音乐盒/武器/其他 或具体枪名（如 AK-47）"
            )

        if command in _PAUSED_MARKET_COLUMNS:
            return await self._market_column_provider(command)

        if command == "大盘":
            broad = await self.get_broad_market()
            try:
                temperature = await self._temperature_provider(broad)
            except Exception as exc:
                logger.warning("Market temperature unavailable error=%s", type(exc).__name__)
                temperature = TemperatureUnavailable()
            return format_broad_market(broad, temperature=temperature)

        if command == "抄底":
            return await self._dip_provider()

        if command == "库存查询":
            return (
                "📦 库存查询\n──────────\n提供Steam64位ID或交易链接\n"
                "格式: 库存查询 7656119xxxxxxxxxx\n──────────\n"
                "ℹ️ 只读已有公开快照，不会触发刷新他人库存"
            )

        if command.startswith("库存查询"):
            identifier = command[len("库存查询") :].strip()
            return await self._inventory_provider(identifier)

        if command == "娱乐系统":
            return (
                "🎮 娱乐系统\n──────────\n1. 开箱 → 发「开箱」\n2. 十连开 → 发「十连开」\n"
                "3. 炼金 → 发「炼金 材料名」\n4. 随机皮肤 → 发「随机皮肤」\n"
                "5. 娱乐视频 → 随机塔菲视频\n6. 娱乐图片 → 随机美图\n──────────\n"
                "ℹ️ 开箱使用箱内真实稀有度目录和公示概率，但结果纯模拟\n"
                "⚠️ 炼金为幻想玩法，不复刻真实游戏合约"
            )

        entertainment = await handle_entertainment_command(
            command,
            resolve_name=self._name_resolver,
        )
        if entertainment is not None:
            return entertainment

        return await self._market_item_command(command)

    async def _market_item_command(self, command: str) -> CommandResult:
        for prefix in (
            "K线高清",
            "k线高清",
            "查询",
            "饰品查询",
            "比价",
            "买卖参考",
            "分析",
            "饰品分析",
            "K线",
            "k线",
        ):
            if not command.startswith(prefix):
                continue

            raw_query = command[len(prefix) :].strip()
            if not raw_query:
                return f"请加名称，例: {prefix} 红线 久经"

            platform_query, platform = raw_query, None
            if prefix in {"K线高清", "k线高清", "K线", "k线"}:
                platform_query, platform = parse_kline_platform_suffix(raw_query)
            style = parse_special_style_query(platform_query)
            query = style.clean_query
            if not query:
                return "请补充饰品名称，特殊款式不能单独查询。"

            name = await self._name_resolver(query)
            if not name:
                return resolution_failure_message(query)

            if prefix in {"查询", "饰品查询"}:
                if style.code is not None:
                    return (
                        f"⚠️ 当前报价接口不能按“{style.label}”区分价格，本次不拿普通款报价冒充。\n"
                        f"可发送“K线 {query} {style.label}”查看对应款式走势。"
                    )
                requests: list[Awaitable[Any]] = [
                    self._market_get(
                        "/open/cs2/v1/price/single",
                        {"marketHashName": name},
                    ),
                    self._market_get(
                        "/open/cs2/v1/price/avg",
                        {"marketHashName": name},
                        300,
                    ),
                ]
                if self._csqaq_side_provider is not None:
                    requests.append(self._csqaq_side_provider(raw_query, name))
                if self._score_summary_provider is not None:
                    requests.append(self._score_summary_provider(name, raw_query))
                results = await asyncio.gather(*requests)
                price, average = results[:2]
                fetched_at = self._wall_clock()
                reply = (
                    f"{name}\n\n{format_price(price, now=fetched_at)}\n"
                    f"──────────\n{format_average_price(average, fetched_at=fetched_at, now=fetched_at)}"
                )
                if len(results) > 2:
                    reply += _csqaq_context_lines(results[2])
                if len(results) > 3 and isinstance(results[3], str):
                    reply += f"\n{results[3]}"
                hoard = await _hoard_reference_line(name)
                if hoard:
                    reply += f"\n{hoard}"
                return reply

            if prefix == "比价":
                if style.code is not None:
                    return (
                        f"⚠️ 比价暂不支持特殊款式（{style.label}），本次不拿普通款价格冒充。\n"
                        f"可发送“K线 {query} {style.label}”查看对应款式走势。"
                    )
                return await self._compare(name, raw_query)

            if prefix == "买卖参考":
                if style.code is not None:
                    return (
                        f"⚠️ 买卖参考暂不支持特殊款式（{style.label}），本次不拿普通款评分冒充。\n"
                        f"可发送“K线 {query} {style.label}”查看对应款式走势。"
                    )
                return await self._score_provider(name, raw_query)

            if prefix in {"K线高清", "k线高清", "K线", "k线"}:
                return ItemKlineScreenshotRequest(
                    name,
                    high_definition=prefix in {"K线高清", "k线高清"},
                    special_style=style.code,
                    platform=platform,
                )

            if style.code is not None:
                return (
                    f"⚠️ “{style.label}”目前只支持K线，不把普通款报价混进特殊款分析。\n"
                    f"请发送“K线 {query} {style.label}”。"
                )
            return await self._analysis(name)
        return None

    def _csqaq_side(self, query: str, name: str) -> Awaitable[dict[str, Any]] | None:
        if self._csqaq_side_provider is None:
            return None
        return self._csqaq_side_provider(query, name)

    async def _compare(self, name: str, query: str) -> str:
        csqaq_side = self._csqaq_side(query, name)
        if csqaq_side is None:
            csqaq_side = _unconfigured_csqaq_side()
        price, csqaq_result, hoard_line = await asyncio.gather(
            self._market_get(
                "/open/cs2/v1/price/single",
                {"marketHashName": name},
            ),
            csqaq_side,
            _hoard_reference_line(name),
        )
        trend_line = await record_and_trend_line(name, price, csqaq_result)
        return format_price_comparison(
            name,
            price,
            csqaq_result,
            now=self._wall_clock(),
            hoard_line=hoard_line,
            trend_line=trend_line,
        )

    async def _analysis(self, name: str) -> str:
        price, average, kline = await asyncio.gather(
            self._market_get(
                "/open/cs2/v1/price/single",
                {"marketHashName": name},
            ),
            self._market_get(
                "/open/cs2/v1/price/avg",
                {"marketHashName": name},
                300,
            ),
            self._market_post(
                "/open/cs2/item/v1/kline",
                {"marketHashName": name, "type": 2},
            ),
        )
        fetched_at = self._wall_clock()
        return format_item_analysis(
            name,
            price,
            kline,
            average_data=average,
            average_fetched_at=fetched_at,
            now=fetched_at,
        )

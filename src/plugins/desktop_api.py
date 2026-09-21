"""桌面 API — 塔菲桌面工作台的薄门面（Bearer 保护，默认关闭）。

纪律（A1 薄门面）：每个端点只做「参数校验 → 调现有 service/repo → JSON 化」，
不写业务逻辑。总开关 ``TAFFY_DESKTOP_API_ENABLED`` 默认 false（fail-closed），
关闭时所有 desktop 端点 404。个性化数据不进 bot 数据库（A2）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from nonebot import get_app, get_driver

from src.core.rate_limit import SlidingWindowLimiter
from src.plugins.admin_api import _health_snapshot, prevent_response_cache, require_admin_api_token
from src.plugins.chat.compare import build_compare_payload
from src.plugins.chat.market import resolve_name
from src.plugins.inventory_monitor import (
    inventory_monitor_service,
)
from src.services import config as config_service
from src.services.case_ev import build_case_ev_payload
from src.services.config import PROJECT_ROOT, env_bool
from src.services.fact_intent import detect_fact_intent
from src.services.index_history import index_history_store
from src.services.market import get_broad_summary, st_get, st_post
from src.services.market_history import market_history
from src.services.market_scoring import build_trade_score_payload
from src.services.market_special_style import validated_special_style
from src.services.market_temperature import TemperatureUnavailable, get_market_temperature
from src.services.market_validation import (
    BroadMarketSnapshot,
    MarketDataError,
    ValidatedQuote,
    validate_broad_market_response,
    validate_quote_response,
)
from src.state.persona_memory_repository import (
    PersonaMemoryCapacityError,
    PersonaScope,
)
from src.state.runtime import (
    alerts as alert_repository,
)
from src.state.runtime import (
    database,
    inventory_monitors,
    persona_memories,
    persona_relationships,
    persona_slang,
    persona_styles,
    reddit_posts,
    schedules,
    sessions,
    usage_log,
    watchlists,
)

_log = logging.getLogger("taffy.desktop")

# ─── 错误类型（对外只暴露类型名 + 中文一句话） ────────────────────────────


class DesktopAPIError(Exception):
    """Base class whose concrete type name becomes the envelope ``error``."""


class InvalidParameterError(DesktopAPIError):
    pass


class NotFoundError(DesktopAPIError):
    pass


class ConflictError(DesktopAPIError):
    pass


class ForbiddenError(DesktopAPIError):
    pass


class CapacityError(DesktopAPIError):
    pass


class OwnerNotConfiguredError(DesktopAPIError):
    pass


class UpstreamUnavailableError(DesktopAPIError):
    pass


_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (InvalidParameterError, 400),
    (NotFoundError, 404),
    (ForbiddenError, 403),
    (ConflictError, 409),
    (CapacityError, 429),
    (OwnerNotConfiguredError, 503),
    (UpstreamUnavailableError, 503),
)


def _error_status(exc: Exception) -> int:
    for kind, status_code in _ERROR_STATUS:
        if isinstance(exc, kind):
            return status_code
    return 500


# ─── 响应包装饰器 ────────────────────────────────────────

_GENERIC_FAILURE_MESSAGE = "桌面服务暂时不可用，请稍后再试。"


def _envelope(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[JSONResponse]]:
    """Wrap one endpoint into the ``{ok,data}`` / ``{ok,error,message}`` contract."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> JSONResponse:
        try:
            data = await fn(*args, **kwargs)
        except DesktopAPIError as exc:
            return JSONResponse(
                status_code=_error_status(exc),
                content={"ok": False, "error": type(exc).__name__, "message": str(exc)},
            )
        except HTTPException as exc:
            # Handlers deliberately raise HTTPException only where the shared
            # admin-style transport shape (Retry-After headers) is required.
            return JSONResponse(
                status_code=exc.status_code,
                content={"ok": False, "error": "HTTPException", "message": str(exc.detail)},
                headers=dict(exc.headers) if exc.headers else None,
            )
        except MarketDataError as exc:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "MarketDataError", "message": exc.user_message},
            )
        except Exception as exc:
            _log.error("Desktop endpoint %s failed error=%s", fn.__name__, type(exc).__name__)
            return JSONResponse(
                status_code=500,
                content={
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": _GENERIC_FAILURE_MESSAGE,
                },
            )
        return JSONResponse(status_code=200, content={"ok": True, "data": data})

    return wrapper


# ─── 总开关 / owner 解析 ─────────────────────────────────

_OWNER_ENV = "DESKTOP_OWNER_QQ"
_SESSION_KEY_PATTERN = re.compile(r"\Ad_[A-Za-z0-9_-]{1,80}\Z")
_AUDIT_TEXT_LIMIT = 200
_CHAT_MAX_MESSAGE_CHARS = 4000


def desktop_api_enabled() -> bool:
    """Read the fail-closed master switch at request time."""

    return env_bool("TAFFY_DESKTOP_API_ENABLED", False)


async def require_desktop_enabled() -> None:
    if not desktop_api_enabled():
        raise HTTPException(status_code=404, detail="Not found")


_OWNER_REASON_OK = "ok"
_OWNER_REASON_MISSING = "missing"
_OWNER_REASON_AMBIGUOUS = "ambiguous"


def _numeric_superusers() -> list[int]:
    r"""Valid numeric superuser ids in deterministic (lexicographic) order.

    ``get_driver().config.superusers`` is a **set**, so NoneBot itself carries no
    usable order.  The raw ``SUPERUSERS`` value in ``.env`` *is* an order-preserving
    JSON list and the repo does read it in order elsewhere
    (``services/embedding.py``, ``services/v4_policy.py`` use
    ``re.findall(r"\d+", runtime_setting("SUPERUSERS"))``), but going through the
    raw file here would re-couple owner resolution from the NoneBot config object to
    the env file's writing format.  Not worth it: with several candidates the answer
    is refused outright (see below), so the ordering never decides anything.
    """

    try:
        entries = get_driver().config.superusers
    except Exception:
        return []
    valid: list[int] = []
    for entry in sorted(str(item).strip() for item in entries):
        if entry.isascii() and entry.isdigit() and int(entry) > 0:
            valid.append(int(entry))
    return valid


def _resolve_owner_decision(config: object | None = None) -> tuple[int | None, str]:
    """The **single** place the desktop-owner rule is decided.

    Returns ``(owner, reason)``.  With more than one candidate superuser and no
    explicit ``DESKTOP_OWNER_QQ`` the answer is ``(None, "ambiguous")``: the owner is
    deliberately **not** guessed, because picking one silently moves the desktop
    owner — and with it persona write scope, the "read someone else's private chat"
    exemption, and desktop audit attribution — to whichever account sorts first
    (M-110, the masked form of M-08: it cannot bite while there is exactly one
    superuser).
    """

    raw = config_service.runtime_setting(_OWNER_ENV)
    if raw is not None and raw.strip():
        candidate = raw.strip()
        if candidate.isascii() and candidate.isdigit() and int(candidate) > 0:
            return int(candidate), _OWNER_REASON_OK
        _log.warning("Ignoring invalid %s setting", _OWNER_ENV)
    candidates = _numeric_superusers()
    if len(candidates) > 1:
        _log.warning(
            "Multiple superusers (%d) and no explicit %s; refusing to guess the desktop owner",
            len(candidates),
            _OWNER_ENV,
        )
        return None, _OWNER_REASON_AMBIGUOUS
    if not candidates:
        return None, _OWNER_REASON_MISSING
    return candidates[0], _OWNER_REASON_OK


def _owner_unavailable_text(purpose: str) -> str:
    """Actionable text for 200 responses that degrade without a resolvable owner.

    ``None`` has **two** causes needing different operator actions: nothing is
    configured (go set the key) versus several superusers and no explicit owner
    (go set the key *explicitly*).  Saying "未配置" in the ambiguous case sends the
    operator to inspect a key that **is** configured — the same "one fact, two
    accounts" defect this batch keeps finding, newly triggered by M-110 widening
    what ``None`` means.
    """

    if _resolve_owner_decision()[1] == _OWNER_REASON_AMBIGUOUS:
        return (
            f"桌面主人未能确定（存在多个 superusers 且未显式配置），{purpose}；"
            "请在 DESKTOP_OWNER_QQ 中显式指定。"
        )
    return f"未配置桌面主人（DESKTOP_OWNER_QQ），{purpose}。"


def resolve_desktop_owner(config: object | None = None) -> int | None:
    """``DESKTOP_OWNER_QQ`` first, else the **single** superuser; ``None`` if refused.

    The setting is read through :func:`src.services.config.runtime_setting` — the
    project-wide layered reader — instead of ``os.environ`` directly.  Reading the
    environment here missed the whole dotenv layer (``.env`` / ``.env.<env>``), so a
    deployment that configured ``DESKTOP_OWNER_QQ`` there silently resolved to a
    superuser instead, i.e. the desktop owner was the wrong account (M-08).
    ``runtime_setting`` keeps the live process environment authoritative, so
    container/secret-manager overrides still win.

    Returns ``None`` both when nothing is configured and when the fallback is
    **ambiguous** (several superusers, no explicit owner) — callers that need to tell
    those apart use :func:`_resolve_owner_decision` or
    :func:`require_desktop_owner`, which reports the difference to the operator.
    """

    owner, _reason = _resolve_owner_decision(config)
    return owner


def require_desktop_owner() -> int:
    owner = resolve_desktop_owner()
    if owner is None:
        # The *rule* lives in :func:`_resolve_owner_decision`; here we only ask it
        # **why** the answer was "no owner" so the operator gets the right hint.
        # Going through the public ``resolve_desktop_owner`` (not the decision
        # helper directly) keeps the documented injection seam working — the
        # envelope tests patch exactly that name.
        if _resolve_owner_decision()[1] == _OWNER_REASON_AMBIGUOUS:
            raise OwnerNotConfiguredError(
                "检测到多个 superusers 且未显式配置 DESKTOP_OWNER_QQ；"
                "为避免桌面主人静默迁移到排序最前的账号，请显式设置 DESKTOP_OWNER_QQ。"
            )
        raise OwnerNotConfiguredError(
            "尚未配置桌面主人 QQ（DESKTOP_OWNER_QQ 或 superusers），该功能不可用。"
        )
    return owner


# ─── 限流（chat 12/min/会话，行情 30/min，写 10/min） ─────────────────────

_RATE_WINDOW_SECONDS = 60.0
_CHAT_LIMIT_PER_SESSION = 12
_MARKET_LIMIT = 30
_WRITE_LIMIT = 10

_chat_limiter = SlidingWindowLimiter(_CHAT_LIMIT_PER_SESSION, _RATE_WINDOW_SECONDS)
_market_limiter = SlidingWindowLimiter(_MARKET_LIMIT, _RATE_WINDOW_SECONDS)
_write_limiter = SlidingWindowLimiter(_WRITE_LIMIT, _RATE_WINDOW_SECONDS)


async def limit_market_reads() -> None:
    if not await _market_limiter.allow("market"):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_RATE_WINDOW_SECONDS))},
        )


async def limit_writes() -> None:
    if not await _write_limiter.allow("write"):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_RATE_WINDOW_SECONDS))},
        )


async def limit_chat(session_key: str) -> None:
    if not await _chat_limiter.allow(f"chat:{session_key}"):
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={"Retry-After": str(int(_RATE_WINDOW_SECONDS))},
        )


# ─── 审计 ────────────────────────────────────────────────


async def _audit(feature: str, command: str, summary: str) -> None:
    """Append one usage row; audit failures never break the response."""

    owner = resolve_desktop_owner()
    if owner is None:
        _log.info("Desktop audit skipped without owner feature=%s", feature)
        return
    try:
        await usage_log.record(
            user_id=owner,
            feature=feature[:64],
            command=command.strip()[:200],
            input_text=" ".join(summary.split())[:_AUDIT_TEXT_LIMIT],
        )
    except Exception as exc:
        _log.warning("Desktop audit write failed error=%s", type(exc).__name__)


# ─── 路由 ────────────────────────────────────────────────

router = APIRouter(
    prefix="/api/desktop",
    dependencies=[
        Depends(require_desktop_enabled),
        Depends(prevent_response_cache),
        Depends(require_admin_api_token),
    ],
)


def _session_key(owner: int, session: str | None) -> str:
    key = session.strip() if session else f"d_{owner}"
    if _SESSION_KEY_PATTERN.fullmatch(key) is None:
        raise InvalidParameterError("会话标识必须以 d_ 开头，只含字母、数字、下划线或连字符。")
    return key


def _persona_scope(owner: int) -> PersonaScope:
    # group_id=0 与 QQ 私聊同一作用域：记忆/风格天然互通（F11）。
    return PersonaScope(owner_user_id=owner, group_id=0)


# ─── Chat ───────────────────────────────────────────────


@router.post("/chat")
@_envelope
async def desktop_chat(
    message: Annotated[str, Body(embed=True, min_length=1, max_length=_CHAT_MAX_MESSAGE_CHARS)],
    session: Annotated[str | None, Body(embed=True, max_length=100)] = None,
) -> dict[str, Any]:
    from src.services.llm import chat_service

    owner = require_desktop_owner()
    session_id = _session_key(owner, session)
    await limit_chat(session_id)
    started = time.monotonic()
    reply = await chat_service.chat(message, session_id, persona_scope=_persona_scope(owner))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    intent = detect_fact_intent(message)
    await _audit("desktop.chat", "/api/desktop/chat", message)
    return {
        "reply": reply,
        "intent": intent.kind.value if intent is not None else None,
        "elapsed_ms": elapsed_ms,
        "session": session_id,
    }


@router.get("/chat/history")
@_envelope
async def desktop_chat_history(
    session: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=40)] = 20,
) -> dict[str, Any]:
    from src.state.runtime import sessions

    owner = require_desktop_owner()
    session_id = _session_key(owner, session)
    messages = await sessions.as_messages(session_id, last_n=limit)
    await _audit("desktop.chat", "/api/desktop/chat/history", f"session={session_id} limit={limit}")
    return {"session": session_id, "messages": messages, "count": len(messages)}


@router.delete("/chat/history")
@_envelope
async def desktop_chat_history_delete(
    session: Annotated[str | None, Query(max_length=100)] = None,
) -> dict[str, Any]:
    from src.state.runtime import sessions

    owner = require_desktop_owner()
    session_id = _session_key(owner, session)
    try:
        async with sessions.session_guard(session_id, 15, wait_timeout=10):
            deleted = bool(await sessions.delete(session_id))
    except TimeoutError as exc:
        raise ConflictError("会话正忙（可能正在生成回复），请稍后再清空。") from exc
    await _audit("desktop.chat", "/api/desktop/chat/history", f"delete session={session_id}")
    return {"session": session_id, "deleted": deleted}


# ─── Market ─────────────────────────────────────────────


def _snapshot_payload(snapshot: BroadMarketSnapshot) -> dict[str, Any]:
    return {
        "index": snapshot.index,
        "change_percent": snapshot.change_percent,
        "change_value": snapshot.change_value,
        "timestamp": snapshot.timestamp,
    }


async def _overview_temperature(broad_raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if broad_raw is None:
        return None
    try:
        result = await get_market_temperature(broad_raw)
    except Exception as exc:
        _log.warning("Overview temperature failed error=%s", type(exc).__name__)
        return None
    if isinstance(result, TemperatureUnavailable):
        return {"available": False, "reason": result.reason}
    payload: dict[str, Any] = {
        "available": True,
        "score": result.score,
        "state": result.temperature_state,
        "detail": result.detail,
        "factors": {
            "change_1d": result.change_1d,
            "change_3d": result.change_3d,
            "change_7d": result.change_7d,
            "deviation_7d": result.deviation_7d,
        },
    }
    return payload


async def _overview_breadth() -> dict[str, Any] | None:
    try:
        breadth = await market_history.breadth()
    except Exception as exc:
        _log.warning("Overview breadth failed error=%s", type(exc).__name__)
        return None
    if breadth is None:
        return None
    return {
        "compared": breadth.compared,
        "rising_ratio": breadth.rising_ratio,
        "median_change": breadth.median_change,
        "sell_depth": breadth.sell_depth,
        "bid_depth": breadth.bid_depth,
        "latest_bucket": breadth.latest_bucket,
    }


@router.get("/market/overview", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_overview() -> dict[str, Any]:
    broad_raw: Mapping[str, Any] | None = None
    broad: dict[str, Any] | None = None
    broad_error: str | None = None
    try:
        broad_raw = await get_broad_summary()
        broad = _snapshot_payload(validate_broad_market_response(broad_raw))
    except MarketDataError as exc:
        _log.warning("Overview broad rejected error=%s", type(exc).__name__)
        broad_error = exc.user_message
    except Exception as exc:
        _log.warning("Overview broad failed error=%s", type(exc).__name__)
        broad_error = "大盘数据暂不可用。"
    temperature = await _overview_temperature(broad_raw)
    breadth = await _overview_breadth()
    await _audit("desktop.market", "/api/desktop/market/overview", "overview")
    # 任一部分失败置 null 不整体失败（fail-open 展示、fail-closed 数据）。
    return {
        "broad": broad,
        "broad_error": broad_error,
        "temperature": temperature,
        "breadth": breadth,
    }


@router.get("/market/index-closes", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_index_closes(
    days: Annotated[int, Query(ge=7, le=365)] = 90,
) -> dict[str, Any]:
    closes = await index_history_store.load_closes()
    cutoff = closes[-1][0].toordinal() - days + 1 if closes else 0
    series = [
        {"date": day.isoformat(), "close": close}
        for day, close in closes
        if day.toordinal() >= cutoff
    ]
    await _audit("desktop.market", "/api/desktop/market/index-closes", f"days={days}")
    return {"points": series, "count": len(series)}


async def _dip_report_text() -> str:
    """Reuse the exact QQ dip pipeline (render_column has no 抄底 branch)."""

    from src.plugins.chat import _dip_report

    return await _dip_report()


@router.get("/market/dip", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_dip() -> dict[str, str]:
    report = await _dip_report_text()
    await _audit("desktop.market", "/api/desktop/market/dip", "dip")
    return {"report": report}


@router.get("/market/prediction", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_prediction() -> dict[str, str]:
    report = await market_history.render_column("预测")
    await _audit("desktop.market", "/api/desktop/market/prediction", "prediction")
    return {"report": report}


_RANKING_BOARDS = ("异动榜", "涨跌榜", "流动性榜")


@router.get("/market/rankings", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_rankings(
    board: Annotated[str, Query(min_length=1, max_length=20)],
    category: Annotated[str, Query(max_length=40)] = "",
    limit: Annotated[int, Query(ge=5, le=20)] = 10,
) -> dict[str, str]:
    if board not in _RANKING_BOARDS:
        raise InvalidParameterError("榜单仅支持 异动榜 / 涨跌榜 / 流动性榜。")
    report = await market_history.render_ranking(board, category, limit)
    await _audit(
        "desktop.market", "/api/desktop/market/rankings", f"board={board} category={category}"
    )
    return {"report": report}


@router.get("/market/search", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_search(
    q: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=20)] = 20,
) -> dict[str, Any]:
    from src.plugins.chat.market import (
        catalog_search_query,
        resolve_catalog_name,
    )
    from src.services.market_catalog import search_market_catalog
    from src.services.market_catalog_store import get_market_catalog_entries

    query = q.strip()
    durable_entries: tuple[Any, ...] = ()
    try:
        durable_entries = await get_market_catalog_entries()
    except Exception as exc:
        _log.warning("Search catalog unavailable error=%s", type(exc).__name__)
    live_entries: tuple[Any, ...] = ()
    try:
        live_entries = await search_market_catalog(catalog_search_query(query))
    except Exception as exc:
        _log.warning("Search live catalog failed error=%s", type(exc).__name__)
    combined = {entry.market_name: entry for entry in (*durable_entries, *live_entries)}
    needle = query.casefold()
    prefix_hits: list[tuple[int, Any]] = []
    contains_hits: list[tuple[int, Any]] = []
    token_hits: list[Any] = []
    for entry in combined.values():
        display = str(entry.display_name).casefold()
        market = str(entry.market_name).casefold()
        if needle in display or needle in market:
            rank = 0 if display.startswith(needle) or market.startswith(needle) else 1
            (prefix_hits if rank == 0 else contains_hits).append((rank, entry))
    if not prefix_hits and not contains_hits:
        # Mixed queries such as "AK红线" are not a contiguous substring of any
        # full name; fall back to requiring every ASCII/CJK token of the query
        # ("ak" + "红线") to appear in the entry instead.
        tokens = [
            token for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", needle) if len(token) >= 2
        ]
        if tokens and tokens != [needle]:
            for entry in combined.values():
                haystack = f"{entry.market_name} {entry.display_name}".casefold()
                if all(token in haystack for token in tokens):
                    token_hits.append(entry)
    # Rank ties must never compare CatalogEntry objects (not orderable);
    # sorting by rank only keeps the stable insertion order.
    ordered = (
        [entry for _, entry in sorted(prefix_hits, key=lambda pair: pair[0])]
        + [entry for _, entry in contains_hits]
        + sorted(token_hits, key=lambda entry: str(entry.market_name))
    )
    exact_name = None
    try:
        exact_name = resolve_catalog_name(query, tuple(combined.values()))
    except Exception as exc:  # pragma: no cover - defensive, matcher never raises today
        _log.warning("Search exact match failed error=%s", type(exc).__name__)
    results = [
        {"market_name": entry.market_name, "display_name": entry.display_name}
        for entry in ordered[:limit]
    ]
    if exact_name is not None and all(item["market_name"] != exact_name for item in results):
        entry = combined[exact_name]
        results.insert(0, {"market_name": entry.market_name, "display_name": entry.display_name})
        results = results[:limit]
    await _audit("desktop.market", "/api/desktop/market/search", f"q={query}")
    return {"results": results, "count": len(results)}


def _quote_payload(quote: ValidatedQuote) -> dict[str, Any]:
    return {
        "platform": quote.platform,
        "sell_price": quote.sell_price,
        "sell_count": quote.sell_count,
        "bid_price": quote.bid_price,
        "bid_count": quote.bid_count,
        "timestamp": quote.timestamp,
    }


def _average_payload(data: Mapping[str, Any]) -> dict[str, Any] | None:
    """JSON 化 7 日均价响应（字段提取与 format_average_price 同口径）。"""

    if not data.get("success") or not isinstance(data.get("data"), Mapping):
        return None
    payload = data["data"]
    overall = payload.get("avgPrice")
    if isinstance(overall, bool) or not isinstance(overall, (int, float)) or not float(overall) > 0:
        return None
    buff: float | None = None
    platforms = payload.get("dataList")
    if isinstance(platforms, Sequence) and not isinstance(platforms, (str, bytes)):
        for item in platforms:
            if isinstance(item, Mapping) and item.get("platform") == "BUFF":
                value = item.get("avgPrice")
                if not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0:
                    buff = float(value)
                break
    return {"overall": float(overall), "buff": buff}


async def _item_quotes(market_name: str) -> list[dict[str, Any]]:
    price = await st_get("/open/cs2/v1/price/single", {"marketHashName": market_name})
    quotes = validate_quote_response(price)
    return [_quote_payload(quote) for quote in quotes]


@router.get("/market/item", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_item(
    name: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict[str, Any]:
    from src.plugins.chat import resolve_name

    market_name = await resolve_name(name.strip())
    if market_name is None:
        raise InvalidParameterError(
            f"未能唯一确认物品「{name.strip()}」；请发送完整 Steam 市场名（含武器、外观、磨损）。"
        )
    quotes_task = asyncio.create_task(_item_quotes(market_name))
    average_task = asyncio.create_task(
        st_get("/open/cs2/v1/price/avg", {"marketHashName": market_name}, 300)
    )
    change_task = asyncio.create_task(market_history.item_change(market_name, hours=24))
    try:
        quotes, average, change = await asyncio.gather(
            quotes_task, average_task, change_task, return_exceptions=True
        )
    finally:
        for task in (quotes_task, average_task, change_task):
            if not task.done():
                task.cancel()
    if isinstance(quotes, BaseException):
        if isinstance(quotes, MarketDataError):
            raise quotes
        raise UpstreamUnavailableError("市场报价获取失败，请稍后重试。") from quotes
    if isinstance(average, BaseException):
        _log.warning("Item average price failed error=%s", type(average).__name__)
        average = None
    item_change_payload = None
    if not isinstance(change, BaseException) and change is not None:
        item_change_payload = {
            "latest_price": change[0],
            "change_percent": change[1],
        }
    await _audit("desktop.market", "/api/desktop/market/item", f"name={market_name}")
    return {
        "name": market_name,
        "quotes": quotes,
        "average": _average_payload(average),
        "change_24h": item_change_payload,
    }


_KLINE_LOCAL_MIN_DAYS = 14
_KLINE_LOCAL_NOTE = "本地快照聚合K线（口径：每小时在售价，仅覆盖采样池品种）"


async def _local_snapshot_candles(market_name: str, days: int = 90) -> list[list[float]] | None:
    """Aggregate real hourly snapshot sell prices into market-day OHLC.

    Reads the service-owned market-history database read-only via the shared
    local-store recipe (the store itself has no public daily-candle method);
    this is the CONTRACT.md-documented option allowed by the V0.2 task.  No
    candle is ever fabricated: fewer than ``_KLINE_LOCAL_MIN_DAYS`` sampled
    days means "no local data" and returns None.
    """

    from datetime import datetime, timezone

    from src.services.config import project_path_setting
    from src.services.local_store import run_local_call
    from src.services.market_history import DEFAULT_HISTORY_PATH
    from src.services.tz import to_market_tz

    history_path = project_path_setting("TAFFY_MARKET_HISTORY_DB", DEFAULT_HISTORY_PATH)
    cutoff = int(time.time()) - days * 86400

    try:
        rows = await run_local_call(
            history_path,
            lambda connection: connection.execute(
                "SELECT bucket_at, sell_price FROM quote_snapshots "
                "WHERE market_name = ? AND bucket_at >= ? ORDER BY bucket_at",
                (market_name, cutoff),
            ).fetchall(),
            timeout_seconds=5.0,
            label="desktop kline fallback",
        )
    except Exception as exc:
        # A missing/locked history database simply means "no local data".
        _log.warning("Desktop local kline fallback unavailable error=%s", type(exc).__name__)
        return None
    by_date: dict[str, list[float]] = {}
    day_epochs: dict[str, float] = {}
    for row in rows:
        bucket = int(row["bucket_at"])
        price = float(row["sell_price"])
        local = to_market_tz(datetime.fromtimestamp(bucket, timezone.utc))
        date_key = local.date().isoformat()
        by_date.setdefault(date_key, []).append(price)
        day_epochs.setdefault(date_key, local.replace(hour=0, minute=0, second=0).timestamp())
    if len(by_date) < _KLINE_LOCAL_MIN_DAYS:
        return None
    candles: list[list[float]] = []
    for date_key in sorted(by_date):
        prices = by_date[date_key]
        candles.append(
            [
                float(day_epochs[date_key]),
                prices[0],
                prices[-1],
                max(prices),
                min(prices),
            ]
        )
    return candles


@router.get("/market/kline", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_kline(
    name: Annotated[str, Query(min_length=1, max_length=200)],
    style: Annotated[str | None, Query(max_length=40)] = None,
    fallback: Annotated[str, Query(pattern="^(auto|off)$")] = "auto",
) -> dict[str, Any]:
    from src.plugins.chat.market import fresh_kline_points

    clean_name = name.strip()
    style_code = validated_special_style(style)
    if style is not None and style.strip() != "" and style_code is None:
        raise InvalidParameterError("未知特殊款式代码，请使用黑珍珠/蓝宝石/一档…等官方款式名。")
    body: dict[str, Any] = {"marketHashName": clean_name, "type": 2}
    if style_code is not None:
        body["specialStyle"] = style_code
    upstream_error: str | None = None
    points: list[tuple[float, float, float, float, float]] | None = None
    try:
        response = await st_post("/open/cs2/item/v1/kline", body)
    except Exception as exc:
        upstream_error = f"上游K线请求失败（{type(exc).__name__}）"
    else:
        points, error = fresh_kline_points(response)
        if points is None:
            assert error is not None
            upstream_error = error.lstrip("⚠️").strip()
    if points is not None:
        await _audit(
            "desktop.market", "/api/desktop/market/kline", f"name={clean_name} style={style_code}"
        )
        return {
            "name": clean_name,
            "style": style_code,
            "points": [list(point) for point in points],
            "count": len(points),
            "source": "upstream",
            "note": None,
        }
    # Upstream unavailable: fall back to honest local snapshot aggregation.
    if fallback == "auto" and style_code is None:
        candles = await _local_snapshot_candles(clean_name)
        if candles:
            await _audit(
                "desktop.market",
                "/api/desktop/market/kline",
                f"name={clean_name} source=local_snapshot",
            )
            return {
                "name": clean_name,
                "style": None,
                "points": candles,
                "count": len(candles),
                "source": "local_snapshot",
                "note": _KLINE_LOCAL_NOTE,
            }
    raise UpstreamUnavailableError(upstream_error or "市场K线暂不可用。")


_WATCHLIST_REFRESH_CAP = 20


@router.get("/market/watchlist-quotes", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_watchlist_quotes(
    refresh: Annotated[int, Query(ge=0, le=1)] = 0,
) -> dict[str, Any]:
    owner = require_desktop_owner()
    items = await watchlists.list_owner(owner)
    entries: list[dict[str, Any]] = []
    for item in items[:_WATCHLIST_REFRESH_CAP]:
        snapshot = await market_history.item_snapshot(item.display_name)
        quotes: list[dict[str, Any]] | None = None
        if refresh:
            try:
                quotes = await _item_quotes(item.display_name)
            except Exception as exc:
                _log.warning("Watchlist refresh failed error=%s", type(exc).__name__)
                quotes = None
        entries.append(
            {
                "display_name": item.display_name,
                "snapshot": snapshot,
                "quotes": quotes,
            }
        )
    await _audit(
        "desktop.market",
        "/api/desktop/market/watchlist-quotes",
        f"refresh={refresh} count={len(entries)}",
    )
    return {"items": entries, "count": len(entries), "truncated": len(items) > len(entries)}


# ─── Alerts ─────────────────────────────────────────────


_ALERT_DIRECTIONS = ("above", "below")


@router.post("/alerts", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_alert_create(
    name: Annotated[str, Body(embed=True, min_length=1, max_length=100)],
    direction: Annotated[str, Body(embed=True)],
    target: Annotated[float, Body(embed=True, gt=0)],
) -> dict[str, Any]:
    owner = require_desktop_owner()
    if direction not in _ALERT_DIRECTIONS:
        raise InvalidParameterError("预警方向只能是 above（低于目标提醒买入）或 below。")
    try:
        Decimal(str(target))
    except InvalidOperation as exc:  # pragma: no cover - pydantic already bounds it
        raise InvalidParameterError("目标价必须是有效数字。") from exc
    try:
        result = await alert_repository.create(
            owner,
            name.strip(),
            target,
            direction=direction,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise InvalidParameterError("预警参数无效，请检查名称与目标价。") from exc
    if result.quota_exceeded or result.alert is None:
        raise CapacityError("预警数量已达上限（每人同时最多 20 条），请先取消部分预警。")
    alert = result.alert
    await _audit(
        "desktop.alerts",
        "/api/desktop/alerts",
        f"create {alert.market_name} {direction} {target}",
    )
    return {
        "id": alert.id,
        "market_name": alert.market_name,
        "direction": alert.direction,
        "target_price_cents": alert.target_price_cents,
        "status": alert.status,
        "created_at": alert.created_at,
    }


# ─── Inventory ──────────────────────────────────────────

_INVENTORY_ACCOUNT_COLUMNS = (
    "steam_id, nickname, source_time, last_success_at, next_check_at, "
    "last_error, consecutive_failures"
)


@router.get("/inventory/state", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_inventory_state(
    event_limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    # Privacy boundary (P1): monitor reads are scoped to the desktop owner's
    # own subscriptions; with no resolvable owner nothing is returned at all.
    owner = resolve_desktop_owner()
    if owner is None:
        await _audit("desktop.inventory", "/api/desktop/inventory/state", "state no-owner")
        return {"accounts": [], "subscriptions": [], "events": []}
    account_rows = await inventory_monitors.database.read(
        f"""
        SELECT {_INVENTORY_ACCOUNT_COLUMNS}
        FROM inventory_monitor_accounts AS a
        WHERE EXISTS (
            SELECT 1 FROM inventory_monitor_subscriptions AS s
            WHERE s.steam_id = a.steam_id AND s.owner_user_id = ?
        )
        ORDER BY nickname LIMIT 100
        """,
        (owner,),
    )
    subscription_rows = await inventory_monitors.database.read(
        """
        SELECT s.owner_user_id, s.destination_type, s.destination_id, s.steam_id,
               a.nickname, s.created_at
        FROM inventory_monitor_subscriptions s
        JOIN inventory_monitor_accounts a ON a.steam_id = s.steam_id
        WHERE s.owner_user_id = ?
        ORDER BY s.created_at DESC LIMIT 200
        """,
        (owner,),
    )
    event_rows = await inventory_monitors.database.read(
        """
        SELECT e.id, e.steam_id, a.nickname, substr(e.message_text, 1, 300) AS summary,
               e.created_at
        FROM inventory_monitor_events e
        JOIN inventory_monitor_accounts a ON a.steam_id = e.steam_id
        WHERE EXISTS (
            SELECT 1 FROM inventory_monitor_subscriptions AS s
            WHERE s.steam_id = e.steam_id AND s.owner_user_id = ?
        )
        ORDER BY e.created_at DESC, e.id DESC LIMIT ?
        """,
        (owner, event_limit),
    )
    await _audit("desktop.inventory", "/api/desktop/inventory/state", "state")
    return {
        "accounts": [dict(row) for row in account_rows],
        "subscriptions": [dict(row) for row in subscription_rows],
        "events": [dict(row) for row in event_rows],
    }


@router.get("/inventory/trajectory", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_inventory_trajectory(
    steam_id: Annotated[str, Query(min_length=15, max_length=64)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """囤货雷达轨迹摘要（P0）：复用 QQ 同源服务层（owner 订阅校验在其内部）。"""

    owner = resolve_desktop_owner()
    if owner is None:
        await _audit("desktop.inventory", "/api/desktop/inventory/trajectory", "no-owner")
        return {"ok_text": _owner_unavailable_text("无法查询轨迹")}
    report_text = await inventory_monitor_service.trajectory(
        owner,
        "private",
        owner,
        steam_id.strip(),
        days=days,
    )
    await _audit("desktop.inventory", "/api/desktop/inventory/trajectory", "trajectory")
    return {"ok_text": report_text}


@router.get("/inventory/profile", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_inventory_profile(
    steam_id: Annotated[str, Query(min_length=15, max_length=64)],
) -> dict[str, Any]:
    """囤货画像只读入口（P1）：不烧 token——仅命中 6 小时缓存时返回，否则引导去 QQ。"""

    from src.services.hoard_report import load_recent_report
    from src.services.inventory_monitor import parse_inventory_identifier

    owner = resolve_desktop_owner()
    if owner is None:
        await _audit("desktop.inventory", "/api/desktop/inventory/profile", "no-owner")
        return {"cached": False, "ok_text": _owner_unavailable_text("无法读取画像缓存")}
    identifier = parse_inventory_identifier(steam_id.strip())
    if identifier is None:
        return {"cached": False, "ok_text": "SteamID 格式不正确（需 17 位 Steam64）。"}
    # Privacy: same owner-subscription gate as trajectory.
    if not await inventory_monitor_service.repository.has_subscription(
        owner, "private", owner, identifier.steam_id
    ):
        return {
            "cached": False,
            "ok_text": "当前未监控该账号；请先在 QQ 内发送「库存监控 SteamID」。",
        }
    now = int(time.time())
    cached = await load_recent_report(
        inventory_monitor_service.repository, identifier.steam_id, now=now
    )
    if cached is None:
        await _audit("desktop.inventory", "/api/desktop/inventory/profile", "cache-miss")
        return {
            "cached": False,
            "ok_text": "暂无缓存画像。请在 QQ 内发送「囤货画像 "
            + identifier.steam_id
            + "」生成（面板不直接调用模型，避免重复烧 token）。",
        }
    await _audit("desktop.inventory", "/api/desktop/inventory/profile", "cache-hit")
    generated_at = cast(int, cached["generated_at"])
    generated_label = time.strftime("%Y-%m-%d %H:%M", time.localtime(generated_at))
    return {
        "cached": True,
        "ok_text": str(cached["payload_json"]),
        "model": str(cached["model"]),
        "generated_at": generated_at,
        "generated_label": generated_label,
    }


# ─── News ───────────────────────────────────────────────


def _verified_list(result: Any) -> list[dict[str, Any]] | None:
    if result is None or not getattr(result, "is_available", False):
        return None
    value = getattr(result, "value", None)
    if not value:
        return []
    items: list[dict[str, Any]] = []
    for entry in value:
        published = getattr(entry, "published_at", None) or getattr(entry, "begins_at", None)
        items.append(
            {
                "title": getattr(entry, "title", None),
                "summary": getattr(entry, "summary", None),
                "url": getattr(entry, "url", None),
                "league": getattr(entry, "league", None),
                "team_a": getattr(entry, "team_a", None),
                "team_b": getattr(entry, "team_b", None),
                "score_a": getattr(entry, "score_a", None),
                "score_b": getattr(entry, "score_b", None),
                "time": (
                    published.isoformat()
                    if published is not None and hasattr(published, "isoformat")
                    else None
                ),
            }
        )
    return items


@router.get("/news/official", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_news_official() -> dict[str, Any]:
    from src.services.official_cs2 import format_official_report, official_cs2

    report = await official_cs2.report(include_updates=True, include_matches=True)
    await _audit("desktop.news", "/api/desktop/news/official", "official")
    return {
        "text": format_official_report(report),
        "updates": _verified_list(report.updates),
        "upcoming": _verified_list(report.upcoming),
        "completed": _verified_list(report.completed),
    }


_REDDIT_LIST_CAP = 20


def _reddit_payload(posts: Sequence[Any], *, unread_only: bool) -> list[dict[str, Any]]:
    return [
        {
            "post_id": post.post_id,
            "subreddit": post.subreddit,
            "title": post.title,
            "summary": post.summary,
            "author": post.author,
            "url": post.url,
            "published_at": post.published_at,
            "unread": unread_only,
        }
        for post in posts[:_REDDIT_LIST_CAP]
    ]


@router.get("/news/reddit", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_news_reddit(
    unread: Annotated[int, Query(ge=0, le=1)] = 0,
) -> dict[str, Any]:
    if unread:
        posts = await reddit_posts.undelivered_posts(_REDDIT_LIST_CAP)
        items = _reddit_payload(posts, unread_only=True)
    else:
        rows = await reddit_posts.database.read(
            """
            SELECT post_id, subreddit, title, summary, author, url, published_at
            FROM reddit_posts WHERE delivered_at IS NOT NULL
            ORDER BY delivered_at DESC LIMIT ?
            """,
            (_REDDIT_LIST_CAP,),
        )
        items = [
            {
                "post_id": str(row["post_id"]),
                "subreddit": str(row["subreddit"]),
                "title": str(row["title"]),
                "summary": str(row["summary"] or ""),
                "author": str(row["author"] or ""),
                "url": str(row["url"]),
                "published_at": int(row["published_at"]),
                "unread": False,
            }
            for row in rows
        ]
    await _audit("desktop.news", "/api/desktop/news/reddit", f"unread={unread}")
    return {"posts": items, "count": len(items), "unread_only": bool(unread)}


@router.post("/news/reddit/read", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_news_reddit_read(
    post_ids: Annotated[list[str], Body(embed=True, min_length=1, max_length=_REDDIT_LIST_CAP)],
) -> dict[str, Any]:
    marked = await reddit_posts.mark_delivered([str(pid) for pid in post_ids])
    await _audit(
        "desktop.news",
        "/api/desktop/news/reddit/read",
        f"mark delivered ids={len(post_ids)} marked={marked}",
    )
    return {"requested": len(post_ids), "marked": marked}


# ─── Persona ────────────────────────────────────────────


def _scope_label(group_id: int) -> str:
    return "私聊" if group_id == 0 else f"群 {group_id}"


@router.get("/persona/profile")
@_envelope
async def desktop_persona_profile() -> dict[str, Any]:
    owner = require_desktop_owner()
    scope = _persona_scope(owner)
    profile = await persona_styles.get(scope)
    memories = await persona_memories.list(scope)
    style = profile.style
    await _audit("desktop.persona", "/api/desktop/persona/profile", "profile")
    return {
        "owner_user_id": owner,
        "scope": scope.label,
        "learning_enabled": profile.learning_enabled,
        "updated_at": profile.updated_at,
        "style": {
            "concise": style.concise,
            "warm": style.warm,
            "teasing": style.teasing,
            "proactive": style.proactive,
            "catchphrase": style.catchphrase,
            "direct": style.direct,
        },
        "nickname": next((m.content for m in memories if m.kind == "nickname"), None),
        "memories": [
            {
                "id": memory.id,
                "kind": memory.kind,
                "content": memory.content,
                "created_at": memory.created_at,
                "updated_at": memory.updated_at,
            }
            for memory in memories
            if memory.kind == "note"
        ],
    }


_STYLE_ACTIONS = ("learning_on", "learning_off", "undo", "reset")


@router.post("/persona/style", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_persona_style(
    action: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    owner = require_desktop_owner()
    scope = _persona_scope(owner)
    if action not in _STYLE_ACTIONS:
        raise InvalidParameterError("action 只支持 learning_on / learning_off / undo / reset。")
    undone = False
    if action == "learning_on":
        await persona_styles.set_learning(scope, True)
    elif action == "learning_off":
        await persona_styles.set_learning(scope, False)
    elif action == "undo":
        applied = await persona_styles.undo(scope)
        undone = applied is not None
        if not undone:
            raise ConflictError("没有可撤销的风格调整记录。")
    else:
        await persona_styles.reset(scope)
    profile = await persona_styles.get(scope)
    await _audit("desktop.persona", "/api/desktop/persona/style", f"action={action}")
    return {
        "action": action,
        "undone": undone,
        "learning_enabled": profile.learning_enabled,
        "style": {
            "concise": profile.style.concise,
            "warm": profile.style.warm,
            "teasing": profile.style.teasing,
            "proactive": profile.style.proactive,
            "catchphrase": profile.style.catchphrase,
            "direct": profile.style.direct,
        },
    }


_MEMORY_KINDS = ("note", "nickname")


@router.post("/persona/memory", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_persona_memory_create(
    content: Annotated[str, Body(embed=True, min_length=1, max_length=200)],
    kind: Annotated[str, Body(embed=True)] = "note",
) -> dict[str, Any]:
    owner = require_desktop_owner()
    scope = _persona_scope(owner)
    if kind not in _MEMORY_KINDS:
        raise InvalidParameterError("记忆类型只能是 note 或 nickname。")
    try:
        if kind == "nickname":
            memory = await persona_memories.set_nickname(scope, content)
        else:
            memory, _created = await persona_memories.remember_note(scope, content)
    except PersonaMemoryCapacityError as exc:
        raise CapacityError("当前范围的长期记忆已满（20 条），请先删除部分记忆。") from exc
    except ValueError as exc:
        raise InvalidParameterError("记忆内容不能为空或超出长度限制。") from exc
    await _audit("desktop.persona", "/api/desktop/persona/memory", f"{kind}: {content}")
    return {
        "id": memory.id,
        "kind": memory.kind,
        "content": memory.content,
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
    }


@router.delete("/persona/memory", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_persona_memory_delete(
    index: Annotated[int, Query(ge=1, le=20)],
) -> dict[str, Any]:
    owner = require_desktop_owner()
    scope = _persona_scope(owner)
    try:
        memory = await persona_memories.delete_at(scope, index)
    except ValueError as exc:
        raise InvalidParameterError("记忆序号必须是正整数。") from exc
    if memory is None:
        raise NotFoundError("该序号没有对应记忆，列表可能已更新。")
    await _audit("desktop.persona", "/api/desktop/persona/memory", f"delete #{index}")
    return {"deleted": memory.content, "index": index}


# ─── Analytics ──────────────────────────────────────────

_DAY_OFFSET = "+8 hours"


@router.get("/analytics/usage")
@_envelope
async def desktop_analytics_usage(
    days: Annotated[int, Query(ge=7, le=90)] = 30,
) -> dict[str, Any]:
    cutoff = int(time.time()) - days * 86400
    daily_rows = await usage_log.database.read(
        """
        SELECT date(created_at, 'unixepoch', ?) AS day, feature, COUNT(*) AS calls
        FROM usage_log WHERE created_at >= ?
        GROUP BY day, feature ORDER BY day, feature
        """,
        (_DAY_OFFSET, cutoff),
    )
    top_rows = await usage_log.database.read(
        """
        SELECT user_id, COUNT(*) AS calls FROM usage_log WHERE created_at >= ?
        GROUP BY user_id ORDER BY calls DESC LIMIT 10
        """,
        (cutoff,),
    )
    total_rows = await usage_log.database.read(
        "SELECT COUNT(*) AS total FROM usage_log WHERE created_at >= ?",
        (cutoff,),
    )
    await _audit("desktop.analytics", "/api/desktop/analytics/usage", f"days={days}")
    return {
        "days": days,
        "daily": [dict(row) for row in daily_rows],
        "top_users": [dict(row) for row in top_rows],
        "total": int(total_rows[0]["total"]) if total_rows else 0,
    }


@router.get("/analytics/alerts")
@_envelope
async def desktop_analytics_alerts(
    days: Annotated[int, Query(ge=7, le=90)] = 30,
) -> dict[str, Any]:
    cutoff = int(time.time()) - days * 86400
    status_rows = await usage_log.database.read(
        "SELECT status, COUNT(*) AS count FROM alerts GROUP BY status"
    )
    delivered_rows = await usage_log.database.read(
        """
        SELECT date(delivered_at, 'unixepoch', ?) AS day, COUNT(*) AS count
        FROM alerts WHERE delivered_at IS NOT NULL AND delivered_at >= ?
        GROUP BY day ORDER BY day
        """,
        (_DAY_OFFSET, cutoff),
    )
    failed_rows = await usage_log.database.read(
        """
        SELECT date(failed_at, 'unixepoch', ?) AS day, COUNT(*) AS count
        FROM alerts WHERE failed_at IS NOT NULL AND failed_at >= ?
        GROUP BY day ORDER BY day
        """,
        (_DAY_OFFSET, cutoff),
    )
    await _audit("desktop.analytics", "/api/desktop/analytics/alerts", f"days={days}")
    return {
        "days": days,
        "by_status": [dict(row) for row in status_rows],
        "delivered_daily": [dict(row) for row in delivered_rows],
        "failed_daily": [dict(row) for row in failed_rows],
    }


# ─── System ─────────────────────────────────────────────


def _bot_version() -> str:
    try:
        return package_version("qq-bot")
    except PackageNotFoundError:
        return "unknown"


@router.get("/system/summary")
@_envelope
async def desktop_system_summary() -> dict[str, Any]:
    from src.services.resilience import circuit_snapshots

    health = await _health_snapshot()
    scheduler_config = await schedules.get()
    csqaq_breakers = {
        snapshot.name: {"state": snapshot.state, "recent_failures": snapshot.recent_failures}
        for snapshot in await circuit_snapshots()
        if snapshot.name.startswith("provider.csqaq")
    }
    await _audit("desktop.system", "/api/desktop/system/summary", "summary")
    # Was ``desktop_owner_configured``: the name said "configured" while the value
    # answered "resolvable", and M-110 gave that a second cause.  Report the
    # decision's reason as well so a health reader can tell "nothing set" from
    # "several superusers, set DESKTOP_OWNER_QQ explicitly".
    owner, owner_reason = _resolve_owner_decision()
    return {
        "health": health,
        "scheduler": {
            **scheduler_config.as_dict(),
            "version": scheduler_config.version,
            "updated_at": scheduler_config.updated_at,
        },
        "version": _bot_version(),
        "uptime_seconds": int(health.get("uptime_seconds") or 0),
        "desktop_owner_resolvable": owner is not None,
        "desktop_owner_reason": owner_reason,
        "csqaq_circuits": csqaq_breakers,
    }


# ─── User Center（批次B：按人看使用情况 + 数据管理权）──────────────────

_USAGE_DAYS_MAX = 90


@router.get("/users", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_users(
    q: Annotated[str | None, Query(max_length=20)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    keyword = q.strip() if q else ""
    if keyword and not keyword.isdigit():
        raise InvalidParameterError("搜索关键字必须是纯数字 QQ 号。")
    since = int(time.time()) - 30 * 86400
    filter_sql = ""
    params: list[Any] = [since]
    if keyword:
        filter_sql = " AND CAST(user_id AS TEXT) LIKE ?"
        params.append(f"%{keyword}%")
    rows = await database.read(
        "SELECT user_id, MAX(created_at) AS last_active, COUNT(*) AS total "
        f"FROM usage_log WHERE created_at >= ?{filter_sql} "
        "GROUP BY user_id ORDER BY last_active DESC LIMIT ?",
        (*params, limit),
    )
    feature_rows = await database.read(
        "SELECT user_id, feature, COUNT(*) AS calls FROM usage_log "
        "WHERE created_at >= ? GROUP BY user_id, feature",
        (since,),
    )
    per_user: dict[int, list[dict[str, Any]]] = {}
    for row in feature_rows:
        uid = int(row["user_id"])
        per_user.setdefault(uid, []).append(
            {"feature": str(row["feature"]), "calls": int(row["calls"])}
        )
    users = [
        {
            "user_id": int(row["user_id"]),
            "last_active_at": int(row["last_active"]),
            "total_calls_30d": int(row["total"]),
            "features": sorted(
                per_user.get(int(row["user_id"]), []), key=lambda item: -item["calls"]
            ),
        }
        for row in rows
    ]
    await _audit("desktop.users", "/api/desktop/users", f"q={keyword or '-'} limit={limit}")
    return {"users": users, "count": len(users)}


async def _usage_count(user_id: int, since: int) -> int:
    rows = await database.read(
        "SELECT COUNT(*) AS c FROM usage_log WHERE user_id = ? AND created_at >= ?",
        (user_id, since),
    )
    return int(rows[0]["c"])


@router.get("/users/{user_id}/overview", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_user_overview(
    user_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    owner = require_desktop_owner()
    now = int(time.time())
    calls_7d = await _usage_count(user_id, now - 7 * 86400)
    calls_30d = await _usage_count(user_id, now - 30 * 86400)
    top_commands = [
        {"command": str(row["command"]), "calls": int(row["c"])}
        for row in await database.read(
            "SELECT command, COUNT(*) AS c FROM usage_log WHERE user_id = ? "
            "AND created_at >= ? GROUP BY command ORDER BY c DESC LIMIT 10",
            (user_id, now - 30 * 86400),
        )
    ]
    groups = [
        {"group_id": int(row["group_id"]), "calls": int(row["c"])}
        for row in await database.read(
            "SELECT group_id, COUNT(*) AS c FROM usage_log WHERE user_id = ? "
            "AND group_id > 0 AND created_at >= ? GROUP BY group_id ORDER BY c DESC LIMIT 5",
            (user_id, now - 30 * 86400),
        )
    ]
    alerts = await alert_repository.list_owner(user_id)
    alert_counts = {"active": 0, "delivered": 0, "failed": 0}
    for item in alerts:
        if item.status in {"pending", "claimed"}:
            alert_counts["active"] += 1
        elif item.status == "delivered":
            alert_counts["delivered"] += 1
        else:
            alert_counts["failed"] += 1
    watch_items = await watchlists.list_owner(user_id)
    scope = PersonaScope(owner_user_id=user_id, group_id=0)
    learning_enabled: bool | None = None
    style_values: dict[str, int] | None = None
    memory_count = 0
    try:
        profile = await persona_styles.get(scope)
        learning_enabled = profile.learning_enabled
        style_values = {
            "concise": profile.style.concise,
            "warm": profile.style.warm,
            "teasing": profile.style.teasing,
            "proactive": profile.style.proactive,
            "catchphrase": profile.style.catchphrase,
            "direct": profile.style.direct,
        }
    except Exception as exc:
        _log.warning("Desktop persona overview failed error=%s", type(exc).__name__)
    try:
        memory_count = len(await persona_memories.list(scope))
    except Exception as exc:
        _log.warning("Desktop memory overview failed error=%s", type(exc).__name__)
    trail = [
        {
            "command": str(row["command"]),
            "detail": str(row["input_text"]),
            "created_at": int(row["created_at"]),
        }
        for row in await database.read(
            "SELECT command, input_text, created_at FROM usage_log "
            "WHERE feature = 'desktop.moderation' AND input_text LIKE ? "
            "ORDER BY id DESC LIMIT 10",
            (f"%user={user_id}%",),
        )
    ]
    await _audit("desktop.users", f"/api/desktop/users/{user_id}/overview", f"user={user_id}")
    return {
        "user_id": user_id,
        "is_owner": user_id == owner,
        "calls_7d": calls_7d,
        "calls_30d": calls_30d,
        "top_commands": top_commands,
        "groups": groups,
        "alerts": alert_counts,
        "watch_count": len(watch_items),
        "persona": {
            "learning_enabled": learning_enabled,
            "style": style_values,
            "memory_count": memory_count,
        },
        "moderation_trail": trail,
    }


@router.get("/users/{user_id}/timeline", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_user_timeline(
    user_id: Annotated[int, Path(ge=1)],
    days: Annotated[int, Query(ge=1, le=_USAGE_DAYS_MAX)] = 7,
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    since = int(time.time()) - days * 86400
    where = "user_id = ? AND created_at >= ?"
    params: list[Any] = [user_id, since]
    if cursor:
        where += " AND id < ?"
        params.append(cursor)
    rows = await database.read(
        f"SELECT id, feature, command, group_id, created_at FROM usage_log "
        f"WHERE {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    )
    events = [
        {
            "id": int(row["id"]),
            "feature": str(row["feature"]),
            "command": str(row["command"]),
            "group_id": int(row["group_id"] or 0),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]
    next_cursor = int(rows[-1]["id"]) if len(rows) == limit else 0
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/timeline",
        f"user={user_id} rows={len(events)}",
    )
    return {"events": events, "next_cursor": next_cursor, "cursor": cursor}


@router.get("/users/{user_id}/chat-log", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_user_chat_log(
    user_id: Annotated[int, Path(ge=1)],
    scope: Annotated[str, Query(pattern="^(group|private)$")] = "group",
    days: Annotated[int, Query(ge=1, le=30)] = 3,
    confirm_view_private: Annotated[int, Query(ge=0, le=1)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
) -> dict[str, Any]:
    owner = require_desktop_owner()
    privacy_notice: str | None = None
    since = int(time.time()) - days * 86400
    if scope == "private":
        if user_id != owner and not confirm_view_private:
            raise ForbiddenError("查看他人私聊需要二次确认（confirm_view_private=1）。")
        if user_id != owner:
            privacy_notice = "⚠️ 你正在查看他人私聊内容，仅自己可见。"
        sql = (
            "SELECT session_key, role, content, created_at FROM session_messages "
            "WHERE session_key = ? AND created_at >= ? ORDER BY id DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (f"p_{user_id}", since, limit)
    else:
        sql = (
            "SELECT session_key, role, content, created_at FROM session_messages "
            "WHERE session_key LIKE ? ESCAPE '\\' AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?"
        )
        params = (f"g\\_%\\_{user_id}", since, limit)
    rows = await database.read(sql, params)
    messages = [
        {
            "session_key": str(row["session_key"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "created_at": int(row["created_at"]),
        }
        for row in rows
    ]
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/chat-log",
        f"user={user_id} scope={scope} rows={len(messages)}",
    )
    return {
        "messages": messages,
        "count": len(messages),
        "scope": scope,
        "privacy_notice": privacy_notice,
    }


@router.post("/users/{user_id}/clear-session", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_user_clear_session(
    user_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    rows = await database.read(
        "SELECT DISTINCT session_key FROM session_messages "
        "WHERE session_key = ? OR session_key LIKE ? ESCAPE '\\'",
        (f"p_{user_id}", f"g\\_%\\_{user_id}"),
    )
    cleared = 0
    for row in rows:
        key = str(row["session_key"])
        async with sessions.session_guard(key, 15, wait_timeout=5):
            if await sessions.delete(key):
                cleared += 1
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/clear-session",
        f"user={user_id} cleared={cleared}",
    )
    return {"cleared_sessions": cleared}


@router.post("/users/{user_id}/clear-memory", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_user_clear_memory(
    user_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    scope = PersonaScope(owner_user_id=user_id, group_id=0)
    deleted = await persona_memories.clear(scope)
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/clear-memory",
        f"user={user_id} deleted={deleted}",
    )
    return {"deleted": deleted}


@router.post("/users/{user_id}/cancel-alerts", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_user_cancel_alerts(
    user_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    alerts = await alert_repository.list_owner(user_id)
    cancelled = 0
    for item in alerts:
        if item.status not in {"pending", "claimed"}:
            continue
        try:
            await alert_repository.admin_delete(item.id)
            cancelled += 1
        except Exception as exc:
            _log.warning("Desktop cancel alert failed error=%s", type(exc).__name__)
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/cancel-alerts",
        f"user={user_id} cancelled={cancelled}",
    )
    return {"cancelled": cancelled}


@router.post("/users/{user_id}/clear-watchlist", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_user_clear_watchlist(
    user_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    items = await watchlists.list_owner(user_id)
    removed = 0
    for index in reversed(range(len(items))):
        if await watchlists.remove_at(user_id, index):
            removed += 1
    await _audit(
        "desktop.users",
        f"/api/desktop/users/{user_id}/clear-watchlist",
        f"user={user_id} removed={removed}",
    )
    return {"removed": removed}


# ─── Moderation（批次C：群务管控，OneBot 实权）──────────────────────────

_MUTE_MAX_MINUTES = 30 * 24 * 60


def _moderation_bot() -> Any:
    from src.plugins.scheduler import connected_bot

    bot = connected_bot()
    if bot is None:
        raise UpstreamUnavailableError("塔菲未连接 QQ（OneBot 不在线），群务操作不可用。")
    return bot


def _translate_action_failure(exc: Exception) -> UpstreamUnavailableError:
    """Map an OneBot action failure to a Chinese, non-leaking user message."""

    info = getattr(exc, "info", None)
    retcode: int | None = None
    wording = ""
    if isinstance(info, dict):
        raw_code = info.get("retcode")
        if isinstance(raw_code, int) and not isinstance(raw_code, bool):
            retcode = raw_code
        wording = str(info.get("wording") or info.get("msg") or "").strip()
    lowered = wording.casefold()
    if any(marker in lowered for marker in ("privilege", "admin", "权限", "管理员", "群主")):
        return UpstreamUnavailableError("塔菲在该群没有管理权限，请先将塔菲设为群管理员再试。")
    if retcode is not None:
        suffix = f"：{wording[:60]}" if wording else "。"
        return UpstreamUnavailableError(f"OneBot 操作失败（retcode={retcode}）{suffix}")
    return UpstreamUnavailableError(f"OneBot 操作失败（{type(exc).__name__}）。")


@router.get("/moderation/groups", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_moderation_groups() -> dict[str, Any]:
    bot = _moderation_bot()
    try:
        groups = await bot.get_group_list()
    except Exception as exc:
        raise _translate_action_failure(exc) from exc
    entries = [
        {"group_id": int(item["group_id"]), "group_name": str(item.get("group_name") or "")}
        for item in groups or []
        if isinstance(item, dict) and item.get("group_id") is not None
    ]
    await _audit("desktop.moderation", "/api/desktop/moderation/groups", f"count={len(entries)}")
    return {"groups": entries, "count": len(entries)}


@router.get("/moderation/groups/{group_id}/members", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_moderation_members(
    group_id: Annotated[int, Path(ge=1)],
) -> dict[str, Any]:
    bot = _moderation_bot()
    try:
        members = await bot.get_group_member_list(group_id=group_id)
    except Exception as exc:
        raise _translate_action_failure(exc) from exc
    entries = [
        {
            "user_id": int(item["user_id"]),
            "nickname": str(item.get("card") or item.get("nickname") or ""),
        }
        for item in members or []
        if isinstance(item, dict) and item.get("user_id") is not None
    ][:200]
    await _audit(
        "desktop.moderation",
        f"/api/desktop/moderation/groups/{group_id}/members",
        f"group={group_id} count={len(entries)}",
    )
    return {"members": entries, "count": len(entries)}


@router.post("/moderation/mute", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_moderation_mute(
    group_id: Annotated[int, Body(ge=1, embed=True)],
    user_id: Annotated[int, Body(ge=1, embed=True)],
    minutes: Annotated[int, Body(ge=0, le=_MUTE_MAX_MINUTES, embed=True)],
) -> dict[str, Any]:
    bot = _moderation_bot()
    try:
        await bot.set_group_ban(group_id=group_id, user_id=user_id, duration=minutes * 60)
    except Exception as exc:
        raise _translate_action_failure(exc) from exc
    await _audit(
        "desktop.moderation",
        "/api/desktop/moderation/mute",
        f"group={group_id} user={user_id} minutes={minutes}",
    )
    return {"result": "ok", "group_id": group_id, "user_id": user_id, "minutes": minutes}


@router.post("/moderation/kick", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_moderation_kick(
    group_id: Annotated[int, Body(ge=1, embed=True)],
    user_id: Annotated[int, Body(ge=1, embed=True)],
    reject_add_request: Annotated[bool, Body(embed=True)] = False,
) -> dict[str, Any]:
    bot = _moderation_bot()
    try:
        await bot.set_group_kick(
            group_id=group_id, user_id=user_id, reject_add_request=reject_add_request
        )
    except Exception as exc:
        raise _translate_action_failure(exc) from exc
    await _audit(
        "desktop.moderation",
        "/api/desktop/moderation/kick",
        f"group={group_id} user={user_id} reject={reject_add_request}",
    )
    return {"result": "ok", "group_id": group_id, "user_id": user_id}


@router.post("/moderation/recall", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_moderation_recall(
    message_id: Annotated[int, Body(embed=True)],
    user_id: Annotated[int | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    bot = _moderation_bot()
    try:
        await bot.delete_msg(message_id=message_id)
    except Exception as exc:
        raise _translate_action_failure(exc) from exc
    await _audit(
        "desktop.moderation",
        "/api/desktop/moderation/recall",
        f"message_id={message_id} user={user_id or '-'}",
    )
    return {"result": "ok", "message_id": message_id}


# ─── V0.4 P5：CSQAQ 行情 / 话术词典 / 亲密度 / 表情包库 ──────────────────

_SLANG_TERM_MAX = 24
_SLANG_MEANING_MAX = 200
_STICKER_NAME_RE = re.compile(r"\A[A-Za-z0-9_\-]{1,64}\.(?:png|jpg|jpeg|webp|gif|bmp)\Z")
_STICKER_MAX_BYTES = 8 * 1024 * 1024


def _resolve_item_name(item: str) -> str:
    """Resolve one user query to a market hash name; shared by market reads."""

    if not item.strip():
        raise InvalidParameterError("请提供饰品名称。")
    return item.strip()


@router.get("/market/compare", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_compare(
    item: Annotated[str, Query(max_length=120)],
    query: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    name = await resolve_name(item) or item.strip()
    await _audit("desktop.market", "/api/desktop/market/compare", f"item={item[:60]}")
    return await build_compare_payload(query or item, name)


@router.get("/market/score", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_score(
    item: Annotated[str, Query(max_length=120)],
    query: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    name = await resolve_name(item) or item.strip()
    payload = await build_trade_score_payload(name, user_query=query or item)
    await _audit("desktop.market", "/api/desktop/market/score", f"item={item[:60]}")
    if payload is None:
        return {"scored": False, "reason": "样本不足（<24 个日线），暂不评分"}
    return {"scored": True, **payload}


@router.get("/market/case-ev", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_case_ev(
    case: Annotated[str, Query(max_length=120)],
) -> dict[str, Any]:
    await _audit("desktop.market", "/api/desktop/market/case-ev", f"case={case[:60]}")
    payload = await build_case_ev_payload(case)
    if payload is None:
        return {"computed": False, "reason": "元数据未同步或无法唯一匹配该箱子"}
    return {"computed": True, **payload}


@router.get("/market/hoard", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_market_hoard(
    item: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    """挂刀概览（无 item）或单品挂刀参考（有 item）——薄门面，同 QQ 门槛。

    数据源为本地挂刀快照（iflow.work priority_archive 转储）；快照缺失
    或单品未被跟踪时如实返回 available=false，绝不编造比例。
    """

    from src.services.hoard_index import hoard_index_store

    await _audit("desktop.market", "/api/desktop/market/hoard", f"item={(item or '')[:60]}")
    if item:
        ratio = await hoard_index_store.item_ratio(item.strip())
        if ratio is None:
            return {"available": False, "reason": "该饰品未被挂刀快照跟踪"}
        return {"available": True, "item": item.strip(), "hoard_ratio": round(ratio, 4)}
    snapshot = await hoard_index_store.latest_snapshot()
    if snapshot is None or snapshot.median_ratio is None:
        return {"available": False, "reason": "挂刀快照尚未同步"}
    return {
        "available": True,
        "fetched_at": snapshot.fetched_at,
        "dump_name": snapshot.dump_name,
        "dump_time": snapshot.dump_time,
        "item_count": snapshot.item_count,
        "cs2_count": snapshot.cs2_count,
        "median_ratio": round(snapshot.median_ratio, 4),
        "p25_ratio": round(snapshot.p25_ratio, 4) if snapshot.p25_ratio is not None else None,
        "p75_ratio": round(snapshot.p75_ratio, 4) if snapshot.p75_ratio is not None else None,
    }


@router.get("/persona/slang", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_slang_list(
    group_id: Annotated[int, Query(ge=0, le=100_000_000)] = 0,
) -> dict[str, Any]:
    pending = await persona_slang.pending(group_id)
    accepted = await persona_slang.accepted_for_injection(group_id, limit=50)
    await _audit("desktop.persona", "/api/desktop/persona/slang", f"list group={group_id}")
    return {
        "group_id": group_id,
        "pending": [entry.__dict__ for entry in pending],
        "accepted": [entry.__dict__ for entry in accepted],
    }


@router.post("/persona/slang", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_slang_write(
    action: Annotated[str, Body(embed=True)],
    group_id: Annotated[int, Body(embed=True)] = 0,
    term: Annotated[str | None, Body(embed=True)] = None,
    meaning: Annotated[str | None, Body(embed=True)] = None,
    entry_id: Annotated[int | None, Body(embed=True)] = None,
    status: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    owner = resolve_desktop_owner()
    if action == "teach":
        if not term or not meaning:
            raise InvalidParameterError("teach 需要 term 与 meaning。")
        if len(term) > _SLANG_TERM_MAX or len(meaning) > _SLANG_MEANING_MAX:
            raise InvalidParameterError("词条或含义长度超限。")
        if owner is None:
            # Previously ``created_by=owner or 1`` silently attributed the entry to
            # the magic id 1.  Attribution is the whole reason this path records
            # ``created_by``; refuse instead of writing a wrong account.
            raise OwnerNotConfiguredError(
                "教新词条需要可解析的桌面主人（用于记录 created_by）；"
                f"当前{_owner_unavailable_text('无法记录归属')}"
            )
        created, is_new = await persona_slang.teach(
            group_id, created_by=owner, term=term, meaning=meaning
        )
        await _audit("desktop.persona", "/api/desktop/persona/slang", f"teach group={group_id}")
        return {"result": "ok", "created": is_new, "entry": created.__dict__}
    if action == "decide":
        if status not in {"accepted", "rejected"} or entry_id is None:
            raise InvalidParameterError("decide 需要 id 与 accepted/rejected 状态。")
        decided = await persona_slang.decide(
            entry_id, scope_group_id=group_id, created_by=owner, status=status
        )
        await _audit(
            "desktop.persona",
            "/api/desktop/persona/slang",
            f"decide id={entry_id} status={status}",
        )
        if decided is None:
            raise NotFoundError("条目不存在或已处理。")
        return {"result": "ok", "entry": decided.__dict__}
    raise InvalidParameterError("action 必须是 teach 或 decide。")


@router.delete("/persona/slang", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_slang_delete(
    entry_id: Annotated[int, Query(alias="id", ge=1)],
    group_id: Annotated[int, Query(ge=0, le=100_000_000)] = 0,
) -> dict[str, Any]:
    # ``group_id`` is a **request-side** addition (response shape unchanged): the
    # repository now pins every id-keyed mutation to a scope, and the private
    # bucket (0) is shared across users, so it also needs the ownership predicate.
    owner = resolve_desktop_owner()
    if group_id == 0 and owner is None:
        raise OwnerNotConfiguredError(
            f"删除私聊词条需要可解析的桌面主人（归属判据）；当前{_owner_unavailable_text('无法核对归属')}"
        )
    removed = await persona_slang.delete(entry_id, scope_group_id=group_id, created_by=owner)
    await _audit("desktop.persona", "/api/desktop/persona/slang", f"delete id={entry_id}")
    if removed is None:
        raise NotFoundError("条目不存在。")
    return {"result": "ok", "entry": removed.__dict__}


@router.get("/persona/relationship", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_relationship(
    user_id: Annotated[int, Query(ge=1, le=100_000_000_000)],
    group_id: Annotated[int, Query(ge=0, le=100_000_000)] = 0,
) -> dict[str, Any]:
    relationship = await persona_relationships.refresh(user_id, group_id)
    await _audit(
        "desktop.persona",
        "/api/desktop/persona/relationship",
        f"user={user_id} group={group_id}",
    )
    return {
        "user_id": relationship.user_id,
        "scope_group_id": relationship.scope_group_id,
        "intimacy": relationship.intimacy,
        "band": relationship.band,
        "interaction_count": relationship.interaction_count,
        "accepted_insight_count": relationship.accepted_insight_count,
        "style_feedback_count": relationship.style_feedback_count,
        "last_interaction_at": relationship.last_interaction_at,
    }


@router.get("/stickers", dependencies=[Depends(limit_market_reads)])
@_envelope
async def desktop_stickers_list() -> dict[str, Any]:
    from src.services import stickers as stickers_service

    manifest = stickers_service._load_manifest()
    await _audit("desktop.stickers", "/api/desktop/stickers", "list")
    return {
        "enabled": stickers_service.stickers_enabled(),
        "stickers": [
            {"file": entry.path.name, "tags": sorted(entry.tags), "desc": entry.description}
            for entry in manifest.entries
        ],
    }


@router.post("/stickers", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_stickers_upload(
    file_name: Annotated[str, Body(embed=True)],
    tags: Annotated[list[str], Body(embed=True)],
    data_base64: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    import base64
    import binascii

    from src.services import stickers as stickers_service

    clean_name = file_name.strip().casefold()
    if not _STICKER_NAME_RE.match(clean_name):
        raise InvalidParameterError("文件名仅允许字母/数字/下划线与受支持的图片扩展名。")
    try:
        payload = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidParameterError("图片内容必须是有效的 base64。") from exc
    if not payload or len(payload) > _STICKER_MAX_BYTES:
        raise InvalidParameterError("图片为空或超过 8MB 上限。")
    clean_tags = [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]
    if not clean_tags:
        raise InvalidParameterError("至少提供一个标签。")
    directory = PROJECT_ROOT / "data" / "stickers"
    directory.mkdir(parents=True, exist_ok=True)
    target = (directory / clean_name).resolve()
    if target.parent != directory.resolve():
        raise InvalidParameterError("路径非法。")
    target.write_bytes(payload)
    manifest_path = directory / "manifest.json"
    try:
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        )
    except (OSError, ValueError):
        manifest = {}
    items = [
        item
        for item in manifest.get("stickers", [])
        if isinstance(item, dict) and item.get("file") != clean_name
    ]
    items.append({"file": clean_name, "tags": clean_tags})
    manifest_path.write_text(
        json.dumps({"stickers": items}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stickers_service.reset_runtime_state()
    await _audit("desktop.stickers", "/api/desktop/stickers", f"upload {clean_name}")
    return {"result": "ok", "file": clean_name, "tags": clean_tags}


@router.delete("/stickers", dependencies=[Depends(limit_writes)])
@_envelope
async def desktop_stickers_delete(
    file_name: Annotated[str, Query(alias="file")],
) -> dict[str, Any]:
    from src.services import stickers as stickers_service

    clean_name = file_name.strip().casefold()
    if not _STICKER_NAME_RE.match(clean_name):
        raise InvalidParameterError("文件名非法。")
    directory = PROJECT_ROOT / "data" / "stickers"
    target = (directory / clean_name).resolve()
    if target.parent != directory.resolve() or not target.is_file():
        raise NotFoundError("文件不存在。")
    target.unlink()
    manifest_path = directory / "manifest.json"
    try:
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        )
    except (OSError, ValueError):
        manifest = {}
    manifest["stickers"] = [
        item
        for item in manifest.get("stickers", [])
        if isinstance(item, dict) and item.get("file") != clean_name
    ]
    manifest_path.write_text(
        json.dumps({"stickers": manifest["stickers"]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    stickers_service.reset_runtime_state()
    await _audit("desktop.stickers", "/api/desktop/stickers", f"delete {clean_name}")
    return {"result": "ok", "file": clean_name}


# ─── Register ───────────────────────────────────────────

_app = get_app()
_app.include_router(router)


__all__ = [
    "CapacityError",
    "ConflictError",
    "DesktopAPIError",
    "InvalidParameterError",
    "NotFoundError",
    "OwnerNotConfiguredError",
    "UpstreamUnavailableError",
    "desktop_api_enabled",
    "require_desktop_enabled",
    "resolve_desktop_owner",
    "router",
]

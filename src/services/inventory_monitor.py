"""Inventory snapshot parsing, comparison, and daily/manual monitor orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Protocol

from src.state.inventory_monitor_repository import (
    DestinationType,
    InventoryAccountClaim,
    InventoryHistoryTrajectoryRow,
    InventoryMonitorRepository,
    InventoryMonitorSubscription,
    SubscribeResult,
)

from .float_resolver import (
    ItemFloatAnchors,
    extract_inspect_url,
    normalize_paintwear,
    parse_sticker_labels,
    serialize_stickers,
)
from .steam_inventory import fetch_inventory_snapshot, parse_inventory_identifier
from .tz import configured_timezone

_MARKET_TZ = configured_timezone()

_MAX_SNAPSHOT_AGE_SECONDS = 24 * 60 * 60
_MAX_ITEMS = 20_000
_MAX_DETAILS = 20
_CLAIM_SECONDS = 90
#: Two wears within this distance are treated as the same physical finish.
_ANCHOR_WEAR_TOLERANCE = 0.0001
_FLOAT_RESOLVE_TIMEOUT_SECONDS = 12.0
_MAX_FLOAT_URLS_PER_ROUND = 8
_TRAJECTORY_DEFAULT_DAYS = 30
_TRAJECTORY_MAX_DAYS = 365
_TRAJECTORY_ROWS_LIMIT = 5_000
_TRAJECTORY_TIMELINE = 8
_TRAJECTORY_TOP_ROWS = 5

logger = logging.getLogger("taffy.services.inventory_monitor")

#: Fire-and-forget email side-channel tasks; kept alive only so the loop does
#: not garbage-collect them mid-flight.
_EMAIL_TASKS: set[asyncio.Task[bool]] = set()

SnapshotFetcher = Callable[[str], Awaitable[Mapping[str, Any]]]


class FloatResolver(Protocol):
    """Inspect-link resolution seam; see :mod:`float_resolver`."""

    async def resolve(self, urls: Sequence[str]) -> dict[str, ItemFloatAnchors]: ...


class InventoryMonitorError(RuntimeError):
    """A public inventory snapshot cannot safely advance the monitor baseline."""


@dataclass(frozen=True, slots=True)
class ItemAnchorRow:
    """Per-item unique-wear anchor columns for history persistence."""

    market_hash_name: str | None
    paint_index: int | None
    paintwear: float | None
    pattern_index: int | None
    stickers_json: str | None


#: Persisted anchor tuple: (market_hash_name, paint_index, paintwear,
#: pattern_index, stickers_json) keyed by item_key.
ItemAnchorValues = tuple[str | None, int | None, float | None, int | None, str | None]

#: History-row anchor tuple with the ``is_missing`` flag for reattachment.
HistoryAnchorValues = tuple[str | None, float | None, bool]


@dataclass(frozen=True, slots=True)
class MonitoredInventoryItem:
    item_key: str
    asset_id: str
    fingerprint: str
    name: str
    is_tradable: bool
    tradable_after: int | None
    price: float | None
    market_hash_name: str | None = None
    paint_index: int | None = None
    paintwear: float | None = None
    pattern_index: int | None = None
    stickers_json: str | None = None
    #: Transient only: used to enrich floats this round, never persisted or logged.
    inspect_url: str | None = None

    @property
    def anchors(self) -> ItemAnchorRow:
        return ItemAnchorRow(
            market_hash_name=self.market_hash_name,
            paint_index=self.paint_index,
            paintwear=self.paintwear,
            pattern_index=self.pattern_index,
            stickers_json=self.stickers_json,
        )

    def with_anchors(self, resolved: ItemFloatAnchors) -> MonitoredInventoryItem:
        return replace(
            self,
            market_hash_name=resolved.market_hash_name or self.market_hash_name or self.name,
            paintwear=resolved.paintwear if self.paintwear is None else self.paintwear,
            paint_index=resolved.paint_index if self.paint_index is None else self.paint_index,
            pattern_index=(
                resolved.pattern_index if self.pattern_index is None else self.pattern_index
            ),
            stickers_json=(
                serialize_stickers(resolved.stickers)
                if resolved.stickers and self.stickers_json is None
                else self.stickers_json
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "item_key": self.item_key,
            "asset_id": self.asset_id,
            "fingerprint": self.fingerprint,
            "name": self.name,
            "is_tradable": self.is_tradable,
            "tradable_after": self.tradable_after,
            "price": self.price,
            "market_hash_name": self.market_hash_name,
            "paint_index": self.paint_index,
            "paintwear": self.paintwear,
            "pattern_index": self.pattern_index,
            "stickers_json": self.stickers_json,
        }


@dataclass(frozen=True, slots=True)
class MonitoredInventorySnapshot:
    steam_id: str
    nickname: str
    source_time: int
    total_count: int
    returned_count: int
    items: tuple[MonitoredInventoryItem, ...]
    comparison_hash: str
    snapshot_json: str

    def item_map(self) -> dict[str, MonitoredInventoryItem]:
        return {item.item_key: item for item in self.items}

    def item_json_map(self) -> dict[str, str]:
        return {
            item.item_key: json.dumps(
                item.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in self.items
        }

    def anchor_map(self) -> dict[str, ItemAnchorRow]:
        return {item.item_key: item.anchors for item in self.items}

    def anchor_map_values(self) -> dict[str, ItemAnchorValues]:
        return {
            key: (
                row.market_hash_name,
                row.paint_index,
                row.paintwear,
                row.pattern_index,
                row.stickers_json,
            )
            for key, row in self.anchor_map().items()
        }

    def with_items(self, items: tuple[MonitoredInventoryItem, ...]) -> MonitoredInventorySnapshot:
        """Return a copy carrying ``items`` plus a freshly serialized baseline."""

        serialized = json.dumps(
            {
                "version": 1,
                "scope": "public_returned_assets",
                "steam_id": self.steam_id,
                "nickname": self.nickname,
                "source_time": self.source_time,
                "total_count": self.total_count,
                "returned_count": len(items),
                "items": [item.to_dict() for item in items],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return replace(self, items=items, snapshot_json=serialized)


@dataclass(frozen=True, slots=True)
class InventoryDifference:
    added: tuple[MonitoredInventoryItem, ...]
    reappeared: tuple[MonitoredInventoryItem, ...]
    removed: tuple[MonitoredInventoryItem, ...]
    state_changes: tuple[tuple[MonitoredInventoryItem, MonitoredInventoryItem], ...]

    @property
    def changed(self) -> bool:
        return bool(self.added or self.reappeared or self.removed or self.state_changes)


@dataclass(frozen=True, slots=True)
class DueCheckSummary:
    claimed: int
    completed: int
    changed: int
    failed: int


@dataclass(frozen=True, slots=True)
class _ClaimOutcome:
    """Structured result of one claimed check; replaces string-prefix control flow."""

    message: str
    changed: bool
    failed: bool


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _finite_price(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return round(parsed, 2)


def _safe_text(value: object, fallback: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return text[:maximum]


def _item_name(
    asset: Mapping[str, Any], classinfos: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    classinfo = classinfos.get(str(asset.get("classinfoKey", "")))
    if not isinstance(classinfo, Mapping):
        raise InventoryMonitorError("库存饰品缺少名称信息")
    name = _safe_text(
        classinfo.get("name") or classinfo.get("marketHashName") or classinfo.get("hashName"),
        "未知饰品",
        180,
    )
    return name, classinfo


def _item_key(asset_id: str, fingerprint: str) -> str:
    if fingerprint:
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return f"fp:{digest}"
    return f"asset:{asset_id}"


def anchor_fingerprint(
    market_hash_name: str | None, paintwear: float | None
) -> tuple[str, float] | None:
    """Secondary identity of one physical finish (name + exact wear).

    Kept deliberately separate from :func:`_item_key`: account-scoped tracking
    stays anchored on ``asset_id`` with zero error, while this pair reconnects
    a history row after an asset_id churn or across accounts later.
    """

    if market_hash_name is None or paintwear is None:
        return None
    return market_hash_name, round(paintwear, 6)


def anchor_matches(
    left_name: str | None,
    left_wear: float | None,
    right_name: str | None,
    right_wear: float | None,
    *,
    tolerance: float = _ANCHOR_WEAR_TOLERANCE,
) -> bool:
    """True when both names equal and wears sit within ``±tolerance``."""

    if not left_name or not right_name or left_name != right_name:
        return False
    if left_wear is None or right_wear is None:
        return False
    return abs(left_wear - right_wear) <= tolerance


def _first_optional_int(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> int | None:
    for source in sources:
        for key in keys:
            parsed = _nonnegative_int(source.get(key))
            if parsed is not None:
                return parsed
    return None


def _first_paintwear(sources: Sequence[Mapping[str, Any]]) -> float | None:
    for source in sources:
        for key in ("paintwear", "paint_wear"):
            parsed = normalize_paintwear(source.get(key))
            if parsed is not None:
                return parsed
    return None


def parse_inline_anchors(
    raw_asset: Mapping[str, Any],
    classinfo: Mapping[str, Any],
) -> ItemAnchorRow:
    """Best-effort anchors straight from the snapshot payload fields."""

    sources = (classinfo, raw_asset)
    stickers_raw = classinfo.get("stickers")
    if not isinstance(stickers_raw, Sequence) or isinstance(stickers_raw, (str, bytes)):
        stickers_raw = raw_asset.get("stickers")
    return ItemAnchorRow(
        market_hash_name=None,
        paintwear=_first_paintwear(sources),
        paint_index=_first_optional_int(sources, ("paint_index", "paintindex")),
        pattern_index=_first_optional_int(sources, ("pattern_index", "pattern")),
        stickers_json=(
            serialize_stickers(parse_sticker_labels(stickers_raw)) if stickers_raw else None
        ),
    )


def _comparison_hash(items: Sequence[MonitoredInventoryItem]) -> str:
    comparable = [
        (item.item_key, item.is_tradable)
        for item in sorted(items, key=lambda candidate: candidate.item_key)
    ]
    payload = json.dumps(comparable, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_monitor_snapshot(
    response: Mapping[str, Any],
    steam_id: str,
    *,
    now: float | None = None,
) -> MonitoredInventorySnapshot:
    """Validate one public snapshot and retain only stable comparison fields."""

    current = time.time() if now is None else now
    data = response.get("data") if response.get("success") else None
    if not isinstance(data, Mapping):
        raise InventoryMonitorError("库存数据暂不可用")
    account = data.get("steamAccount")
    inventory = data.get("inventory")
    if not isinstance(account, Mapping) or not isinstance(inventory, Mapping):
        raise InventoryMonitorError("库存数据格式无效")
    summary = inventory.get("summary")
    if not isinstance(summary, Mapping):
        raise InventoryMonitorError("库存数据缺少汇总信息")
    if summary.get("privacy") is True or summary.get("notPrivacy") is False:
        raise InventoryMonitorError("该账号库存为私密，无法监控")
    if summary.get("empty") is True:
        raise InventoryMonitorError("库存暂时显示为空，未更新监控基准")

    source_time = _nonnegative_int(summary.get("lastFetchTime"))
    if source_time is None or source_time > current + 5 * 60:
        raise InventoryMonitorError("库存数据时间异常")
    if current - source_time > _MAX_SNAPSHOT_AGE_SECONDS:
        raise InventoryMonitorError("库存数据已超过24小时，未更新监控基准")

    total = _nonnegative_int(summary.get("total"))
    assets = inventory.get("assets")
    classinfos = inventory.get("classinfos")
    if (
        total is None
        or not isinstance(assets, Sequence)
        or isinstance(assets, (str, bytes))
        or not isinstance(classinfos, Mapping)
        or len(assets) > _MAX_ITEMS
    ):
        raise InventoryMonitorError("库存明细不完整，未更新监控基准")
    if total > 0 and not assets:
        raise InventoryMonitorError("库存明细仍在同步，未更新监控基准")

    parsed_items: list[MonitoredInventoryItem] = []
    used_keys: set[str] = set()
    for raw_asset in assets:
        if not isinstance(raw_asset, Mapping):
            raise InventoryMonitorError("库存包含无效饰品记录")
        asset_id = _safe_text(raw_asset.get("assetId"), "", 80)
        if not asset_id:
            raise InventoryMonitorError("库存饰品缺少唯一标识")
        fingerprint = _safe_text(raw_asset.get("fingerprint"), "", 500)
        name, classinfo = _item_name(raw_asset, classinfos)
        raw_tradable = classinfo.get("tradable")
        tradable = raw_tradable is True or raw_tradable == 1 or raw_tradable == "1"
        tradable_after = _nonnegative_int(classinfo.get("tradableTime"))
        is_tradable = tradable and (tradable_after is None or tradable_after <= int(current))
        inline = parse_inline_anchors(raw_asset, classinfo)
        inspect_url = extract_inspect_url(raw_asset) or extract_inspect_url(classinfo)
        key = _item_key(asset_id, fingerprint)
        if key in used_keys:
            key = f"asset:{asset_id}"
        if key in used_keys:
            raise InventoryMonitorError("库存包含重复饰品标识")
        used_keys.add(key)
        parsed_items.append(
            MonitoredInventoryItem(
                item_key=key,
                asset_id=asset_id,
                fingerprint=fingerprint,
                name=name,
                is_tradable=is_tradable,
                tradable_after=tradable_after,
                price=_finite_price(raw_asset.get("evaluatePrice")),
                market_hash_name=name[:256],
                paint_index=inline.paint_index,
                paintwear=inline.paintwear,
                pattern_index=inline.pattern_index,
                stickers_json=inline.stickers_json,
                inspect_url=inspect_url,
            )
        )

    parsed_items.sort(key=lambda item: item.item_key)
    digest = _comparison_hash(parsed_items)
    nickname = _safe_text(account.get("nickname") or account.get("steamId"), steam_id, 100)
    serialized = json.dumps(
        {
            "version": 1,
            "scope": "public_returned_assets",
            "steam_id": steam_id,
            "nickname": nickname,
            "source_time": source_time,
            "total_count": total,
            "returned_count": len(parsed_items),
            "items": [item.to_dict() for item in parsed_items],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return MonitoredInventorySnapshot(
        steam_id=steam_id,
        nickname=nickname,
        source_time=source_time,
        total_count=total,
        returned_count=len(parsed_items),
        items=tuple(parsed_items),
        comparison_hash=digest,
        snapshot_json=serialized,
    )


def decode_monitor_snapshot(payload: str) -> MonitoredInventorySnapshot:
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise InventoryMonitorError("已保存的库存基准损坏") from exc
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise InventoryMonitorError("已保存的库存基准版本无效")
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_ITEMS:
        raise InventoryMonitorError("已保存的库存基准明细无效")
    items: list[MonitoredInventoryItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            raise InventoryMonitorError("已保存的库存基准包含无效饰品")
        item_key = _safe_text(raw_item.get("item_key"), "", 200)
        asset_id = _safe_text(raw_item.get("asset_id"), "", 80)
        if not item_key or not asset_id or not isinstance(raw_item.get("is_tradable"), bool):
            raise InventoryMonitorError("已保存的库存基准缺少饰品标识")
        items.append(
            MonitoredInventoryItem(
                item_key=item_key,
                asset_id=asset_id,
                fingerprint=_safe_text(raw_item.get("fingerprint"), "", 500),
                name=_safe_text(raw_item.get("name"), "未知饰品", 180),
                is_tradable=bool(raw_item["is_tradable"]),
                tradable_after=_nonnegative_int(raw_item.get("tradable_after")),
                price=_finite_price(raw_item.get("price")),
                market_hash_name=(
                    text[:256]
                    if isinstance(text := raw_item.get("market_hash_name"), str) and text.strip()
                    else None
                ),
                paint_index=_nonnegative_int(raw_item.get("paint_index")),
                paintwear=normalize_paintwear(raw_item.get("paintwear")),
                pattern_index=_nonnegative_int(raw_item.get("pattern_index")),
                stickers_json=(
                    stickers[:2000]
                    if isinstance(stickers := raw_item.get("stickers_json"), str)
                    and len(stickers) <= 2000
                    else None
                ),
            )
        )
    items.sort(key=lambda item: item.item_key)
    steam_id = _safe_text(raw.get("steam_id"), "", 17)
    source_time = _nonnegative_int(raw.get("source_time"))
    total = _nonnegative_int(raw.get("total_count"))
    returned = _nonnegative_int(raw.get("returned_count"))
    if not steam_id or source_time is None or total is None or returned is None:
        raise InventoryMonitorError("已保存的库存基准汇总无效")
    return MonitoredInventorySnapshot(
        steam_id=steam_id,
        nickname=_safe_text(raw.get("nickname"), steam_id, 100),
        source_time=source_time,
        total_count=total,
        returned_count=returned,
        items=tuple(items),
        comparison_hash=_comparison_hash(items),
        snapshot_json=payload,
    )


def compare_inventory_snapshots(
    previous: MonitoredInventorySnapshot,
    current: MonitoredInventorySnapshot,
    *,
    previously_missing: frozenset[str] = frozenset(),
    reattached_keys: frozenset[str] = frozenset(),
) -> InventoryDifference:
    old = previous.item_map()
    new = current.item_map()
    added_keys = sorted(new.keys() - old.keys())
    removed_keys = sorted(old.keys() - new.keys())
    reappearance_keys = previously_missing | reattached_keys
    reappeared_keys = [key for key in added_keys if key in reappearance_keys]
    new_keys = [key for key in added_keys if key not in reappearance_keys]
    state_changes = [
        (old[key], new[key])
        for key in sorted(old.keys() & new.keys())
        if old[key].is_tradable != new[key].is_tradable
    ]
    return InventoryDifference(
        added=tuple(new[key] for key in new_keys),
        reappeared=tuple(new[key] for key in reappeared_keys),
        removed=tuple(old[key] for key in removed_keys),
        state_changes=tuple(state_changes),
    )


def match_history_reattachment(
    current_items: Sequence[MonitoredInventoryItem],
    history_anchors: Mapping[str, HistoryAnchorValues],
    *,
    candidate_keys: frozenset[str],
    tolerance: float = _ANCHOR_WEAR_TOLERANCE,
) -> dict[str, str]:
    """Reconnect brand-new item keys to earlier history rows by wear anchor.

    Only applies when the previous row is currently missing (asset churn
    scenario), and only on an exact name plus ``±tolerance`` wear match.
    """

    missing_by_name: dict[str, list[tuple[float, str]]] = {}
    for row_key, (name, wear, is_missing) in history_anchors.items():
        if not is_missing or name is None or wear is None:
            continue
        missing_by_name.setdefault(name, []).append((wear, row_key))
    if not missing_by_name:
        return {}

    matches: dict[str, str] = {}
    grouped: dict[str, list[MonitoredInventoryItem]] = {}
    for item in current_items:
        if item.item_key in candidate_keys and item.paintwear is not None:
            grouped.setdefault(item.name, []).append(item)
    for name, group in grouped.items():
        rows = sorted(missing_by_name.get(name, ()))
        claimed = set(matches.values())
        for item in group:
            target = next(
                (
                    row_key
                    for row_wear, row_key in rows
                    if row_key not in claimed
                    and anchor_matches(
                        item.name, item.paintwear, name, row_wear, tolerance=tolerance
                    )
                ),
                None,
            )
            if target is not None:
                matches[item.item_key] = target
                claimed.add(target)
    return matches


def _item_state(item: MonitoredInventoryItem) -> str:
    return "可交易" if item.is_tradable else "不可交易"


def _aggregate_items(
    items: Sequence[MonitoredInventoryItem],
) -> list[tuple[str, str, int]]:
    """Group same-named items into (name, state, count) rows."""

    counts: dict[tuple[str, str], int] = {}
    for item in items:
        key = (item.name, _item_state(item))
        counts[key] = counts.get(key, 0) + 1
    return [
        (name, state, count)
        for (name, state), count in sorted(counts.items(), key=lambda pair: pair[0][0])
    ]


def _aggregate_line(
    name: str,
    state: str,
    count: int,
    *,
    marker: str = "+",
) -> str:
    suffix = f"× {count}" if count > 1 else ""
    return f"{marker} {name}（{state}）{suffix}"


def _append_details(
    lines: list[str],
    details: Sequence[str],
    remaining: int,
) -> int:
    available = max(0, _MAX_DETAILS - remaining)
    selected = list(details[:available])
    lines.extend(selected)
    return remaining + len(selected)


def format_inventory_change(
    snapshot: MonitoredInventorySnapshot,
    difference: InventoryDifference,
    *,
    checked_at: int,
) -> str:
    lines = [
        "📦 【库存变化提醒】",
        f"账号: {snapshot.nickname}",
        f"检测时间: {datetime.fromtimestamp(checked_at, tz=_MARKET_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        (
            f"新增 {len(difference.added) + len(difference.reappeared)}｜"
            f"移除 {len(difference.removed)}｜状态变化 {len(difference.state_changes)}"
        ),
    ]
    shown = 0
    if difference.reappeared:
        lines.append("\n重新出现:")
        details = [
            f"{_aggregate_line(name, state, count)}\n"
            "  判断: 曾从公开库存消失后重新出现；可能是从仓库组件取出、交易返回或其他回流"
            for name, state, count in _aggregate_items(difference.reappeared)
        ]
        shown = _append_details(lines, details, shown)
    if difference.added and shown < _MAX_DETAILS:
        lines.append("\n新增:")
        details = []
        for name, state, count in _aggregate_items(difference.added):
            reason = (
                "新出现在公开库存；来源无法确认"
                if state == "可交易"
                else "新获得；市场购买或交易收货均有可能"
            )
            details.append(f"{_aggregate_line(name, state, count)}\n  判断: {reason}")
        shown = _append_details(lines, details, shown)
    if difference.removed and shown < _MAX_DETAILS:
        lines.append("\n移除:")
        details = [
            f"{_aggregate_line(name, state, count, marker='-')}\n"
            "  判断: 已离开公开库存；出售、交易或存入仓库组件均有可能"
            for name, state, count in _aggregate_items(difference.removed)
        ]
        shown = _append_details(lines, details, shown)
    if difference.state_changes and shown < _MAX_DETAILS:
        lines.append("\n状态变化:")
        counts: dict[str, int] = {}
        conclusions: dict[str, str] = {}
        for previous, current in difference.state_changes:
            counts[current.name] = counts.get(current.name, 0) + 1
            if current.name not in conclusions:
                conclusions[current.name] = (
                    "交易限制已解除，不计入新增"
                    if not previous.is_tradable and current.is_tradable
                    else "交易状态变为不可交易，不计入新增"
                )
        details = [
            f"↻ {name}{'× ' + str(count) if count > 1 else ''}\n  判断: {conclusions[name]}"
            for name, count in sorted(counts.items())
        ]
        shown = _append_details(lines, details, shown)

    # Same unit on both sides of the comparison: display rows are aggregated
    # (name, state) groups, so the "omitted" count must be expressed in those
    # same rows — 30 identical items shown as one row omit nothing.
    total_details = (
        len(_aggregate_items(difference.added))
        + len(_aggregate_items(difference.reappeared))
        + len(_aggregate_items(difference.removed))
        + len({current.name for _previous, current in difference.state_changes})
    )
    if total_details > shown:
        lines.append(f"\n其余 {total_details - shown} 项变化已省略。")
    lines.extend(
        [
            "\nℹ️ 变化原因基于公开库存历史推断，不代表交易记录。",
            "ℹ️ 仅监控接口返回的公开饰品；仓库组件内物品不可见。",
        ]
    )
    message = "\n".join(lines)
    if len(message) > 4_000:
        message = message[:3_940].rstrip() + "\nℹ️ 变化较多，消息已截断。"
    return message


def _bounded_trajectory_days(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= _TRAJECTORY_MAX_DAYS:
        raise ValueError("trajectory window is out of range")
    return value


@dataclass(frozen=True, slots=True)
class TrajectorySummary:
    added: int
    removed: int
    present: int
    hidden_periods: int
    top_added: tuple[tuple[str, int], ...]
    top_removed: tuple[tuple[str, int], ...]
    timeline: tuple[str, ...]


def _display_name(row: InventoryHistoryTrajectoryRow) -> str:
    return row.market_hash_name or row.name


def _summarize_trajectory(
    rows: Sequence[InventoryHistoryTrajectoryRow],
    window_start: int,
    window_end: int,
) -> TrajectorySummary:
    """Aggregate raw history rows into counts; no per-asset dumps."""

    added = 0
    removed = 0
    present = 0
    hidden_periods = 0
    added_counts: dict[str, int] = {}
    removed_counts: dict[str, int] = {}
    timeline_events: list[tuple[int, str]] = []
    for row in rows:
        if row.last_missing_at is None:
            present += 1
        else:
            hidden_periods += 1
        if row.first_seen_at >= window_start:
            added += 1
            label = _display_name(row)
            added_counts[label] = added_counts.get(label, 0) + 1
            timeline_events.append((row.first_seen_at, f"+ {label}"))
        if row.last_missing_at is not None and window_start <= row.last_missing_at <= window_end:
            removed += 1
            label = _display_name(row)
            removed_counts[label] = removed_counts.get(label, 0) + 1
            timeline_events.append((row.last_missing_at, f"- {label}"))

    def top(counts: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
        ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return tuple(ordered[:_TRAJECTORY_TOP_ROWS])

    timeline_events.sort(key=lambda event: event[0])
    timeline = [
        f"{datetime.fromtimestamp(stamp, tz=_MARKET_TZ).strftime('%m-%d %H:%M')} {label}"
        for stamp, label in timeline_events[:_TRAJECTORY_TIMELINE]
    ]
    return TrajectorySummary(
        added=added,
        removed=removed,
        present=present,
        hidden_periods=hidden_periods,
        top_added=top(added_counts),
        top_removed=top(removed_counts),
        timeline=tuple(timeline),
    )


def format_trajectory_report(
    nickname: str,
    summary: TrajectorySummary,
    *,
    window_days: int,
    window_start: int,
    checked_at: int,
) -> str:
    lines = [
        "📦 【库存轨迹】",
        f"账号: {nickname}",
        (
            f"统计窗口: 近{window_days}天"
            f"（自 {datetime.fromtimestamp(window_start, tz=_MARKET_TZ).strftime('%Y-%m-%d')} 起）"
        ),
        f"当前在场记录: {summary.present} 条",
        f"窗口内新增 {summary.added}｜离场 {summary.removed}",
    ]
    if summary.top_added:
        lines.append("新增最多:")
        lines.extend(f"  + {name} × {count}" for name, count in summary.top_added)
    if summary.top_removed:
        lines.append("离场最多:")
        lines.extend(f"  - {name} × {count}" for name, count in summary.top_removed)
    if summary.timeline:
        lines.append("近期时间线:")
        lines.extend(f"  {entry}" for entry in summary.timeline)
    lines.append("\nℹ️ 仅统计公开库存可见变化；仓库组件内物品不可见，隐藏时段如实计入而非推测去向。")
    message = "\n".join(lines)
    if len(message) > 4_000:
        message = message[:3_940].rstrip() + "\nℹ️ 记录较多，消息已截断。"
    return message


class InventoryMonitorService:
    def __init__(
        self,
        repository: InventoryMonitorRepository,
        *,
        fetcher: SnapshotFetcher = fetch_inventory_snapshot,
        wall_clock: Callable[[], float] = time.time,
        float_resolver: FloatResolver | None = None,
        email_notifier=None,
        report_generator: Callable[..., Any] | None = None,
    ) -> None:
        self.repository = repository
        self._fetcher = fetcher
        self._wall_clock = wall_clock
        self._float_resolver = float_resolver
        self._email_notifier = email_notifier
        self._report_generator = report_generator
        self._inflight: dict[str, asyncio.Task[MonitoredInventorySnapshot]] = {}
        self._inflight_lock = asyncio.Lock()

    async def _profile_rows(
        self,
        steam_id: str,
    ) -> tuple[str, tuple[InventoryHistoryTrajectoryRow, ...]] | None:
        return await self.repository.trajectory_snapshot(
            steam_id,
            since=0,
            limit=_TRAJECTORY_ROWS_LIMIT,
        )

    async def _enrich_floats(
        self,
        items: tuple[MonitoredInventoryItem, ...],
        target_keys: frozenset[str],
    ) -> tuple[MonitoredInventoryItem, ...]:
        """Fill wear anchors for newly appeared items through the resolver.

        Any resolver problem degrades to leaving the anchor NULL; the caller's
        baseline and events are untouched (hard requirement: no-float mode).
        Only whitelisted inspect links of ``target_keys`` are resolved, capped
        per round so quota spend and latency stay bounded.
        """

        if self._float_resolver is None:
            return items
        pending = [
            item.inspect_url
            for item in items
            if item.item_key in target_keys
            and item.paintwear is None
            and item.inspect_url is not None
        ]
        if not pending:
            return items
        urls = pending[:_MAX_FLOAT_URLS_PER_ROUND]
        try:
            async with asyncio.timeout(_FLOAT_RESOLVE_TIMEOUT_SECONDS):
                resolved = await self._float_resolver.resolve(tuple(urls))
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Float resolution timed out urls=%d", len(urls))
            return items
        except Exception as exc:
            logger.warning("Float resolution failed error=%s", type(exc).__name__)
            return items
        if not resolved:
            return items
        updated: list[MonitoredInventoryItem] = []
        for item in items:
            anchors = resolved.get(item.inspect_url or "")
            if item.paintwear is None and anchors is not None:
                updated.append(item.with_anchors(anchors))
            else:
                updated.append(item)
        return tuple(updated)

    async def _fetch(self, steam_id: str) -> MonitoredInventorySnapshot:
        response = await self._fetcher(steam_id)
        return parse_monitor_snapshot(response, steam_id, now=self._wall_clock())

    def _reap_inflight(self, steam_id: str, task: asyncio.Task[MonitoredInventorySnapshot]) -> None:
        """Drop the single-flight slot once its task finishes.

        Runs synchronously inside the event loop (no await points), so the
        dict needs no lock here.  Without this callback a round whose waiters
        all cancelled would leave a finished task parked in ``_inflight`` and
        hand its stale result to the next caller.
        """

        if self._inflight.get(steam_id) is task:
            self._inflight.pop(steam_id, None)

    async def snapshot(self, steam_id: str) -> MonitoredInventorySnapshot:
        async with self._inflight_lock:
            task = self._inflight.get(steam_id)
            if task is None:
                task = asyncio.create_task(self._fetch(steam_id))
                self._inflight[steam_id] = task
                task.add_done_callback(partial(self._reap_inflight, steam_id))
        try:
            return await asyncio.shield(task)
        finally:
            self._reap_inflight(steam_id, task)

    async def start(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
        raw_identifier: str,
    ) -> tuple[SubscribeResult, MonitoredInventorySnapshot] | str:
        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return "未能确认库存账号：请提供17位 Steam64 ID 或完整交易链接。"
        try:
            snapshot = await self.snapshot(identifier.steam_id)
            keys = frozenset(item.item_key for item in snapshot.items)
            enriched = await self._enrich_floats(snapshot.items, keys)
            if enriched != snapshot.items:
                snapshot = snapshot.with_items(enriched)
        except InventoryMonitorError as exc:
            return f"⚠️ {exc}，库存监控未开启。"
        except Exception:
            return "⚠️ 库存主动请求失败，库存监控未开启；请稍后重试。"
        result = await self.repository.subscribe(
            owner_user_id,
            destination_type,
            destination_id,
            steam_id=snapshot.steam_id,
            nickname=snapshot.nickname,
            snapshot_json=snapshot.snapshot_json,
            snapshot_hash=snapshot.comparison_hash,
            source_time=snapshot.source_time,
            items=snapshot.item_json_map(),
            anchors=snapshot.anchor_map_values(),
            now=int(self._wall_clock()),
        )
        return result, snapshot

    async def _advance_claim(
        self,
        claim: InventoryAccountClaim,
        *,
        exclude_delivery: tuple[int, DestinationType, int] | None = None,
    ) -> "_ClaimOutcome":
        timestamp = int(self._wall_clock())
        try:
            current = await self.snapshot(claim.account.steam_id)
            previous = decode_monitor_snapshot(claim.account.snapshot_json)
            if current.source_time < claim.account.source_time:
                raise InventoryMonitorError("库存数据时间倒退")
            if previous.returned_count > 0 and current.returned_count == 0:
                raise InventoryMonitorError("库存明细暂时为空")
            missing = await self.repository.missing_item_keys(claim.account.steam_id)
            history_anchors = await self.repository.item_history_anchors(claim.account.steam_id)
            current_map = current.item_map()
            previous_map = previous.item_map()
            # Only newly seen keys get float resolution (incremental; never a
            # full history backfill).
            float_targets = frozenset(current_map.keys() - previous_map.keys())
            enriched_items = await self._enrich_floats(current.items, float_targets)
            if enriched_items != current.items:
                current = current.with_items(enriched_items)
                current_map = current.item_map()
            reattachments = match_history_reattachment(
                current.items,
                history_anchors,
                candidate_keys=frozenset(float_targets - missing),
            )
            difference = compare_inventory_snapshots(
                previous,
                current,
                previously_missing=missing,
                reattached_keys=frozenset(reattachments),
            )
            message = (
                format_inventory_change(current, difference, checked_at=timestamp)
                if difference.changed
                else None
            )
            removed_keys = tuple(sorted(previous_map.keys() - current_map.keys()))
            committed = await self.repository.commit_snapshot(
                claim,
                nickname=current.nickname,
                snapshot_json=current.snapshot_json,
                snapshot_hash=current.comparison_hash,
                source_time=current.source_time,
                items=current.item_json_map(),
                anchors=current.anchor_map_values(),
                removed_item_keys=removed_keys,
                message_text=message,
                exclude_delivery=exclude_delivery,
                now=timestamp,
            )
            if not committed.applied:
                return _ClaimOutcome(
                    "同一账号的另一次检查已先完成，请查看最新结果。", changed=False, failed=True
                )
            if message is not None:
                # Email is a best-effort side channel; QQ delivery semantics
                # above are unchanged and a mail problem must never fail the
                # monitor tick (fire-and-forget with internal backoff).
                if self._email_notifier is not None and self._email_notifier.enabled:
                    task = asyncio.create_task(
                        self._email_notifier.send_text(
                            f"[囤货雷达] {current.nickname} 库存变化",
                            message,
                        )
                    )
                    _EMAIL_TASKS.add(task)
                    task.add_done_callback(_EMAIL_TASKS.discard)
                return _ClaimOutcome(message, changed=True, failed=False)
            source_label = datetime.fromtimestamp(current.source_time, tz=_MARKET_TZ).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if current.source_time <= claim.account.source_time:
                return _ClaimOutcome(
                    "📦 【库存主动检查】\n"
                    f"账号: {current.nickname}\n"
                    f"数据时间仍为: {source_label}\n"
                    "本次请求成功，但尚未产生更新的库存快照。",
                    changed=False,
                    failed=False,
                )
            return _ClaimOutcome(
                "📦 【库存主动检查】\n"
                f"账号: {current.nickname}\n"
                f"数据时间: {source_label}\n"
                "未发现库存变化。",
                changed=False,
                failed=False,
            )
        except InventoryMonitorError as exc:
            await self.repository.record_account_failure(
                claim,
                type(exc).__name__,
                now=timestamp,
            )
            return _ClaimOutcome(f"⚠️ {exc}，上一次有效基准未被覆盖。", changed=False, failed=True)
        except Exception as exc:
            await self.repository.record_account_failure(
                claim,
                type(exc).__name__,
                now=timestamp,
            )
            return _ClaimOutcome(
                "⚠️ 库存主动请求失败，上一次有效基准未被覆盖。", changed=False, failed=True
            )

    async def check_now(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
        raw_identifier: str,
    ) -> tuple[str, bool]:
        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return "未能确认库存账号：请提供17位 Steam64 ID 或完整交易链接。", False
        if not await self.repository.has_subscription(
            owner_user_id,
            destination_type,
            destination_id,
            identifier.steam_id,
        ):
            return (
                "当前会话尚未监控该账号；私聊和每个群的库存监控相互独立，"
                "请在当前会话发送“库存监控 SteamID”。",
                False,
            )
        claim = await self.repository.claim_account(
            identifier.steam_id,
            _CLAIM_SECONDS,
            now=int(self._wall_clock()),
        )
        if claim is None:
            return "该账号正在进行库存检查，请稍后查看结果。", False
        outcome = await self._advance_claim(
            claim,
            exclude_delivery=(owner_user_id, destination_type, destination_id),
        )
        return outcome.message, outcome.changed

    async def check_due(self, *, limit: int = 20) -> DueCheckSummary:
        claims = await self.repository.claim_due_accounts(
            limit,
            _CLAIM_SECONDS,
            now=int(self._wall_clock()),
        )
        completed = 0
        changed = 0
        failed = 0

        async def process(claim: InventoryAccountClaim) -> None:
            nonlocal completed, changed, failed
            outcome = await self._advance_claim(claim)
            if outcome.failed:
                failed += 1
            else:
                completed += 1
                if outcome.changed:
                    changed += 1

        semaphore = asyncio.Semaphore(3)

        async def bounded(claim: InventoryAccountClaim) -> None:
            async with semaphore:
                await process(claim)

        await asyncio.gather(*(bounded(claim) for claim in claims))
        return DueCheckSummary(len(claims), completed, changed, failed)

    async def list_destination(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
    ) -> tuple[InventoryMonitorSubscription, ...]:
        return await self.repository.list_destination(
            owner_user_id,
            destination_type,
            destination_id,
        )

    async def trajectory(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
        raw_identifier: str,
        days: object = None,
    ) -> str:
        """Private-trajectory summary restricted to subscribed destinations."""

        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return "未能确认库存账号：请提供17位 Steam64 ID 或完整交易链接。\n格式: 库存轨迹 SteamID [天数]"
        try:
            window_days = (
                _TRAJECTORY_DEFAULT_DAYS
                if days is None
                else _bounded_trajectory_days(int(str(days)))
            )
        except (TypeError, ValueError):
            return f"天数需为 1~{_TRAJECTORY_MAX_DAYS} 的整数。\n格式: 库存轨迹 SteamID [天数]"
        # Privacy boundary: trajectories are only readable through a session
        # that actually monitors the account (owner-scoped subscription).
        if not await self.repository.has_subscription(
            owner_user_id,
            destination_type,
            destination_id,
            identifier.steam_id,
        ):
            return "当前会话尚未监控该账号；请先发送“库存监控 SteamID”后再查询轨迹。"
        window_end = int(self._wall_clock())
        window_start = max(0, window_end - window_days * 24 * 60 * 60)
        record = await self.repository.trajectory_snapshot(
            identifier.steam_id,
            since=window_start,
            limit=_TRAJECTORY_ROWS_LIMIT,
        )
        if record is None:
            return "暂无该账号的观测记录。"
        nickname, rows = record
        summary = _summarize_trajectory(rows, window_start, window_end)
        return format_trajectory_report(
            nickname,
            summary,
            window_days=window_days,
            window_start=window_start,
            checked_at=window_end,
        )

    async def profile(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
        raw_identifier: str,
    ) -> str:
        """AI hoard profile for one subscribed account (owner-scoped)."""

        from .hoard_report import HoardReportError, generate_report

        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return (
                "未能确认库存账号：请提供17位 Steam64 ID 或完整交易链接。\n格式: 囤货画像 SteamID"
            )
        if not await self.repository.has_subscription(
            owner_user_id,
            destination_type,
            destination_id,
            identifier.steam_id,
        ):
            return "当前会话尚未监控该账号；请先发送“库存监控 SteamID”后再生成画像。"
        record = await self.repository.trajectory_snapshot(identifier.steam_id, since=0)
        if record is None:
            return "暂无该账号的观测记录。"
        nickname, rows = record
        generator = self._report_generator
        if generator is None:
            from .hoard_report import generate_via_llm as generator  # noqa: N813

        try:
            report = await generate_report(
                self.repository,
                identifier.steam_id,
                nickname,
                rows,
                now=int(self._wall_clock()),
                generator=generator,
            )
        except HoardReportError as exc:
            return f"⚠️ 画像未生成：{exc}"
        except Exception as exc:
            logger.warning("Hoard report generation failed error=%s", type(exc).__name__)
            return "⚠️ 画像服务暂时不可用，请稍后重试。"
        header = "" if report.cached else ""
        suffix = "\n（缓存于近期，未重新调用模型）" if report.cached else ""
        body = f"{header}{report.content.strip()}"[:3_900]
        generated_label = datetime.fromtimestamp(report.generated_at, tz=_MARKET_TZ).strftime(
            "%Y-%m-%d %H:%M"
        )
        return f"{body}\n\n模型: {report.model}\n生成时间: {generated_label}{suffix}"

    async def profile_status(self) -> str:
        """Token-free introspection: cache health and last generation time."""

        rows = await self.repository.database.read(
            """
            SELECT steam_id, generated_at, model FROM inventory_monitor_reports
            ORDER BY generated_at DESC LIMIT 5
            """
        )
        if not rows:
            return (
                "📊 【囤货画像状态】\n"
                "尚无任何画像缓存。\n"
                "发送「囤货画像 SteamID」可生成第一份画像（同账号6小时内直接复用）。"
            )
        lines = ["📊 【囤货画像状态】"]
        now = int(self._wall_clock())
        for row in rows:
            generated_at = int(row["generated_at"])
            fresh = "✅ 缓存有效" if now - generated_at < 6 * 60 * 60 else "⏳ 已过期待刷新"
            lines.append(
                f"- {row['steam_id']}: "
                f"{datetime.fromtimestamp(generated_at, tz=_MARKET_TZ).strftime('%m-%d %H:%M')}"
                f"｜{row['model']}｜{fresh}"
            )
        lines.append("\n同账号画像 6 小时内命中缓存不重复烧 token。")
        return "\n".join(lines)

    async def cancel(
        self,
        owner_user_id: int,
        destination_type: DestinationType,
        destination_id: int,
        raw_identifier: str,
    ) -> bool | None:
        identifier = parse_inventory_identifier(raw_identifier)
        if identifier is None:
            return None
        return await self.repository.unsubscribe(
            owner_user_id,
            destination_type,
            destination_id,
            identifier.steam_id,
        )


__all__ = [
    "DueCheckSummary",
    "InventoryDifference",
    "InventoryMonitorError",
    "InventoryMonitorService",
    "ItemAnchorRow",
    "ItemAnchorValues",
    "MonitoredInventoryItem",
    "MonitoredInventorySnapshot",
    "TrajectorySummary",
    "anchor_fingerprint",
    "anchor_matches",
    "compare_inventory_snapshots",
    "decode_monitor_snapshot",
    "format_inventory_change",
    "format_trajectory_report",
    "match_history_reattachment",
    "parse_inline_anchors",
    "parse_monitor_snapshot",
]

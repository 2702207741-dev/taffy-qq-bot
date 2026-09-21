"""Feature-level authorization with default-open semantics and cache.

A missing override means the feature is allowed, so every existing user keeps
working until an administrator explicitly denies something.  The registry
below is the single source of truth for feature categories and their display
names, shared by plugins, the admin API and the management panel.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from src.state.permission_repository import PermissionRepository

logger = logging.getLogger("taffy.authority")

FEATURES: dict[str, str] = {
    "chat": "普通聊天",
    "market": "行情查询（大盘/查询/分析/K线/抄底）",
    "watchlist": "自选列表",
    "alert": "价格预警",
    "inventory": "库存监控",
    "compare": "饰品对比",
    "news": "CS2 新闻/赛事",
    "entertainment": "娱乐（开箱/炼金/随机皮肤）",
}

DENIED_MESSAGE = "⛔ 该功能未对你开放，如需使用请联系管理员。"

#: LRU bound for cached decisions; the (user, feature) space is unbounded, so
#: a long-lived bot must not grow this cache without limit.
_MAX_CACHE_ENTRIES = 4_096


class FeatureAuthority:
    """Cached per-(user, feature) permission decision."""

    def __init__(self, permissions: PermissionRepository) -> None:
        self._permissions = permissions
        self._cache: OrderedDict[tuple[int, str], bool | None] = OrderedDict()
        self._lock = asyncio.Lock()

    def _remember(self, key: tuple[int, str], value: bool | None) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    async def allowed(self, user_id: int, feature: str) -> bool:
        """Return whether ``user_id`` may use ``feature`` (default: yes)."""

        key = (user_id, feature)
        cached = self._cache.get(key, _MISSING)
        if cached is not _MISSING:
            self._cache.move_to_end(key)
            return True if cached is None else bool(cached)
        async with self._lock:
            cached = self._cache.get(key, _MISSING)
            if cached is _MISSING:
                override = await self._permissions.get(user_id, feature)
                cached = override
                self._remember(key, override)
        return True if cached is None else bool(cached)

    async def set(self, user_id: int, feature: str, allowed: bool) -> None:
        await self._permissions.set(user_id, feature, allowed)
        self._remember((user_id, feature), allowed)

    async def clear(self, user_id: int, feature: str) -> bool:
        removed = await self._permissions.clear(user_id, feature)
        self._cache.pop((user_id, feature), None)
        return removed

    def invalidate(self, user_id: int | None = None, feature: str | None = None) -> None:
        if user_id is None and feature is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if (user_id is None or key[0] == user_id) and (feature is None or key[1] == feature):
                self._cache.pop(key, None)


_MISSING = object()

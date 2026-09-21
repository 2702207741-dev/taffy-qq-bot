"""Desktop API 契约测试 — 开关/鉴权/限流/审计/信封与端点形状。

参照 test_admin_api.py 的模式（httpx.ASGITransport + 独立 FastAPI app）。
"""

from __future__ import annotations

import importlib
import secrets
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
import nonebot
from fastapi import FastAPI

from src.services.hoard_index import hoard_index_store


def _token() -> str:
    return secrets.token_urlsafe(32)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class DesktopApiTestBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        nonebot.get_driver().config.superusers = {"10001"}
        cls.desktop_api = importlib.import_module("src.plugins.desktop_api")
        cls.desktop_module = cls.desktop_api
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        cls.app = FastAPI()
        cls.app.include_router(cls.desktop_api.router)

    def setUp(self) -> None:
        # Fresh generous limiters per test; rate-limit tests override them.
        self.desktop_api._chat_limiter = self._limiter(100_000)
        self.desktop_api._market_limiter = self._limiter(100_000)
        self.desktop_api._write_limiter = self._limiter(100_000)

    def _owner_env(self, value: str | None = "10001") -> None:
        """Set/clear DESKTOP_OWNER_QQ for the duration of one test."""

        pairs = {} if value is None else {"DESKTOP_OWNER_QQ": value}
        patcher = patch.dict("os.environ", pairs, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _enable(self) -> str:
        """Turn the master switch on with a strong expected token."""

        token = _token()
        enabled = patch.object(self.desktop_api, "desktop_api_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        auth = patch.object(self.admin_api, "get_admin_api_token", return_value=token)
        auth.start()
        self.addCleanup(auth.stop)
        return token

    async def _request(self, method: str, path: str, **kwargs: Any):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    @staticmethod
    def _limiter(limit: int):
        module = importlib.import_module("src.core.rate_limit")
        return module.SlidingWindowLimiter(limit, 60.0)


class OwnerResolutionTests(DesktopApiTestBase):
    def test_env_owner_takes_priority(self) -> None:
        self._owner_env("20002")
        self.assertEqual(self.desktop_api.resolve_desktop_owner(), 20002)

    def test_invalid_env_falls_back_to_superusers(self) -> None:
        self._owner_env("not-a-number")
        self.assertEqual(self.desktop_api.resolve_desktop_owner(), 10001)

    def _layered(self, value: str | None) -> None:
        """只播种**分层读取**那一层（``src.services.config.runtime_setting``）。

        既有的 ``_owner_env`` 喂的是 ``os.environ`` —— 那正是 M-08 缺陷直读的
        错误通道，所以三条老用例在缺陷存在时也全绿。本方法补上真正的用户路径。
        """

        patcher = patch("src.services.config.runtime_setting", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_owner_comes_from_layered_settings(self) -> None:
        """M-08：只经分层读取提供的 owner 必须生效。

        修复前 ``resolve_desktop_owner`` 直读 ``os.environ``，因此这里会落到
        superusers（10001）而不是 30003 —— 也就是**解析到了错账号**。
        """

        self._owner_env(None)  # 进程环境里没有
        self._layered("30003")

        self.assertEqual(self.desktop_api.resolve_desktop_owner(), 30003)

    def test_process_environment_still_wins_over_layered_settings(self) -> None:
        """反向对照：两层都有时进程环境胜出（``runtime_setting`` 的既有契约）。

        这条防的是「彻底不再读 os.environ」那种错修法 —— 它会破坏容器/密钥
        管理器在导入后覆盖配置的能力。

        **不能** patch ``runtime_setting`` 本身：优先级就实现在它的第一行，
        patch 掉它等于绕过被断言的对象（我第一版正是如此，拿到 30003 而非 20002）。
        正确做法是**两层都播种**，再调用真实函数：

        * 进程环境层：``os.environ``
        * dotenv 层：``_RUNTIME_SETTINGS``（casefold 键）
        """

        self._owner_env("20002")
        config_module = importlib.import_module("src.services.config")
        patcher = patch.object(config_module, "_RUNTIME_SETTINGS", {"desktop_owner_qq": "30003"})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.assertEqual(self.desktop_api.resolve_desktop_owner(), 20002)

    def test_multiple_superusers_without_explicit_owner_is_refused(self) -> None:
        """M-110：多个 superusers 且未显式配置时**拒绝解析**，不替运维挑账号。

        今天线上只有一个 superuser，所以这条风险被掩盖；只要有人加第二个，
        桌面主人就会静默迁移到排序最前的那个账号 —— 连带 persona 写入范围、
        「查看他人私聊」的豁免判定、桌面审计归属一起搬家（M-08 的原始危害换了个
        诱因）。**故意不在断言里钉「选哪一个」** —— 那等于把坏行为固化。
        """

        self._owner_env(None)
        driver_config = nonebot.get_driver().config
        original = driver_config.superusers
        driver_config.superusers = {"9999", "20002"}
        try:
            self.assertIsNone(self.desktop_api.resolve_desktop_owner())
            with self.assertRaises(self.desktop_api.OwnerNotConfiguredError) as ctx:
                self.desktop_api.require_desktop_owner()
        finally:
            driver_config.superusers = original

        self.assertIn("DESKTOP_OWNER_QQ", str(ctx.exception))

    def test_explicit_owner_resolves_despite_multiple_superusers(self) -> None:
        """逃生口：显式配置了 owner 时，多个 superusers 不影响解析。"""

        self._owner_env("20002")
        driver_config = nonebot.get_driver().config
        original = driver_config.superusers
        driver_config.superusers = {"9999", "20002"}
        try:
            self.assertEqual(self.desktop_api.resolve_desktop_owner(), 20002)
        finally:
            driver_config.superusers = original

    def test_single_superuser_still_falls_back(self) -> None:
        """线上现状（`.env` 只有一个 superuser）不受 M-110 影响。"""

        self._owner_env(None)
        self.assertEqual(self.desktop_api.resolve_desktop_owner(), 10001)

    def test_missing_both_returns_none(self) -> None:
        self._owner_env(None)
        driver_config = nonebot.get_driver().config
        original = driver_config.superusers
        driver_config.superusers = set()
        try:
            self.assertIsNone(self.desktop_api.resolve_desktop_owner())
        finally:
            driver_config.superusers = original


class SwitchAndAuthTests(DesktopApiTestBase):
    """开关关 → 404（fail-closed，先于鉴权）；开关开无令牌/错令牌 → 401。"""

    async def test_disabled_returns_404_even_without_token(self) -> None:
        response = await self._request("GET", "/api/desktop/system/summary")
        self.assertEqual(response.status_code, 404)

    async def test_enabled_without_token_is_401(self) -> None:
        self._enable()
        response = await self._request("GET", "/api/desktop/system/summary")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    async def test_wrong_token_is_401(self) -> None:
        self._enable()
        # 与 admin 套件同纪律：隔离失败计数器，避免污染全量运行时的共享预算。
        fresh_limiter = self._limiter(10_000)
        with patch.object(self.admin_api, "_auth_failure_limiter", fresh_limiter):
            response = await self._request(
                "GET", "/api/desktop/system/summary", headers=_auth(_token())
            )
        self.assertEqual(response.status_code, 401)

    async def test_every_endpoint_hidden_when_disabled(self) -> None:
        paths = [
            ("GET", "/api/desktop/chat/history"),
            ("DELETE", "/api/desktop/chat/history"),
            ("POST", "/api/desktop/chat"),
            ("GET", "/api/desktop/market/overview"),
            ("GET", "/api/desktop/market/index-closes"),
            ("GET", "/api/desktop/market/dip"),
            ("GET", "/api/desktop/market/prediction"),
            ("GET", "/api/desktop/market/rankings"),
            ("GET", "/api/desktop/market/search"),
            ("GET", "/api/desktop/market/item"),
            ("GET", "/api/desktop/market/kline"),
            ("GET", "/api/desktop/market/watchlist-quotes"),
            ("POST", "/api/desktop/alerts"),
            ("GET", "/api/desktop/inventory/state"),
            ("GET", "/api/desktop/news/official"),
            ("GET", "/api/desktop/news/reddit"),
            ("POST", "/api/desktop/news/reddit/read"),
            ("GET", "/api/desktop/persona/profile"),
            ("POST", "/api/desktop/persona/style"),
            ("POST", "/api/desktop/persona/memory"),
            ("DELETE", "/api/desktop/persona/memory"),
            ("GET", "/api/desktop/analytics/usage"),
            ("GET", "/api/desktop/analytics/alerts"),
            ("GET", "/api/desktop/system/summary"),
        ]
        for method, path in paths:
            response = await self._request(method, path)
            self.assertEqual(response.status_code, 404, f"{method} {path}")


class EnvelopeContractTests(DesktopApiTestBase):
    """成功信封 {ok,data} 与业务错误信封 {ok,error:<类型名>,message}。"""

    async def test_success_envelope_shape(self) -> None:
        token = self._enable()
        snapshot = {"status": "ready", "ready": True, "uptime_seconds": 5}
        scheduler_config = SimpleNamespace(
            as_dict=lambda: {"enabled": False}, version=3, updated_at=123
        )
        with (
            patch.object(self.desktop_api, "_health_snapshot", AsyncMock(return_value=snapshot)),
            patch.object(
                self.desktop_api.schedules, "get", AsyncMock(return_value=scheduler_config)
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/system/summary", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["health"]["status"], "ready")
        self.assertEqual(body["data"]["scheduler"]["version"], 3)
        self.assertIn("uptime_seconds", body["data"])
        self.assertIn("version", body["data"])

    async def test_owner_not_configured_maps_to_semantic_503(self) -> None:
        token = self._enable()
        payload = {"name": "AK-47 | Redline", "direction": "below", "target": 9.9}
        with patch.object(self.desktop_api, "resolve_desktop_owner", return_value=None):
            response = await self._request(
                "POST", "/api/desktop/alerts", json=payload, headers=_auth(token)
            )
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "OwnerNotConfiguredError")
        self.assertIn("QQ", body["message"])

    async def test_chat_owner_not_configured_is_semantic_503(self) -> None:
        token = self._enable()
        with patch.object(self.desktop_api, "resolve_desktop_owner", return_value=None):
            response = await self._request(
                "POST",
                "/api/desktop/chat",
                json={"message": "你好"},
                headers=_auth(token),
            )
            persona = await self._request(
                "GET", "/api/desktop/persona/profile", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "OwnerNotConfiguredError")
        self.assertEqual(persona.status_code, 503)

    async def test_unexpected_failure_exposes_only_type_name(self) -> None:
        token = self._enable()
        with (
            patch.object(
                self.desktop_api,
                "_health_snapshot",
                AsyncMock(side_effect=RuntimeError("secret detail")),
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/system/summary", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error"], "RuntimeError")
        self.assertNotIn("secret detail", body["message"])


class RateLimitTests(DesktopApiTestBase):
    async def test_market_rate_limit_returns_429_with_retry_after(self) -> None:
        token = self._enable()
        index_store = importlib.import_module("src.services.index_history").index_history_store
        with (
            patch.object(self.desktop_api, "_market_limiter", self._limiter(1)),
            patch.object(index_store, "load_closes", AsyncMock(return_value=())),
        ):
            first = await self._request(
                "GET", "/api/desktop/market/index-closes", headers=_auth(token)
            )
            second = await self._request(
                "GET", "/api/desktop/market/index-closes", headers=_auth(token)
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIsNotNone(second.headers.get("retry-after"))

    async def test_write_rate_limit_returns_429(self) -> None:
        token = self._enable()
        self._owner_env()
        payload = {"name": "AK-47 | Redline", "direction": "below", "target": 10.0}
        alert = SimpleNamespace(
            id="x",
            market_name="AK-47 | Redline",
            direction="below",
            target_price_cents=1000,
            status="pending",
            created_at=1755000000,
        )
        result = SimpleNamespace(created=True, alert=alert, quota_exceeded=False)
        with (
            patch.object(self.desktop_api, "_write_limiter", self._limiter(1)),
            patch.object(
                self.desktop_api.alert_repository,
                "create",
                AsyncMock(return_value=result),
            ),
        ):
            first = await self._request(
                "POST", "/api/desktop/alerts", json=payload, headers=_auth(token)
            )
            second = await self._request(
                "POST", "/api/desktop/alerts", json=payload, headers=_auth(token)
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    async def test_chat_limiter_keyed_per_session(self) -> None:
        token = self._enable()
        self._owner_env()

        async def fake_chat(message: str, session_id: str, **_: Any) -> str:
            return f"喵:{session_id}"

        llm_service = importlib.import_module("src.services.llm").chat_service
        with (
            patch.object(llm_service, "chat", side_effect=fake_chat),
            patch.object(self.desktop_api, "_chat_limiter", self._limiter(1)),
        ):
            first = await self._request(
                "POST", "/api/desktop/chat", json={"message": "你好"}, headers=_auth(token)
            )
            blocked = await self._request(
                "POST", "/api/desktop/chat", json={"message": "你好"}, headers=_auth(token)
            )
            other_session = await self._request(
                "POST",
                "/api/desktop/chat",
                json={"message": "你好", "session": "d_10001_alt"},
                headers=_auth(token),
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(other_session.status_code, 200)


class ChatEndpointTests(DesktopApiTestBase):
    async def test_chat_uses_desktop_session_and_private_persona_scope(self) -> None:
        token = self._enable()
        self._owner_env()
        captured: dict[str, Any] = {}

        async def fake_chat(message: str, session_id: str, **kwargs: Any) -> str:
            captured.update(message=message, session=session_id, scope=kwargs.get("persona_scope"))
            return "好的喵～"

        llm_service = importlib.import_module("src.services.llm").chat_service
        with patch.object(llm_service, "chat", side_effect=fake_chat):
            response = await self._request(
                "POST",
                "/api/desktop/chat",
                json={"message": "查一下红线"},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["reply"], "好的喵～")
        self.assertEqual(captured["session"], "d_10001")
        self.assertEqual(captured["scope"].owner_user_id, 10001)
        self.assertEqual(captured["scope"].group_id, 0)
        self.assertIsInstance(data["elapsed_ms"], int)

    async def test_chat_rejects_foreign_session_prefix(self) -> None:
        token = self._enable()
        self._owner_env()
        response = await self._request(
            "POST",
            "/api/desktop/chat",
            json={"message": "hi", "session": "p_999"},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "InvalidParameterError")

    async def test_chat_history_reads_default_session(self) -> None:
        token = self._enable()
        self._owner_env()
        sessions = importlib.import_module("src.state.runtime").sessions
        messages = [{"role": "user", "content": "hi"}]
        as_messages = AsyncMock(return_value=messages)
        with patch.object(sessions, "as_messages", as_messages):
            response = await self._request("GET", "/api/desktop/chat/history", headers=_auth(token))
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["messages"][0]["content"], "hi")
        as_messages.assert_awaited_once_with("d_10001", last_n=20)

    async def test_chat_history_delete_conflicts_when_busy(self) -> None:
        token = self._enable()
        self._owner_env()
        sessions = importlib.import_module("src.state.runtime").sessions

        class BusyGuard:
            async def __aenter__(self) -> str:
                raise TimeoutError("busy")

            async def __aexit__(self, *_: Any) -> bool:
                return False

        with patch.object(sessions, "session_guard", lambda *a, **k: BusyGuard()):
            response = await self._request(
                "DELETE", "/api/desktop/chat/history", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "ConflictError")


class MarketEndpointTests(DesktopApiTestBase):
    async def test_overview_degrades_each_part_to_null(self) -> None:
        token = self._enable()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("upstream down")

        with (
            patch.object(self.desktop_api, "get_broad_summary", AsyncMock(side_effect=boom)),
            patch.object(self.desktop_api.market_history, "breadth", AsyncMock(side_effect=boom)),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/overview", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIsNone(data["broad"])
        self.assertIsNone(data["breadth"])
        # 大盘失败时温度无从计算，也置 null。
        self.assertIsNone(data["temperature"])
        self.assertIn("不可用", data["broad_error"])

    async def test_overview_temperature_unavailable_is_honest(self) -> None:
        token = self._enable()
        now_ms = int(__import__("time").time() * 1000)
        broad_raw = {
            "success": True,
            "updateTime": now_ms - 60_000,
            "data": {
                "broadMarketIndex": 100.0,
                "diffYesterdayRatio": 0.1,
                "updateTime": now_ms - 60_000,
            },
        }
        temperature_mod = importlib.import_module("src.services.market_temperature")
        unavailable = temperature_mod.TemperatureUnavailable("大盘数据暂不可用")
        breadth_mod = importlib.import_module("src.services.market_history")
        breadth = breadth_mod.BreadthSnapshot(2500, 42.0, -0.31, 12000, 8000, 1234567800)
        with (
            patch.object(self.desktop_api, "get_broad_summary", AsyncMock(return_value=broad_raw)),
            patch.object(
                self.desktop_api, "get_market_temperature", AsyncMock(return_value=unavailable)
            ),
            patch.object(
                self.desktop_api.market_history, "breadth", AsyncMock(return_value=breadth)
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/overview", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["temperature"]["available"])
        self.assertIsNone(data["broad_error"])

    async def test_overview_full_payload(self) -> None:
        token = self._enable()
        now_ms = int(__import__("time").time() * 1000)
        broad_raw = {
            "success": True,
            "updateTime": now_ms - 60_000,
            "data": {
                "broadMarketIndex": 102.5,
                "diffYesterdayRatio": 1.2,
                "diffYesterday": 1.22,
                "updateTime": now_ms - 60_000,
            },
        }
        temperature_mod = importlib.import_module("src.services.market_temperature")
        temperature = temperature_mod.MarketTemperature(
            score=-15.0,
            change_1d=-0.5,
            change_3d=-1.2,
            change_7d=-2.0,
            deviation_7d=-0.8,
            current_state="低位承压",
            detail="短线回撤",
            temperature_state="偏冷",
        )
        history_mod = importlib.import_module("src.services.market_history")
        breadth = history_mod.BreadthSnapshot(2500, 42.0, -0.31, 12000, 8000, 1234567800)
        with (
            patch.object(self.desktop_api, "get_broad_summary", AsyncMock(return_value=broad_raw)),
            patch.object(
                self.desktop_api, "get_market_temperature", AsyncMock(return_value=temperature)
            ),
            patch.object(
                self.desktop_api.market_history, "breadth", AsyncMock(return_value=breadth)
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/overview", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertAlmostEqual(data["broad"]["index"], 102.5)
        self.assertAlmostEqual(data["temperature"]["score"], -15.0)
        self.assertAlmostEqual(data["temperature"]["factors"]["change_3d"], -1.2)
        self.assertEqual(data["breadth"]["compared"], 2500)

    async def test_index_closes_filters_by_days(self) -> None:
        token = self._enable()
        closes = (
            (date(2026, 1, 1), 100.0),
            (date(2026, 8, 20), 101.0),
            (date(2026, 8, 21), 103.0),
        )
        index_store = importlib.import_module("src.services.index_history").index_history_store
        with patch.object(index_store, "load_closes", AsyncMock(return_value=closes)):
            response = await self._request(
                "GET", "/api/desktop/market/index-closes?days=7", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        points = response.json()["data"]["points"]
        self.assertEqual([point["date"] for point in points], ["2026-08-20", "2026-08-21"])
        self.assertEqual(points[-1]["close"], 103.0)

    async def test_dip_reuses_qq_pipeline(self) -> None:
        token = self._enable()
        chat_pkg = importlib.import_module("src.plugins.chat")
        with patch.object(chat_pkg, "_dip_report", AsyncMock(return_value="📉 抄底卡")):
            response = await self._request("GET", "/api/desktop/market/dip", headers=_auth(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["report"], "📉 抄底卡")

    async def test_rankings_rejects_unknown_board(self) -> None:
        token = self._enable()
        response = await self._request(
            "GET",
            "/api/desktop/market/rankings?board=%E7%83%AD%E5%BA%A6%E6%A6%9C",
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "InvalidParameterError")

    async def test_rankings_passes_through_parameters(self) -> None:
        token = self._enable()
        render = AsyncMock(return_value="📊 【24小时涨跌榜】")
        with patch.object(self.desktop_api.market_history, "render_ranking", render):
            response = await self._request(
                "GET",
                "/api/desktop/market/rankings?board=%E6%B6%A8%E8%B7%8C%E6%A6%9C&category=%E5%88%80%E5%85%B7&limit=15",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("涨跌榜", response.json()["data"]["report"])
        render.assert_awaited_once_with("涨跌榜", "刀具", 15)

    async def test_prediction_reuses_render_column(self) -> None:
        token = self._enable()
        render = AsyncMock(return_value="🔮 预测卡")
        with patch.object(self.desktop_api.market_history, "render_column", render):
            response = await self._request(
                "GET", "/api/desktop/market/prediction", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["report"], "🔮 预测卡")
        render.assert_awaited_once_with("预测")

    async def test_search_merges_catalog_sources(self) -> None:
        token = self._enable()
        catalog_mod = importlib.import_module("src.services.market_catalog")
        store_mod = importlib.import_module("src.services.market_catalog_store")
        durable = SimpleNamespace(
            market_name="AK-47 | Redline (Field-Tested)", display_name="AK-47 | 红线 (久经)"
        )
        live = ()
        exact = MagicMock(return_value=None)
        with (
            patch.object(
                store_mod, "get_market_catalog_entries", AsyncMock(return_value=(durable,))
            ),
            patch.object(catalog_mod, "search_market_catalog", AsyncMock(return_value=live)),
            patch.object(
                importlib.import_module("src.plugins.chat.market"),
                "resolve_catalog_name",
                exact,
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/search?q=%E7%BA%A2%E7%BA%BF", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["results"][0]["display_name"], "AK-47 | 红线 (久经)")

    async def test_search_prefix_query_with_rank_ties_does_not_crash(self) -> None:
        token = self._enable()
        catalog_mod = importlib.import_module("src.services.market_catalog")
        store_mod = importlib.import_module("src.services.market_catalog_store")
        entries = tuple(
            SimpleNamespace(market_name=name, display_name=name)
            for name in (
                "AK-47 | Redline (Field-Tested)",
                "AK-47 | Asiimov (Field-Tested)",
                "AWP | Asiimov (Field-Tested)",
            )
        )
        exact = MagicMock(return_value=None)
        with (
            patch.object(store_mod, "get_market_catalog_entries", AsyncMock(return_value=entries)),
            patch.object(catalog_mod, "search_market_catalog", AsyncMock(return_value=())),
            patch.object(
                importlib.import_module("src.plugins.chat.market"),
                "resolve_catalog_name",
                exact,
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/search?q=AK-47", headers=_auth(token)
            )
        # Rank ties on the prefix tier must never compare catalog entries.
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(
            {item["market_name"] for item in data["results"]},
            {"AK-47 | Redline (Field-Tested)", "AK-47 | Asiimov (Field-Tested)"},
        )

    async def test_search_mixed_alias_query_falls_back_to_token_match(self) -> None:
        token = self._enable()
        catalog_mod = importlib.import_module("src.services.market_catalog")
        store_mod = importlib.import_module("src.services.market_catalog_store")
        entries = tuple(
            SimpleNamespace(market_name=name, display_name=display)
            for name, display in (
                (
                    "AK-47 | Redline (Field-Tested)",
                    "AK-47 | 红线 (久经)",
                ),
                ("AWP | Asiimov (Field-Tested)", "AWP | 二西莫夫 (久经)"),
            )
        )
        exact = MagicMock(return_value=None)
        with (
            patch.object(store_mod, "get_market_catalog_entries", AsyncMock(return_value=entries)),
            patch.object(catalog_mod, "search_market_catalog", AsyncMock(return_value=())),
            patch.object(
                importlib.import_module("src.plugins.chat.market"),
                "resolve_catalog_name",
                exact,
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/search?q=AK%E7%BA%A2%E7%BA%BF", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["market_name"], "AK-47 | Redline (Field-Tested)")

    async def test_item_structures_quotes_average_change(self) -> None:
        token = self._enable()
        self._owner_env()
        average_data = {
            "success": True,
            "data": {"avgPrice": 12.9, "dataList": [{"platform": "BUFF", "avgPrice": 13.1}]},
        }
        chat_pkg = importlib.import_module("src.plugins.chat")
        quotes_payload = [
            {
                "platform": "BUFF",
                "sell_price": 12.34,
                "sell_count": 88,
                "bid_price": 11.0,
                "bid_count": 5,
                "timestamp": 1755000000.0,
            }
        ]
        with (
            patch.object(chat_pkg, "resolve_name", AsyncMock(return_value="AK-47 | Redline")),
            patch.object(self.desktop_api, "_item_quotes", AsyncMock(return_value=quotes_payload)),
            patch.object(self.desktop_api, "st_get", AsyncMock(return_value=average_data)),
            patch.object(
                self.desktop_api.market_history,
                "item_change",
                AsyncMock(return_value=(12.34, 2.5)),
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/item?name=%E7%BA%A2%E7%BA%BF", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["name"], "AK-47 | Redline")
        self.assertEqual(data["quotes"][0]["sell_price"], 12.34)
        self.assertAlmostEqual(data["average"]["overall"], 12.9)
        self.assertAlmostEqual(data["average"]["buff"], 13.1)
        self.assertAlmostEqual(data["change_24h"]["change_percent"], 2.5)

    async def test_kline_validates_before_returning_points(self) -> None:
        token = self._enable()
        valid_points = [
            (1754980000 + i * 86400, 10.0 + i, 10.5 + i, 11.0 + i, 9.5 + i) for i in range(30)
        ]
        market_pkg = importlib.import_module("src.plugins.chat.market")
        post = AsyncMock(return_value={"success": True})
        with (
            patch.object(self.desktop_api, "st_post", post),
            patch.object(market_pkg, "fresh_kline_points", return_value=(valid_points, None)),
        ):
            response = await self._request(
                "GET",
                "/api/desktop/market/kline?name=AK-47%20Redline",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["count"], 30)
        self.assertEqual(len(data["points"][0]), 5)
        args = post.call_args.args
        self.assertEqual(args[0], "/open/cs2/item/v1/kline")
        self.assertEqual(args[1]["marketHashName"], "AK-47 Redline")
        self.assertNotIn("specialStyle", args[1])

    async def test_kline_fail_closed_on_bad_upstream(self) -> None:
        token = self._enable()
        market_pkg = importlib.import_module("src.plugins.chat.market")
        with (
            patch.object(self.desktop_api, "st_post", AsyncMock(return_value={"success": True})),
            patch.object(
                market_pkg,
                "fresh_kline_points",
                return_value=(None, "⚠️ 市场K线已过期，本次拒绝展示。"),
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/kline?name=x", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "UpstreamUnavailableError")
        self.assertIn("过期", body["message"])

    async def test_kline_rejects_unknown_style(self) -> None:
        token = self._enable()
        response = await self._request(
            "GET", "/api/desktop/market/kline?name=x&style=notastyle", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 400)

    async def test_watchlist_quotes_local_snapshot_without_refresh(self) -> None:
        token = self._enable()
        self._owner_env()
        watchlists = importlib.import_module("src.state.runtime").watchlists
        items = [SimpleNamespace(display_name="AK-47 | Redline")]
        snapshot = {
            "latest_price": 12.3,
            "data_time": 1755000000.0,
            "week_average": 12.0,
            "week_change": 1.0,
            "samples": 40.0,
        }
        snap = AsyncMock(return_value=snapshot)
        with (
            patch.object(watchlists, "list_owner", AsyncMock(return_value=items)),
            patch.object(self.desktop_api.market_history, "item_snapshot", snap),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/watchlist-quotes", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["items"][0]["snapshot"]["latest_price"], 12.3)
        self.assertIsNone(data["items"][0]["quotes"])
        snap.assert_awaited_once_with("AK-47 | Redline")


class AlertAndPersonaEndpointTests(DesktopApiTestBase):
    async def test_alert_create_maps_quota_to_capacity_error(self) -> None:
        token = self._enable()
        self._owner_env()
        result = SimpleNamespace(created=False, alert=None, quota_exceeded=True)
        with patch.object(
            self.desktop_api.alert_repository, "create", AsyncMock(return_value=result)
        ):
            response = await self._request(
                "POST",
                "/api/desktop/alerts",
                json={"name": "AK-47 | Redline", "direction": "below", "target": 9.9},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "CapacityError")

    async def test_alert_create_returns_created_record_for_owner(self) -> None:
        token = self._enable()
        self._owner_env()
        alert = SimpleNamespace(
            id="abc123",
            market_name="AK-47 | Redline",
            direction="below",
            target_price_cents=int(Decimal("9.90") * 100),
            status="pending",
            created_at=1755000000,
        )
        result = SimpleNamespace(created=True, alert=alert, quota_exceeded=False)
        create = AsyncMock(return_value=result)
        with patch.object(self.desktop_api.alert_repository, "create", create):
            response = await self._request(
                "POST",
                "/api/desktop/alerts",
                json={"name": " AK-47 | Redline ", "direction": "below", "target": 9.9},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["id"], "abc123")
        self.assertEqual(create.call_args.args[0], 10001)
        self.assertEqual(create.call_args.kwargs["direction"], "below")

    async def test_alert_create_rejects_bad_direction(self) -> None:
        token = self._enable()
        self._owner_env()
        response = await self._request(
            "POST",
            "/api/desktop/alerts",
            json={"name": "x", "direction": "sideways", "target": 1.0},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 400)

    async def test_persona_profile_merges_style_and_memories(self) -> None:
        token = self._enable()
        self._owner_env()
        style_ctx = SimpleNamespace(
            concise=1, warm=2, teasing=0, proactive=0, catchphrase=-1, direct=1
        )
        profile = SimpleNamespace(learning_enabled=True, style=style_ctx, updated_at=1755000000)
        memories = (
            SimpleNamespace(id=1, kind="nickname", content="小塔菲", created_at=1, updated_at=1),
            SimpleNamespace(
                id=2, kind="note", content="主人喜欢简洁回复", created_at=2, updated_at=2
            ),
        )
        get_style = AsyncMock(return_value=profile)
        list_memories = AsyncMock(return_value=memories)
        with (
            patch.object(self.desktop_api.persona_styles, "get", get_style),
            patch.object(self.desktop_api.persona_memories, "list", list_memories),
        ):
            response = await self._request(
                "GET", "/api/desktop/persona/profile", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["nickname"], "小塔菲")
        self.assertEqual(data["style"]["warm"], 2)
        self.assertEqual(len(data["memories"]), 1)
        self.assertEqual(data["memories"][0]["content"], "主人喜欢简洁回复")
        scope_arg = get_style.call_args.args[0]
        self.assertEqual((scope_arg.owner_user_id, scope_arg.group_id), (10001, 0))

    async def test_persona_style_learning_toggle(self) -> None:
        token = self._enable()
        self._owner_env()
        style_ctx = SimpleNamespace(
            concise=0, warm=0, teasing=0, proactive=0, catchphrase=0, direct=0
        )
        profile = SimpleNamespace(learning_enabled=True, style=style_ctx, updated_at=1)
        set_learning = AsyncMock(return_value=profile)
        with (
            patch.object(self.desktop_api.persona_styles, "set_learning", set_learning),
            patch.object(self.desktop_api.persona_styles, "get", AsyncMock(return_value=profile)),
        ):
            on = await self._request(
                "POST",
                "/api/desktop/persona/style",
                json={"action": "learning_on"},
                headers=_auth(token),
            )
            off = await self._request(
                "POST",
                "/api/desktop/persona/style",
                json={"action": "learning_off"},
                headers=_auth(token),
            )
        self.assertEqual(on.status_code, 200)
        self.assertTrue(on.json()["data"]["learning_enabled"])
        self.assertEqual(off.status_code, 200)
        set_learning.assert_any_await(ANY, True)
        set_learning.assert_any_await(ANY, False)

    async def test_persona_style_undo_conflict_when_nothing_to_undo(self) -> None:
        token = self._enable()
        self._owner_env()
        with patch.object(self.desktop_api.persona_styles, "undo", AsyncMock(return_value=None)):
            response = await self._request(
                "POST",
                "/api/desktop/persona/style",
                json={"action": "undo"},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "ConflictError")

    async def test_persona_memory_capacity_maps_to_429(self) -> None:
        token = self._enable()
        self._owner_env()
        repo_mod = importlib.import_module("src.state.persona_memory_repository")
        capacity_error = repo_mod.PersonaMemoryCapacityError("当前范围的长期记忆已满")
        with patch.object(
            self.desktop_api.persona_memories,
            "remember_note",
            AsyncMock(side_effect=capacity_error),
        ):
            response = await self._request(
                "POST",
                "/api/desktop/persona/memory",
                json={"content": "记住我喜欢简洁"},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"], "CapacityError")

    async def test_persona_memory_delete_missing_index_is_404(self) -> None:
        token = self._enable()
        self._owner_env()
        with patch.object(
            self.desktop_api.persona_memories, "delete_at", AsyncMock(return_value=None)
        ):
            response = await self._request(
                "DELETE", "/api/desktop/persona/memory?index=3", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "NotFoundError")


class NewsAnalyticsInventoryTests(DesktopApiTestBase):
    async def test_news_official_combines_text_and_structured(self) -> None:
        token = self._enable()
        official_mod = importlib.import_module("src.services.official_cs2")
        update = SimpleNamespace(
            title="更新日志", summary="修复", url="https://example.com", published_at=None
        )
        available = SimpleNamespace(is_available=True, value=[update], reason=None)
        unavailable = SimpleNamespace(is_available=False, value=None, reason=None)
        report = SimpleNamespace(updates=available, upcoming=unavailable, completed=unavailable)
        with (
            patch.object(official_mod.official_cs2, "report", AsyncMock(return_value=report)),
            patch.object(official_mod, "format_official_report", return_value="🎮 【官方更新】"),
        ):
            response = await self._request(
                "GET", "/api/desktop/news/official", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["text"], "🎮 【官方更新】")
        self.assertEqual(data["updates"][0]["title"], "更新日志")
        self.assertIsNone(data["upcoming"])

    async def test_news_reddit_unread_then_mark_read(self) -> None:
        token = self._enable()
        reddit_repo = importlib.import_module("src.state.runtime").reddit_posts
        post = SimpleNamespace(
            post_id="t3_abc",
            subreddit="CS2Leak",
            title="新箱爆料",
            summary="",
            author="x",
            url="https://reddit.com/r/t3_abc",
            published_at=1755000000,
        )
        with patch.object(
            reddit_repo, "undelivered_posts", AsyncMock(return_value=(post,))
        ) as undelivered:
            unread_response = await self._request(
                "GET", "/api/desktop/news/reddit?unread=1", headers=_auth(token)
            )
        self.assertEqual(unread_response.status_code, 200)
        self.assertTrue(unread_response.json()["data"]["posts"][0]["unread"])
        undelivered.assert_awaited_once()
        mark = AsyncMock(return_value=1)
        with patch.object(reddit_repo, "mark_delivered", mark):
            read_response = await self._request(
                "POST",
                "/api/desktop/news/reddit/read",
                json={"post_ids": ["t3_abc"]},
                headers=_auth(token),
            )
        self.assertEqual(read_response.json()["data"]["marked"], 1)

    async def test_analytics_usage_aggregates_readonly_sql(self) -> None:
        token = self._enable()

        async def read(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            del params
            if "GROUP BY day, feature" in sql:
                return [{"day": "2026-08-21", "feature": "chat", "calls": 5}]
            if "GROUP BY user_id" in sql:
                return [{"user_id": 10001, "calls": 9}]
            return [{"total": 14}]

        with patch.object(self.desktop_api.usage_log.database, "read", side_effect=read):
            response = await self._request(
                "GET", "/api/desktop/analytics/usage?days=30", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["daily"][0]["calls"], 5)
        self.assertEqual(data["top_users"][0]["user_id"], 10001)
        self.assertEqual(data["total"], 14)

    async def test_analytics_alerts_sections(self) -> None:
        token = self._enable()

        async def read(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            del params
            if "GROUP BY status" in sql:
                return [{"status": "delivered", "count": 3}]
            if "delivered_at IS NOT NULL" in sql:
                return [{"day": "2026-08-21", "count": 2}]
            return [{"day": "2026-08-22", "count": 1}]

        with patch.object(self.desktop_api.usage_log.database, "read", side_effect=read):
            response = await self._request(
                "GET", "/api/desktop/analytics/alerts?days=30", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["by_status"][0]["count"], 3)
        self.assertEqual(data["delivered_daily"][0]["count"], 2)
        self.assertEqual(data["failed_daily"][0]["count"], 1)

    async def test_inventory_state_returns_three_sections(self) -> None:
        token = self._enable()
        self._owner_env("10001")

        captured: list[tuple[str, tuple[Any, ...]]] = []

        async def read(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
            captured.append((sql, params))
            if "substr(e.message_text" in sql:
                return [{"id": "e1", "summary": "新增物品"}]
            if "s.owner_user_id, s.destination_type" in sql:
                return []
            if "consecutive_failures" in sql:
                return [{"steam_id": "76561198000000000", "nickname": "主号"}]
            raise AssertionError(f"unexpected query: {sql}")

        with patch.object(self.desktop_api.inventory_monitors.database, "read", side_effect=read):
            response = await self._request(
                "GET", "/api/desktop/inventory/state", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["accounts"][0]["steam_id"], "76561198000000000")
        self.assertEqual(data["subscriptions"], [])
        self.assertEqual(data["events"][0]["id"], "e1")
        # P1 privacy boundary: every section query is bound to the owner.
        self.assertTrue(captured, "expected at least one query")
        for sql, params in captured:
            self.assertIn(10001, tuple(params), f"query not owner-scoped: {sql}")

    async def test_inventory_state_without_owner_returns_empty(self) -> None:
        token = self._enable()
        self._owner_env(None)
        # Owner resolution falls back to superusers; clear them too so the
        # no-owner fail-closed branch is actually exercised.
        driver_config = nonebot.get_driver().config
        original_superusers = driver_config.superusers
        driver_config.superusers = set()
        try:

            async def read(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
                del sql, params
                raise AssertionError("no query should run without a resolvable owner")

            with patch.object(
                self.desktop_api.inventory_monitors.database, "read", side_effect=read
            ):
                response = await self._request(
                    "GET", "/api/desktop/inventory/state", headers=_auth(token)
                )
        finally:
            driver_config.superusers = original_superusers
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data, {"accounts": [], "subscriptions": [], "events": []})

    async def test_inventory_trajectory_delegates_to_service_with_owner(self) -> None:
        token = self._enable()
        self._owner_env("10001")

        calls: list[tuple[Any, ...]] = []

        async def trajectory(owner, dest_type, dest_id, steam_id, days=None):
            calls.append((owner, dest_type, dest_id, steam_id, days))
            return "📦 【库存轨迹】测试"

        with patch.object(
            self.desktop_module.inventory_monitor_service, "trajectory", side_effect=trajectory
        ):
            response = await self._request(
                "GET",
                "/api/desktop/inventory/trajectory?steam_id=76561198000000000&days=7",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["ok_text"], "📦 【库存轨迹】测试")
        self.assertEqual(calls, [(10001, "private", 10001, "76561198000000000", 7)])

    async def test_inventory_trajectory_without_owner_fails_closed(self) -> None:
        token = self._enable()
        self._owner_env(None)
        driver_config = nonebot.get_driver().config
        original_superusers = driver_config.superusers
        driver_config.superusers = set()
        try:

            async def trajectory(*_args: Any, **_kwargs: Any):
                raise AssertionError("trajectory must not run without an owner")

            with patch.object(
                self.desktop_module.inventory_monitor_service, "trajectory", side_effect=trajectory
            ):
                response = await self._request(
                    "GET",
                    "/api/desktop/inventory/trajectory?steam_id=76561198000000000",
                    headers=_auth(token),
                )
        finally:
            driver_config.superusers = original_superusers
        self.assertEqual(response.status_code, 200)
        self.assertIn("未配置桌面主人", response.json()["data"]["ok_text"])

    async def test_inventory_profile_cache_hit_and_miss(self) -> None:
        token = self._enable()
        self._owner_env("10001")

        async def has_subscription(*_args: Any) -> bool:
            return True

        with (
            patch.object(
                self.desktop_module.inventory_monitor_service.repository,
                "has_subscription",
                side_effect=has_subscription,
            ),
            patch(
                "src.services.hoard_report.load_recent_report",
                return_value={
                    "payload_json": "【囤货画像】AI\n机器推断，非结论",
                    "model": "deepseek-v4-flash",
                    "generated_at": 1_787_795_200,
                },
            ),
        ):
            response = await self._request(
                "GET",
                "/api/desktop/inventory/profile?steam_id=76561198000000000",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["cached"])
        self.assertIn("机器推断，非结论", data["ok_text"])
        self.assertEqual(data["model"], "deepseek-v4-flash")

        # 未命中：fail-soft 引导去 QQ 生成，面板绝不烧 token
        with (
            patch.object(
                self.desktop_module.inventory_monitor_service.repository,
                "has_subscription",
                side_effect=has_subscription,
            ),
            patch("src.services.hoard_report.load_recent_report", return_value=None),
        ):
            response = await self._request(
                "GET",
                "/api/desktop/inventory/profile?steam_id=76561198000000000",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["cached"])
        self.assertIn("囤货画像", data["ok_text"])

    async def test_inventory_profile_rejects_unsubscribed_owner(self) -> None:
        token = self._enable()
        self._owner_env("10001")

        async def has_subscription(*_args: Any) -> bool:
            return False

        with (
            patch.object(
                self.desktop_module.inventory_monitor_service.repository,
                "has_subscription",
                side_effect=has_subscription,
            ),
            patch("src.services.hoard_report.load_recent_report") as loader,
        ):
            response = await self._request(
                "GET",
                "/api/desktop/inventory/profile?steam_id=76561198000000000",
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["cached"])
        self.assertIn("库存监控", data["ok_text"])
        loader.assert_not_called()  # 越权时连缓存查询都不发起


class InventoryRealSchemaTests(unittest.IsolatedAsyncioTestCase):
    """回归护栏：inventory/state 的只读 SQL 必须与真实 schema 列对齐。

    此前的 mock 测试放过「subscriptions 表没有 nickname 列」的实机
    OperationalError（500），因此这里直接对沙箱库执行真实 SQL。
    """

    @classmethod
    def setUpClass(cls) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        cls.desktop_api = importlib.import_module("src.plugins.desktop_api")
        cls.desktop_module = cls.desktop_api
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        cls.app = FastAPI()
        cls.app.include_router(cls.desktop_api.router)

    def _enable(self) -> str:
        token = _token()
        enabled = patch.object(self.desktop_api, "desktop_api_enabled", return_value=True)
        enabled.start()
        self.addCleanup(enabled.stop)
        auth = patch.object(self.admin_api, "get_admin_api_token", return_value=token)
        auth.start()
        self.addCleanup(auth.stop)
        return token

    async def _request(self, method: str, path: str, **kwargs: Any):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_inventory_state_runs_real_sql_against_sandbox_schema(self) -> None:
        token = self._enable()
        repo = self.desktop_api.inventory_monitors
        await repo.initialize()  # 沙箱库建表（conftest 已重定向 TAFFY_STATE_DB）
        try:
            response = await self._request(
                "GET", "/api/desktop/inventory/state?event_limit=5", headers=_auth(token)
            )
        finally:
            pass
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["data"]["accounts"], list)
        self.assertIsInstance(body["data"]["subscriptions"], list)
        self.assertIsInstance(body["data"]["events"], list)


class AuditTests(DesktopApiTestBase):
    async def test_chat_writes_usage_log_row_truncated(self) -> None:
        token = self._enable()
        self._owner_env()
        llm_service = importlib.import_module("src.services.llm").chat_service

        async def fake_chat(message: str, session_id: str, **_: Any) -> str:
            return "喵"

        record = AsyncMock(return_value=1)
        with (
            patch.object(llm_service, "chat", side_effect=fake_chat),
            patch.object(self.desktop_api.usage_log, "record", record),
        ):
            response = await self._request(
                "POST",
                "/api/desktop/chat",
                json={"message": "长" * 500},
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        kwargs = record.await_args.kwargs
        self.assertEqual(kwargs["feature"], "desktop.chat")
        self.assertEqual(kwargs["command"], "/api/desktop/chat")
        self.assertLessEqual(len(kwargs["input_text"]), 200)
        self.assertEqual(kwargs["user_id"], 10001)


class KlineLocalFallbackTests(DesktopApiTestBase):
    """A3：上游 K 线失败时的本地快照聚合兜底。"""

    ITEM = "Local Fallback Item (Field-Tested)"

    async def _seed_snapshots(self, name: str, days: int) -> None:
        import os
        import time as time_mod
        from pathlib import Path

        from src.services.local_store import connect_local_database

        history_path = Path(os.environ["TAFFY_MARKET_HISTORY_DB"])
        base = ((int(time_mod.time()) - days * 86400) // 3600) * 3600
        connection = connect_local_database(
            history_path, timeout_seconds=5.0, label="kline fallback test"
        )
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS quote_snapshots ("
                "bucket_at INTEGER NOT NULL, market_name TEXT NOT NULL, "
                "sell_price REAL NOT NULL, bid_price REAL, sell_count INTEGER, "
                "bid_count INTEGER, data_time INTEGER NOT NULL, "
                "PRIMARY KEY (bucket_at, market_name))"
            )
            for day in range(days):
                connection.execute(
                    "INSERT OR REPLACE INTO quote_snapshots "
                    "(bucket_at, market_name, sell_price, data_time) VALUES (?, ?, ?, ?)",
                    (base + day * 86400, name, 100.0 + day, base),
                )
            connection.commit()
        finally:
            connection.close()

    async def test_upstream_failure_falls_back_to_local_snapshots(self) -> None:
        token = self._enable()
        await self._seed_snapshots(self.ITEM, days=20)
        with patch.object(self.desktop_api, "st_post", AsyncMock(side_effect=RuntimeError("boom"))):
            response = await self._request(
                "GET",
                "/api/desktop/market/kline?name=" + self.ITEM.replace(" ", "%20"),
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["source"], "local_snapshot")
        self.assertEqual(data["count"], 20)
        self.assertEqual(len(data["points"][0]), 5)
        self.assertIn("本地快照聚合", data["note"])

    async def test_insufficient_local_samples_still_fails_closed(self) -> None:
        token = self._enable()
        item = "Sparse Local Item (Factory New)"
        await self._seed_snapshots(item, days=3)
        with patch.object(self.desktop_api, "st_post", AsyncMock(side_effect=RuntimeError("boom"))):
            response = await self._request(
                "GET",
                "/api/desktop/market/kline?name=" + item.replace(" ", "%20"),
                headers=_auth(token),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "UpstreamUnavailableError")

    async def test_upstream_success_marks_source(self) -> None:
        token = self._enable()
        market_pkg = importlib.import_module("src.plugins.chat.market")
        points = [(1754980000 + i * 86400, 10.0, 10.5, 11.0, 9.5) for i in range(20)]
        with (
            patch.object(self.desktop_api, "st_post", AsyncMock(return_value={"success": True})),
            patch.object(market_pkg, "fresh_kline_points", return_value=(points, None)),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/kline?name=AK-47%20Redline", headers=_auth(token)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["source"], "upstream")


class UserCenterTests(DesktopApiTestBase):
    """批次B：按人看使用情况 + 隐私红线 + 数据管理动作。"""

    _users = (987001, 987002, 987003)

    async def _insert_many(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        for row in rows:
            await self._db.write(sql, row)

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from src.state.runtime import database

        self._db = database
        await self._db.initialize()
        import time as time_mod

        now = int(time_mod.time())
        rows = []
        for index, uid in enumerate(self._users):
            for _ in range(3 if uid == 987001 else 1):
                rows.append((uid, 111, "chat", "大盘", "大盘", now - index))
        await self._insert_many(
            "INSERT INTO usage_log (user_id, group_id, feature, command, input_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    async def asyncTearDown(self) -> None:
        await self._db.write("DELETE FROM usage_log WHERE user_id IN (?, ?, ?)", self._users)
        await self._db.write(
            "DELETE FROM session_messages WHERE session_key LIKE ? ESCAPE '\\'",
            ("g\\_%\\_987003",),
        )
        await self._db.write("DELETE FROM session_messages WHERE session_key = ?", ("p_987003",))
        await super().asyncTearDown()

    async def test_users_list_and_search_filter(self) -> None:
        token = self._enable()
        response = await self._request("GET", "/api/desktop/users", headers=_auth(token))
        self.assertEqual(response.status_code, 200)
        found = {u["user_id"]: u for u in response.json()["data"]["users"]}
        self.assertIn(987001, found)
        self.assertEqual(found[987001]["total_calls_30d"], 3)
        self.assertEqual(found[987001]["features"][0]["feature"], "chat")
        response = await self._request("GET", "/api/desktop/users?q=98700", headers=_auth(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["data"]["users"]), 3)
        response = await self._request("GET", "/api/desktop/users?q=abc", headers=_auth(token))
        self.assertEqual(response.status_code, 400)

    async def test_timeline_cursor_pagination(self) -> None:
        token = self._enable()
        import time as time_mod

        now = int(time_mod.time())
        await self._insert_many(
            "INSERT INTO usage_log (user_id, group_id, feature, command, input_text, created_at) "
            "VALUES (?, 0, 'market', '查询', '查询', ?)",
            [(987002, now - i) for i in range(60)],
        )
        response = await self._request(
            "GET", "/api/desktop/users/987002/timeline?limit=50", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data["events"]), 50)
        self.assertGreater(data["next_cursor"], 0)
        response2 = await self._request(
            "GET",
            f"/api/desktop/users/987002/timeline?limit=50&cursor={data['next_cursor']}",
            headers=_auth(token),
        )
        data2 = response2.json()["data"]
        self.assertEqual(len(data2["events"]), 11)
        self.assertEqual(data2["next_cursor"], 0)

    async def test_chat_log_privacy_red_line(self) -> None:
        import time as time_mod

        time = time_mod
        token = self._enable()
        self._owner_env()
        await self._insert_many(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("p_987003", "user", "私聊秘密", int(time.time()) - 60),
                ("g_111_987003", "user", "群聊公开", int(time.time()) - 60),
            ],
        )
        # 群聊：直接可见。
        response = await self._request(
            "GET", "/api/desktop/users/987003/chat-log?scope=group", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["count"], 1)
        # 他人私聊：默认 403 红线。
        response = await self._request(
            "GET", "/api/desktop/users/987003/chat-log?scope=private", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "ForbiddenError")
        # 二次确认后放行 + 警示。
        response = await self._request(
            "GET",
            "/api/desktop/users/987003/chat-log?scope=private&confirm_view_private=1",
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["count"], 1)
        self.assertIn("他人私聊", body["privacy_notice"])
        # 主人自己的私聊：无需确认。
        response = await self._request(
            "GET", "/api/desktop/users/10001/chat-log?scope=private", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["privacy_notice"])

    async def test_overview_and_management_actions(self) -> None:
        import time as time_mod

        token = self._enable()
        self._owner_env()
        from src.state.runtime import watchlists

        await watchlists.add(987003, "测试自选A")
        await watchlists.add(987003, "测试自选B")
        response = await self._request(
            "GET", "/api/desktop/users/987003/overview", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["is_owner"])
        self.assertEqual(data["watch_count"], 2)
        self.assertIsNotNone(data["persona"])

        await self._insert_many(
            "INSERT INTO session_messages (session_key, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            [("p_987003", "user", "待清理", int(time_mod.time()) - 60)],
        )
        response = await self._request(
            "POST", "/api/desktop/users/987003/clear-session", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["cleared_sessions"], 1)

        response = await self._request(
            "POST", "/api/desktop/users/987003/clear-watchlist", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["removed"], 2)
        remaining = await watchlists.list_owner(987003)
        self.assertEqual(len(remaining), 0)


class ModerationTests(DesktopApiTestBase):
    """批次C：OneBot 群务管控。"""

    @staticmethod
    def _fake_failure(retcode: int, wording: str) -> Exception:
        class FakeActionFailed(Exception):
            def __init__(self) -> None:
                super().__init__(wording)
                self.info = {"retcode": retcode, "wording": wording}

        return FakeActionFailed()

    def _patch_bot(self, bot: Any) -> None:
        scheduler = importlib.import_module("src.plugins.scheduler")
        patcher = patch.object(scheduler, "connected_bot", lambda: bot)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_groups_and_members(self) -> None:
        token = self._enable()
        bot = MagicMock()
        bot.get_group_list = AsyncMock(return_value=[{"group_id": 123, "group_name": "测试群"}])
        bot.get_group_member_list = AsyncMock(
            return_value=[{"user_id": 987004, "nickname": "小号", "card": "卡片"}]
        )
        self._patch_bot(bot)
        response = await self._request(
            "GET", "/api/desktop/moderation/groups", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["groups"][0]["group_id"], 123)
        response = await self._request(
            "GET", "/api/desktop/moderation/groups/123/members", headers=_auth(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["members"][0]["user_id"], 987004)

    async def test_mute_requires_connected_bot(self) -> None:
        token = self._enable()
        self._patch_bot(None)
        response = await self._request(
            "POST",
            "/api/desktop/moderation/mute",
            json={"group_id": 123, "user_id": 987004, "minutes": 10},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("未连接", response.json()["message"])

    async def test_mute_maps_privilege_failure(self) -> None:
        token = self._enable()
        bot = MagicMock()
        bot.set_group_ban = AsyncMock(side_effect=self._fake_failure(1024, "群主权限不足"))
        self._patch_bot(bot)
        response = await self._request(
            "POST",
            "/api/desktop/moderation/mute",
            json={"group_id": 123, "user_id": 987004, "minutes": 10},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("管理权限", response.json()["message"])

    async def test_mute_kick_recall_success_params(self) -> None:
        token = self._enable()
        bot = MagicMock()
        bot.set_group_ban = AsyncMock(return_value=None)
        bot.set_group_kick = AsyncMock(return_value=None)
        bot.delete_msg = AsyncMock(return_value=None)
        self._patch_bot(bot)
        response = await self._request(
            "POST",
            "/api/desktop/moderation/mute",
            json={"group_id": 123, "user_id": 987004, "minutes": 10},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 200)
        bot.set_group_ban.assert_awaited_once_with(group_id=123, user_id=987004, duration=600)
        response = await self._request(
            "POST",
            "/api/desktop/moderation/kick",
            json={"group_id": 123, "user_id": 987004, "reject_add_request": True},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 200)
        bot.set_group_kick.assert_awaited_once_with(
            group_id=123, user_id=987004, reject_add_request=True
        )
        response = await self._request(
            "POST",
            "/api/desktop/moderation/recall",
            json={"message_id": 556677, "user_id": 987004},
            headers=_auth(token),
        )
        self.assertEqual(response.status_code, 200)
        bot.delete_msg.assert_awaited_once_with(message_id=556677)


class HoardEndpointTests(DesktopApiTestBase):
    """窗口B：挂刀端点薄门面（快照缺失/单品未跟踪时如实 unavailable）。"""

    def setUp(self) -> None:
        super().setUp()
        self.token = self._enable()

    async def test_hoard_overview_returns_snapshot(self) -> None:
        snapshot = SimpleNamespace(
            fetched_at=1_800_000_000,
            dump_name="2026-05-20-12-15.zip",
            dump_time=1_779_249_738,
            item_count=4000,
            cs2_count=3284,
            median_ratio=0.7785,
            p25_ratio=0.7285,
            p75_ratio=0.8066,
        )
        with patch.object(hoard_index_store, "latest_snapshot", AsyncMock(return_value=snapshot)):
            response = await self._request(
                "GET", "/api/desktop/market/hoard", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["available"])
        self.assertEqual(body["data"]["median_ratio"], 0.7785)
        self.assertEqual(body["data"]["dump_name"], "2026-05-20-12-15.zip")

    async def test_hoard_overview_unavailable_without_snapshot(self) -> None:
        with patch.object(hoard_index_store, "latest_snapshot", AsyncMock(return_value=None)):
            response = await self._request(
                "GET", "/api/desktop/market/hoard", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["data"]["available"])
        self.assertIn("尚未同步", body["data"]["reason"])

    async def test_hoard_item_ratio_and_untracked(self) -> None:
        with patch.object(hoard_index_store, "item_ratio", AsyncMock(return_value=0.72)):
            response = await self._request(
                "GET",
                "/api/desktop/market/hoard?item=AK-47%20%7C%20Redline%20(Field-Tested)",
                headers=_auth(self.token),
            )
            self.assertTrue(response.json()["data"]["available"])
            self.assertEqual(response.json()["data"]["hoard_ratio"], 0.72)

        with patch.object(hoard_index_store, "item_ratio", AsyncMock(return_value=None)):
            response = await self._request(
                "GET", "/api/desktop/market/hoard?item=Unknown", headers=_auth(self.token)
            )
            body = response.json()
            self.assertFalse(body["data"]["available"])
            self.assertIn("未被挂刀快照跟踪", body["data"]["reason"])


if __name__ == "__main__":
    unittest.main()


class OwnerUnavailableTextTests(DesktopApiTestBase):
    """M-110 的语义扩展：``None`` 现在有**两个**原因，文案必须区分。

    否则运维会去检查一个**已经配好**的键 —— 本批第六例「同一事实两个口径」，
    且是由 M-110 扩大 ``None`` 的含义**新触发**的（改之前 ``None`` 只代表 missing，
    旧文案是对的）。
    """

    def _ambiguous(self) -> None:
        """让 owner 解析落到 ambiguous：多个 superusers 且未显式配置。"""

        self._owner_env(None)
        driver_config = nonebot.get_driver().config
        original = driver_config.superusers
        driver_config.superusers = {"9999", "20002"}
        self.addCleanup(lambda: setattr(driver_config, "superusers", original))

    async def test_trajectory_text_is_actionable_when_ambiguous(self) -> None:
        token = self._enable()
        self._ambiguous()

        response = await self._request(
            "GET",
            "/api/desktop/inventory/trajectory?steam_id=76561198000000000&days=30",
            headers=_auth(token),
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["data"]["ok_text"]
        self.assertIn("未显式配置", text)
        self.assertIn("DESKTOP_OWNER_QQ", text)
        self.assertNotIn(
            "未配置桌面主人（DESKTOP_OWNER_QQ），无法查询轨迹",
            text,
            "ambiguous 时断言「未配置」会把运维指向一个已配置的键",
        )

    async def test_missing_owner_text_still_says_not_configured(self) -> None:
        """反向对照：真的什么都没配时，文案仍是「未配置」（两种情形不得混成一种）。"""

        token = self._enable()
        self._owner_env(None)
        driver_config = nonebot.get_driver().config
        original = driver_config.superusers
        driver_config.superusers = set()
        try:
            response = await self._request(
                "GET",
                "/api/desktop/inventory/trajectory?steam_id=76561198000000000&days=30",
                headers=_auth(token),
            )
        finally:
            driver_config.superusers = original

        self.assertEqual(response.status_code, 200)
        self.assertIn("未配置桌面主人", response.json()["data"]["ok_text"])

    async def test_teach_refuses_instead_of_attributing_to_user_1(self) -> None:
        """归属不再静默写成 ``user_id=1``：拒绝并给出可执行提示。"""

        token = self._enable()
        self._ambiguous()
        repo = SimpleNamespace(
            teach=AsyncMock(return_value=(SimpleNamespace(id=1, term="6"), True)),
        )

        with patch.object(self.desktop_api, "persona_slang", repo):
            response = await self._request(
                "POST",
                "/api/desktop/persona/slang",
                json={"action": "teach", "group_id": 0, "term": "6", "meaning": "厉害"},
                headers=_auth(token),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "OwnerNotConfiguredError")
        repo.teach.assert_not_awaited()

    async def test_teach_passes_the_resolved_owner_as_created_by(self) -> None:
        """有 owner 时 ``created_by`` 必须是该 owner，不是魔数 1。"""

        token = self._enable()
        self._owner_env("20002")
        repo = SimpleNamespace(
            teach=AsyncMock(return_value=(SimpleNamespace(id=1, term="6"), True)),
        )

        with patch.object(self.desktop_api, "persona_slang", repo):
            response = await self._request(
                "POST",
                "/api/desktop/persona/slang",
                json={"action": "teach", "group_id": 0, "term": "6", "meaning": "厉害"},
                headers=_auth(token),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(repo.teach.await_args.kwargs["created_by"], 20002)

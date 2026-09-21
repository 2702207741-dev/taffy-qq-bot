"""Admin API endpoint tests — market feature status card (mock data sources)."""

from __future__ import annotations

import importlib
import secrets
import time
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from fastapi import FastAPI

from src.core.rate_limit import SlidingWindowLimiter
from src.services.market_history import HistoryStatus
from src.services.market_prediction import ScenarioStats
from src.services.market_temperature import MarketTemperature, TemperatureUnavailable


class MarketStatusEndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        app = FastAPI()
        app.include_router(cls.admin_api.router)
        cls.app = app

    async def _request(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _temperature() -> MarketTemperature:
        return MarketTemperature(
            score=-15.0,
            change_1d=-0.5,
            change_3d=-1.2,
            change_7d=-2.0,
            deviation_7d=-0.8,
            current_state="低位承压",
            detail="短线回撤，7日趋势仍弱",
            temperature_state="偏冷",
        )

    @staticmethod
    def _history_status(
        *,
        latest_count: int = 2_025,
        sample_count: int = 2_500,
        hours_collected: float = 25.0,
        ranking_overlap: int = 2_100,
        latest_bucket: int | None = None,
    ) -> HistoryStatus:
        bucket = int(time.time()) // 3600 * 3600
        return HistoryStatus(
            catalog_count=sample_count,
            latest_count=latest_count,
            latest_bucket=bucket if latest_bucket is None else latest_bucket,
            oldest_bucket=bucket - 30 * 3600,
            hours_collected=hours_collected,
            ranking_overlap=ranking_overlap,
        )

    async def test_market_status_requires_bearer_token(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request("GET", "/api/admin/market-status")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")

    async def test_market_status_returns_three_readonly_items(self) -> None:
        expected = secrets.token_urlsafe(32)
        token = expected
        closes = ((date(2026, 8, 1), 100.0), (date(2026, 8, 2), 101.0))
        scenario = ScenarioStats(horizon=30, count=36, up_ratio=0.42, median_change=0.8)
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api, "get_broad_summary", AsyncMock(return_value={"success": True})
            ),
            patch.object(
                self.admin_api,
                "get_market_temperature",
                AsyncMock(return_value=self._temperature()),
            ),
            patch.object(
                self.admin_api.index_history_store,
                "load_closes",
                AsyncMock(return_value=closes),
            ),
            patch.object(
                self.admin_api,
                "historical_scenario_probs",
                return_value=[scenario],
            ),
            patch.object(
                self.admin_api.market_history,
                "status",
                AsyncMock(return_value=self._history_status()),
            ),
        ):
            response = await self._request(
                "GET", "/api/admin/market-status", headers=self._auth_headers(token)
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        body = response.json()
        self.assertTrue(body["temperature"]["available"])
        self.assertEqual(body["temperature"]["display"], "-15（偏冷）")
        self.assertEqual(body["temperature"]["score"], -15.0)
        self.assertTrue(body["prediction"]["available"])
        self.assertEqual(body["prediction"]["display"], "30日上涨率 42%｜置信度 高")
        self.assertEqual(body["prediction"]["horizon"], 30)
        self.assertEqual(body["prediction"]["up_ratio"], 0.42)
        self.assertEqual(body["prediction"]["count"], 36)
        self.assertEqual(body["prediction"]["confidence"], "高")
        self.assertTrue(body["ranking"]["available"])
        self.assertTrue(body["ranking"]["ready"])
        self.assertEqual(body["ranking"]["display"], "三桶同样本 2100/500｜覆盖 81%｜已解锁")
        self.assertEqual(body["ranking"]["hours_collected"], 25.0)
        self.assertAlmostEqual(body["ranking"]["coverage"], 0.81)

    async def test_market_status_degrades_each_item_honestly(self) -> None:
        expected = secrets.token_urlsafe(32)
        with (
            patch.object(
                self.admin_api, "get_broad_summary", AsyncMock(return_value={"success": False})
            ),
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api,
                "get_market_temperature",
                AsyncMock(return_value=TemperatureUnavailable()),
            ),
            patch.object(
                self.admin_api.index_history_store,
                "load_closes",
                AsyncMock(return_value=()),
            ),
            patch.object(
                self.admin_api.market_history,
                "status",
                AsyncMock(return_value=self._history_status(ranking_overlap=100)),
            ),
        ):
            response = await self._request(
                "GET",
                "/api/admin/market-status",
                headers=self._auth_headers(expected),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["temperature"]["available"])
        self.assertEqual(body["temperature"]["display"], "暂不可用")
        self.assertFalse(body["prediction"]["available"])
        self.assertEqual(body["prediction"]["display"], "积累中")
        self.assertFalse(body["ranking"]["ready"])
        self.assertIn("三桶同样本 100/500", body["ranking"]["display"])
        self.assertIn("三桶未齐", body["ranking"]["display"])
        self.assertNotIn("已解锁", body["ranking"]["display"])

    async def test_market_status_ranking_not_ready_paths(self) -> None:
        expected = secrets.token_urlsafe(32)
        headers = self._auth_headers(expected)
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api,
                "get_broad_summary",
                AsyncMock(return_value={"success": False}),
            ),
        ):
            # 三桶齐但覆盖率不足 -> 覆盖未达标
            with patch.object(
                self.admin_api.market_history,
                "status",
                AsyncMock(
                    return_value=self._history_status(
                        latest_count=1_500, sample_count=2_500, hours_collected=25.0
                    )
                ),
            ):
                low_coverage = await self._request(
                    "GET", "/api/admin/market-status", headers=headers
                )
            # 最新快照过期 -> 快照过期
            stale_bucket = int(time.time()) // 3600 * 3600 - 2 * 3600
            with patch.object(
                self.admin_api.market_history,
                "status",
                AsyncMock(
                    return_value=self._history_status(
                        hours_collected=25.0, latest_bucket=stale_bucket
                    )
                ),
            ):
                stale = await self._request("GET", "/api/admin/market-status", headers=headers)

        self.assertEqual(low_coverage.status_code, 200)
        self.assertFalse(low_coverage.json()["ranking"]["ready"])
        self.assertIn("覆盖 60%", low_coverage.json()["ranking"]["display"])
        self.assertIn("覆盖未达标", low_coverage.json()["ranking"]["display"])
        self.assertEqual(stale.status_code, 200)
        self.assertFalse(stale.json()["ranking"]["ready"])
        self.assertIn("快照过期", stale.json()["ranking"]["display"])

    async def test_market_status_storage_failure_is_fail_closed(self) -> None:
        expected = secrets.token_urlsafe(32)
        with (
            patch.object(
                self.admin_api, "get_broad_summary", AsyncMock(return_value={"success": False})
            ),
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api.market_history,
                "status",
                AsyncMock(side_effect=RuntimeError("database locked")),
            ),
        ):
            response = await self._request(
                "GET", "/api/admin/market-status", headers=self._auth_headers(expected)
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Market storage is unavailable")


class AuthRateLimitTests(unittest.IsolatedAsyncioTestCase):
    """Auth-failure throttling: 429 after N failed attempts per TCP peer."""

    @classmethod
    def setUpClass(cls) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        app = FastAPI()
        app.include_router(cls.admin_api.router)
        cls.app = app

    async def _request(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_failed_attempts_beyond_threshold_are_throttled(self) -> None:
        expected = secrets.token_urlsafe(32)
        wrong = secrets.token_urlsafe(32)
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api,
                "_auth_failure_limiter",
                SlidingWindowLimiter(3, 300),
            ),
        ):
            statuses = []
            for _ in range(4):
                response = await self._request(
                    "GET",
                    "/api/admin/status",
                    headers={"Authorization": f"Bearer {wrong}"},
                )
                statuses.append(response.status_code)

        self.assertEqual(statuses[:3], [401, 401, 401])
        self.assertEqual(statuses[3], 429)
        self.assertEqual(response.headers.get("retry-after"), "300")

    async def test_successful_requests_do_not_consume_failure_budget(self) -> None:
        expected = secrets.token_urlsafe(32)
        wrong = secrets.token_urlsafe(32)
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api,
                "_auth_failure_limiter",
                SlidingWindowLimiter(2, 300),
            ),
        ):
            # 失败 1 次（允许）→ 成功（不得计数）→ 再失败 1 次（仍允许，额度=2）→ 再失败 1 次（429）
            failed_once = await self._request(
                "GET", "/api/admin/status", headers={"Authorization": f"Bearer {wrong}"}
            )
            succeeded = await self._request(
                "GET",
                "/api/admin/status",
                headers={"Authorization": f"Bearer {expected}"},
            )
            failed_twice = await self._request(
                "GET", "/api/admin/status", headers={"Authorization": f"Bearer {wrong}"}
            )
            throttled = await self._request(
                "GET", "/api/admin/status", headers={"Authorization": f"Bearer {wrong}"}
            )

        self.assertEqual(failed_once.status_code, 401)
        self.assertEqual(succeeded.status_code, 200)
        self.assertEqual(failed_twice.status_code, 401)
        self.assertEqual(throttled.status_code, 429)


if __name__ == "__main__":
    unittest.main()

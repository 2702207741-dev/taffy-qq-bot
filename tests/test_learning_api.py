from __future__ import annotations

import importlib
import secrets
import unittest
from unittest.mock import patch

import httpx
import nonebot
from fastapi import FastAPI

from src.state.runtime import learning_insights


class AdminLearningApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def _seed(self, content: str = "关注 AK 系列") -> int:
        await learning_insights.initialize()
        insight = await learning_insights.create_pending(70_001, 0, "preference", content, "依据")
        assert insight is not None
        return insight.id

    async def test_learning_endpoints_require_token_and_serve_lists(self) -> None:
        insight_id = await self._seed()
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            unauthorized = await self._request("GET", "/api/admin/learning")
            self.assertEqual(unauthorized.status_code, 401)
            response = await self._request(
                "GET",
                "/api/admin/learning?status=pending",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["insights"][0]["content"], "关注 AK 系列")

            accepted = await self._request(
                "POST",
                f"/api/admin/learning/{insight_id}/accept",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(accepted.status_code, 200)
            self.assertEqual(accepted.json()["accepted"], str(insight_id))
            # double accept conflicts
            again = await self._request(
                "POST",
                f"/api/admin/learning/{insight_id}/accept",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(again.status_code, 409)

    async def test_learning_stats_and_delete(self) -> None:
        insight_id = await self._seed(content="关注手套系列")
        expected = secrets.token_urlsafe(32)
        headers = {"Authorization": f"Bearer {expected}"}
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            stats = await self._request("GET", "/api/admin/learning/stats", headers=headers)
            self.assertEqual(stats.json()["pending"], 1)
            # delete on pending conflicts
            conflict = await self._request(
                "DELETE",
                f"/api/admin/learning/{insight_id}",
                headers=headers,
            )
            self.assertEqual(conflict.status_code, 409)
            accept = await self._request(
                "POST",
                f"/api/admin/learning/{insight_id}/accept",
                headers=headers,
            )
            self.assertEqual(accept.status_code, 200)
            deleted = await self._request(
                "DELETE",
                f"/api/admin/learning/{insight_id}",
                headers=headers,
            )
            self.assertEqual(deleted.status_code, 200)
            missing = await self._request(
                "DELETE",
                "/api/admin/learning/999999",
                headers=headers,
            )
            self.assertEqual(missing.status_code, 404)

    async def test_learning_status_filter_rejects_bad_values(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request(
                "GET",
                "/api/admin/learning?status=weird",
                headers={"Authorization": f"Bearer {expected}"},
            )
        self.assertEqual(response.status_code, 422)

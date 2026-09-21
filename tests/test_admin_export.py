from __future__ import annotations

import csv
import importlib
import io
import secrets
import unittest
from unittest.mock import patch

import httpx
import nonebot
from fastapi import FastAPI

from src.state.runtime import permissions, usage_log


class AdminExportTests(unittest.IsolatedAsyncioTestCase):
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

    async def _seed(self) -> int:
        await permissions.initialize()
        await permissions.set(60_001, "chat", True)
        await permissions.set(60_001, "news", False)
        await usage_log.record(
            user_id=60_001,
            group_id=100,
            feature="chat",
            command='=HYPERLINK("x")',
            input_text="=1+1",
            output_text=None,
        )
        await usage_log.record(
            user_id=60_002,
            group_id=100,
            feature="chat",
            command="普通命令",
            input_text="第一行\n=cmd|' /C calc',带逗号,\"带引号\"",
            output_text="ok\n@SUM(A1:A2)",
        )
        return 2

    async def test_usage_export_streams_csv_with_formula_neutralised(self) -> None:
        await self._seed()
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            unauthorized = await self._request(
                "GET",
                "/api/admin/usage/export",
            )
            self.assertEqual(unauthorized.status_code, 401)
            response = await self._request(
                "GET",
                "/api/admin/usage/export?user=60001&format=csv",
                headers={"Authorization": f"Bearer {expected}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/csv")
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        text = response.text
        self.assertIn("id,user_id,group_id,feature,command,input_text,output_text,created_at", text)
        parsed = list(csv.reader(io.StringIO(text)))
        self.assertEqual(parsed[0][4], "command")
        self.assertEqual(parsed[1][4], '\'=HYPERLINK("x")')
        self.assertNotIn("\n=HYPERLINK", text)

    async def test_usage_export_round_trips_multi_line_injection_attempts(self) -> None:
        await self._seed()
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request(
                "GET",
                "/api/admin/usage/export?user=60002&format=csv",
                headers={"Authorization": f"Bearer {expected}"},
            )
        self.assertEqual(response.status_code, 200)
        parsed = list(csv.reader(io.StringIO(response.text)))
        self.assertEqual(parsed[0][6], "output_text")
        target = [row for row in parsed[1:] if "第一行" in (row[5] or "")]
        self.assertTrue(target)
        data = dict(zip(parsed[0], target[-1], strict=True))
        # Round-trip preserves the original cell content (commas, quotes and
        # newlines included) while every embedded line stays formula-neutral.
        self.assertEqual(data["input_text"], "第一行\n'=cmd|' /C calc',带逗号,\"带引号\"")
        self.assertEqual(data["output_text"], "ok\n'@SUM(A1:A2)")

    async def test_usage_export_rejects_unknown_feature_and_bad_format(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            unknown = await self._request(
                "GET",
                "/api/admin/usage/export?feature=nope",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(unknown.status_code, 400)
            bad_format = await self._request(
                "GET",
                "/api/admin/usage/export?format=xlsx",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(bad_format.status_code, 422)

    async def test_permissions_export_streams_csv_with_header(self) -> None:
        await self._seed()
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request(
                "GET",
                "/api/admin/permissions/export?format=csv",
                headers={"Authorization": f"Bearer {expected}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("user_id,feature,allowed,updated_at", response.text)
        self.assertIn("60001,chat,True", response.text)
        self.assertIn("60001,news,False", response.text)

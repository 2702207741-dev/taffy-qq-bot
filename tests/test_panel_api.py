from __future__ import annotations

import importlib
import secrets
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from fastapi import FastAPI

from src.state.runtime import (
    alerts as alert_repository,
)
from src.state.runtime import (
    database,
    schedules,
    usage_log,
    watchlists,
)


class AdminPanelApiTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncSetUp(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self._token_patch = patch.object(
            self.admin_api, "get_admin_api_token", return_value=self.token
        )
        self._token_patch.start()
        await usage_log.initialize()
        await alert_repository.initialize()
        await watchlists.initialize()
        await schedules.initialize()

    async def asyncTearDown(self) -> None:
        self._token_patch.stop()
        await database.write("DELETE FROM usage_log")
        await database.write("DELETE FROM alerts")
        await database.write("DELETE FROM watchlist_items")
        await database.write("DELETE FROM broadcast_deliveries")
        await database.write("DELETE FROM background_job_runs")

    async def _seed_usage(self, user_id: int, feature: str, created_at: int) -> None:
        await database.write(
            "INSERT INTO usage_log (user_id, group_id, feature, command, input_text, created_at) "
            "VALUES (?, 0, ?, 'cmd', '输入', ?)",
            (user_id, feature, created_at),
        )

    # --- stats endpoints ----------------------------------------------------

    async def test_stats_endpoints_require_token(self) -> None:
        unauthorized = await self._request("GET", "/api/admin/stats/summary")
        self.assertEqual(unauthorized.status_code, 401)

    async def test_stats_summary_with_data(self) -> None:
        now = int(time.time())
        await self._seed_usage(80_001, "chat", now - 60)
        await self._seed_usage(80_002, "market", now - 120)
        created = await alert_repository.create(
            80_001, "AK-47 | Redline (Field-Tested)", 12345, direction="above"
        )
        self.assertTrue(created.created)
        delivered = await alert_repository.claim_due(10, 60, now=now)
        self.assertTrue(delivered)
        result = await alert_repository.mark_delivered(
            delivered[0].id, delivered[0].lease_token or "", delivered_at=now
        )
        self.assertTrue(result.applied)
        await watchlists.add(80_001, "AK-47 | Redline (Field-Tested)")
        response = await self._request("GET", "/api/admin/stats/summary", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["today_usage"], 2)
        self.assertEqual(body["active_users"], 2)
        self.assertGreaterEqual(body["alerts_pending"], 0)
        self.assertGreaterEqual(body["alerts_delivered"], 1)
        self.assertEqual(body["watchlist_total"], 1)
        self.assertIn("uptime_seconds", body)

    async def test_stats_usage_trend_zero_fills_and_bounds(self) -> None:
        now = int(time.time())
        await self._seed_usage(80_003, "chat", now - 3600)
        response = await self._request(
            "GET", "/api/admin/stats/usage-trend?days=7", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["days"], 7)
        self.assertEqual(len(body["labels"]), 7)
        self.assertEqual(len(body["values"]), 7)
        self.assertEqual(sum(body["values"]), 1)
        overflow = await self._request(
            "GET", "/api/admin/stats/usage-trend?days=91", headers=self.headers
        )
        self.assertEqual(overflow.status_code, 422)

    async def test_stats_feature_pie_and_market_trend(self) -> None:
        now = int(time.time())
        await self._seed_usage(80_004, "chat", now - 60)
        await self._seed_usage(80_004, "chat", now - 61)
        await self._seed_usage(80_004, "market", now - 62)
        pie = await self._request("GET", "/api/admin/stats/feature-pie", headers=self.headers)
        self.assertEqual(pie.status_code, 200)
        body = pie.json()
        self.assertEqual(body["labels"][0], "chat")
        self.assertEqual(body["values"][0], 2)
        market = await self._request("GET", "/api/admin/stats/market-trend", headers=self.headers)
        self.assertEqual(market.status_code, 200)
        self.assertIn("labels", market.json())
        self.assertIn("values", market.json())

    # --- broadcast ----------------------------------------------------------

    async def test_broadcast_validates_input(self) -> None:
        empty_groups = await self._request(
            "POST",
            "/api/admin/broadcast",
            headers=self.headers,
            json={"group_ids": [], "payload_text": "你好"},
        )
        self.assertEqual(empty_groups.status_code, 422)
        too_many = await self._request(
            "POST",
            "/api/admin/broadcast",
            headers=self.headers,
            json={"group_ids": list(range(1, 52)), "payload_text": "你好"},
        )
        self.assertEqual(too_many.status_code, 422)
        too_long = await self._request(
            "POST",
            "/api/admin/broadcast",
            headers=self.headers,
            json={"group_ids": [100], "payload_text": "x" * 2001},
        )
        self.assertEqual(too_long.status_code, 422)
        unauthorized = await self._request(
            "POST", "/api/admin/broadcast", json={"group_ids": [100], "payload_text": "你好"}
        )
        self.assertEqual(unauthorized.status_code, 401)

    async def test_broadcast_sends_and_records_deliveries(self) -> None:
        bot = AsyncMock()
        bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
        with patch.object(self.admin_api, "_current_bot", return_value=bot):
            response = await self._request(
                "POST",
                "/api/admin/broadcast",
                headers=self.headers,
                json={"group_ids": [200, 201], "payload_text": "面板测试广播"},
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["succeeded"], 2)
        self.assertEqual(bot.send_group_msg.await_count, 2)
        sent_text = str(bot.send_group_msg.await_args.kwargs["message"])
        self.assertIn("面板测试广播", sent_text)
        rows = await database.read(
            "SELECT group_id, status FROM broadcast_deliveries WHERE schedule_key = ?",
            (body["schedule_key"],),
        )
        self.assertEqual({row["status"] for row in rows}, {"delivered"})
        self.assertEqual({int(row["group_id"]) for row in rows}, {200, 201})

    async def test_broadcast_without_bot_returns_503(self) -> None:
        with patch.object(self.admin_api, "_current_bot", return_value=None):
            response = await self._request(
                "POST",
                "/api/admin/broadcast",
                headers=self.headers,
                json={"group_ids": [200], "payload_text": "你好"},
            )
        self.assertEqual(response.status_code, 503)

    # --- scheduler PUT ------------------------------------------------------

    async def test_scheduler_update_success_and_conflict(self) -> None:
        current = await schedules.get()
        updated = await self._request(
            "PUT",
            "/api/admin/scheduler",
            headers=self.headers,
            json={"enabled": True, "times": ["08:00", "12:30"], "groups": [300]},
        )
        self.assertEqual(updated.status_code, 200)
        body = updated.json()
        self.assertEqual(body["times"], ["08:00", "12:30"])
        self.assertEqual(body["groups"], ["300"])
        self.assertEqual(body["version"], current.version + 1)

        stale = await self._request(
            "PUT",
            "/api/admin/scheduler",
            headers=self.headers,
            json={"enabled": False, "expected_version": current.version},
        )
        self.assertEqual(stale.status_code, 409)

        invalid = await self._request(
            "PUT",
            "/api/admin/scheduler",
            headers=self.headers,
            json={"times": ["25:00"]},
        )
        self.assertEqual(invalid.status_code, 422)

    # --- deletion proxies ---------------------------------------------------

    async def test_alert_delete_and_missing(self) -> None:
        created = await alert_repository.create(
            80_010, "AWP | Dragon Lore (Factory New)", 999_999, direction="below"
        )
        self.assertTrue(created.created)
        deleted = await self._request(
            "DELETE", f"/api/admin/alerts/{created.alert.id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            deleted.json()["deleted"]["market_name"], "AWP | Dragon Lore (Factory New)"
        )
        missing = await self._request(
            "DELETE", "/api/admin/alerts/nonexistent-id", headers=self.headers
        )
        self.assertEqual(missing.status_code, 404)

    async def test_watchlist_delete_and_missing(self) -> None:
        await watchlists.add(80_011, "AK-47 | Redline (Field-Tested)")
        await watchlists.add(80_011, "AWP | Asiimov (Field-Tested)")
        removed = await self._request(
            "DELETE", "/api/admin/watchlist/80011/1", headers=self.headers
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            removed.json()["deleted"]["display_name"], "AK-47 | Redline (Field-Tested)"
        )
        items = await watchlists.list_owner(80_011)
        self.assertEqual([item.display_name for item in items], ["AWP | Asiimov (Field-Tested)"])
        missing = await self._request(
            "DELETE", "/api/admin/watchlist/80011/9", headers=self.headers
        )
        self.assertEqual(missing.status_code, 404)

    # --- events -------------------------------------------------------------

    async def test_events_incremental_and_limit(self) -> None:
        now = int(time.time())
        await self._seed_usage(80_020, "chat", now - 10)
        await self._seed_usage(80_021, "market", now - 20)
        latest = await self._request("GET", "/api/admin/events?limit=50", headers=self.headers)
        self.assertEqual(latest.status_code, 200)
        body = latest.json()
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(item["kind"] == "usage" for item in body["events"]))
        self.assertEqual(body["events"][0]["time"], now - 10)

        incremental = await self._request(
            "GET", f"/api/admin/events?since={now - 15}", headers=self.headers
        )
        events = incremental.json()["events"]
        self.assertEqual([item["time"] for item in events], [now - 10])

        bounded = await self._request("GET", "/api/admin/events?limit=1", headers=self.headers)
        self.assertEqual(bounded.json()["count"], 1)

    async def test_events_includes_alert_and_job_events(self) -> None:
        now = int(time.time())
        created = await alert_repository.create(80_030, "M4A4 | Howl", 50_000, direction="above")
        self.assertTrue(created.created)
        claimed = await alert_repository.claim_due(5, 60, now=now)
        self.assertTrue(claimed)
        result = await alert_repository.mark_delivered(
            claimed[0].id, claimed[0].lease_token or "", delivered_at=now
        )
        self.assertTrue(result.applied)
        await database.write(
            "INSERT INTO background_job_runs "
            "(run_key, job_name, status, attempts, lease_owner, lease_expires_at, "
            "last_error, updated_at, completed_at) "
            "VALUES ('panel-test-job', 'warm-cache', 'completed', 3, NULL, NULL, "
            "'WarmCacheFailed', ?, ?)",
            (now - 5, now - 5),
        )
        response = await self._request(
            "GET", f"/api/admin/events?since={now - 60}", headers=self.headers
        )
        kinds = {item["kind"] for item in response.json()["events"]}
        self.assertIn("alert_delivered", kinds)
        self.assertIn("job_failed", kinds)

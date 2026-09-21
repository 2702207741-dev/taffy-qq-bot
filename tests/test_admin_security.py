from __future__ import annotations

import importlib
import secrets
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from fastapi import FastAPI

from src.core.config import normalize_admin_api_token, secure_onebot_host


class IngressSecurityTests(unittest.TestCase):
    def test_public_host_without_both_onebot_credentials_uses_loopback(self) -> None:
        result = secure_onebot_host(
            {
                "HOST": "0.0.0.0",
                "ONEBOT_WS_URLS": '["ws://127.0.0.1:3001"]',
            }
        )

        self.assertEqual(result.host, "127.0.0.1")
        self.assertTrue(result.downgraded_to_loopback)

    def test_public_host_with_only_one_credential_still_uses_loopback(self) -> None:
        result = secure_onebot_host(
            {
                "HOST": "0.0.0.0",
                "ONEBOT_V11_ACCESS_TOKEN": secrets.token_urlsafe(32),
            }
        )

        self.assertEqual(result.host, "127.0.0.1")
        self.assertTrue(result.downgraded_to_loopback)

    def test_public_host_rejects_weak_onebot_credentials(self) -> None:
        result = secure_onebot_host(
            {
                "HOST": "0.0.0.0",
                "ONEBOT_V11_ACCESS_TOKEN": "public-placeholder-token",
                "ONEBOT_V11_SECRET": "public-placeholder-secret",
            }
        )

        self.assertEqual(result.host, "127.0.0.1")
        self.assertTrue(result.downgraded_to_loopback)

    def test_public_host_requires_both_onebot_credentials(self) -> None:
        result = secure_onebot_host(
            {
                "HOST": "0.0.0.0",
                "ONEBOT_V11_ACCESS_TOKEN": secrets.token_urlsafe(32),
                "ONEBOT_V11_SECRET": secrets.token_urlsafe(32),
                "TAFFY_INGRESS_TLS_TERMINATED": "true",
            }
        )

        self.assertEqual(result.host, "0.0.0.0")
        self.assertFalse(result.downgraded_to_loopback)

    def test_public_host_with_credentials_but_no_tls_confirmation_uses_loopback(self) -> None:
        result = secure_onebot_host(
            {
                "HOST": "0.0.0.0",
                "ONEBOT_V11_ACCESS_TOKEN": secrets.token_urlsafe(32),
                "ONEBOT_V11_SECRET": secrets.token_urlsafe(32),
            }
        )

        self.assertEqual(result.host, "127.0.0.1")
        self.assertTrue(result.downgraded_to_loopback)

    def test_admin_token_rejects_short_configuration(self) -> None:
        self.assertIsNone(normalize_admin_api_token("not-long-enough"))
        self.assertIsNone(normalize_admin_api_token("a" * 64))
        self.assertIsNotNone(normalize_admin_api_token(secrets.token_urlsafe(32)))


class AdminApiSecurityTests(unittest.IsolatedAsyncioTestCase):
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

    def assert_response_does_not_contain(self, response, *tokens: str) -> None:
        rendered_headers = repr(response.headers)
        for token in tokens:
            self.assertFalse(token in response.text)
            self.assertFalse(token in rendered_headers)

    async def test_permission_endpoints_require_token_and_validate_feature(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            unauthorized = await self._request("GET", "/api/admin/permissions/features")
            self.assertEqual(unauthorized.status_code, 401)
            authorized = await self._request(
                "GET",
                "/api/admin/permissions/features",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(authorized.status_code, 200)
            body = authorized.json()
            self.assertTrue(any(item["name"] == "alert" for item in body["features"]))
            unknown = await self._request(
                "GET",
                "/api/admin/permissions?feature=nope",
                headers={"Authorization": f"Bearer {expected}"},
            )
            self.assertEqual(unknown.status_code, 400)

    async def test_unconfigured_admin_api_is_fail_closed(self) -> None:
        supplied = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=None):
            response = await self._request(
                "GET",
                "/api/admin/status",
                headers={"Authorization": f"Bearer {supplied}"},
            )

        self.assertEqual(response.status_code, 503)
        self.assert_response_does_not_contain(response, supplied)

    async def test_missing_token_is_rejected_even_with_local_forwarded_address(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request(
                "GET",
                "/api/admin/status",
                headers={"X-Forwarded-For": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("www-authenticate"), "Bearer")
        self.assert_response_does_not_contain(response, expected)

    async def test_wrong_token_uses_constant_time_comparison_without_leaking_tokens(
        self,
    ) -> None:
        expected = secrets.token_urlsafe(32)
        supplied = secrets.token_urlsafe(32)
        compare_digest = secrets.compare_digest
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api.secrets,
                "compare_digest",
                wraps=compare_digest,
            ) as comparator,
        ):
            response = await self._request(
                "GET",
                "/api/admin/status",
                headers={"Authorization": f"Bearer {supplied}"},
            )

        self.assertEqual(response.status_code, 401)
        comparator.assert_called_once()
        self.assert_response_does_not_contain(response, expected, supplied)

    async def test_non_ascii_bearer_token_is_failure_not_crash_and_is_rate_counted(
        self,
    ) -> None:
        """A3 regression: latin-1 header bytes must not bypass the failure limiter."""

        expected = secrets.token_urlsafe(32)
        fresh_limiter = self.admin_api.SlidingWindowLimiter(2, 300)
        request = SimpleNamespace(client=SimpleNamespace(host="unit-test-peer"))
        credentials = self.admin_api.HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="ÿÿÿÿÿÿÿÿ",
        )
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(self.admin_api, "_auth_failure_limiter", fresh_limiter),
        ):
            statuses = []
            for _ in range(3):
                try:
                    await self.admin_api.require_admin_api_token(credentials, request)
                    statuses.append(200)
                except self.admin_api.HTTPException as exc:
                    statuses.append(exc.status_code)

        self.assertEqual(statuses[:2], [401, 401])
        self.assertEqual(statuses[2], 429)

    def test_log_file_resolution_matches_sanitized_logging_directory(self) -> None:
        """A7 regression: panel log views follow the real sink directory."""

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"TAFFY_LOG_DIR": tmp}):
                resolved = self.admin_api._log_file()
            self.assertEqual(resolved, Path(tmp) / "taffy.log")

        from src.core.logging_privacy import resolve_log_directory

        self.assertEqual(
            resolve_log_directory(self.admin_api._ROOT).name,
            "logs",
        )

    async def test_logs_endpoint_returns_sanitized_sink_content(self) -> None:
        """A7 regression: a populated log file is served instead of "(file not found)"."""

        import tempfile
        from pathlib import Path

        expected = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "taffy.log"
            log_file.write_text("2026-08-22 INFO taffy hello\n", encoding="utf-8")
            with (
                patch.dict("os.environ", {"TAFFY_LOG_DIR": tmp}),
                patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            ):
                response = await self._request(
                    "GET",
                    "/api/admin/logs/raw?lines=5",
                    headers={"Authorization": f"Bearer {expected}"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertIn("taffy hello", response.text)

    async def test_valid_bearer_token_works_from_any_forwarded_address(self) -> None:
        expected = secrets.token_urlsafe(32)
        with patch.object(self.admin_api, "get_admin_api_token", return_value=expected):
            response = await self._request(
                "GET",
                "/api/admin/status",
                headers={
                    "Authorization": f"Bearer {expected}",
                    "X-Forwarded-For": "203.0.113.7",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assert_response_does_not_contain(response, expected)

    async def test_session_delete_uses_shared_store_and_cross_worker_guard(self) -> None:
        class FakeStore:
            def __init__(self) -> None:
                self._store = {"group:1": [{"role": "user", "content": "hello"}]}
                self.guard_entered = False

            async def get(self, key: str):
                return list(self._store.get(key, []))

            async def list_summaries(self, *, limit: int, offset: int):
                del limit, offset
                return [
                    {
                        "key": "group:1",
                        "message_count": 1,
                        "total_chars": 5,
                        "last_preview": "hello",
                        "updated_at": 1,
                    }
                ]

            async def delete(self, key: str) -> bool:
                return self._store.pop(key, None) is not None

            @asynccontextmanager
            async def session_guard(self, key: str, ttl: float, *, wait_timeout: float):
                del key, ttl, wait_timeout
                self.guard_entered = True
                yield "token"

        fake_store = FakeStore()
        with patch.object(self.admin_api, "_session_store", return_value=fake_store):
            summaries = await self.admin_api._session_summaries(limit=100, offset=0)
            deleted = await self.admin_api._delete_session("group:1")

        self.assertEqual(summaries[0]["key"], "group:1")
        self.assertTrue(deleted)
        self.assertNotIn("group:1", fake_store._store)
        self.assertTrue(fake_store.guard_entered)

    async def test_alert_admin_response_never_exposes_internal_lease_token(self) -> None:
        expected = secrets.token_urlsafe(32)
        lease_token = "internal-fencing-token-must-not-leak"
        item = SimpleNamespace(
            id="alert-1",
            owner_user_id=1,
            group_id=2,
            market_name="item",
            target_price_cents=1_000,
            direction="above",
            status="claimed",
            attempts=0,
            next_retry_at=None,
            lease_token=lease_token,
            lease_expires_at=100,
            last_error=None,
            last_observed_price_cents=None,
            created_at=1,
            updated_at=1,
            delivered_at=None,
            failed_at=None,
            message_id=None,
        )
        page = SimpleNamespace(items=(item,), total=1, offset=0)
        fake_repository = SimpleNamespace(admin_page=AsyncMock(return_value=page))
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(self.admin_api, "alert_repository", fake_repository),
        ):
            response = await self._request(
                "GET",
                "/api/admin/alerts",
                headers={"Authorization": f"Bearer {expected}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(lease_token, response.text)
        self.assertNotIn("lease_token", response.text)

    async def test_admin_can_inspect_and_retry_failed_alert_without_token_leak(self) -> None:
        expected = secrets.token_urlsafe(32)
        item = SimpleNamespace(
            id="failed-alert",
            owner_user_id=1,
            group_id=None,
            market_name="item",
            target_price_cents=1_000,
            direction="below",
            status="failed",
            attempts=8,
            next_retry_at=None,
            lease_token=None,
            lease_expires_at=None,
            last_error="TimeoutError",
            last_observed_price_cents=None,
            created_at=1,
            updated_at=2,
            delivered_at=None,
            failed_at=2,
            message_id=None,
        )
        page = SimpleNamespace(items=(item,), total=1, offset=0)
        mutation = SimpleNamespace(applied=True, alert=item)
        fake_repository = SimpleNamespace(
            admin_page=AsyncMock(return_value=page),
            retry_failed=AsyncMock(return_value=mutation),
        )
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(self.admin_api, "alert_repository", fake_repository),
        ):
            listed = await self._request(
                "GET",
                "/api/admin/alerts?status=failed",
                headers={"Authorization": f"Bearer {expected}"},
            )
            retried = await self._request(
                "POST",
                "/api/admin/alerts/failed-alert/retry",
                headers={"Authorization": f"Bearer {expected}"},
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["alerts"][0]["failed_at"], 2)
        self.assertNotIn("lease_token", listed.text)
        self.assertEqual(retried.status_code, 200)
        fake_repository.retry_failed.assert_awaited_once_with("failed-alert")

    async def test_admin_can_retry_or_discard_failed_broadcast(self) -> None:
        expected = secrets.token_urlsafe(32)
        item = SimpleNamespace(
            schedule_key="market-index:slot",
            group_id=1001,
            status="failed",
            attempts=8,
            next_retry_at=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error="TimeoutError",
            payload_text="safe payload",
            created_at=1,
            updated_at=2,
            delivered_at=None,
            failed_at=2,
            message_id=None,
        )
        fake_repository = SimpleNamespace(
            list=AsyncMock(return_value=[item]),
            retry_failed=AsyncMock(return_value=True),
            discard_failed=AsyncMock(return_value=True),
        )
        headers = {"Authorization": f"Bearer {expected}"}
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(self.admin_api, "broadcasts", fake_repository),
        ):
            listed = await self._request(
                "GET",
                "/api/admin/broadcast-deliveries?status=failed",
                headers=headers,
            )
            retried = await self._request(
                "POST",
                "/api/admin/broadcast-deliveries/market-index:slot/1001/retry",
                headers=headers,
            )
            discarded = await self._request(
                "DELETE",
                "/api/admin/broadcast-deliveries/market-index:slot/1001",
                headers=headers,
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["deliveries"][0]["status"], "failed")
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(discarded.status_code, 200)

    async def test_group_pulse_audit_requires_token_and_never_leaks_text(self) -> None:
        """/group-pulse 审计端点：Bearer 保护；只返回台账字段，绝无消息原文。"""

        expected = secrets.token_urlsafe(32)
        entry = SimpleNamespace(
            id=1,
            group_id=1001,
            enabled=1,
            reason="willing",
            fingerprint="a" * 64,
            created_at=123456,
            kind="interjection",
        )
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected),
            patch.object(
                self.admin_api,
                "group_pulse_log",
                SimpleNamespace(
                    recent=AsyncMock(return_value=[entry]),
                    enabled_groups=AsyncMock(return_value=[1001]),
                    counts=AsyncMock(return_value={"interjection_count": 1, "switch_count": 2}),
                ),
            ),
        ):
            unauthorized = await self._request("GET", "/api/admin/group-pulse")
            self.assertEqual(unauthorized.status_code, 401)

            authorized = await self._request(
                "GET",
                "/api/admin/group-pulse",
                headers={"Authorization": f"Bearer {expected}"},
            )
        self.assertEqual(authorized.status_code, 200)
        body = authorized.json()
        # M-109：含糊的 count 已拆成按行-kind 分开的两个口径
        self.assertNotIn("count", body)
        self.assertEqual(body["interjection_count"], 1)
        self.assertEqual(body["switch_count"], 2)
        self.assertEqual(body["enabled_groups"], [1001])
        self.assertEqual(body["entries"][0]["kind"], "interjection")
        self.assertEqual(body["entries"][0]["reason"], "willing")
        self.assertEqual(body["entries"][0]["fingerprint"], "a" * 64)


class AdminPanelSecurityHeaderTests(unittest.IsolatedAsyncioTestCase):
    """B2 regression: every panel response carries hardening headers."""

    @classmethod
    def setUpClass(cls) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        cls.app = cls.admin_api.app

    async def _get(self, path: str):
        transport = httpx.ASGITransport(app=self.app)
        # This is the real plugin app, so it carries the Host guard; an
        # invented hostname such as "test" is now refused like any other.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8080"
        ) as client:
            return await client.get(path)

    async def test_panel_page_carries_security_headers(self) -> None:
        response = await self._get("/admin-panel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn(
            "default-src 'self'",
            response.headers["content-security-policy"],
        )

    async def test_panel_static_assets_carry_security_headers(self) -> None:
        response = await self._get("/admin-panel/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")


if __name__ == "__main__":
    unittest.main()

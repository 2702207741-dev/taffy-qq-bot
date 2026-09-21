"""Host-header guard regressions for the loopback OneBot/API listener.

The shipped default leaves ``ONEBOT_V11_SECRET`` blank, so the adapter performs
no signature check; before this guard anything able to reach the port could post
an event whose ``user_id`` decides privilege.  These tests pin the rebinding shape
that is now refused and the local/remote-by-address flows that must keep working.
"""

from __future__ import annotations

import importlib
import unittest
from typing import Any

import httpx
import nonebot
from fastapi import FastAPI

from src.core.ingress_guard import (
    HostGuardMiddleware,
    describe_guard,
    extra_hosts_from_env,
    host_is_allowed,
    parse_allowed_hosts,
)


class HostDecisionTests(unittest.TestCase):
    def test_loopback_names_and_literal_ips_are_accepted(self) -> None:
        for header in (
            "127.0.0.1:8080",
            "localhost",
            "LOCALHOST:8080",
            "[::1]:8080",
            "10.0.0.5:8080",
            "203.0.113.9",
        ):
            self.assertTrue(host_is_allowed(header), header)

    def test_rebound_hostname_is_refused_without_an_allowlist(self) -> None:
        for header in ("attacker.example", "attacker.example:8080", "evil.test"):
            self.assertFalse(host_is_allowed(header), header)

    def test_allowlisted_names_and_wildcard(self) -> None:
        allowed = frozenset({"bot.example.com"})
        self.assertTrue(host_is_allowed("bot.example.com:8443", allowed))
        self.assertFalse(host_is_allowed("other.example.com", allowed))
        self.assertTrue(host_is_allowed("anything.example", frozenset({"*"})))

    def test_missing_or_unusable_host_is_refused(self) -> None:
        self.assertFalse(host_is_allowed(None))
        self.assertFalse(host_is_allowed("   "))
        self.assertFalse(host_is_allowed(":8080"))

    def test_parsing_normalises_case_and_trailing_dots(self) -> None:
        self.assertEqual(
            parse_allowed_hosts(" Bot.Example.COM. , "), frozenset({"bot.example.com"})
        )
        self.assertEqual(parse_allowed_hosts(None), frozenset())

    def test_env_reader_returns_names_not_secrets(self) -> None:
        self.assertEqual(extra_hosts_from_env({}), frozenset())
        self.assertEqual(
            extra_hosts_from_env({"TAFFY_ALLOWED_HOSTS": "bot.example.com"}),
            frozenset({"bot.example.com"}),
        )
        self.assertIn("bot.example.com", describe_guard(frozenset({"bot.example.com"})))
        self.assertIn("TAFFY_ALLOWED_HOSTS", describe_guard(frozenset()))


class GuardedHttpTests(unittest.IsolatedAsyncioTestCase):
    def _app(self, extra: frozenset[str] = frozenset()) -> FastAPI:
        app = FastAPI()
        app.add_middleware(HostGuardMiddleware, extra_hosts=extra)

        @app.post("/onebot/v11/http")
        async def _webhook() -> dict[str, str]:  # pragma: no cover - assertion target
            return {"accepted": "yes"}

        return app

    async def _post(self, app: FastAPI, host: str | None) -> httpx.Response:
        headers = {} if host is None else {"host": host}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8080"
        ) as client:
            return await client.post("/onebot/v11/http", json={}, headers=headers)

    async def test_local_unsigned_event_still_reaches_the_adapter(self) -> None:
        response = await self._post(self._app(), "127.0.0.1:8080")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": "yes"})

    async def test_rebound_host_is_rejected_without_reaching_the_adapter(
        self,
    ) -> None:
        response = await self._post(self._app(), "attacker.example:8080")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("accepted", response.text)

    async def test_remote_access_by_literal_ip_keeps_working(self) -> None:
        response = await self._post(self._app(), "203.0.113.9:8080")
        self.assertEqual(response.status_code, 200)

    async def test_operator_listed_name_is_accepted(self) -> None:
        app = self._app(frozenset({"bot.example.com"}))
        self.assertEqual((await self._post(app, "bot.example.com")).status_code, 200)
        self.assertEqual((await self._post(app, "other.example.com")).status_code, 400)


class GuardedWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, host: bytes) -> tuple[list[Any], list[Any]]:
        sent: list[Any] = []
        reached: list[Any] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            reached.append(scope["type"])

        async def receive() -> dict[str, str]:
            return {"type": "websocket.connect"}

        async def send(message: Any) -> None:
            sent.append(message)

        guard = HostGuardMiddleware(app, extra_hosts=frozenset())
        scope = {"type": "websocket", "headers": [(b"host", host)]}
        await guard(scope, receive, send)
        return sent, reached

    async def test_rebound_host_closes_the_socket_before_handling(self) -> None:
        sent, reached = await self._run(b"attacker.example:8080")
        self.assertEqual(reached, [])
        self.assertEqual(
            sent, [{"type": "websocket.close", "code": 1008, "reason": "Invalid Host"}]
        )

    async def test_loopback_host_reaches_the_adapter(self) -> None:
        sent, reached = await self._run(b"127.0.0.1:8080")
        self.assertEqual(reached, ["websocket"])
        self.assertEqual(sent, [])


class AdminAppWiringTests(unittest.TestCase):
    def test_registration_puts_the_guard_on_the_shared_app(self) -> None:
        nonebot.init(_env_file=(), driver="~fastapi")
        admin_api = importlib.import_module("src.plugins.admin_api")
        classes = [entry.cls for entry in admin_api.app.user_middleware]
        self.assertIn(HostGuardMiddleware, classes)


class ConfigSurfaceTests(unittest.TestCase):
    def test_the_new_key_is_declared_in_both_places(self) -> None:
        config = importlib.import_module("src.core.config")
        self.assertIn("TAFFY_ALLOWED_HOSTS", config.KNOWN_ENV_KEYS)
        example = importlib.import_module("pathlib").Path(".env.example")
        text = example.read_text(encoding="utf-8") if example.exists() else ""
        self.assertIn("TAFFY_ALLOWED_HOSTS=", text)

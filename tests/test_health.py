from __future__ import annotations

import importlib
import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import nonebot
from fastapi import FastAPI

from src.core import health


class FakeTask:
    def __init__(self, *, done: bool = False, cancelled: bool = False) -> None:
        self._done = done
        self._cancelled = cancelled

    def done(self) -> bool:
        return self._done

    def cancelled(self) -> bool:
        return self._cancelled


def healthy_tasks() -> dict[str, FakeTask]:
    return {name: FakeTask() for name in health.REQUIRED_SCHEDULER_TASKS}


class HealthSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        self.config = SimpleNamespace(
            host="127.0.0.1",
            taffy_api_key="configured-secret",
            steamdt_api_key="configured-market-secret",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    async def test_ready_requires_connection_scheduler_storage_and_configuration(self) -> None:
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["status"], "ready")
        self.assertEqual(snapshot["checks"]["onebot_connection"]["status"], "pass")
        self.assertEqual(snapshot["checks"]["scheduler"]["running"], 5)
        self.assertEqual(snapshot["checks"]["storage"]["writable"], 2)
        self.assertEqual(snapshot["checks"]["critical_config"]["status"], "pass")

    async def test_disconnected_onebot_is_not_ready(self) -> None:
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=0,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["onebot_connection"]["status"], "fail")

    async def test_application_tasks_alone_are_enough_for_scheduler_readiness(self) -> None:
        """operations/maintenance live outside _scheduler_tasks after lifecycle split."""
        tasks = {
            "broadcast": FakeTask(),
            "alerts": FakeTask(),
            "inventory-monitors": FakeTask(),
            "maintenance": FakeTask(),
            "operations": FakeTask(),
        }

        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=tasks,
            state_ready=True,
        )

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["scheduler"]["status"], "pass")
        self.assertEqual(snapshot["checks"]["scheduler"]["tasks"]["maintenance"], "running")
        self.assertEqual(snapshot["checks"]["scheduler"]["tasks"]["operations"], "running")

    async def test_missing_application_tasks_are_reported_stopped(self) -> None:
        tasks = {
            "broadcast": FakeTask(),
            "alerts": FakeTask(),
            "inventory-monitors": FakeTask(),
        }

        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=tasks,
            state_ready=True,
        )

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["scheduler"]["status"], "fail")
        self.assertEqual(snapshot["checks"]["scheduler"]["tasks"]["operations"], "stopped")
        self.assertEqual(snapshot["checks"]["scheduler"]["tasks"]["maintenance"], "stopped")

    async def test_completed_scheduler_task_is_not_ready(self) -> None:
        tasks = healthy_tasks()
        tasks["alerts"] = FakeTask(done=True)

        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=tasks,
            state_ready=True,
        )

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["scheduler"]["status"], "fail")
        self.assertEqual(snapshot["checks"]["scheduler"]["tasks"]["alerts"], "stopped")

    async def test_disabled_cache_warmer_is_not_a_readiness_dependency(self) -> None:
        tasks = {
            name: FakeTask() for name in health.REQUIRED_SCHEDULER_TASKS if name != "warm-cache"
        }
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={"TAFFY_ENABLE_CACHE_WARMER": "false"},
            onebot_connection_count=1,
            scheduler_tasks=tasks,
            state_ready=True,
        )

        self.assertTrue(snapshot["ready"])
        scheduler_check = snapshot["checks"]["scheduler"]
        self.assertEqual(scheduler_check["required"], 5)
        self.assertNotIn("warm-cache", scheduler_check["tasks"])

    async def test_cache_warmer_is_excluded_by_default_from_readiness(self) -> None:
        tasks = {
            name: FakeTask() for name in health.REQUIRED_SCHEDULER_TASKS if name != "warm-cache"
        }
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=tasks,
            state_ready=True,
        )

        self.assertTrue(snapshot["ready"])
        self.assertNotIn("warm-cache", snapshot["checks"]["scheduler"]["tasks"])

    async def test_enabled_cache_warmer_is_a_readiness_dependency(self) -> None:
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={"TAFFY_ENABLE_CACHE_WARMER": "true"},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["scheduler"]["required"], 6)
        self.assertIn("warm-cache", snapshot["checks"]["scheduler"]["tasks"])

    async def test_database_state_is_a_required_readiness_dependency(self) -> None:
        snapshot = await health.collect_health_snapshot(
            config=self.config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=False,
        )

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["database"], {"status": "fail", "ready": False})

    async def test_runtime_database_failure_is_reported_not_raised(self) -> None:
        with patch("src.state.runtime.state_healthcheck", AsyncMock(side_effect=RuntimeError)):
            snapshot = await health.collect_health_snapshot(
                config=self.config,
                root=self.root,
                environ={},
                onebot_connection_count=1,
                scheduler_tasks=healthy_tasks(),
                storage_directories=(self.root,),
            )
        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["database"]["status"], "fail")

    def test_storage_paths_runtime_tasks_and_probe_helpers_fail_closed(self) -> None:
        directories = health._storage_directories(
            self.root,
            None,
            {
                "TAFFY_STATE_DB": "state/custom.db",
                "TAFFY_SCHEDULE_CONFIG": str(self.root / "cfg" / "schedule.json"),
            },
        )
        self.assertIn(self.root / "state", directories)
        self.assertIn(self.root / "cfg", directories)
        self.assertTrue(health._probe_directory_writable(self.root))
        self.assertFalse(health._probe_directory_writable(self.root / "missing"))
        self.assertFalse(health._task_is_running(object()))

        broken = SimpleNamespace(
            done=lambda: (_ for _ in ()).throw(RuntimeError()),
            cancelled=lambda: False,
        )
        self.assertFalse(health._task_is_running(broken))
        with patch.object(
            health.sys,
            "modules",
            {"src.plugins.scheduler": SimpleNamespace(_scheduler_tasks={"x": object()})},
        ):
            self.assertIn("x", health._runtime_scheduler_tasks())
        with patch.object(health.sys, "modules", {}):
            self.assertEqual(health._runtime_scheduler_tasks(), {})

    def test_runtime_scheduler_tasks_merges_application_registry(self) -> None:
        application_only = SimpleNamespace(
            _application_tasks={"operations": FakeTask(), "maintenance": FakeTask()},
            _scheduler_tasks={},
        )
        with patch.object(health.sys, "modules", {"src.plugins.scheduler": application_only}):
            merged = health._runtime_scheduler_tasks()
        self.assertEqual(set(merged), {"operations", "maintenance"})

        split = SimpleNamespace(
            _application_tasks={"operations": FakeTask()},
            _scheduler_tasks={"broadcast": FakeTask()},
        )
        with patch.object(health.sys, "modules", {"src.plugins.scheduler": split}):
            merged = health._runtime_scheduler_tasks()
        self.assertEqual(set(merged), {"operations", "broadcast"})

    def test_runtime_onebot_count_returns_zero_when_adapter_runtime_is_unavailable(self) -> None:
        with patch("nonebot.get_bots", side_effect=RuntimeError):
            self.assertEqual(health._runtime_onebot_connection_count(), 0)

    async def test_unwritable_state_directory_is_not_ready(self) -> None:
        def probe(path: Path) -> bool:
            return path != self.root / "data"

        with patch.object(health, "_probe_directory_writable", side_effect=probe):
            snapshot = await health.collect_health_snapshot(
                config=self.config,
                root=self.root,
                environ={},
                onebot_connection_count=1,
                scheduler_tasks=healthy_tasks(),
                state_ready=True,
            )

        self.assertFalse(snapshot["ready"])
        self.assertEqual(snapshot["checks"]["storage"]["status"], "fail")
        self.assertEqual(snapshot["checks"]["storage"]["writable"], 1)

    async def test_filesystem_probes_run_off_event_loop(self) -> None:
        event_loop_thread = threading.get_ident()
        probe_threads: list[int] = []

        def probe(path: Path) -> bool:
            probe_threads.append(threading.get_ident())
            return path == self.root

        with patch.object(health, "_probe_directory_writable", side_effect=probe):
            await health.collect_health_snapshot(
                config=self.config,
                root=self.root,
                environ={},
                onebot_connection_count=1,
                scheduler_tasks=healthy_tasks(),
                storage_directories=[self.root],
                state_ready=True,
            )

        self.assertEqual(len(probe_threads), 1)
        self.assertNotEqual(probe_threads[0], event_loop_thread)

    async def test_missing_llm_key_and_unsafe_public_ingress_are_not_ready(self) -> None:
        config = SimpleNamespace(host="0.0.0.0")
        snapshot = await health.collect_health_snapshot(
            config=config,
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        settings = snapshot["checks"]["critical_config"]["settings"]
        self.assertFalse(snapshot["ready"])
        self.assertEqual(settings["llm_provider"], "missing_or_unsafe")
        self.assertEqual(settings["onebot_ingress"], "missing_or_unsafe")

    async def test_weak_public_onebot_credentials_are_not_ready(self) -> None:
        snapshot = await health.collect_health_snapshot(
            config={
                "host": "0.0.0.0",
                "taffy_api_key": "configured-secret",
                "steamdt_api_key": "configured-market-secret",
                "onebot_v11_secret": "weak-placeholder-secret",
                "onebot_v11_access_token": "weak-placeholder-token",
            },
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        self.assertFalse(snapshot["ready"])
        settings = snapshot["checks"]["critical_config"]["settings"]
        self.assertEqual(settings["onebot_ingress"], "missing_or_unsafe")

    async def test_snapshot_never_serializes_secrets_paths_or_bot_ids(self) -> None:
        sentinel = "health-sensitive-value-that-must-never-leak"
        bot_id = "987654321012345678"
        snapshot = await health.collect_health_snapshot(
            config={
                "host": "0.0.0.0",
                "taffy_api_key": sentinel,
                "steamdt_api_key": sentinel,
                "onebot_v11_secret": sentinel,
                "onebot_v11_access_token": sentinel,
            },
            root=self.root,
            environ={},
            onebot_connection_count=1,
            scheduler_tasks=healthy_tasks(),
            state_ready=True,
        )

        rendered = json.dumps(snapshot)
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(bot_id, rendered)


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            nonebot.get_driver()
        except ValueError:
            nonebot.init(_env_file=(), driver="~fastapi")
        cls.admin_api = importlib.import_module("src.plugins.admin_api")
        app = FastAPI()
        app.include_router(cls.admin_api.health_router)
        app.include_router(cls.admin_api.router)
        cls.app = app

    async def _request(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_liveness_is_anonymous_and_does_not_run_readiness_checks(self) -> None:
        with patch.object(self.admin_api, "_health_snapshot", new=AsyncMock()) as snapshot:
            response = await self._request("GET", "/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "alive"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        snapshot.assert_not_awaited()

    async def test_anonymous_readiness_is_minimal_and_returns_503(self) -> None:
        detailed = {
            "status": "not_ready",
            "ready": False,
            "uptime_seconds": 1,
            "checks": {"critical_config": {"secret": "must-not-escape"}},
        }
        with patch.object(
            self.admin_api,
            "_health_snapshot",
            new=AsyncMock(return_value=detailed),
        ):
            response = await self._request("GET", "/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("must-not-escape", response.text)

    async def test_anonymous_readiness_returns_200_when_ready(self) -> None:
        with patch.object(
            self.admin_api,
            "_health_snapshot",
            new=AsyncMock(return_value={"status": "ready", "ready": True}),
        ):
            response = await self._request("GET", "/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ready"})

    async def test_bearer_protected_admin_status_reuses_detailed_snapshot(self) -> None:
        expected_token = secrets.token_urlsafe(32)
        detailed = {
            "status": "ready",
            "ready": True,
            "uptime_seconds": 9,
            "checks": {"scheduler": {"status": "pass"}},
        }
        health_snapshot = AsyncMock(return_value=detailed)
        with (
            patch.object(self.admin_api, "get_admin_api_token", return_value=expected_token),
            patch.object(self.admin_api, "_health_snapshot", new=health_snapshot),
        ):
            response = await self._request(
                "GET",
                "/api/admin/status",
                headers={"Authorization": f"Bearer {expected_token}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["health"], detailed)
        health_snapshot.assert_awaited_once()
        self.assertNotIn(expected_token, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_runtime_health_snapshot_is_short_lived_cached(self) -> None:
        detailed = {
            "status": "ready",
            "ready": True,
            "uptime_seconds": 1,
            "checks": {},
        }
        collector = AsyncMock(return_value=detailed)
        previous_value = self.admin_api._health_cache_value
        previous_expiry = self.admin_api._health_cache_expires_at
        try:
            self.admin_api._health_cache_value = None
            self.admin_api._health_cache_expires_at = 0.0
            with patch.object(self.admin_api, "collect_health_snapshot", new=collector):
                first = await self.admin_api._health_snapshot()
                second = await self.admin_api._health_snapshot()
        finally:
            self.admin_api._health_cache_value = previous_value
            self.admin_api._health_cache_expires_at = previous_expiry

        self.assertIs(first, detailed)
        self.assertIs(second, detailed)
        collector.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

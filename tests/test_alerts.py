from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import nonebot
import pytest
from nonebot.exception import FinishedException

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(_env_file=())

from src.core.database import StateDatabase
from src.plugins import alert
from src.state.alert_repository import AlertRepository


class AlertDeliveryServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = AlertRepository(
            StateDatabase(Path(self.temporary.name) / "state.db"),
            legacy_path=None,
        )
        await self.repository.initialize()
        self.repository_patch = patch.object(
            alert,
            "alert_repository",
            self.repository,
        )
        self.repository_patch.start()

    async def asyncTearDown(self) -> None:
        self.repository_patch.stop()
        self.temporary.cleanup()

    async def _run_check(
        self,
        bot: SimpleNamespace,
        *,
        price: float = 120,
    ) -> None:
        with (
            patch("src.plugins.chat.resolve_name", AsyncMock(return_value="market-name")),
            patch(
                "src.plugins.chat_utils.st_get",
                AsyncMock(
                    return_value={
                        "success": True,
                        "updateTime": time.time(),
                        "data": [{"platform": "BUFF", "sellPrice": price}],
                    }
                ),
            ),
        ):
            await alert.check_alerts(bot)

    async def test_failed_delivery_returns_to_pending_with_retry(self) -> None:
        await self.repository.create(101, "AK红线", 100, group_id=9001, now=1)
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(side_effect=RuntimeError("QQ unavailable")),
            send_private_msg=AsyncMock(),
        )

        await self._run_check(bot)

        saved = (await self.repository.list_owner(101))[0]
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.attempts, 1)
        self.assertEqual(saved.last_error, "RuntimeError")
        self.assertIsNotNone(saved.next_retry_at)

    async def test_success_uses_structured_segments_and_commits_message_id(self) -> None:
        malicious = "[CQ:image,file=http://127.0.0.1/private]"
        await self.repository.create(101, malicious, 100, group_id=9001, now=1)
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(return_value={"message_id": 88}),
            send_private_msg=AsyncMock(),
        )

        await self._run_check(bot)

        saved = (await self.repository.list_owner(101))[0]
        self.assertEqual(saved.status, "delivered")
        self.assertEqual(saved.message_id, "88")
        message = bot.send_group_msg.await_args.kwargs["message"]
        self.assertEqual([segment.type for segment in message], ["at", "text"])
        self.assertIn("[CQ:image", message[1].data["text"])
        self.assertNotIn("image", [segment.type for segment in message])

    async def test_below_direction_triggers_only_when_price_reaches_or_crosses_target(self) -> None:
        created = await self.repository.create(
            101,
            "AK-47 | Redline (Field-Tested)",
            100,
            direction="below",
            now=1,
        )
        self.assertTrue(created.created)
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(),
            send_private_msg=AsyncMock(return_value={"message_id": 89}),
        )

        await self._run_check(bot, price=80)

        saved = (await self.repository.list_owner(101))[0]
        self.assertEqual(saved.status, "delivered")
        message = str(bot.send_private_msg.await_args.kwargs["message"])
        self.assertIn("跌到目标价", message)

    async def test_creation_resolves_name_and_validates_current_price_before_saving(self) -> None:
        event = SimpleNamespace(
            user_id=101,
            get_plaintext=lambda: "预警 跌到 AK红线 久经 80",
        )
        finish = AsyncMock(side_effect=FinishedException())
        now = time.time()
        with (
            patch.object(alert._command_limiter, "allow", AsyncMock(return_value=True)),
            patch(
                "src.plugins.chat.resolve_name",
                AsyncMock(return_value="AK-47 | Redline (Field-Tested)"),
            ),
            patch(
                "src.plugins.chat_utils.st_get",
                AsyncMock(
                    return_value={
                        "success": True,
                        "updateTime": now,
                        "data": [{"platform": "BUFF", "sellPrice": 100}],
                    }
                ),
            ),
            patch.object(alert.alert_cmd, "finish", finish),
            pytest.raises(FinishedException),
        ):
            await alert.handle_alert_command(object(), event)  # type: ignore[arg-type]

        saved = await self.repository.list_owner(101)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].market_name, "AK-47 | Redline (Field-Tested)")
        self.assertEqual(saved[0].direction, "below")
        rendered = str(finish.await_args.args[0])
        self.assertIn("当前价", rendered)
        self.assertIn("跌到", rendered)

    async def test_below_target_is_deferred_without_failure_attempt(self) -> None:
        await self.repository.create(101, "AK红线", 150, now=1)
        bot = SimpleNamespace(
            send_group_msg=AsyncMock(),
            send_private_msg=AsyncMock(),
        )

        await self._run_check(bot, price=120)

        saved = (await self.repository.list_owner(101))[0]
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.attempts, 0)
        self.assertEqual(saved.last_observed_price_cents, 12_000)
        self.assertIsNotNone(saved.next_retry_at)
        bot.send_group_msg.assert_not_awaited()
        bot.send_private_msg.assert_not_awaited()

    async def test_upstream_failure_never_consumes_delivery_retry_budget(self) -> None:
        market_name = "AK-47 | Redline (Field-Tested)"
        await self.repository.create(101, market_name, 100, now=1)
        bot = SimpleNamespace(send_group_msg=AsyncMock(), send_private_msg=AsyncMock())
        upstream = AsyncMock(side_effect=TimeoutError("upstream timeout"))
        with patch("src.plugins.chat_utils.st_get", upstream):
            await alert.check_alerts(bot)

        saved = (await self.repository.list_owner(101))[0]
        self.assertEqual(saved.status, "pending")
        self.assertEqual(saved.attempts, 0)
        self.assertEqual(saved.evaluation_failures, 1)
        self.assertIn("TimeoutError", saved.last_evaluation_error or "")
        self.assertIsNone(saved.last_error)
        upstream.assert_awaited_once_with(
            "/open/cs2/v1/price/single",
            {"marketHashName": market_name},
        )
        bot.send_private_msg.assert_not_awaited()

    def test_claim_lease_covers_the_bounded_evaluation_queue(self) -> None:
        waves = (alert._CHECK_BATCH_SIZE + alert._ALERT_CONCURRENCY - 1) // alert._ALERT_CONCURRENCY
        minimum = int((45 + alert._ALERT_SEND_TIMEOUT_SECONDS + 2) * waves + 90)
        self.assertGreaterEqual(alert._claim_lease_seconds(), minimum)


if __name__ == "__main__":
    unittest.main()

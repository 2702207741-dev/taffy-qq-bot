"""Desktop API V0.4 P5 endpoints: compare / score / slang / relationship / stickers."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_desktop_api import DesktopApiTestBase, _auth


class CompareScoreEndpointTests(DesktopApiTestBase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.compare_module = importlib.import_module("src.plugins.chat.compare")
        cls.scoring_module = importlib.import_module("src.services.market_scoring")

    def setUp(self) -> None:
        super().setUp()
        self._auth_limiter = patch.object(
            self.admin_api, "_auth_failure_limiter", self._limiter(10_000)
        )
        self._auth_limiter.start()
        self.addCleanup(self._auth_limiter.stop)
        self.token = self._enable()

    async def test_compare_returns_structured_payload(self) -> None:
        payload = {"market_hash_name": "x", "steamdt": {"quotes": []}, "spreads": []}
        mock_payload = AsyncMock(return_value=payload)
        with (
            patch.object(
                self.desktop_api,
                "resolve_name",
                AsyncMock(return_value="Item | One (Field-Tested)"),
            ),
            patch.object(self.desktop_api, "build_compare_payload", mock_payload),
        ):
            response = await self._request(
                "GET",
                "/api/desktop/market/compare?item=Item%20One",
                headers=_auth(self.token),
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        mock_payload.assert_awaited_once_with("Item One", "Item | One (Field-Tested)")

    async def test_compare_propagates_fail_closed_error(self) -> None:
        with (
            patch.object(self.desktop_api, "resolve_name", AsyncMock(return_value="Item One")),
            patch.object(
                self.desktop_api,
                "build_compare_payload",
                AsyncMock(return_value={"csqaq_error": "CSQAQ 数据暂时不可用"}),
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/compare?item=Item", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("CSQAQ", body["data"]["csqaq_error"])

    async def test_score_unscored_reports_reason(self) -> None:
        with (
            patch.object(self.desktop_api, "resolve_name", AsyncMock(return_value="Item One")),
            patch.object(
                self.desktop_api, "build_trade_score_payload", AsyncMock(return_value=None)
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/score?item=Item", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertFalse(body["data"]["scored"])
        self.assertIn("样本不足", body["data"]["reason"])

    async def test_score_scored_payload_has_factors_and_disclaimer(self) -> None:
        payload = {
            "scored": True,
            "market_hash_name": "Item One",
            "sample_days": 40,
            "buy": {
                "total": 62.0,
                "note": None,
                "factors": [{"name": "超卖", "score": 100.0, "weight": 0.25, "basis": "RSI=27.0"}],
            },
            "sell": {"total": 41.0, "note": None, "factors": []},
            "disclaimer": "历史规则回测参考，不构成交易建议。",
        }
        with (
            patch.object(self.desktop_api, "resolve_name", AsyncMock(return_value="Item One")),
            patch.object(
                self.desktop_api, "build_trade_score_payload", AsyncMock(return_value=payload)
            ),
        ):
            response = await self._request(
                "GET", "/api/desktop/market/score?item=Item", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["data"]["scored"])
        self.assertEqual(body["data"]["buy"]["total"], 62.0)
        self.assertIn("不构成交易建议", body["data"]["disclaimer"])


class SlangEndpointTests(DesktopApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._auth_limiter = patch.object(
            self.admin_api, "_auth_failure_limiter", self._limiter(10_000)
        )
        self._auth_limiter.start()
        self.addCleanup(self._auth_limiter.stop)
        self.token = self._enable()
        self.repo = SimpleNamespace(
            pending=AsyncMock(return_value=[]),
            accepted_for_injection=AsyncMock(return_value=[]),
            teach=AsyncMock(),
            decide=AsyncMock(),
            delete=AsyncMock(),
        )

    async def test_list_returns_pending_and_accepted(self) -> None:
        entry = SimpleNamespace(
            id=3,
            scope_group_id=0,
            created_by=1,
            term="6",
            meaning="厉害",
            status="accepted",
            confidence=2,
            created_at=1,
            accepted_at=2,
        )
        self.repo.pending.return_value = []
        self.repo.accepted_for_injection.return_value = [entry]
        with patch.object(self.desktop_api, "persona_slang", self.repo):
            response = await self._request(
                "GET", "/api/desktop/persona/slang?group_id=0", headers=_auth(self.token)
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["accepted"][0]["term"], "6")
        self.repo.accepted_for_injection.assert_awaited_once_with(0, limit=50)

    async def test_teach_and_decide_and_delete(self) -> None:
        created = SimpleNamespace(id=5, __dict__={"id": 5})
        self.repo.teach.return_value = (created, True)
        with patch.object(self.desktop_api, "persona_slang", self.repo):
            teach = await self._request(
                "POST",
                "/api/desktop/persona/slang",
                json={"action": "teach", "group_id": 2001, "term": "6", "meaning": "厉害"},
                headers=_auth(self.token),
            )
            self.assertEqual(teach.status_code, 200)
            self.assertTrue(teach.json()["data"]["created"])

            self.repo.decide.return_value = SimpleNamespace(id=5, __dict__={"id": 5})
            decide = await self._request(
                "POST",
                "/api/desktop/persona/slang",
                json={"action": "decide", "entry_id": 5, "status": "accepted"},
                headers=_auth(self.token),
            )
            self.assertEqual(decide.status_code, 200)

            self.repo.delete.return_value = None
            deleted = await self._request(
                "DELETE", "/api/desktop/persona/slang?id=99", headers=_auth(self.token)
            )
            self.assertEqual(deleted.status_code, 404)

        self.repo.teach.assert_awaited_once_with(2001, created_by=10001, term="6", meaning="厉害")

    async def test_delete_threads_the_scope_and_keeps_the_old_default(self) -> None:
        """批 2 第 1 项：DELETE 必须把 scope 传下去，且省略 `group_id` 时沿用旧行为。

        省略 `group_id` 是**既有客户端**的调用形态（`test_teach_and_decide_and_delete`
        就这么调）—— 这条钉住「新增参数是可选的、默认 0」，因此请求侧向后兼容。
        """

        self.repo.delete.return_value = SimpleNamespace(id=99, __dict__={"id": 99})
        with patch.object(self.desktop_api, "persona_slang", self.repo):
            default = await self._request(
                "DELETE", "/api/desktop/persona/slang?id=99", headers=_auth(self.token)
            )
            grouped = await self._request(
                "DELETE",
                "/api/desktop/persona/slang?id=99&group_id=2001",
                headers=_auth(self.token),
            )

        self.assertEqual(default.status_code, 200)
        self.assertEqual(grouped.status_code, 200)
        calls = self.repo.delete.await_args_list
        self.assertEqual(calls[0].kwargs, {"scope_group_id": 0, "created_by": 10001})
        self.assertEqual(calls[1].kwargs, {"scope_group_id": 2001, "created_by": 10001})

    async def test_private_delete_without_resolvable_owner_is_refused(self) -> None:
        """私聊桶（0）是跨用户共享的：缺归属判据必须拒绝，不得退化成「只按 id」。"""

        with (
            patch.object(self.desktop_api, "persona_slang", self.repo),
            patch.object(self.desktop_api, "resolve_desktop_owner", return_value=None),
        ):
            response = await self._request(
                "DELETE", "/api/desktop/persona/slang?id=99", headers=_auth(self.token)
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "OwnerNotConfiguredError")
        self.repo.delete.assert_not_awaited()

    async def test_group_delete_does_not_require_an_owner(self) -> None:
        """反向对照：群 scope 删词条不该被私聊配置绑死（否则群管理也一起瘫）。"""

        self.repo.delete.return_value = SimpleNamespace(id=99, __dict__={"id": 99})
        with (
            patch.object(self.desktop_api, "persona_slang", self.repo),
            patch.object(self.desktop_api, "resolve_desktop_owner", return_value=None),
        ):
            response = await self._request(
                "DELETE",
                "/api/desktop/persona/slang?id=99&group_id=2001",
                headers=_auth(self.token),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.repo.delete.await_args.kwargs, {"scope_group_id": 2001, "created_by": None}
        )

    async def test_invalid_action_rejected(self) -> None:
        with patch.object(self.desktop_api, "persona_slang", self.repo):
            response = await self._request(
                "POST",
                "/api/desktop/persona/slang",
                json={"action": "explode"},
                headers=_auth(self.token),
            )
        self.assertEqual(response.status_code, 400)


class RelationshipEndpointTests(DesktopApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._auth_limiter = patch.object(
            self.admin_api, "_auth_failure_limiter", self._limiter(10_000)
        )
        self._auth_limiter.start()
        self.addCleanup(self._auth_limiter.stop)
        self.token = self._enable()

    async def test_relationship_refreshes_and_returns_band(self) -> None:
        relationship = SimpleNamespace(
            user_id=10001,
            scope_group_id=0,
            intimacy=11.0,
            band="点头之交",
            interaction_count=2,
            accepted_insight_count=1,
            style_feedback_count=0,
            last_interaction_at=1_800_000_000,
        )
        repo = SimpleNamespace(refresh=AsyncMock(return_value=relationship))
        with patch.object(self.desktop_api, "persona_relationships", repo):
            response = await self._request(
                "GET",
                "/api/desktop/persona/relationship?user_id=10001&group_id=0",
                headers=_auth(self.token),
            )
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["band"], "点头之交")
        repo.refresh.assert_awaited_once_with(10001, 0)


class StickerEndpointTests(DesktopApiTestBase):
    def setUp(self) -> None:
        super().setUp()
        self._auth_limiter = patch.object(
            self.admin_api, "_auth_failure_limiter", self._limiter(10_000)
        )
        self._auth_limiter.start()
        self.addCleanup(self._auth_limiter.stop)
        self.token = self._enable()
        self.library = Path(self.tmpdir.name) / "data" / "stickers"
        patcher = patch.object(self.desktop_api, "PROJECT_ROOT", Path(self.tmpdir.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        import src.services.stickers as stickers_module

        stickers_module.reset_runtime_state()
        library_patcher = patch.object(stickers_module, "_STICKER_DIR", self.library)
        library_patcher.start()
        self.addCleanup(library_patcher.stop)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        import tempfile

        cls.tmpdir = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmpdir.cleanup()
        super().tearDownClass()

    async def test_upload_list_and_delete_roundtrip(self) -> None:
        (self.library).mkdir(parents=True, exist_ok=True)

        import base64

        body = {
            "file_name": "happy.png",
            "tags": ["开心", "大笑"],
            "data_base64": base64.b64encode(b"png-bytes").decode(),
        }
        upload = await self._request(
            "POST", "/api/desktop/stickers", json=body, headers=_auth(self.token)
        )
        self.assertEqual(upload.status_code, 200)
        self.assertTrue((self.library / "happy.png").is_file())
        manifest = json.loads((self.library / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["stickers"][0]["tags"], ["开心", "大笑"])

        listing = await self._request("GET", "/api/desktop/stickers", headers=_auth(self.token))
        data = listing.json()["data"]
        self.assertEqual(data["stickers"][0]["file"], "happy.png")

        removed = await self._request(
            "DELETE", "/api/desktop/stickers?file=happy.png", headers=_auth(self.token)
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse((self.library / "happy.png").exists())
        self.assertEqual(
            json.loads((self.library / "manifest.json").read_text(encoding="utf-8"))["stickers"], []
        )

    async def test_upload_rejects_traversal_and_bad_names(self) -> None:
        (self.library).mkdir(parents=True, exist_ok=True)
        import base64

        for bad_name in ("../evil.png", "sub/dir.png", "ok.txt"):
            response = await self._request(
                "POST",
                "/api/desktop/stickers",
                json={
                    "file_name": bad_name,
                    "tags": ["x"],
                    "data_base64": base64.b64encode(b"x").decode(),
                },
                headers=_auth(self.token),
            )
            self.assertEqual(response.status_code, 400, bad_name)

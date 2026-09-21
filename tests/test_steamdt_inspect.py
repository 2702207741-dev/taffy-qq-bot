"""Tests for SteamDT inspect screenshots (quota, parsing, chained flow)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services import steamdt_inspect
from src.services.steamdt_inspect import (
    build_inspect_result,
    consume_inspect_quota,
    parse_inspect_response,
)
from src.state.runtime import database


@pytest.fixture(autouse=True)
async def _clean_inspect_ledger():
    await database.initialize()
    yield
    await database.write("DELETE FROM app_meta WHERE key LIKE 'inspect:%'")


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAFFY_INSPECT_ENABLED", "true")


def _wear_ok() -> dict:
    return {
        "success": True,
        "data": {
            "sync": True,
            "itemPreviewData": {"floatWear": "0.1000", "paintseed": 7},
        },
    }


def _inspect_ok() -> dict:
    return {
        "success": True,
        "data": {
            "sync": True,
            "screenshot": {
                "screenshots": {
                    "front": ["https://img.example/1.png"],
                    "back": ["https://img.example/2.png"],
                    "detail": [],
                },
                "existSticker": True,
            },
        },
    }


async def test_quota_per_user_cap_and_global_budget(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    assert await consume_inspect_quota("u-cap") is True
    assert await consume_inspect_quota("u-cap") is True
    assert await consume_inspect_quota("u-cap") is True
    assert await consume_inspect_quota("u-cap") is False
    monkeypatch.setattr(steamdt_inspect, "_DAILY_BUDGET", 1)
    assert await consume_inspect_quota("u-fresh") is False


async def test_parse_paths() -> None:
    ok = parse_inspect_response(_inspect_ok())
    assert ok.status == "ok"
    assert ok.front == ("https://img.example/1.png",)
    assert ok.back == ("https://img.example/2.png",)
    assert ok.detail == () and ok.exist_sticker is True
    pending = parse_inspect_response({"success": True, "data": {"sync": False, "taskId": "t"}})
    assert pending.status == "pending" and "t" in pending.message
    assert parse_inspect_response({"success": False, "errorMsg": "x"}).status == "error"
    empty = parse_inspect_response(
        {"success": True, "data": {"sync": True, "screenshot": {"screenshots": {}}}}
    )
    assert empty.status == "error"
    junk_urls = parse_inspect_response(
        {
            "success": True,
            "data": {
                "sync": True,
                "screenshot": {"screenshots": {"front": ["ftp://x", 123]}},
            },
        }
    )
    assert junk_urls.status == "error"


async def test_download_rejects_unreachable() -> None:
    # Fail at the transport layer instead of connecting to a fixed port:
    # the real _download_image runs against a client whose transport
    # raises ConnectError, so the function must honestly return None.
    import httpx as _httpx

    def unreachable_transport() -> _httpx.AsyncBaseTransport:
        def handler(request: _httpx.Request) -> _httpx.Response:
            raise _httpx.ConnectError("synthetic unreachable", request=request)

        return _httpx.MockTransport(handler)

    original = steamdt_inspect.httpx.AsyncClient

    def failing_client(*args: object, **kwargs: object) -> _httpx.AsyncClient:
        kwargs["transport"] = unreachable_transport()
        return original(*args, **kwargs)

    with patch.object(steamdt_inspect.httpx, "AsyncClient", failing_client):
        assert await steamdt_inspect._download_image("https://img.example.test/nope.png") is None


async def test_build_flow_success(enabled: None) -> None:
    with (
        patch.object(
            steamdt_inspect, "st_post", AsyncMock(side_effect=[_wear_ok(), _inspect_ok()])
        ),
        patch.object(steamdt_inspect, "_download_image", AsyncMock(return_value=b"png")),
    ):
        caption, images = await build_inspect_result(
            "steam://x/+csgo_econ_action_preview S1A2D3", "u-flow"
        )
    assert "float: 0.1000" in caption and "有印花" in caption
    assert images == [b"png", b"png"]


async def test_build_flow_wear_first_and_quota(enabled: None) -> None:
    with patch.object(steamdt_inspect, "st_post", AsyncMock(return_value={"success": False})):
        caption, images = await build_inspect_result(
            "steam://x/+csgo_econ_action_preview S1A2D3", "u-w"
        )
    assert caption.startswith("⚠️ 先查磨损失败") and images == []


async def test_build_flow_disabled_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    # NOTE: patch the switch itself — runtime_setting falls back to the host
    # .env, so delenv alone cannot force the factory-off default hermetically.
    monkeypatch.setattr(steamdt_inspect, "inspect_enabled", lambda: False)
    caption, images = await build_inspect_result("steam://x", "u-d")
    assert caption == "检视功能暂未开放。" and images == []
    monkeypatch.setattr(steamdt_inspect, "inspect_enabled", lambda: True)
    caption, images = await build_inspect_result("说句话", "u-d")
    assert caption.startswith("格式：检视") and images == []

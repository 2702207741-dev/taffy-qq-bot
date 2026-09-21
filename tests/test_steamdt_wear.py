"""Tests for SteamDT wear lookup (detection, parsing, rendering)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.services import steamdt_wear
from src.services.steamdt_wear import (
    build_wear_reply,
    detect_wear_request,
    parse_wear_response,
    render_wear,
)

LINK = (
    "steam://rungame/730/76561202255233023/+csgo_econ_action_preview%20"
    "S76561198369689629A42204298773D11704554603329073305"
)


def test_detect_v1_steam_link() -> None:
    kind, payload = detect_wear_request(f"查磨损 {LINK}") or (None, {})
    assert kind == "v1"
    assert payload["inspectUrl"] == LINK


def test_detect_v1_http_link() -> None:
    url = "https://example.com/x?csgo_econ_action_preview&S1A2D3"
    detected = detect_wear_request(url)
    assert detected is not None and detected[0] == "v1"


def test_detect_v2_asmd_params() -> None:
    detected = detect_wear_request("s=76561198369689629&m=0&a=42204298773&d=11704554603329073305")
    assert detected is not None
    kind, payload = detected
    assert kind == "v2"
    assert payload == {
        "s": 76561198369689629,
        "m": 0,
        "a": 42204298773,
        "d": "11704554603329073305",
    }


def test_detect_rejects_garbage() -> None:
    assert detect_wear_request("AK-47 | Redline") is None
    assert detect_wear_request("") is None
    assert detect_wear_request("s=1&m=2") is None


def _ok_payload(**overrides):
    preview = {
        "floatWear": "0.1523",
        "paintseed": 123,
        "stickers": [
            {"slot": 0, "stickerId": 4567, "wear": 0.01},
            {"slot": 3, "stickerId": 765, "wear": 0.5},
        ],
        "keychains": [{"id": 9, "pattern": 42}],
    }
    preview.update(overrides.pop("preview", {}))
    data = {"sync": True, "success": True, "taskId": "", "itemPreviewData": preview}
    data.update(overrides)
    return {"success": True, "data": data, "errorCode": 0, "errorMsg": ""}


def test_parse_ok_full_payload() -> None:
    result = parse_wear_response(_ok_payload())
    assert result.status == "ok"
    assert result.float_wear == 0.1523
    assert result.paint_seed == 123
    assert result.stickers == ((0, 4567, 0.01), (3, 765, 0.5))
    assert result.keychains == ((9, 42),)


def test_parse_pending_and_errors() -> None:
    pending = parse_wear_response(
        {"success": True, "data": {"sync": False, "taskId": "t-1"}, "errorCode": 0}
    )
    assert pending.status == "pending" and "t-1" in pending.message
    failed = parse_wear_response({"success": False, "errorMsg": "限流了"})
    assert failed.status == "error" and "限流了" in failed.message
    assert parse_wear_response({"success": True}).status == "error"
    assert parse_wear_response(_ok_payload(preview={"floatWear": "n/a"})).status == "error"
    assert parse_wear_response(_ok_payload(preview={"floatWear": "1.5"})).status == "error"
    assert parse_wear_response(None).status == "error"


def test_render_paths() -> None:
    text = render_wear(parse_wear_response(_ok_payload()))
    assert "float: 0.1523" in text and "种子: 123" in text
    assert "槽0 磨损0.0100" in text and "#9 模板42" in text
    bare = render_wear(parse_wear_response(_ok_payload(preview={"stickers": [], "keychains": []})))
    assert "印花: 无" in bare and "挂饰: 无" in bare
    assert render_wear(parse_wear_response({"success": False})).startswith("⚠️")
    assert render_wear(parse_wear_response({"success": True, "data": {"sync": False}})).startswith(
        "⏳"
    )


async def test_build_reply_end_to_end_with_mock() -> None:
    with patch.object(steamdt_wear, "st_post", AsyncMock(return_value=_ok_payload())) as mocked:
        reply = await build_wear_reply(LINK)
        mocked.assert_awaited_once()
    assert "float: 0.1523" in reply
    assert "查磨损 steam://" in await build_wear_reply("")


async def test_build_reply_fetch_failure_is_honest() -> None:
    with patch.object(steamdt_wear, "st_post", AsyncMock(side_effect=RuntimeError("boom"))):
        assert "失败" in await build_wear_reply(LINK)

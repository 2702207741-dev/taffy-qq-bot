"""SteamDT inspect screenshots (front/back/detail) over inspect links.

Flow per request: parse the link (reusing the wear detector) → wear must
resolve synchronously first (the API needs known wear) → consume one quota
unit → fetch screenshots → download and hand PNG bytes to the caller for
the guarded image path.  Upstream allows 100/day; this module enforces a
smaller global budget plus a per-user cap, both in ``app_meta`` (no schema
migration, same pattern as the vision budget).  Every failure answers
honestly; nothing is invented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

import httpx

from .config import bounded_env_int, env_bool
from .market import st_post
from .steamdt_wear import WEAR_V1_PATH, WEAR_V2_PATH, detect_wear_request, parse_wear_response

logger = logging.getLogger("taffy.steamdt_inspect")

_INSPECT_V1_PATH = "/open/cs2/v1/inspect"
_INSPECT_V2_PATH = "/open/cs2/v2/inspect"
_DAILY_BUDGET = bounded_env_int("TAFFY_INSPECT_DAILY_BUDGET", 30, 0, 100)
_PER_USER_DAILY_CAP = 3
_MAX_IMAGES = 3
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_DOWNLOAD_TIMEOUT = 15.0
_USAGE = (
    "格式：检视 检视链接\n"
    "例如：检视 steam://rungame/730/76561202255233023/+csgo_econ_action_preview%20S76561198369689629A42204298773D11704554603329073305\n"
    "每日限量（全站额度 + 每人 3 次），出图失败会明说。"
)


def inspect_enabled() -> bool:
    """Kill switch (default off)."""

    return env_bool("TAFFY_INSPECT_ENABLED", False)


def _today_key() -> str:
    from datetime import datetime, timezone

    from .tz import to_market_tz

    return to_market_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d")


async def consume_inspect_quota(user_id: int | str) -> bool:
    """Reserve one global + one per-user unit for today; ``False`` when out."""

    from src.state.runtime import database

    day = _today_key()
    global_key = f"inspect:{day}"
    user_key = f"inspect:user:{user_id}:{day}"
    try:
        rows = await database.read(
            "SELECT key, value FROM app_meta WHERE key IN (?, ?)", (global_key, user_key)
        )
        used = {str(row["key"]): int(row["value"]) for row in rows}
        if used.get(global_key, 0) >= _DAILY_BUDGET:
            return False
        if used.get(user_key, 0) >= _PER_USER_DAILY_CAP:
            return False
        for key in (global_key, user_key):
            await database.write(
                """
                INSERT INTO app_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = value + 1, updated_at =
                    CAST(strftime('%s', 'now') AS INTEGER)
                """,
                (key, str(used.get(key, 0) + 1)),
            )
        return True
    except Exception as exc:
        logger.warning("Inspect quota check failed error=%s", type(exc).__name__)
        return False


@dataclass(frozen=True, slots=True)
class InspectResult:
    """Parsed inspect outcome: ``ok`` / ``pending`` / ``error``."""

    status: str
    front: tuple[str, ...] = ()
    back: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    exist_sticker: bool = False
    message: str = ""


def _url_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        item for item in value if isinstance(item, str) and item.startswith(("http://", "https://"))
    )


def parse_inspect_response(response: object) -> InspectResult:
    """Project a SteamDT inspect payload onto :class:`InspectResult`."""

    if not isinstance(response, Mapping) or not response.get("success"):
        message = ""
        if isinstance(response, Mapping):
            message = str(response.get("errorMsg") or "")
        return InspectResult(status="error", message=message or "上游返回失败")
    data = response.get("data")
    if not isinstance(data, Mapping):
        return InspectResult(status="error", message="上游返回结构异常")
    if not data.get("sync"):
        task_id = str(data.get("taskId") or "").strip()
        suffix = f"（taskId {task_id}）" if task_id else ""
        return InspectResult(
            status="pending",
            message=(
                "检视图已提交异步任务"
                f"{suffix}，但塔菲没有公网回调地址，暂不支持等待结果；"
                "请过会儿重发链接再试。"
            ),
        )
    shot = data.get("screenshot")
    if not isinstance(shot, Mapping):
        return InspectResult(status="error", message="未返回检视图数据")
    pictures = shot.get("screenshots")
    if not isinstance(pictures, Mapping):
        return InspectResult(status="error", message="未返回检视图数据")
    front = _url_list(pictures.get("front"))
    back = _url_list(pictures.get("back"))
    detail = _url_list(pictures.get("detail"))
    if not front and not back and not detail:
        return InspectResult(status="error", message="检视图为空")
    return InspectResult(
        status="ok",
        front=front,
        back=back,
        detail=detail,
        exist_sticker=bool(shot.get("existSticker")),
    )


async def _download_image(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = await response.aread()
    except Exception as exc:
        logger.warning("Inspect image download failed error=%s", type(exc).__name__)
        return None
    if not body or len(body) > _MAX_IMAGE_BYTES:
        return None
    return body


async def build_inspect_result(text: str, user_id: int | str) -> tuple[str, list[bytes]]:
    """Run wear → quota → inspect → download; returns ``(caption, pngs)``."""

    if not inspect_enabled():
        return ("检视功能暂未开放。", [])
    detected = detect_wear_request(text)
    if detected is None:
        return (_USAGE, [])
    kind, payload = detected
    try:
        wear_response = await st_post(WEAR_V1_PATH if kind == "v1" else WEAR_V2_PATH, payload)
    except Exception as exc:
        logger.warning("Inspect wear leg failed error=%s", type(exc).__name__)
        return ("⚠️ 磨损前置查询失败，无法生成检视图，请稍后再试。", [])
    wear = parse_wear_response(wear_response)
    if wear.status != "ok" or wear.float_wear is None:
        reason = wear.message or "未查到有效磨损"
        return (f"⚠️ 先查磨损失败（{reason}），暂不能生成检视图。", [])
    if not await consume_inspect_quota(user_id):
        return ("⏳ 今日检视额度用完了（全站限量 + 每人每天 3 次），明天再来。", [])
    try:
        response = await st_post(_INSPECT_V1_PATH if kind == "v1" else _INSPECT_V2_PATH, payload)
    except Exception as exc:
        logger.warning("Inspect lookup failed error=%s", type(exc).__name__)
        return ("⚠️ 检视查询请求失败，请稍后再试。（已消耗 1 次今日额度）", [])
    result = parse_inspect_response(response)
    if result.status == "pending":
        return (f"⏳ {result.message}", [])
    if result.status != "ok":
        detail = f"：{result.message}" if result.message else ""
        return (f"⚠️ 检视图这次没生成出来{detail}。（已消耗 1 次今日额度）", [])
    caption = f"🖼️ 检视图｜float: {wear.float_wear:.4f}"
    if wear.paint_seed is not None:
        caption += f"｜种子: {wear.paint_seed}"
    if result.exist_sticker:
        caption += "｜有印花"
    picked: list[str] = []
    for group in (result.front, result.back, result.detail):
        if group and len(picked) < _MAX_IMAGES:
            picked.append(group[0])
    images: list[bytes] = []
    for url in picked:
        data = await _download_image(url)
        if data is not None:
            images.append(data)
    if not images:
        return (caption + "\n⚠️ 图片下载失败，只剩文字版。", [])
    return (caption, images)


__all__ = [
    "InspectResult",
    "build_inspect_result",
    "consume_inspect_quota",
    "inspect_enabled",
    "parse_inspect_response",
]

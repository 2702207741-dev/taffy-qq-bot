"""SteamDT wear lookup (float/seed/stickers/keychains) over inspect links.

``v1`` takes a raw inspect link, ``v2`` takes ASMD ``s/m/a/d`` params
(using the documentation's field names, matched case-insensitively).
Synchronous responses render directly; asynchronous ones (``taskId``,
which needs a public callback URL this bot does not have) and failures
stay honest instead of inventing numbers.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .market import st_post

logger = logging.getLogger("taffy.steamdt_wear")

_WEAR_V1_PATH = "/open/cs2/v1/wear"
WEAR_V1_PATH = _WEAR_V1_PATH
_WEAR_V2_PATH = "/open/cs2/v2/wear"
WEAR_V2_PATH = _WEAR_V2_PATH
_URL_RE = re.compile(r"(steam://\S+|https?://\S+)", re.IGNORECASE)
_ASMD_PATTERNS = (
    ("s", re.compile(r"s=(\d+)", re.IGNORECASE)),
    ("m", re.compile(r"m=(\d+)", re.IGNORECASE)),
    ("a", re.compile(r"a=(\d+)", re.IGNORECASE)),
    ("d", re.compile(r"d=([A-Za-z0-9]+)", re.IGNORECASE)),
)
_USAGE = (
    "格式：查磨损 检视链接\n"
    "例如：查磨损 steam://rungame/730/76561202255233023/+csgo_econ_action_preview%20S76561198369689629A42204298773D11704554603329073305\n"
    "或 ASMD 参数：查磨损 s=76561198369689629 m=0 a=42204298773 d=11704554603329073305"
)


def detect_wear_request(text: str) -> tuple[str, dict[str, Any]] | None:
    """Split a command body into ``(kind, payload)``; ``None`` when unparsable."""

    body = text.strip()
    match = _URL_RE.search(body)
    if match:
        link = match.group(1).rstrip(".,!?)»」")
        lowered = link.lower()
        if "csgo_econ_action_preview" in lowered or lowered.startswith("steam://"):
            return ("v1", {"inspectUrl": link})
    asmd: dict[str, Any] = {}
    for key, pattern in _ASMD_PATTERNS:
        found = pattern.search(body)
        if found is None:
            break
        asmd[key] = found.group(1)
    else:
        return (
            "v2",
            {"s": int(asmd["s"]), "m": int(asmd["m"]), "a": int(asmd["a"]), "d": asmd["d"]},
        )
    return None


def _opt_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        try:
            result = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if result >= 0 else None


@dataclass(frozen=True, slots=True)
class WearResult:
    """Parsed wear outcome: ``ok`` / ``pending`` (async task) / ``error``."""

    status: str
    float_wear: float | None = None
    paint_seed: int | None = None
    stickers: tuple[tuple[int | None, int | None, float], ...] = ()
    keychains: tuple[tuple[int, int], ...] = ()
    message: str = ""


def _sticker_entries(
    preview: Mapping[str, Any],
) -> tuple[tuple[int | None, int | None, float], ...]:
    entries = []
    raw = preview.get("stickers")
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        wear = _opt_float(item.get("wear"))
        if wear is None:
            continue
        slot = _opt_int(item.get("slot"))
        sticker_id = _opt_int(item.get("stickerId", item.get("sticker_id")))
        entries.append((slot, sticker_id, wear))
    return tuple(entries)


def _keychain_entries(preview: Mapping[str, Any]) -> tuple[tuple[int, int], ...]:
    entries = []
    raw = preview.get("keychains")
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        kid = _opt_int(item.get("id"))
        pattern = _opt_int(item.get("pattern"))
        if kid is None or pattern is None:
            continue
        entries.append((kid, pattern))
    return tuple(entries)


def parse_wear_response(response: object) -> WearResult:
    """Project a SteamDT wear payload onto :class:`WearResult` (never raises)."""

    if not isinstance(response, Mapping) or not response.get("success"):
        message = ""
        if isinstance(response, Mapping):
            message = str(response.get("errorMsg") or "")
        return WearResult(status="error", message=message or "上游返回失败")
    data = response.get("data")
    if not isinstance(data, Mapping):
        return WearResult(status="error", message="上游返回结构异常")
    if not data.get("sync"):
        task_id = str(data.get("taskId") or "").strip()
        suffix = f"（taskId {task_id}）" if task_id else ""
        return WearResult(
            status="pending",
            message=(
                "磨损查询已提交异步任务"
                f"{suffix}，但塔菲没有公网回调地址，暂不支持等待结果；"
                "请过会儿重发链接再查（同步命中即直接返回）。"
            ),
        )
    preview = data.get("itemPreviewData")
    if not isinstance(preview, Mapping):
        return WearResult(status="error", message="未返回磨损数据")
    float_wear = _opt_float(preview.get("floatWear"))
    if float_wear is None or not 0.0 <= float_wear <= 1.0:
        return WearResult(status="error", message="未返回有效磨损数值")
    return WearResult(
        status="ok",
        float_wear=float_wear,
        paint_seed=_opt_int(preview.get("paintseed")),
        stickers=_sticker_entries(preview),
        keychains=_keychain_entries(preview),
    )


def render_wear(result: WearResult) -> str:
    """Render a parsed result; every number comes from the payload."""

    if result.status == "pending":
        return f"⏳ {result.message}"
    if result.status != "ok" or result.float_wear is None:
        detail = f"：{result.message}" if result.message else ""
        return f"⚠️ 磨损这次没查到{detail}，请检查检视链接是否完整。"
    lines = ["🔍 磨损查询", f"float: {result.float_wear:.4f}"]
    if result.paint_seed is not None:
        lines[-1] += f"｜种子: {result.paint_seed}"
    if result.stickers:
        parts = []
        for slot, _sticker_id, wear in result.stickers:
            slot_text = f"槽{slot}" if slot is not None else "槽?"
            parts.append(f"{slot_text} 磨损{wear:.4f}")
        lines.append("印花: " + "、".join(parts))
    else:
        lines.append("印花: 无")
    if result.keychains:
        lines.append(
            "挂饰: " + "、".join(f"#{kid} 模板{pattern}" for kid, pattern in result.keychains)
        )
    else:
        lines.append("挂饰: 无")
    lines.append("ℹ️ 磨损来自 SteamDT 实时查询，仅供参考。")
    return "\n".join(lines)


async def build_wear_reply(text: str) -> str:
    """Render the ``查磨损`` reply for a raw command body."""

    detected = detect_wear_request(text)
    if detected is None:
        return _USAGE
    kind, payload = detected
    try:
        response = await st_post(_WEAR_V1_PATH if kind == "v1" else _WEAR_V2_PATH, payload)
    except Exception as exc:
        logger.warning("Wear lookup failed error=%s", type(exc).__name__)
        return "⚠️ 磨损查询请求失败，请稍后再试。"
    return render_wear(parse_wear_response(response))


__all__ = [
    "WEAR_V1_PATH",
    "WEAR_V2_PATH",
    "WearResult",
    "build_wear_reply",
    "detect_wear_request",
    "parse_wear_response",
    "render_wear",
]

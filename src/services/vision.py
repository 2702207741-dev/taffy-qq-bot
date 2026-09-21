"""VLM vision service: a cheap vision model as Taffy's eyes.

Design: the configured vision endpoint (default GLM-4V-Flash, long-term
free tier) turns one user image into a short Chinese description plus tag
hints; the description is injected into the DeepSeek persona context as
``[图片: ...]``.  Everything is fail-closed — an unconfigured key, a dead
endpoint, an oversized image or an exhausted daily budget simply means "no
description", never a guess.  Descriptions are cached by image hash in the
``image_descriptions`` table so repeated stickers cost nothing.

Privacy: images are only sent to the configured vision provider when the
operator enables the feature; descriptions live in the local state
database; nothing is sent to the chat model beyond the bracketed text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

from .config import (
    PROJECT_ROOT,
    bounded_base_url,
    bounded_env_int,
    bounded_env_text,
    env_bool,
    env_secret,
)
from .http_client import request_json
from .tz import to_market_tz

logger = logging.getLogger("taffy.services.vision")

_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
_DEFAULT_MODEL = "glm-4v-flash"
_MAX_IMAGE_BYTES = bounded_env_int("TAFFY_VISION_MAX_IMAGE_BYTES", 5_242_880, 1024, 20_971_520)
_DAILY_BUDGET = bounded_env_int("TAFFY_VISION_DAILY_BUDGET", 100, 0, 5_000)
_TIMEOUT_SECONDS = bounded_env_int("TAFFY_VISION_TIMEOUT_SECONDS", 20, 5, 60)
_MAX_CACHE_ROWS = 20_000

_INBOX_DIR = PROJECT_ROOT / "data" / "images" / "inbox"
_INBOX_MAX_FILES = 500

_VISION_BASE_URL = bounded_base_url("TAFFY_VISION_BASE_URL", _DEFAULT_BASE_URL)
_VISION_MODEL = bounded_env_text("TAFFY_VISION_MODEL", _DEFAULT_MODEL, maximum=100)

_DESCRIBE_PROMPT = (
    "用不超过 60 字的中文描述这张图片的主体内容和情绪，然后给出 2-4 个简短标签。"
    "严格按格式输出一行：描述|标签1,标签2,标签3。不要输出其他任何文字。"
)


def vision_enabled() -> bool:
    if not env_bool("TAFFY_VISION_ENABLED", False):
        return False
    if not _VISION_BASE_URL or not _VISION_MODEL:
        return False
    return bool(vision_api_key())


def vision_api_key() -> str:
    return env_secret("TAFFY_VISION_API_KEY")


def _today_key() -> str:
    from datetime import datetime, timezone

    return to_market_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d")


async def _consume_daily_budget() -> bool:
    """Reserve one vision call for the current market day; False when out."""

    from src.state.runtime import database

    key = f"vision:{_today_key()}"
    try:
        rows = await database.read("SELECT value FROM app_meta WHERE key = ?", (key,))
        used = int(rows[0]["value"]) if rows else 0
        if used >= _DAILY_BUDGET:
            return False
        await database.write(
            """
            INSERT INTO app_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + 1, updated_at =
                CAST(strftime('%s', 'now') AS INTEGER)
            """,
            (key, str(used + 1)),
        )
        return True
    except Exception as exc:
        logger.warning("Vision budget check failed error=%s", type(exc).__name__)
        return False


async def _cached_description(image_sha256: str) -> tuple[str, list[str]] | None:
    from src.state.runtime import database

    try:
        rows = await database.read(
            "SELECT description, tags_json FROM image_descriptions WHERE image_sha256 = ?",
            (image_sha256,),
        )
    except Exception as exc:
        logger.warning("Vision cache read failed error=%s", type(exc).__name__)
        return None
    if not rows:
        return None
    try:
        tags = json.loads(str(rows[0]["tags_json"]))
    except json.JSONDecodeError:
        tags = []
    return (
        str(rows[0]["description"]),
        [str(tag) for tag in tags if isinstance(tag, str)][:6],
    )


async def _store_description(image_sha256: str, description: str, tags: list[str]) -> None:
    from src.state.runtime import database

    try:
        await database.write(
            """
            INSERT INTO image_descriptions (image_sha256, description, tags_json, model)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(image_sha256) DO UPDATE SET
                description = excluded.description,
                tags_json = excluded.tags_json,
                model = excluded.model
            """,
            (
                image_sha256,
                description[:500],
                json.dumps(tags[:6], ensure_ascii=False)[:300],
                _VISION_MODEL[:100],
            ),
        )
        # Bounded cache: drop the oldest rows when the table grows past cap.
        if await _cache_over_cap():
            await database.write(
                """
                DELETE FROM image_descriptions WHERE image_sha256 IN (
                    SELECT image_sha256 FROM image_descriptions
                    ORDER BY created_at ASC LIMIT ?
                )
                """,
                (_MAX_CACHE_ROWS // 10,),
            )
    except Exception as exc:
        logger.warning("Vision cache write failed error=%s", type(exc).__name__)


async def _cache_over_cap() -> bool:
    from src.state.runtime import database

    rows = await database.read("SELECT COUNT(*) AS total FROM image_descriptions")
    return bool(rows) and int(rows[0]["total"]) > _MAX_CACHE_ROWS


def _data_url(path: Path) -> tuple[str, str] | None:
    """Return ``(sha256, data_url)`` for a local image, or None."""

    try:
        payload = path.read_bytes()
    except OSError as exc:
        logger.warning("Vision image read failed error=%s", type(exc).__name__)
        return None
    if not payload or len(payload) > _MAX_IMAGE_BYTES:
        return None
    digest = hashlib.sha256(payload).hexdigest()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return digest, f"data:{mime};base64,{encoded}"


def _parse_description(output: object) -> tuple[str, list[str]] | None:
    text = str(output or "").strip()
    if not text or "|" not in text:
        return None
    description, _, tag_part = text.partition("|")
    description = description.strip()
    tags = [tag.strip() for tag in tag_part.split(",") if tag.strip()]
    if not 2 <= len(description) <= 200 or not tags:
        return None
    return description[:200], tags[:6]


async def describe_image(path: Path) -> tuple[str, list[str]] | None:
    """Describe one local image; ``None`` means fail-closed "no description"."""

    if not vision_enabled() or not path.is_file():
        return None
    prepared = _data_url(path)
    if prepared is None:
        return None
    digest, data_url = prepared
    cached = await _cached_description(digest)
    if cached is not None:
        return cached
    if not await _consume_daily_budget():
        logger.info("Vision daily budget exhausted; skipping description")
        return None
    payload = {
        "model": _VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                ],
            }
        ],
        "max_tokens": 200,
    }
    try:
        response = await request_json(
            "POST",
            f"{_VISION_BASE_URL}/chat/completions",
            attempts=1,
            timeout=_TIMEOUT_SECONDS,
            total_timeout=min(float(_TIMEOUT_SECONDS + 5), 60.0),
            headers={
                "Authorization": f"Bearer {vision_api_key()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        output = response["choices"][0]["message"]["content"]
    except Exception as exc:
        logger.warning("Vision request failed error=%s", type(exc).__name__)
        return None
    parsed = _parse_description(output)
    if parsed is None:
        logger.info("Vision description rejected by format validation")
        return None
    await _store_description(digest, parsed[0], parsed[1])
    return parsed


async def download_inbound_image(url: object, file_id: object) -> Path | None:
    """Persist the first inbound image; QQ media URLs expire within ~2h.

    Primary path: HTTP download of the OneBot ``image`` segment URL through
    the shared (proxy-aware) client.  Fallback: the adapter's ``get_image``
    API, which NapCat answers with a local file path.  Returns ``None``
    when both fail — callers simply skip the description.
    """

    import asyncio
    import unicodedata

    from .http_client import lease_http_client

    _INBOX_DIR.mkdir(parents=True, exist_ok=True)

    async def persist(payload: bytes, suffix: str) -> Path | None:
        if not payload or len(payload) > _MAX_IMAGE_BYTES:
            return None
        digest = hashlib.sha256(payload).hexdigest()
        target = _INBOX_DIR / f"{digest}{suffix}"
        if not target.exists():
            target.write_bytes(payload)
        # Bounded inbox: drop the oldest files when the directory grows.
        try:
            files = sorted(
                (p for p in _INBOX_DIR.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            excess = len(files) - _INBOX_MAX_FILES
            for stale in files[: max(0, excess)]:
                stale.unlink(missing_ok=True)
        except OSError:
            pass
        return target

    if isinstance(url, str) and url.startswith(("http://", "https://")):
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                async with lease_http_client() as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    payload = response.content
            suffix = ".png"
            lowered = url.casefold()
            for candidate in (".gif", ".jpg", ".jpeg", ".webp", ".bmp"):
                if candidate in lowered:
                    suffix = ".jpeg" if candidate == ".jpg" else candidate
                    break
            saved = await persist(payload, suffix)
            if saved is not None:
                return saved
        except Exception as exc:
            logger.warning("Inbound image download failed error=%s", type(exc).__name__)

    if isinstance(file_id, str) and file_id.strip():
        adapter_bot = _first_onebot_bot()
        if adapter_bot is None:
            return None
        try:
            result = await adapter_bot.call_api(
                "get_image", file=unicodedata.normalize("NFC", file_id.strip())
            )
            local = result.get("file") if isinstance(result, dict) else None
            if isinstance(local, str) and local:
                path = Path(local)
                if path.is_file() and path.stat().st_size <= _MAX_IMAGE_BYTES:
                    payload = path.read_bytes()
                    return await persist(payload, path.suffix.casefold() or ".png")
        except Exception as exc:
            logger.warning("get_image fallback failed error=%s", type(exc).__name__)
    return None


def _first_onebot_bot() -> Any:
    try:
        from nonebot import get_bots
        from nonebot.adapters.onebot.v11 import Bot as OneBotBot

        for bot in get_bots().values():
            if isinstance(bot, OneBotBot):
                return bot
    except Exception:
        return None
    return None


async def describe_for_context(path: Path) -> str | None:
    """One-line bracketed context snippet, or None when unavailable."""

    result = await describe_image(path)
    if result is None:
        return None
    description, tags = result
    if tags:
        return f"[图片: {description}（{','.join(tags[:3])}）]"
    return f"[图片: {description}]"


async def tag_pending_images(paths: list[Path], *, limit: int = 10) -> int:
    """Offline VLM tagging for curated stickers; returns tagged count."""

    from src.services.stickers import apply_description

    tagged = 0
    for path in paths[: max(1, min(int(limit), 50))]:
        result = await describe_image(_first_frame(path) or path)
        if result is None:
            continue
        description, tags = result
        if apply_description(path, description, tags):
            tagged += 1
    return tagged


async def describe_pending_stickers(paths: list[Path], *, limit: int = 10) -> int:
    """Reconnaissance phase two: VLM-describe auto-collected pending stickers.

    Descriptions land in a sidecar file next to the pending image so the
    review command can show what the image is about before acceptance.
    The curated library is untouched — acceptance stays the only gate.
    """

    from src.services.stickers import load_pending_metadata, save_pending_metadata

    described = 0
    for path in paths[: max(1, min(int(limit), 50))]:
        if load_pending_metadata(path) is not None:
            continue  # already described
        result = await describe_image(_first_frame(path) or path)
        if result is None:
            continue
        description, tags = result
        if save_pending_metadata(path, description, tags):
            described += 1
    return described


def _first_frame(path: Path) -> Path | None:
    """Extract the first frame of an animated image as a temporary PNG."""

    if path.suffix.casefold() not in {".gif", ".webp"}:
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.seek(0)
            target = path.with_suffix(".first-frame.png")
            image.convert("RGBA").save(target, "PNG")
            return target
    except Exception as exc:
        logger.warning("First-frame extraction failed error=%s", type(exc).__name__)
        return None


__all__ = [
    "describe_for_context",
    "describe_image",
    "download_inbound_image",
    "tag_pending_images",
    "vision_api_key",
    "vision_enabled",
]

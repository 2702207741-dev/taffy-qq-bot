"""Bounded FFmpeg transcoding and structured OneBot video/image delivery."""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import tempfile
from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment

from src.core.messages import text_message
from src.services.config import runtime_setting

TRANSCODE_TIMEOUT = 120.0
_VIDEO_SEMAPHORE = asyncio.Semaphore(1)
_IMAGE_SEMAPHORE = asyncio.Semaphore(1)
_ALLOWED_VIDEO_ENCODERS = {"libx264", "libopenh264"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_IMAGE_BYTES = 8 * 1_024 * 1_024
_SEND_TIMEOUT = 30.0


def video_paths() -> tuple[str, str | None]:
    """Resolve the configured video directory and FFmpeg executable."""

    project_root = Path(__file__).resolve().parents[3]
    default_video_dir = project_root.parent / "塔菲视频"
    video_dir = str(Path(runtime_setting("TAFFY_VIDEO_DIR") or str(default_video_dir)).expanduser())

    configured = (runtime_setting("FFMPEG_PATH") or "").strip()
    ffmpeg = configured or shutil.which("ffmpeg")
    if not ffmpeg:
        bundled = project_root.parent / ".local" / "bin" / "ffmpeg.exe"
        ffmpeg = str(bundled) if bundled.is_file() else None
    return video_dir, ffmpeg


def image_paths() -> str:
    """Resolve the configured image directory (default: ComfyUI output)."""

    configured = (runtime_setting("TAFFY_IMAGE_DIR") or "").strip()
    if configured:
        return str(Path(configured).expanduser())
    return "D:\\ComfyUI\\output"


def video_encoder(ffmpeg: str | Path) -> str:
    """Choose a known-safe H.264 encoder for the configured executable."""

    configured = (runtime_setting("FFMPEG_VIDEO_ENCODER") or "").strip()
    if configured in _ALLOWED_VIDEO_ENCODERS:
        return configured
    project_root = Path(__file__).resolve().parents[3]
    bundled = project_root.parent / ".local" / "bin" / "ffmpeg.exe"
    try:
        if Path(ffmpeg).resolve() == bundled.resolve():
            return "libopenh264"
    except OSError:
        pass
    return "libx264"


async def stop_process(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float = 5.0,
) -> None:
    """Terminate a child process, escalating to kill after the grace period."""

    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_period)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


def _child_environment() -> dict[str, str]:
    return {
        key: value
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
        if (value := os.environ.get(key))
    }


async def transcode_video(
    source: str | Path,
    ffmpeg: str | Path,
    *,
    timeout: float = TRANSCODE_TIMEOUT,
) -> Path:
    """Transcode one video to a temporary QQ-compatible MP4.

    The caller owns the returned path. On every failure or cancellation this
    function stops the child and deletes the partial file before returning.
    """

    output: Path | None = None
    process: asyncio.subprocess.Process | None = None
    try:
        async with _VIDEO_SEMAPHORE:
            file_descriptor, output_name = tempfile.mkstemp(prefix="taffy_", suffix=".mp4")
            os.close(file_descriptor)
            output = Path(output_name)
            encoder = video_encoder(ffmpeg)
            encoder_options = (
                ["-profile:v", "baseline", "-level", "3.0", "-preset", "ultrafast"]
                if encoder == "libx264"
                else ["-b:v", "1500k"]
            )
            process = await asyncio.create_subprocess_exec(
                str(ffmpeg),
                "-y",
                "-i",
                str(source),
                "-c:v",
                encoder,
                *encoder_options,
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-vf",
                "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease",
                str(output),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_child_environment(),
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError as exc:
                await stop_process(process)
                raise RuntimeError("ffmpeg timed out") from exc

            if process.returncode != 0:
                logger.error(
                    "ffmpeg failed rc={} stderr={}",
                    process.returncode,
                    stderr[-500:],
                )
                raise RuntimeError("ffmpeg failed")
        if output is None:
            raise RuntimeError("ffmpeg output was not created")
        return output
    except BaseException:
        if process is not None and process.returncode is None:
            await stop_process(process)
        if output is not None:
            output.unlink(missing_ok=True)
        raise


async def send_random_video(bot: Bot, event: MessageEvent) -> None:
    """Choose, transcode, and send one local video as a structured segment."""

    video_dir, ffmpeg = video_paths()
    try:
        videos = list(Path(video_dir).glob("*.mp4"))
    except OSError as exc:
        logger.warning("Video directory unavailable error={}", type(exc).__name__)
        await bot.send(event, text_message("视频库存暂时无法访问，请联系管理员。"))
        return
    if not videos:
        await bot.send(event, text_message("视频库存空了喵，快去找主人补充~"))
        return
    if not ffmpeg:
        await bot.send(event, text_message("视频转码组件未配置，请联系管理员。"))
        return

    output: Path | None = None
    try:
        output = await transcode_video(random.choice(videos), ffmpeg)
        async with _VIDEO_SEMAPHORE:
            await asyncio.wait_for(
                bot.send(event, MessageSegment.video(file=str(output))),
                timeout=_SEND_TIMEOUT,
            )
    except TimeoutError:
        logger.warning("Video send timed out; upload queue likely busy")
        await bot.send(event, text_message("视频太多了发不过来喵，稍等再试~"))
    except Exception as exc:
        logger.exception("Video processing failed error={}", type(exc).__name__)
        await bot.send(event, text_message("视频处理失败了喵，请稍后再试。"))
    finally:
        if output is not None:
            output.unlink(missing_ok=True)


async def send_random_image(bot: Bot, event: MessageEvent) -> None:
    """Choose and send one random image from the configured directory."""

    image_dir = image_paths()
    try:
        images = [
            path
            for path in Path(image_dir).iterdir()
            if path.is_file()
            and path.suffix.casefold() in _IMAGE_EXTENSIONS
            and path.stat().st_size <= _MAX_IMAGE_BYTES
        ]
    except OSError as exc:
        logger.warning("Image directory unavailable error={}", type(exc).__name__)
        await bot.send(event, text_message("图片库存暂时无法访问，请联系管理员。"))
        return
    if not images:
        await bot.send(
            event,
            text_message("图片库存空了喵（或都是大图），快去找主人补充~"),
        )
        return

    try:
        chosen = random.choice(images)
        async with _IMAGE_SEMAPHORE:
            await asyncio.wait_for(
                bot.send(
                    event,
                    MessageSegment.image(file=f"file:///{chosen.as_posix()}"),
                ),
                timeout=_SEND_TIMEOUT,
            )
    except TimeoutError:
        logger.warning("Image send timed out; upload queue likely busy")
        await bot.send(event, text_message("图片太多了发不过来喵，稍等再试~"))
    except Exception as exc:
        logger.exception("Image processing failed error={}", type(exc).__name__)
        await bot.send(event, text_message("图片处理失败了喵，请稍后再试。"))


async def send_generated_image(bot: Bot, event: MessageEvent, png: bytes) -> bool:
    """Send one freshly rendered PNG through the guarded image path.

    Returns whether the image itself went out; fallback text replies (size
    limits, timeouts, send failures) report ``False`` so callers can audit
    the command honestly.
    """

    if len(png) > _MAX_IMAGE_BYTES:
        logger.warning("Generated image exceeds the send limit bytes={}", len(png))
        await bot.send(event, text_message("图片太大了发不过去喵，请稍后再试。"))
        return False
    if not png:
        await bot.send(event, text_message("图片生成失败了喵，请稍后再试。"))
        return False

    output: Path | None = None
    try:
        file_descriptor, output_name = tempfile.mkstemp(prefix="taffy_card_", suffix=".png")
        output = Path(output_name)
        with open(file_descriptor, "wb") as handle:
            handle.write(png)
        async with _IMAGE_SEMAPHORE:
            await asyncio.wait_for(
                bot.send(
                    event,
                    MessageSegment.image(file=f"file:///{output.as_posix()}"),
                ),
                timeout=_SEND_TIMEOUT,
            )
        return True
    except TimeoutError:
        logger.warning("Generated image send timed out; upload queue likely busy")
        await bot.send(event, text_message("图片太多了发不过来喵，稍等再试~"))
        return False
    except Exception as exc:
        logger.exception("Generated image send failed error={}", type(exc).__name__)
        await bot.send(event, text_message("图片处理失败了喵，请稍后再试。"))
        return False
    finally:
        if output is not None:
            output.unlink(missing_ok=True)


# Compatibility names historically defined in the package entrypoint.
_stop_process = stop_process
_video_paths = video_paths
_send_random_video = send_random_video
_send_random_image = send_random_image
